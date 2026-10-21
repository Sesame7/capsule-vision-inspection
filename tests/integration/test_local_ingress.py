"""Local ingress through the real application/coordinator/worker, without GPU."""

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from threading import Event
from threading import enumerate as threads

import numpy as np
import pytest
from numpy.typing import NDArray
from tests.application_support import WAIT, close, prepared
from tests.cycle_support import image_result

from vimo_demo.application import (
    ApplicationCapacityError,
    ApplicationStateError,
    LocalCycleIngress,
    PreparedConfiguration,
    ProductionApplication,
    ProductionCounters,
    ProductionRuntimeSession,
)
from vimo_demo.inference import ImageResult
from vimo_demo.inference._validation import uint8_array
from vimo_demo.inspection import CameraId, CycleId, CycleStateError, UnknownCameraError
from vimo_demo.inspection.production import (
    InspectionLayout,
    ProductionInspectionResult,
    ProductJudgment,
    TechnicalStatus,
)
from vimo_demo.runtime import CoordinatorCapacityError


class ProbeEngine:
    def __init__(self, block: bool) -> None:
        self.block = block
        self.entered, self.release = Event(), Event()
        self.calls = 0
        self.values: list[int] = []

    def infer(self, image_bgr: NDArray[np.generic]) -> ImageResult:
        self.calls += 1
        if self.calls == 2 and self.block:  # First production image after warm-up.
            self.entered.set()
            assert self.release.wait(WAIT)
        assert image_bgr.flags.owndata and image_bgr.flags.c_contiguous
        if self.calls > 1:
            assert not image_bgr.flags.writeable
        self.values.append(int(image_bgr[0, 0, 0]))
        return image_result(uint8_array(image_bgr))

    def close(self) -> None:
        pass


@contextmanager
def running(
    path: Path, *, block: bool = False, max_pending: int = 8
) -> Generator[tuple[ProductionApplication, PreparedConfiguration, list[ProbeEngine]]]:
    candidate = prepared(path)
    cameras = tuple(
        replace(camera, camera_id=CameraId(name))
        for camera, name in zip(
            reversed(candidate.configuration.layout.cameras),
            ("top", "side-B", "底部", "left", "side-A", "right"),
            strict=True,
        )
    )
    candidate = replace(
        candidate,
        # This lower-domain identity test deliberately uses non-machine names;
        # normal persisted MachineLayout accepts only CAM1..CAM5/CAM6.
        configuration=replace(
            candidate.configuration,
            layout=InspectionLayout(cameras),
            channels=tuple(
                replace(row, camera_id=camera.camera_id)
                for row in candidate.configuration.channels
                for camera in cameras
                if any(slot.channel_id == row.channel_id for slot in camera.slots)
            ),
        ),
    )
    engines: list[ProbeEngine] = []
    sessions: list[ProductionRuntimeSession] = []

    def engine_factory(_: PreparedConfiguration) -> ProbeEngine:
        engine = ProbeEngine(block)
        engines.append(engine)
        return engine

    def factory(
        config: PreparedConfiguration, generation: int
    ) -> ProductionRuntimeSession:
        session = ProductionRuntimeSession(
            config,
            generation,
            warmup=lambda _: (np.zeros((20, 60, 3), dtype=np.uint8),),
            warmup_iterations=1,
            engine_factory=engine_factory,
            max_ready=1,
            cycle_timeout=30,
        )
        sessions.append(session)
        return session

    app = ProductionApplication(factory, max_pending_cycles=max_pending)
    try:
        app.configure(candidate)
        app.start().result(WAIT)
        yield app, candidate, engines
    finally:
        for engine in engines:
            engine.release.set()
        close(app)
        assert not (
            {app.thread_name} | {n for s in sessions for n in s.thread_names}
        ) & {t.name for t in threads()}


def images() -> tuple[NDArray[np.uint8], ...]:
    return tuple(np.full((20, 60, 3), i + 1, dtype=np.uint8) for i in range(6))


def test_six_images_identity_order_and_increasing_cycles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Equal capture metadata and sequence=0 must never determine membership.
    monkeypatch.setattr("vimo_demo.application.ingress.monotonic", lambda: 0.0)
    with running(tmp_path) as (app, config, engines):
        ingress = LocalCycleIngress(app, config.configuration.layout)
        assert app.snapshot().counters == ProductionCounters()
        for cycle_id in (1, 2):
            receipt = ingress.submit(images())
            assert receipt.accepted.done()
            result = receipt.result.result(WAIT)
            assert isinstance(result, ProductionInspectionResult)
            assert result.cycle_id == CycleId(cycle_id)
            assert (
                tuple(c.camera_id for c in result.cameras)
                == config.configuration.layout.camera_ids
            )
            assert all(
                c.frame_id
                and c.frame_id.cycle_id == cycle_id
                and c.frame_id.sequence == 0
                for c in result.cameras
            )
            assert result.assessment.technical_status is TechnicalStatus.SUCCESS
            assert result.assessment.judgment is ProductJudgment.OK
            assert app.snapshot().latest_production_result is result
        assert engines[0].values == [0, *range(1, 7), *range(1, 7)]
        assert app.snapshot().counters.cycles_completed == 2
        assert app.snapshot().counters.channel_ok == 72


