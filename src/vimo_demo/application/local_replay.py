"""Session-local source gating for supported local replay, not Production pause."""

from concurrent.futures import Future
from dataclasses import dataclass
from enum import Enum
from threading import Condition, Event
from typing import Protocol

from .models import ApplicationStateError


class ReplayState(Enum):
    AUTOMATIC = "automatic"
    PAUSING = "pausing"
    PAUSED = "paused"
    STEPPING = "stepping"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class ReplaySnapshot:
    generation: int
    state: ReplayState


def _future() -> Future[None]:
    future: Future[None] = Future()
    future.set_running_or_notify_cancel()
    return future


class LocalReplayControl:
    """One source owner claims whole groups; commands never wait for inspection.

    Futures observe safe boundaries, not source/thread exit or FINS ACK. Repeated
    Step shares the current Future; there is at most one outstanding step permit.
    """

    def __init__(self, generation: int, *, single_step: bool = False) -> None:
        self.generation = generation
        self._condition = Condition()
        self.changed = Event()  # Also interrupts the existing source pacing wait.
        self._state = ReplayState.STEPPING if single_step else ReplayState.AUTOMATIC
        self._inflight = False
        self._reset_pacing = True
        self._operation = _future() if single_step else None

    def snapshot(self) -> ReplaySnapshot:
        with self._condition:
            return ReplaySnapshot(self.generation, self._state)

    def _notify(self) -> None:
        self.changed.set()
        self._condition.notify_all()

    def pause(self) -> Future[None]:
        with self._condition:
            if self._state is ReplayState.STOPPED:
                raise ApplicationStateError("Local replay has stopped")
            if self._state in (ReplayState.PAUSING, ReplayState.STEPPING):
                assert self._operation is not None
                return self._operation
            completed = _future()
            self._reset_pacing = True
            if self._inflight:
                self._state = ReplayState.PAUSING
                self._operation = completed
                self._notify()
                return completed
            self._state = ReplayState.PAUSED
            self._notify()
        completed.set_result(None)
        return completed

    def resume(self) -> Future[None]:
        with self._condition:
            if self._state is not ReplayState.PAUSED:
                raise ApplicationStateError("Continue requires paused local replay")
            self._state = ReplayState.AUTOMATIC
            self._reset_pacing = True
            self._notify()
        completed = _future()
        completed.set_result(None)
        return completed

    def step(self) -> Future[None]:
        with self._condition:
            if self._state is ReplayState.STEPPING:
                assert self._operation is not None
                return self._operation
            if self._state is not ReplayState.PAUSED:
                raise ApplicationStateError("Step requires paused local replay")
            self._state = ReplayState.STEPPING
            self._operation = _future()
            self._reset_pacing = True
            self._notify()
            return self._operation

    def wait_for_turn(self) -> bool:
        """Source-only wait, interruptible by shutdown, with no polling/spinning."""
        with self._condition:
            self._condition.wait_for(lambda: self._state is not ReplayState.PAUSED)
            self.changed.clear()
            return self._state is not ReplayState.STOPPED

    def pacing_start(self, previous: float | None) -> float | None:
        with self._condition:
            return (
                previous
                if self._state is ReplayState.AUTOMATIC and not self._reset_pacing
                else None
            )

    def claim(self) -> bool:
        """Linearize Pause against the next source group's preparation/admission."""
        with self._condition:
            if self._inflight or self._state not in (
                ReplayState.AUTOMATIC,
                ReplayState.STEPPING,
            ):
                return False
            self._inflight = True
            self._reset_pacing = False
            return True

    def complete_cycle(self) -> None:
        """Call only after the formal terminal result and display publication."""
        completed = None
        with self._condition:
            self._inflight = False
            if self._state in (ReplayState.PAUSING, ReplayState.STEPPING):
                self._state = ReplayState.PAUSED
                completed, self._operation = self._operation, None
            self._notify()
        if completed is not None:
            completed.set_result(None)

    def stop(self, error: BaseException | None = None) -> None:
        with self._condition:
            self._state = ReplayState.STOPPED
            completed, self._operation = self._operation, None
            self._notify()
        if completed is not None:
            completed.set_exception(
                error or ApplicationStateError("Local replay ended before completion")
            )


class LocalReplayCommands(Protocol):
    """The local composition command boundary consumed by the optional Qt view."""

    @property
    def replay_available(self) -> bool: ...
    def replay_snapshot(self) -> ReplaySnapshot | None: ...
    def pause_replay(self) -> Future[None]: ...
    def continue_replay(self) -> Future[None]: ...
    def step_replay(self) -> Future[None]: ...
