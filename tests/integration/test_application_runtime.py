"""Actual application/coordinator/worker with controlled fake native engine."""

from dataclasses import replace
from pathlib import Path
from threading import Event, get_ident
from threading import enumerate as threads

import numpy as np
import pytest
from numpy.typing import NDArray
from tests.unit.test_configured_execution import config
from tests.unit.test_production_application import WAIT, close, prepared
from tests.unit.test_production_inspection import inferred

from vimo_demo.application import (
    ApplicationError,
    ApplicationState,
    ProductionApplication,
    ProductionCounters,
    ProductionCycle,
    ProductionRuntimeSession,
)
from vimo_demo.inference import ImageResult
from vimo_demo.inspection import CycleId, Frame, FrameId
from vimo_demo.inspection.production import TechnicalStatus
from vimo_demo.runtime import CoordinatorCapacityError, CoordinatorStoppedError


class Engine:
    def __init__(
        self,
        trace: list[tuple[str, int]],
        *,
        fail: bool = False,
        block_call: int | None = None,
    ) -> None:
        self.trace = trace
        self.fail = fail
        self.block_call = block_call
        self.calls = 0
        self.entered = Event()
        self.release = Event()
        trace.append(("create", get_ident()))

    def infer(self, image_bgr: NDArray[np.generic]) -> ImageResult:
        self.calls += 1
        self.trace.append(("infer", get_ident()))
        if self.calls == self.block_call:
            self.entered.set()
            assert self.release.wait(WAIT)
        if self.fail:
            raise ValueError("warmup inference failed")
        return inferred()

    def close(self) -> None:
        self.trace.append(("close", get_ident()))


def test_real_composition_warmup_cycles_stop_and_restart(tmp_path: Path) -> None:
    candidate = replace(prepared(tmp_path), configuration=config(tmp_path))
    trace: list[tuple[str, int]] = []
    sessions: list[ProductionRuntimeSession] = []

    def factory(prepared_config: object, generation: int) -> ProductionRuntimeSession:
        session = ProductionRuntimeSession(
            candidate,
            generation,
            warmup=lambda _: (
                np.zeros((20, 60, 3), dtype=np.uint8),
                np.ones((20, 60, 3), dtype=np.uint8),
            ),
            warmup_iterations=2,
            engine_factory=lambda _: Engine(trace),
            cycle_timeout=30,
        )
        sessions.append(session)
        return session

    app = ProductionApplication(factory)
    try:
        app.configure(candidate)
        app.start().result(WAIT)
        assert [name for name, _ in trace] == ["create", "infer", "infer"]
        assert app.snapshot().counters == ProductionCounters()
        assert app.snapshot().latest_production_result is None
        cycle = app.begin_cycle(CycleId(1))
        cycle.accepted.result(WAIT)
        assert app.snapshot().counters.cycles_started == 1
        for camera in reversed(candidate.configuration.layout.cameras):
            app.submit_frame(
                Frame(
                    FrameId(CycleId(1), camera.camera_id, 0),
                    0,
                    np.zeros((20, 60, 3), dtype=np.uint8),
                )
            ).result(WAIT)
        result = cycle.result.result(WAIT)
        assert len(result.channels) == 36 and app.snapshot().counters.channel_ok == 36
        app.stop().result(WAIT)
        assert app.snapshot().state is ApplicationState.READY
        assert not set(sessions[0].thread_names) & {t.name for t in threads()}
        assert len({ident for _, ident in trace}) == 1 and trace[0][1] != get_ident()
        assert [name for name, _ in trace] == ["create", *["infer"] * 8, "close"]
        app.start().result(WAIT)
        assert len(sessions) == 2
        assert app.snapshot().counters.cycles_completed == 1
        assert app.snapshot().latest_production_result is None
    finally:
        close(app)
    assert not (
        {app.thread_name}
        | {name for session in sessions for name in session.thread_names}
    ) & {t.name for t in threads()}


def test_real_warmup_failure_cleans_up_without_activation(tmp_path: Path) -> None:
    candidate = prepared(tmp_path)
    trace: list[tuple[str, int]] = []
    session = ProductionRuntimeSession(
        candidate,
        1,
        warmup=lambda _: (np.zeros((20, 60, 3), dtype=np.uint8),),
        engine_factory=lambda _: Engine(trace, fail=True),
    )
    app = ProductionApplication(lambda _prepared, _generation: session)
    try:
        app.configure(candidate)
        with pytest.raises(ApplicationError, match="warmup inference failed"):
            app.start().result(WAIT)
        assert app.snapshot().state is ApplicationState.READY
        assert app.snapshot().active is None and not app.snapshot().runtime_owned
        assert app.snapshot().counters == ProductionCounters()
        assert [name for name, _ in trace] == ["create", "infer", "close"]
        assert len({ident for _, ident in trace}) == 1
    finally:
        close(app)


