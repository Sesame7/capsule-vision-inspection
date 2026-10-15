"""Opt-in single-cycle composition, using controlled fake hardware/engine owners."""

import subprocess
import sys
from collections.abc import Callable, Sequence
from concurrent.futures import Future
from pathlib import Path
from threading import Event, get_ident
from threading import enumerate as threads
from time import perf_counter

import numpy as np
import pytest
from numpy.typing import NDArray
from PySide6.QtWidgets import QApplication
from tests.application_support import WAIT
from tests.cycle_support import image_result
from tests.local_input_support import input_files
from tests.qt_support import until

from vimo_demo.application import (
    ApplicationError,
    ApplicationState,
    ApplicationStateError,
    ProductionCounters,
)
from vimo_demo.application.basler import (
    AcquiredImage,
    AcquisitionGroup,
    AcquisitionIssue,
    AcquisitionMetadata,
    AcquisitionReport,
    ArmedAcquisition,
    BaslerAcquisitionCancelled,
    BaslerAcquisitionError,
    BaslerAcquisitionSource,
    CameraCleanup,
    CameraReadiness,
)
from vimo_demo.communication.reporting import LocalReportingSettings
from vimo_demo.configuration import (
    CameraFolder,
    CameraSources,
    load_machine_layout,
    save_camera_sources,
)
from vimo_demo.inference import ImageResult
from vimo_demo.inference._validation import uint8_array
from vimo_demo.inspection import CycleId
from vimo_demo.ui import local_production
from vimo_demo.ui.production_bridge import QtProductionBridge
from vimo_demo.ui.production_presentation import matching_camera

# Provisional field mapping in configured CAM1..CAM6 order; explicitly includes
# the CAM5/CAM6 order that differs from sorting serials. This is test data only.
SERIALS = ("25334173", "25334174", "25334178", "25334180", "25334188", "25334184")


class Engine:
    def __init__(self) -> None:
        self.warm, self.release_warm = Event(), Event()
        self.active, self.release_active = Event(), Event()
        self.release_active.set()
        self.calls: list[tuple[int, int]] = []
        self.closed = False
        self.fail_warmup = False

    def infer(self, image_bgr: NDArray[np.generic]) -> ImageResult:
        image = uint8_array(image_bgr)
        self.calls.append((int(image[0, 0, 0]), get_ident()))
        if len(self.calls) == 1:
            self.warm.set()
            assert self.release_warm.wait(WAIT)
            if self.fail_warmup:
                raise ValueError("Warm-up failed")
        if len(self.calls) == 3:
            self.active.set()
            assert self.release_active.wait(WAIT)
        assert not image.flags.writeable
        return image_result(image)

    def close(self) -> None:
        self.closed = True


class Acquisition(BaslerAcquisitionSource):
    def __init__(self, serials: Sequence[str], *, timeout: float = 30.0) -> None:
        super().__init__(serials, timeout=timeout)
        self.entered, self.release, self.cleaned = Event(), Event(), Event()
        self.called = self.running = self.unsafe = self.ignore_cancel = False
        self.problem = ""
        self.wrong_order = self.wrong_shape = False
        self.owner: int | None = None

    @property
    def cleanup_safe(self) -> bool:
        return not self.running and not self.unsafe

    def acquire(
        self,
        *,
        on_armed: Callable[[ArmedAcquisition], None] | None = None,
        cancel: Event | None = None,
    ) -> AcquisitionGroup:
        self.called, self.running, self.owner = True, True, get_ident()
        now = perf_counter()
        armed = ArmedAcquisition(
            self.serials,
            now,
            now + self.timeout,
            tuple(CameraReadiness(s, True, True, now) for s in self.serials),
        )
        try:
            if on_armed is not None:
                on_armed(armed)
            self.entered.set()
            assert self.release.wait(
                WAIT
            )  # Controlled native-call boundary, no sleeps.
            images: list[AcquiredImage] = []
            for index, serial in enumerate(self.serials):
                pixels = np.full(
                    (10, 30, 3) if self.wrong_shape else (20, 60, 3),
                    100 + index,
                    dtype=np.uint8,
                )
                pixels.setflags(write=False)
                metadata = AcquisitionMetadata(
                    serial,
                    now,
                    True,
                    0,
                    "",
                    60,
                    20,
                    123,
                    1200,
                    9000 + index,
                    87,
                    777777,
                    0,
                )
                images.append(AcquiredImage(serial, pixels, metadata))
            if self.problem == "missing":
                images.pop()
            issue = (
                (AcquisitionIssue("acquisition", None, self.problem),)
                if self.problem
                else ()
            )
            cleanup = tuple(
                CameraCleanup(s, True, not self.unsafe, True, True)
                for s in self.serials
            )
            report = AcquisitionReport(
                self.serials,
                now,
                armed,
                perf_counter(),
                (),
                tuple(i.metadata for i in images),
                issue,
                cleanup,
            )
            if self.unsafe or self.problem:
                raise BaslerAcquisitionError(report)
            if cancel is not None and cancel.is_set() and not self.ignore_cancel:
                raise BaslerAcquisitionCancelled(report)
            if self.wrong_order:
                images.reverse()
            return AcquisitionGroup(tuple(images), report)
        finally:
            self.running = False
            self.cleaned.set()


