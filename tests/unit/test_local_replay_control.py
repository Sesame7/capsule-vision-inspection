"""Deterministic source-boundary and single-permit tests; no engine or Qt."""

from threading import Event, Thread

import pytest

from vimo_demo.application import (
    ApplicationState,
    ApplicationStateError,
    ProductionApplicationSnapshot,
    ProductionCounters,
)
from vimo_demo.application.local_replay import LocalReplayControl, ReplayState
from vimo_demo.ui.production_presentation import production_actions


def test_pause_during_claim_waits_for_the_formal_cycle_boundary() -> None:
    control = LocalReplayControl(4)
    assert control.wait_for_turn() and control.claim()
    paused = control.pause()
    assert not paused.done() and not paused.cancel()
    assert control.snapshot().state is ReplayState.PAUSING
    assert control.pause() is paused and not control.claim()
    control.complete_cycle()
    paused.result(0)
    assert control.snapshot().state is ReplayState.PAUSED
    assert control.snapshot().generation == 4


def test_idle_pause_and_one_step_permit_even_with_repeated_commands() -> None:
    control = LocalReplayControl(1)
    control.pause().result(0)
    assert not control.claim()
    stepped = control.step()
    assert all(control.step() is stepped for _ in range(20))
    assert control.wait_for_turn() and control.claim()
    assert not control.claim() and not stepped.done()
    assert control.pause() is stepped
    with pytest.raises(ApplicationStateError):
        control.resume()
    control.complete_cycle()
    stepped.result(0)
    assert control.snapshot().state is ReplayState.PAUSED
    assert not control.claim()


def test_initial_step_has_one_permit_and_no_automatic_tail() -> None:
    control = LocalReplayControl(1, single_step=True)
    stepped = control.step()
    assert control.claim()
    control.complete_cycle()
    stepped.result(0)
    assert control.snapshot().state is ReplayState.PAUSED
    assert not control.claim()


def test_resume_and_steps_reset_pacing_without_accumulated_pause_debt() -> None:
    control = LocalReplayControl(1)
    assert control.pacing_start(10) is None
    assert control.claim()
    control.complete_cycle()
    assert control.pacing_start(10) == 10
    control.pause().result(0)
    control.resume().result(0)
    assert control.changed.is_set()
    assert control.wait_for_turn() and not control.changed.is_set()
    assert control.pacing_start(10) is None  # Resume never uses the pre-pause start.
    assert control.claim()
    control.complete_cycle()
    assert control.pacing_start(400) == 400  # Next auto turn anchors to its new start.
    control.pause().result(0)
    control.step()
    assert control.pacing_start(400) is None


def test_operator_wait_is_woken_by_stop_without_polling_or_residual_thread() -> None:
    control = LocalReplayControl(1)
    control.pause().result(0)
    entered = Event()
    outcomes: list[bool] = []

    def source() -> None:
        entered.set()
        outcomes.append(control.wait_for_turn())

    owner = Thread(target=source, name="test-paused-replay")
    owner.start()
    try:
        assert entered.wait(2)
        control.stop()
    finally:
        control.stop()
        owner.join(2)
    assert not owner.is_alive() and outcomes == [False]


@pytest.mark.parametrize("step", [False, True])
def test_shutdown_settles_pending_operation_without_fake_completion(step: bool) -> None:
    control = LocalReplayControl(1, single_step=step)
    assert control.claim()
    operation = control.step() if step else control.pause()
    control.stop()
    control.stop()
    with pytest.raises(ApplicationStateError, match="ended before completion"):
        operation.result(0)
    control.complete_cycle()  # A late terminal observation never replaces the error.
    assert control.snapshot().state is ReplayState.STOPPED
    assert not control.claim()


@pytest.mark.parametrize(
    "state,replay,expected",
    [
        (
            ApplicationState.READY,
            ReplayState.STOPPED,
            ("start", None, None, None),
        ),
        (
            ApplicationState.RUNNING,
            ReplayState.AUTOMATIC,
            (None, "replay_pause", "stop", None),
        ),
        (
            ApplicationState.RUNNING,
            ReplayState.PAUSED,
            ("replay_continue", "replay_step", "stop", None),
        ),
        (ApplicationState.RUNNING, ReplayState.PAUSING, (None, None, "stop", None)),
        (ApplicationState.RUNNING, ReplayState.STEPPING, (None, None, "stop", None)),
        (ApplicationState.RUNNING, None, (None, None, "stop", None)),
        (ApplicationState.STARTING, ReplayState.STEPPING, (None, None, "stop", None)),
        (ApplicationState.STOPPING, ReplayState.STOPPED, (None, None, None, None)),
        (ApplicationState.FAULT, ReplayState.STOPPED, (None, None, None, "reset")),
    ],
)
def test_fixed_player_actions_keep_unsupported_pause_disabled(
    state: ApplicationState,
    replay: ReplayState | None,
    expected: tuple[str | None, ...],
) -> None:
    snapshot = ProductionApplicationSnapshot(
        state, 1, None, None, None, ProductionCounters(), None, False, None
    )
    assert tuple(production_actions(snapshot, replay).values()) == expected
