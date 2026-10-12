"""Deterministic lifecycle/counter tests with controlled session Futures/events."""

from concurrent.futures import TimeoutError
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from threading import Event

import pytest
from tests.application_support import (
    WAIT,
    FakeSession,
    ReplayFuture,
    close,
    prepared,
    production,
)

from vimo_demo.application import (
    ApplicationCapacityError,
    ApplicationError,
    ApplicationState,
    ApplicationStateError,
    PreparedConfiguration,
    ProductionApplication,
    ProductionCounters,
)
from vimo_demo.inspection import CycleId
from vimo_demo.inspection.production import ProductJudgment, TechnicalStatus


def test_start_is_async_transaction_and_stop_during_start_cannot_activate(
    tmp_path: Path,
) -> None:
    session = FakeSession(automatic_start=False)
    app = ProductionApplication(lambda _prepared, _generation: session)
    try:
        assert app.snapshot().state is ApplicationState.UNCONFIGURED
        with pytest.raises(ApplicationStateError):
            app.start()
        app.configure(prepared(tmp_path))
        assert app.snapshot().state is ApplicationState.READY
        started = app.start()
        assert session.start_entered.wait(WAIT)
        assert not started.done() and app.snapshot().state is ApplicationState.STARTING
        assert app.snapshot().active is None
        with pytest.raises(ApplicationStateError):
            app.configure(prepared(tmp_path))
        stopped = app.stop()
        assert app.stop() is stopped
        assert app.snapshot().state is ApplicationState.STOPPING
        session.ready.set_result(None)
        stopped.result(WAIT)
        with pytest.raises(ApplicationError):
            started.result(WAIT)
        assert app.snapshot().state is ApplicationState.READY
        assert app.snapshot().active is None
        assert app.snapshot().counters == ProductionCounters()
    finally:
        close(app)
    assert app.snapshot().state is ApplicationState.CLOSED
    with pytest.raises(ApplicationStateError):
        app.start()


def test_candidate_failure_preserves_active_and_later_start_is_fresh(
    tmp_path: Path,
) -> None:
    sessions = [FakeSession(), FakeSession(automatic_start=False), FakeSession()]
    sessions[1].ready.set_exception(ValueError("warmup failed"))
    creations: list[int] = []

    def factory(config: PreparedConfiguration, generation: int) -> FakeSession:
        creations.append(generation)
        return sessions[len(creations) - 1]

    app = ProductionApplication(factory)
    first = prepared(tmp_path)
    try:
        app.configure(first)
        app.start().result(WAIT)
        assert app.snapshot().active is not None
        old = app.snapshot().active
        app.stop().result(WAIT)
        candidate = replace(
            first, configuration=replace(first.configuration, recipe_id="other")
        )
        app.configure(candidate)
        with pytest.raises(ApplicationError, match="warmup failed") as failure:
            app.start().result(WAIT)
        assert isinstance(failure.value.__cause__, ValueError)
        assert app.snapshot().state is ApplicationState.READY
        assert app.snapshot().active == old
        assert app.snapshot().candidate != old
        assert app.snapshot().last_issue is not None
        app.start().result(WAIT)
        assert app.snapshot().active == app.snapshot().candidate
        assert creations == [1, 2, 3]
        sessions[0].finished.replay()  # Obsolete generation cannot fault new runtime.
        assert app.snapshot().state is ApplicationState.RUNNING
    finally:
        close(app)


def test_factory_failure_and_cleanup_failure_are_distinct(tmp_path: Path) -> None:
    def broken(config: PreparedConfiguration, generation: int) -> FakeSession:
        raise ValueError("factory failed before resources")

    app = ProductionApplication(broken)
    try:
        app.configure(prepared(tmp_path))
        with pytest.raises(ApplicationError):
            app.start().result(WAIT)
        assert app.snapshot().state is ApplicationState.READY
        assert not app.snapshot().runtime_owned
    finally:
        close(app)
    session = FakeSession(automatic_start=False)
    session.ready.set_exception(ValueError("construction failed"))
    session.cleanup_safe = False
    app = ProductionApplication(lambda _prepared, _generation: session)
    try:
        app.configure(prepared(tmp_path))
        with pytest.raises(ApplicationError):
            app.start().result(WAIT)
        assert app.snapshot().state is ApplicationState.FAULT
        assert app.snapshot().runtime_owned
        with pytest.raises(ApplicationStateError):
            app.reset_fault()
        with pytest.raises(ApplicationError):
            app.close().result(WAIT)
        assert app.snapshot().state is ApplicationState.FAULT
        session.cleanup_safe = True  # Simulated explicit restoration of safe cleanup.
        app.stop().result(WAIT)
        app.reset_fault()
        assert app.snapshot().state is ApplicationState.READY
    finally:
        session.cleanup_safe = True
        close(app)


