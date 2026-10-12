"""Controlled application builders and session doubles shared by tests."""

import hashlib
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path
from threading import Event

from tests.production_support import cycle_result, layout
from vimo_demo.application import (
    ApplicationError,
    PreparedConfiguration,
    ProductionApplication,
    ProductionCycle,
    prepare_configuration,
)
from vimo_demo.configuration import (
    ModelRegistry,
    load_machine_layout,
    load_model_definition,
    load_recipe,
    resolve_configuration,
)
from vimo_demo.inspection import CycleId, Frame
from vimo_demo.inspection.production import (
    Assessment,
    CheckResult,
    FindingSource,
    ProductionInspectionResult,
    ProductionPipeline,
    ProductJudgment,
    Reason,
    TechnicalStatus,
)

WAIT = 5.0
FIXTURES = Path(__file__).parent / "fixtures/configuration"


def prepared(tmp_path: Path) -> PreparedConfiguration:
    path = tmp_path / "synthetic.vimosln"
    path.write_bytes(b"synthetic")
    model = replace(
        load_model_definition(FIXTURES / "general.json"),
        model_path=str(path),
        expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )
    config = resolve_configuration(
        load_machine_layout(FIXTURES / "machine.json"),
        load_recipe(FIXTURES / "defaults.json"),
        ModelRegistry((model,)),
        "general",
    )
    return prepare_configuration(config, base_directory=tmp_path)


def production(
    cycle: int = 1,
    status: TechnicalStatus = TechnicalStatus.SUCCESS,
    judgment: ProductJudgment = ProductJudgment.OK,
) -> ProductionInspectionResult:
    config = layout()
    base = ProductionPipeline(config).from_cycle(cycle_result(config))
    assessment = Assessment(status, judgment)
    reason = (
        ()
        if status is TechnicalStatus.DISABLED or judgment is ProductJudgment.OK
        else (Reason(FindingSource.VIMO, "synthetic"),)
    )
    cameras = tuple(
        replace(
            camera,
            frame_id=replace(camera.frame_id, cycle_id=CycleId(cycle))
            if camera.frame_id
            else None,
            channels=tuple(
                replace(
                    row,
                    checks=(
                        row.checks[0],
                        CheckResult(FindingSource.VIMO, assessment, reason),
                    ),
                )
                for row in camera.channels
            ),
        )
        for camera in base.cameras
    )
    return replace(base, cycle_id=CycleId(cycle), cameras=cameras)


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