def test_active_whole_cycle_finishes_queued_cycle_rejected_and_backlog_bounded(
    tmp_path: Path,
) -> None:
    candidate = prepared(tmp_path)
    trace: list[tuple[str, int]] = []
    engines: list[Engine] = []

    def engine_factory(_: object) -> Engine:
        engine = Engine(trace, block_call=2)
        engines.append(engine)
        return engine

    session = ProductionRuntimeSession(
        candidate,
        1,
        warmup_iterations=1,
        warmup=lambda _: (np.zeros((20, 60, 3), dtype=np.uint8),),
        engine_factory=engine_factory,
        max_ready=1,
        cycle_timeout=30,
    )
    app = ProductionApplication(lambda _prepared, _generation: session)
    try:
        app.configure(candidate)
        app.start().result(WAIT)
        cycles: list[ProductionCycle] = []
        for cycle_id in (1, 2, 3):
            handle = app.begin_cycle(CycleId(cycle_id))
            cycles.append(handle)
            handle.accepted.result(WAIT)
            for camera in candidate.configuration.layout.cameras:
                app.submit_frame(
                    Frame(
                        FrameId(CycleId(cycle_id), camera.camera_id, 0),
                        0,
                        np.zeros((20, 60, 3), dtype=np.uint8),
                    )
                ).result(WAIT)
            if cycle_id == 1:
                assert engines[0].entered.wait(WAIT)
        with pytest.raises(CoordinatorCapacityError):
            cycles[2].result.result(WAIT)
        assert app.snapshot().counters.cycles_started == 3
        stopped = app.stop()
        assert not stopped.done()
        engines[0].release.set()
        cycles[0].result.result(WAIT)
        with pytest.raises(CoordinatorStoppedError):
            cycles[1].result.result(WAIT)
        stopped.result(WAIT)
        assert app.snapshot().state is ApplicationState.READY
        assert app.snapshot().counters.cycles_completed == 1
        assert (
            engines[0].calls == 7
        )  # One warm-up, then the entire active six-frame cycle.
    finally:
        for engine in engines:
            engine.release.set()
        close(app)


def test_close_during_native_warmup_waits_and_skips_remaining_iterations(
    tmp_path: Path,
) -> None:
    candidate = prepared(tmp_path)
    trace: list[tuple[str, int]] = []
    created = Event()
    engines: list[Engine] = []

    def factory(_: object) -> Engine:
        engine = Engine(trace, block_call=1)
        engines.append(engine)
        created.set()
        return engine

    session = ProductionRuntimeSession(
        candidate,
        1,
        warmup=lambda _: (np.zeros((20, 60, 3), dtype=np.uint8),),
        engine_factory=factory,
    )
    app = ProductionApplication(lambda _prepared, _generation: session)
    try:
        app.configure(candidate)
        startup = app.start()
        assert created.wait(WAIT) and engines[0].entered.wait(WAIT)
        closing = app.close()
        assert not closing.done() and not app.join(0)
        assert app.snapshot().state is ApplicationState.STOPPING
        engines[0].release.set()
        with pytest.raises(ApplicationError):
            startup.result(WAIT)
        closing.result(WAIT)
        assert app.join(WAIT)
        assert app.snapshot().state is ApplicationState.CLOSED
        assert app.snapshot().active is None
        assert app.snapshot().counters == ProductionCounters()
        assert [name for name, _ in trace] == ["create", "infer", "close"]
        assert not set(session.thread_names) & {t.name for t in threads()}
    finally:
        for engine in engines:
            engine.release.set()
        close(app)


class NativeFailure(BaseException):
    """A terminal worker failure, distinct from an ordinary image exception."""


@pytest.mark.parametrize("fatal", [False, True])
def test_runtime_frame_failure_is_not_confused_with_lifecycle_failure(
    tmp_path: Path, fatal: bool
) -> None:
    candidate = prepared(tmp_path)
    trace: list[tuple[str, int]] = []

    class FailingEngine(Engine):
        def infer(self, image_bgr: NDArray[np.generic]) -> ImageResult:
            result = super().infer(image_bgr)
            if self.calls == 2:
                if fatal:
                    raise NativeFailure("terminal native failure")
                raise ValueError("one image failed")
            return result

    session = ProductionRuntimeSession(
        candidate,
        1,
        warmup_iterations=1,
        warmup=lambda _: (np.zeros((20, 60, 3), dtype=np.uint8),),
        engine_factory=lambda _: FailingEngine(trace),
    )
    app = ProductionApplication(lambda _prepared, _generation: session)
    try:
        app.configure(candidate)
        app.start().result(WAIT)
        cycle = app.begin_cycle(CycleId(1))
        cycle.accepted.result(WAIT)
        for camera in candidate.configuration.layout.cameras:
            app.submit_frame(
                Frame(
                    FrameId(CycleId(1), camera.camera_id, 0),
                    0,
                    np.zeros((20, 60, 3), dtype=np.uint8),
                )
            ).result(WAIT)
        if fatal:
            with pytest.raises(NativeFailure):
                cycle.result.result(WAIT)
            # Automatic fault cleanup may already have released the session.
            # Stop reports a cleanup error only when it observes that cleanup.
            try:
                app.stop().result(WAIT)
            except ApplicationError:
                pass
            assert app.snapshot().state is ApplicationState.FAULT
            assert app.snapshot().last_issue is not None
            assert not app.snapshot().runtime_owned
            assert app.snapshot().counters.cycles_completed == 0
            app.reset_fault()
            assert app.snapshot().state is ApplicationState.READY
        else:
            result = cycle.result.result(WAIT)
            assert result.assessment.technical_status is TechnicalStatus.ERROR
            assert app.snapshot().state is ApplicationState.RUNNING
            assert app.snapshot().counters.technical_error_cycles == 1
            assert [name for name, _ in trace].count("infer") == 7
            app.stop().result(WAIT)
    finally:
        close(app)
    assert not set(session.thread_names) & {t.name for t in threads()}


def test_artifact_changed_after_preparation_never_constructs_engine(
    tmp_path: Path,
) -> None:
    candidate = prepared(tmp_path)
    candidate.model_path.write_bytes(b"changed")
    trace: list[tuple[str, int]] = []
    session = ProductionRuntimeSession(
        candidate,
        1,
        warmup=lambda _: (),
        engine_factory=lambda _: Engine(trace),
    )
    app = ProductionApplication(lambda _prepared, _generation: session)
    try:
        app.configure(candidate)
        with pytest.raises(ApplicationError):
            app.start().result(WAIT)
        assert not trace
        assert app.snapshot().state is ApplicationState.READY
        assert app.snapshot().active is None
    finally:
        close(app)
