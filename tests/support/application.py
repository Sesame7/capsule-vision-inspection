"""Controlled application builders and session doubles shared by tests."""

import hashlib
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path
from threading import Event, get_ident

import numpy as np
from numpy.typing import NDArray

from capsule_vision.application import (
    ApplicationError,
    PreparedConfiguration,
    ProductionApplication,
    ProductionCycle,
    prepare_configuration,
)
from capsule_vision.configuration import (
    MachineLayoutDefinition,
    ModelRegistry,
    ResolvedInspectionConfiguration,
    load_machine_layout,
    load_model_definition,
    load_recipe,
    resolve_configuration,
)
from capsule_vision.inference import ImageResult
from capsule_vision.inference._validation import uint8_array
from capsule_vision.inspection import (
    CollectedCycle,
    CycleCollector,
    CycleId,
    CycleStatus,
    Frame,
    FrameId,
)
from capsule_vision.inspection.production import (
    Assessment,
    CameraInspectionResult,
    CameraTiming,
    ChannelInspectionResult,
    CheckResult,
    FindingSource,
    InspectionLayout,
    ProductionInspectionResult,
    ProductJudgment,
    Reason,
    TechnicalStatus,
)
from tests.support.cycles import image_result
from tests.support.production import layout

WAIT = 5.0
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/configuration"


def prepared(tmp_path: Path) -> PreparedConfiguration:
    path = tmp_path / "general" / "model.vimosln"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(b"synthetic")
    model = replace(
        load_model_definition(FIXTURES / "models/general/model.json"),
        model_path=str(path),
        expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )
    config = resolve_configuration(
        load_machine_layout(FIXTURES / "machine.tsv"),
        load_recipe(FIXTURES / "product-a.json"),
        ModelRegistry((model,)),
    )
    return prepare_configuration(config, base_directory=tmp_path)


def production(
    cycle: int = 1,
    status: TechnicalStatus = TechnicalStatus.SUCCESS,
    judgment: ProductJudgment = ProductJudgment.OK,
) -> ProductionInspectionResult:
    config = layout()
    assessment = Assessment(status, judgment)
    reason = (
        ()
        if status is TechnicalStatus.DISABLED or judgment is ProductJudgment.OK
        else (Reason(FindingSource.VIMO, "synthetic"),)
    )
    cameras = tuple(
        CameraInspectionResult(
            camera,
            FrameId(CycleId(cycle), camera.camera_id, 7),
            (20, 60, 3),
            tuple(
                ChannelInspectionResult(
                    camera.camera_id,
                    row.channel_id,
                    row.slot,
                    (
                        CheckResult(
                            FindingSource.TRADITIONAL,
                            Assessment(
                                TechnicalStatus.DISABLED, ProductJudgment.UNKNOWN
                            ),
                        ),
                        CheckResult(FindingSource.VIMO, assessment, reason),
                    ),
                )
                for row in camera.slots
            ),
            (),
            CameraTiming(None, 10, 0),
        )
        for camera in config.cameras
    )
    return ProductionInspectionResult(
        CycleId(cycle), CycleStatus.COMPLETE, config, cameras, 2, 2.01, 0
    )


class ReplayFuture[T](Future[T]):
    def __init__(self) -> None:
        super().__init__()
        self.callbacks: list[Callable[[Future[T]], object]] = []

    def add_done_callback(self, fn: Callable[[Future[T]], object]) -> None:
        self.callbacks.append(fn)
        super().add_done_callback(fn)

    def replay(self) -> None:
        for callback in self.callbacks:
            callback(self)