@pytest.fixture
def acquisitions(monkeypatch: pytest.MonkeyPatch) -> list[Acquisition]:
    sources: list[Acquisition] = []

    def factory(serials: Sequence[str], *, timeout: float = 30.0) -> Acquisition:
        source = Acquisition(serials, timeout=timeout)
        sources.append(source)
        return source

    monkeypatch.setattr(local_production, "BaslerAcquisitionSource", factory)
    return sources


def cleanup(
    composition: local_production.LocalComposition,
    engines: list[Engine],
    sources: list[Acquisition],
) -> None:
    for source in sources:
        source.release.set()
        source.unsafe = False  # Fake recovery control only, no native API calls.
    for engine in engines:
        engine.release_warm.set()
        engine.release_active.set()
    composition.application.close().result(WAIT)
    assert composition.application.join(WAIT)


@pytest.mark.parametrize("saved_folders", [False, True])
def test_one_cycle_warmup_mapping_accounting_and_matching_display(
    qapp: QApplication,
    tmp_path: Path,
    acquisitions: list[Acquisition],
    saved_folders: bool,
) -> None:
    engine = Engine()
    sources: list[Future[None]] = []
    original = {t.ident for t in threads()}
    notices: list[ArmedAcquisition | AcquisitionReport] = []
    model, machine, recipe, paths = input_files(tmp_path)
    if saved_folders:
        save_camera_sources(
            tmp_path / "camera_sources.json",
            CameraSources(
                tuple(
                    CameraFolder(camera, str(tmp_path))
                    for camera in load_machine_layout(machine).layout.camera_ids
                )
            ),
        )
    composition = local_production.compose(
        model,
        machine,
        recipe,
        () if saved_folders else paths,
        engine_factory=lambda _: engine,
        observe_source=sources.append,
        basler_serials=SERIALS,
        observe_acquisition=notices.append,
    )
    app = composition.application
    bridge = QtProductionBridge(app, composition.store, poll_ms=5)
    try:
        assert not composition.store.snapshot().frames
        startup = app.start()
        until(engine.warm.is_set)
        assert app.snapshot().state is ApplicationState.STARTING
        assert app.snapshot().counters == ProductionCounters()
        assert not any(source.called for source in acquisitions)
        assert not composition.store.snapshot().frames
        engine.release_warm.set()
        until(lambda: acquisitions[-1].entered.is_set())
        source = acquisitions[-1]
        assert startup.done() and startup.exception() is None
        assert app.snapshot().state is ApplicationState.RUNNING
        assert app.snapshot().latest_production_result is None
        assert app.snapshot().counters == ProductionCounters()
        assert [value for value, _ in engine.calls] == [1, 1]
        assert source.serials == SERIALS and source.owner != get_ident()
        source.release.set()
        until(lambda: app.snapshot().state is ApplicationState.READY)
        snapshot = app.snapshot()
        result = snapshot.latest_production_result
        assert result is not None and result.cycle_id == CycleId(1)
        assert len(result.channels) == 36
        assert (
            snapshot.counters.cycles_started == snapshot.counters.cycles_completed == 1
        )
        assert (
            snapshot.counters.channel_ok
            + snapshot.counters.channel_ng
            + snapshot.counters.channel_unknown
            + snapshot.counters.channel_disabled
            == 36
        )
        assert [value for value, _ in engine.calls] == [1, 1, *range(100, 106)]
        assert len({owner for _, owner in engine.calls}) == 1
        assert engine.calls[0][1] not in (get_ident(), source.owner)
        assert engine.closed and source.cleaned.is_set()
        frames = composition.store.snapshot().frames
        assert (
            tuple(row.frame_id.camera_id for row in frames) == composition.store.cameras
        )
        assert [int(row.image[0, 0, 0]) for row in frames] == list(range(100, 106))
        assert all(
            row.frame_id.sequence == 0 and row.frame_id.cycle_id == 1 for row in frames
        )
        assert all(matching_camera(row, snapshot) is not None for row in frames)
        bridge.refresh()
        qapp.processEvents()
        assert (
            app.snapshot().counters.cycles_completed == 1
        )  # No automatic second attempt.
        assert sources[0].done() and sources[0].exception() is None
        assert sum(isinstance(n, ArmedAcquisition) for n in notices) == 1
        assert sum(isinstance(n, AcquisitionReport) for n in notices) == 1
    finally:
        cleanup(composition, [engine], acquisitions)
        bridge.detach()
    assert not {t.ident for t in threads()} - original