@pytest.mark.parametrize("storage", ["owned", "view", "fortran", "readonly"])
def test_copy_before_return_allows_immediate_source_reuse(
    tmp_path: Path, storage: str
) -> None:
    with running(tmp_path, block=True) as (app, config, engines):
        buffer = np.full((20, 60, 3), 7, dtype=np.uint8)
        source = buffer
        if storage == "view":
            source = buffer[:, ::-1, :]
        elif storage == "fortran":
            buffer = np.asfortranarray(buffer)
            source = buffer
        elif storage == "readonly":
            source = buffer.view()
            source.setflags(write=False)
        ingress = LocalCycleIngress(app, config.configuration.layout)
        receipt = ingress.submit((source,) * 6)
        buffer[:] = (
            99  # Reuse immediately after submit, before native inspection reads.
        )
        assert engines[0].entered.wait(WAIT)
        assert not receipt.result.done()
        engines[0].release.set()
        receipt.result.result(WAIT)
        assert engines[0].values == [0, *([7] * 6)]
        assert buffer.flags.writeable  # Frame did not freeze external storage.


@pytest.mark.parametrize(
    "invalid",
    [
        np.zeros((20, 60, 3), dtype=np.float32),
        np.zeros((20, 60), dtype=np.uint8),
        np.zeros((20, 60, 4), dtype=np.uint8),
        np.zeros((0, 60, 3), dtype=np.uint8),
    ],
)
def test_invalid_last_image_has_no_partial_admission(
    tmp_path: Path, invalid: NDArray[np.generic]
) -> None:
    with running(tmp_path) as (app, config, _):
        ingress = LocalCycleIngress(app, config.configuration.layout)
        with pytest.raises((TypeError, ValueError)):
            ingress.submit((*images()[:5], invalid))
        assert app.snapshot().counters == ProductionCounters()
        assert app.snapshot().latest_production_result is None
        assert ingress.submit(images()).result.result(WAIT).cycle_id == 1


@pytest.mark.parametrize("count", [0, 5, 7])
def test_wrong_image_count_is_explicit(tmp_path: Path, count: int) -> None:
    with running(tmp_path) as (app, config, _):
        ingress = LocalCycleIngress(app, config.configuration.layout)
        with pytest.raises(ValueError, match="Expected 6 images"):
            ingress.submit((images()[0],) * count)
        assert app.snapshot().counters == ProductionCounters()


def test_session_restart_requires_new_ingress(tmp_path: Path) -> None:
    with running(tmp_path) as (app, config, _):
        layout = config.configuration.layout
        ingress = LocalCycleIngress(app, layout)
        assert ingress.submit(images()).result.result(WAIT).cycle_id == 1
        app.stop().result(WAIT)
        with pytest.raises(ApplicationStateError):
            ingress.submit(images())
        with pytest.raises(ApplicationStateError):
            LocalCycleIngress(app, layout)
        app.start().result(WAIT)
        with pytest.raises(ApplicationStateError):
            ingress.submit(images())
        assert (
            LocalCycleIngress(app, layout).submit(images()).result.result(WAIT).cycle_id
            == 1
        )
        assert app.snapshot().counters.cycles_completed == 2


def test_application_capacity_rejection_not_retried(tmp_path: Path) -> None:
    with running(tmp_path, block=True, max_pending=1) as (app, config, engines):
        ingress = LocalCycleIngress(app, config.configuration.layout)
        first = ingress.submit(images())
        assert engines[0].entered.wait(WAIT)
        with pytest.raises(ApplicationCapacityError):
            ingress.submit(images())
        assert app.snapshot().counters.cycles_started == 1
        engines[0].release.set()
        first.result.result(WAIT)
        assert ingress.submit(images()).result.result(WAIT).cycle_id == 3


def test_runtime_ready_capacity_is_existing_result_exception(tmp_path: Path) -> None:
    with running(tmp_path, block=True) as (app, config, engines):
        ingress = LocalCycleIngress(app, config.configuration.layout)
        first = ingress.submit(images())
        assert engines[0].entered.wait(WAIT)
        second = ingress.submit(images())
        third = ingress.submit(images())
        with pytest.raises(CoordinatorCapacityError):
            third.result.result(WAIT)
        engines[0].release.set()
        first.result.result(WAIT)
        second.result.result(WAIT)
        assert app.snapshot().counters.cycles_completed == 2


def test_existing_begin_rejection_propagates(tmp_path: Path) -> None:
    with running(tmp_path) as (app, config, _):
        first = LocalCycleIngress(app, config.configuration.layout)
        first.submit(images()).result.result(WAIT)
        # Deliberate misuse: a second source in the SAME session reuses ID 1.
        with pytest.raises(CycleStateError):
            LocalCycleIngress(app, config.configuration.layout).submit(images())
        assert app.snapshot().counters.cycles_started == 1


def test_wrong_layout_frame_rejection_and_existing_partial_shutdown(
    tmp_path: Path,
) -> None:
    with running(tmp_path) as (app, config, _):
        layout = config.configuration.layout
        wrong = replace(
            layout,
            cameras=(
                *layout.cameras[:-1],
                replace(layout.cameras[-1], camera_id=CameraId("unknown")),
            ),
        )
        ingress = LocalCycleIngress(app, wrong)
        with pytest.raises(UnknownCameraError):
            ingress.submit(images())
        assert app.snapshot().counters.cycles_started == 1
        assert app.snapshot().counters.cycles_completed == 0
        app.stop().result(WAIT)  # No ingress retry, fabricated judgment or rollback.
        result = app.snapshot().latest_production_result
        assert result is not None and result.cycle_id == 1
        assert result.assessment.technical_status is TechnicalStatus.ERROR
        assert result.assessment.judgment is ProductJudgment.UNKNOWN