class FakeSession:
    def __init__(self, *, automatic_start: bool = True) -> None:
        self.ready: Future[None] = Future()
        if automatic_start:
            self.ready.set_result(None)
        self.finished: ReplayFuture[None] = ReplayFuture()
        self.failure: BaseException | None = None
        self.cleanup_safe = True
        self.start_entered = Event()
        self.stop_requested = Event()
        self.join_entered = Event()
        self.join_gate: Event | None = None
        self.join_result = True
        self.cycles: list[ProductionCycle] = []
        self.stop_calls = 0

    def start(self) -> Future[None]:
        self.start_entered.set()
        return self.ready

    def begin_cycle(self, cycle_id: CycleId) -> ProductionCycle:
        result = ProductionCycle(ReplayFuture(), ReplayFuture())
        self.cycles.append(result)
        return result

    def submit_frame(self, frame: Frame) -> Future[None]:
        result: Future[None] = Future()
        result.set_result(None)
        return result

    def expire(self) -> Future[None]:
        result: Future[None] = Future()
        result.set_result(None)
        return result

    def request_stop(self) -> None:
        self.stop_calls += 1
        self.stop_requested.set()

    def join(self, timeout: float | None = None) -> bool:
        self.join_entered.set()
        if self.join_gate is not None and not self.join_gate.wait(timeout):
            return False
        if not self.join_result:
            return False
        for cycle in self.cycles:
            if not cycle.accepted.done():
                cycle.accepted.set_exception(ApplicationError("Fake unadmitted stop"))
            if not cycle.result.done():
                cycle.result.set_exception(ApplicationError("Fake queued stop"))
        if not self.finished.done():
            self.finished.set_result(None)
        return True


def close(app: ProductionApplication) -> None:
    app.close().result(WAIT)
    assert app.join(WAIT)


def config(tmp_path: Path, count: int = 6) -> ResolvedInspectionConfiguration:
    metadata = prepared(tmp_path).configuration.model
    registry = ModelRegistry((metadata, replace(metadata, model_id="special")))
    return resolve_configuration(
        MachineLayoutDefinition(
            InspectionLayout(
                load_machine_layout(FIXTURES / "machine.tsv").layout.cameras[:count]
            )
        ),
        load_recipe(FIXTURES / "product-b.json"),
        registry,
    )


def collected(configuration: ResolvedInspectionConfiguration) -> CollectedCycle:
    collector = CycleCollector(configuration.layout.camera_ids, timeout=100)
    collector.begin(CycleId(1))
    cycle = None
    # Arrival order must not dictate configured inspection/result ordering.
    for index, camera in reversed(tuple(enumerate(configuration.layout.cameras))):
        # PRESENT input lets policy tests exercise traditional success/error;
        # the corner pixel remains the independent camera invocation marker.
        pixels = np.full((20, 60, 3), 30, dtype=np.uint8)
        pixels[0, 0] = index
        cycle = collector.accept(
            Frame(
                FrameId(CycleId(1), camera.camera_id, 0),
                0,
                pixels,
            )
        )
    assert cycle is not None
    return cycle


class ControlledEngine:
    def __init__(self, *, fail_warmup: bool = False, fail_image: bool = False) -> None:
        self.warm = Event()
        self.release_warm = Event()
        self.active = Event()
        self.release_active = Event()
        self.calls: list[tuple[int, int]] = []
        self.closed = False
        self.fail_warmup, self.fail_image = fail_warmup, fail_image

    def infer(self, image_bgr: NDArray[np.generic]) -> ImageResult:
        self.calls.append((int(image_bgr[0, 0, 0]), get_ident()))
        if len(self.calls) == 1:
            self.warm.set()
            assert self.release_warm.wait(WAIT)
            if self.fail_warmup:
                raise ValueError("warmup failed")
        if len(self.calls) == 3:
            self.active.set()
            assert self.release_active.wait(WAIT)
            if self.fail_image:
                raise ValueError("single image failure")
        return image_result(uint8_array(image_bgr))

    def close(self) -> None:
        self.closed = True


class ReplayEngine:
    """A single-call Event gate; each generation receives a new fake engine."""

    def __init__(self, *, fail: str | None = None) -> None:
        self.calls: list[int] = []
        self.closed = False
        self.hold_call: int | None = None
        self.entered, self.release = Event(), Event()
        self.fail = fail

    def infer(self, image_bgr: NDArray[np.generic]) -> ImageResult:
        self.calls.append(int(image_bgr[0, 0, 0]))
        if len(self.calls) == self.hold_call:
            self.entered.set()
            assert self.release.wait(WAIT)
        if self.fail == "warmup" and len(self.calls) == 1:
            raise ValueError("controlled warmup failure")
        if self.fail == "fatal" and len(self.calls) == 3:
            raise KeyboardInterrupt("controlled runtime terminal failure")
        return image_result(uint8_array(image_bgr))

    def close(self) -> None:
        self.closed = True