@pytest.mark.parametrize(
    "problem", ["missing", "unexpected extra frame", "ambiguous trigger"]
)
def test_failed_acquisition_has_no_cycle_judgments_or_fake_ng(
    qapp: QApplication, tmp_path: Path, acquisitions: list[Acquisition], problem: str
) -> None:
    engine = Engine()
    engine.release_warm.set()
    sources: list[Future[None]] = []
    composition = local_production.compose(
        *input_files(tmp_path),
        engine_factory=lambda _: engine,
        basler_serials=SERIALS,
        observe_source=sources.append,
        observe_acquisition=lambda _: None,
    )
    try:
        composition.application.start().result(WAIT)
        until(lambda: acquisitions[-1].entered.is_set())
        source = acquisitions[-1]
        source.problem = problem
        source.release.set()
        until(
            lambda: composition.application.snapshot().state is ApplicationState.READY
        )
        snapshot = composition.application.snapshot()
        assert snapshot.counters == ProductionCounters()
        assert snapshot.latest_production_result is None
        assert not composition.store.snapshot().frames
        assert len(engine.calls) == 2 and engine.closed
        assert isinstance(sources[0].exception(), BaslerAcquisitionError)
        assert source.cleaned.is_set()
    finally:
        cleanup(composition, [engine], acquisitions)


@pytest.mark.parametrize("close", [False, True])
@pytest.mark.parametrize("late_group", [False, True])
def test_stop_close_wait_for_actual_source_exit_and_drop_late_groups(
    qapp: QApplication,
    tmp_path: Path,
    acquisitions: list[Acquisition],
    close: bool,
    late_group: bool,
) -> None:
    engine = Engine()
    engine.release_warm.set()
    composition = local_production.compose(
        *input_files(tmp_path),
        engine_factory=lambda _: engine,
        basler_serials=SERIALS,
        observe_acquisition=lambda _: None,
    )
    app = composition.application
    try:
        app.start().result(WAIT)
        until(lambda: acquisitions[-1].entered.is_set())
        source = acquisitions[-1]
        source.ignore_cancel = late_group
        stopped = app.close() if close else app.stop()
        assert app.snapshot().state is ApplicationState.STOPPING
        assert not stopped.done() and not source.cleaned.is_set()
        with pytest.raises(ApplicationStateError):
            app.start()
        source.release.set()
        stopped.result(WAIT)
        assert app.snapshot().state is (
            ApplicationState.CLOSED if close else ApplicationState.READY
        )
        assert app.snapshot().counters == ProductionCounters()
        assert not composition.store.snapshot().frames
        assert source.cleaned.is_set() and engine.closed and len(engine.calls) == 2
    finally:
        cleanup(composition, [engine], acquisitions)