@pytest.mark.parametrize(
    "status,judgment,field",
    [
        (TechnicalStatus.SUCCESS, ProductJudgment.OK, "channel_ok"),
        (TechnicalStatus.SUCCESS, ProductJudgment.NG, "channel_ng"),
        (TechnicalStatus.ERROR, ProductJudgment.UNKNOWN, "channel_unknown"),
        (TechnicalStatus.ERROR, ProductJudgment.NG, "channel_ng"),
        (TechnicalStatus.DISABLED, ProductJudgment.UNKNOWN, "channel_disabled"),
    ],
)
def test_channel_counter_partition_and_cycle_error_is_not_fault(
    tmp_path: Path, status: TechnicalStatus, judgment: ProductJudgment, field: str
) -> None:
    session = FakeSession()
    app = ProductionApplication(lambda _prepared, _generation: session)
    try:
        app.configure(prepared(tmp_path))
        app.start().result(WAIT)
        observed = app.begin_cycle(CycleId(1))
        assert app.snapshot().counters.cycles_started == 0
        session.cycles[0].accepted.set_result(None)
        observed.accepted.result(WAIT)
        session.cycles[0].result.set_result(
            production(status=status, judgment=judgment)
        )
        observed.result.result(WAIT)
        counters = app.snapshot().counters
        assert counters.cycles_started == counters.cycles_completed == 1
        assert getattr(counters, field) == 36
        assert (
            sum(
                (
                    counters.channel_ok,
                    counters.channel_ng,
                    counters.channel_unknown,
                    counters.channel_disabled,
                )
            )
            == 36
        )
        assert counters.production_ng_cycles == int(judgment is ProductJudgment.NG)
        assert counters.technical_error_cycles == int(status is TechnicalStatus.ERROR)
        assert app.snapshot().state is ApplicationState.RUNNING
        for future in (session.cycles[0].accepted, session.cycles[0].result):
            assert isinstance(future, ReplayFuture)
            future.replay()
        assert app.snapshot().counters == counters
    finally:
        close(app)


def test_admission_capacity_batch_and_stale_results(tmp_path: Path) -> None:
    sessions = [FakeSession(), FakeSession()]
    app = ProductionApplication(
        lambda _, generation: sessions[generation - 1], max_pending_cycles=1
    )
    config = prepared(tmp_path)
    try:
        app.configure(config)
        app.start().result(WAIT)
        assert app.snapshot().batch_id is None
        rejected = app.begin_cycle(CycleId(1))
        with pytest.raises(ApplicationCapacityError):
            app.begin_cycle(CycleId(2))
        sessions[0].cycles[0].accepted.set_exception(ValueError("collector capacity"))
        with pytest.raises(ValueError):
            rejected.result.result(WAIT)
        assert app.snapshot().counters.cycles_started == 0
        cycle = app.begin_cycle(CycleId(2))
        source = sessions[0].cycles[1]
        source.accepted.set_result(None)
        source.result.set_result(production(2))
        cycle.result.result(WAIT)
        with pytest.raises(ApplicationStateError):
            app.start_batch("new")
        app.stop().result(WAIT)
        saved = app.snapshot().counters
        app.configure(
            replace(
                config, configuration=replace(config.configuration, recipe_id="new")
            )
        )
        assert app.snapshot().counters == saved
        app.start().result(WAIT)
        assert app.snapshot().latest_production_result is None
        assert app.snapshot().counters == saved
        assert isinstance(source.result, ReplayFuture)
        source.result.replay()
        assert app.snapshot().counters == saved
        assert app.snapshot().latest_production_result is None
        app.stop().result(WAIT)
        app.start_batch("batch-2")
        assert app.snapshot().batch_id == "batch-2"
        assert app.snapshot().counters == ProductionCounters()
        assert app.snapshot().latest_production_result is None
    finally:
        close(app)


def test_blocked_stop_does_not_fake_ready_or_close_and_counts_active_result(
    tmp_path: Path,
) -> None:
    session = FakeSession()
    session.join_gate = Event()
    app = ProductionApplication(lambda _prepared, _generation: session)
    try:
        app.configure(prepared(tmp_path))
        app.start().result(WAIT)
        cycle = app.begin_cycle(CycleId(1))
        session.cycles[0].accepted.set_result(None)
        stopped = app.stop()
        assert session.join_entered.wait(WAIT)
        with pytest.raises(TimeoutError):
            stopped.result(0)
        assert app.snapshot().state is ApplicationState.STOPPING
        with pytest.raises(ApplicationStateError):
            app.begin_cycle(CycleId(2))
        closed = app.close()
        assert not closed.done() and not app.join(0)
        session.cycles[0].result.set_result(production())
        cycle.result.result(WAIT)
        assert app.snapshot().counters.cycles_completed == 1
        session.join_gate.set()
        closed.result(WAIT)
        assert app.join(WAIT)
    finally:
        session.join_gate.set()
        close(app)


def test_unexpected_runtime_termination_fault_reset_and_snapshot_immutability(
    tmp_path: Path,
) -> None:
    session = FakeSession()
    session.join_gate = Event()
    app = ProductionApplication(lambda _prepared, _generation: session)
    try:
        app.configure(prepared(tmp_path))
        app.start().result(WAIT)
        session.failure = RuntimeError("worker terminal failure")
        session.finished.set_result(None)
        assert session.join_entered.wait(WAIT)
        stopped = app.stop()
        session.join_gate.set()
        with pytest.raises(ApplicationError):
            stopped.result(WAIT)
        snapshot = app.snapshot()
        assert snapshot.state is ApplicationState.FAULT and not snapshot.runtime_owned
        assert snapshot.last_issue is not None
        with pytest.raises(ApplicationStateError):
            app.begin_cycle(CycleId(1))
        field_name = "batch_id"
        with pytest.raises(FrozenInstanceError):
            setattr(snapshot, field_name, "bad")
        app.reset_fault()
        assert app.snapshot().state is ApplicationState.READY
    finally:
        session.join_gate.set()
        close(app)