def test_stop_after_acquisition_cleanup_before_admission_drops_group(
    qapp: QApplication, tmp_path: Path, acquisitions: list[Acquisition]
) -> None:
    engine = Engine()
    engine.release_warm.set()

    def observation(row: ArmedAcquisition | AcquisitionReport) -> None:
        if isinstance(row, AcquisitionReport):
            composition.application.stop()

    composition = local_production.compose(
        *input_files(tmp_path),
        engine_factory=lambda _: engine,
        basler_serials=SERIALS,
        observe_acquisition=observation,
    )
    try:
        composition.application.start().result(WAIT)
        until(lambda: acquisitions[-1].entered.is_set())
        acquisitions[-1].release.set()
        until(
            lambda: composition.application.snapshot().state is ApplicationState.READY
        )
        assert composition.application.snapshot().counters == ProductionCounters()
        assert not composition.store.snapshot().frames
    finally:
        cleanup(composition, [engine], acquisitions)


def test_generation_isolation_and_counters_retained_on_explicit_restart(
    qapp: QApplication, tmp_path: Path, acquisitions: list[Acquisition]
) -> None:
    engines: list[Engine] = []

    def factory(_: object) -> Engine:
        engine = Engine()
        engine.release_warm.set()
        engines.append(engine)
        return engine

    composition = local_production.compose(
        *input_files(tmp_path),
        engine_factory=factory,
        basler_serials=SERIALS,
        observe_acquisition=lambda _: None,
    )
    app = composition.application
    try:
        for generation in (1, 2):
            app.start().result(WAIT)
            until(lambda: acquisitions[-1].entered.is_set())
            acquisitions[-1].release.set()
            until(lambda: app.snapshot().state is ApplicationState.READY)
            assert app.snapshot().generation == generation
            assert app.snapshot().counters.cycles_completed == generation
            assert all(
                row.generation == generation
                for row in composition.store.snapshot().frames
            )
            latest = app.snapshot().latest_production_result
            assert latest is not None and latest.cycle_id == 1
        assert len(engines) == 2 and all(engine.closed for engine in engines)
        assert sum(source.called for source in acquisitions) == 2
    finally:
        cleanup(composition, engines, acquisitions)


def test_unsafe_device_cleanup_remains_fault_owned(
    qapp: QApplication, tmp_path: Path, acquisitions: list[Acquisition]
) -> None:
    engine = Engine()
    engine.release_warm.set()
    composition = local_production.compose(
        *input_files(tmp_path),
        engine_factory=lambda _: engine,
        basler_serials=SERIALS,
        observe_acquisition=lambda _: None,
    )
    app = composition.application
    try:
        app.start().result(WAIT)
        until(lambda: acquisitions[-1].entered.is_set())
        source = acquisitions[-1]
        source.unsafe = True
        source.release.set()
        until(lambda: app.snapshot().state is ApplicationState.FAULT)
        assert app.snapshot().runtime_owned and source.cleaned.is_set()
        assert app.snapshot().counters == ProductionCounters()
        with pytest.raises(ApplicationError, match="Stop failed"):
            app.stop().result(WAIT)
        assert app.snapshot().runtime_owned
    finally:
        cleanup(composition, [engine], acquisitions)


@pytest.mark.parametrize("close", [False, True])
def test_stop_close_after_admission_finishes_active_inspection(
    qapp: QApplication, tmp_path: Path, acquisitions: list[Acquisition], close: bool
) -> None:
    engine = Engine()
    engine.release_warm.set()
    engine.release_active.clear()
    composition = local_production.compose(
        *input_files(tmp_path),
        engine_factory=lambda _: engine,
        basler_serials=SERIALS,
        observe_acquisition=lambda _: None,
    )
    app = composition.application
    try:
        app.start().result(WAIT)
        until(lambda: acquisitions[-1].entered.is_set())
        acquisitions[-1].release.set()
        until(engine.active.is_set)
        assert acquisitions[-1].cleaned.is_set()
        stopped = app.close() if close else app.stop()
        assert not stopped.done() and not engine.closed
        engine.release_active.set()
        stopped.result(WAIT)
        assert app.snapshot().counters.cycles_completed == 1
        assert len(engine.calls) == 8 and engine.closed
        assert len(composition.store.snapshot().frames) == 6
    finally:
        cleanup(composition, [engine], acquisitions)


def test_cli_requires_mapping_confirmation_and_rejects_plc_reporting() -> None:
    script = Path(__file__).resolve().parents[2] / "scripts/run_local_production_ui.py"
    base = [
        sys.executable,
        str(script),
        "--model-config",
        "unused",
        "--machine-config",
        "unused",
        "--recipe",
        "unused",
        "--basler-serials",
        *SERIALS,
    ]
    for extra, message in (
        ([], "--basler-mapping-confirmed"),
        (
            ["--basler-mapping-confirmed", "--report-first-station"],
            "cannot perform PLC reporting",
        ),
    ):
        result = subprocess.run(
            [*base, *extra], capture_output=True, text=True, timeout=20
        )
        assert result.returncode == 2 and message in result.stderr


@pytest.mark.parametrize("option", ["replay", "reporting"])
def test_programmatic_composition_rejects_incompatible_modes(
    tmp_path: Path, option: str
) -> None:
    with pytest.raises(ValueError, match="cannot replay or report"):
        local_production.compose(
            *input_files(tmp_path),
            basler_serials=SERIALS,
            replay_controls=option == "replay",
            reporting=LocalReportingSettings() if option == "reporting" else None,
        )


@pytest.mark.parametrize("problem", ["order", "shape"])
def test_invalid_acquired_group_is_not_admitted(
    qapp: QApplication, tmp_path: Path, acquisitions: list[Acquisition], problem: str
) -> None:
    engine = Engine()
    engine.release_warm.set()
    futures: list[Future[None]] = []
    composition = local_production.compose(
        *input_files(tmp_path),
        engine_factory=lambda _: engine,
        basler_serials=SERIALS,
        observe_source=futures.append,
        observe_acquisition=lambda _: None,
    )
    try:
        composition.application.start().result(WAIT)
        until(lambda: acquisitions[-1].entered.is_set())
        acquisitions[-1].wrong_order = problem == "order"
        acquisitions[-1].wrong_shape = problem == "shape"
        acquisitions[-1].release.set()
        until(
            lambda: composition.application.snapshot().state is ApplicationState.READY
        )
        assert isinstance(futures[0].exception(), ValueError)
        assert composition.application.snapshot().counters == ProductionCounters()
        assert not composition.store.snapshot().frames
    finally:
        cleanup(composition, [engine], acquisitions)


def test_failed_warmup_never_calls_hardware_source(
    qapp: QApplication, tmp_path: Path, acquisitions: list[Acquisition]
) -> None:
    engine = Engine()
    engine.fail_warmup = True
    engine.release_warm.set()
    composition = local_production.compose(
        *input_files(tmp_path),
        engine_factory=lambda _: engine,
        basler_serials=SERIALS,
        observe_acquisition=lambda _: None,
    )
    try:
        ready = composition.application.start()
        until(ready.done)
        assert ready.exception() is not None
        assert composition.application.snapshot().state is ApplicationState.READY
        assert composition.application.snapshot().active is None
        assert composition.application.snapshot().counters == ProductionCounters()
        assert not composition.store.snapshot().frames
        assert not any(source.called for source in acquisitions)
        assert engine.closed
    finally:
        cleanup(composition, [engine], acquisitions)
