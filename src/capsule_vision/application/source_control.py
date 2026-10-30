"""Generation-scoped Folder replay controls and the shared source-command boundary."""

from concurrent.futures import Future
from dataclasses import dataclass
from enum import Enum
from threading import Condition, Event
from typing import Protocol, runtime_checkable

from .models import ApplicationStateError


class SourceControlState(Enum):
    AUTOMATIC = "automatic"
    PAUSING = "pausing"
    PAUSED = "paused"
    STEPPING = "stepping"
    RESUMING = "resuming"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class SourceControlSnapshot:
    generation: int
    state: SourceControlState


def _future() -> Future[None]:
    future: Future[None] = Future()
    future.set_running_or_notify_cancel()
    return future


class FolderReplayControl:
    """One source owner claims whole groups; commands never wait for inspection.

    Futures observe safe boundaries, not source/thread exit or FINS ACK. Repeated
    Step shares the current Future; there is at most one outstanding step permit.
    """

    def __init__(self, generation: int, *, single_step: bool = False) -> None:
        self.generation = generation
        self._condition = Condition()
        self.changed = Event()  # Also interrupts the existing source pacing wait.
        self._state = (
            SourceControlState.STEPPING if single_step else SourceControlState.AUTOMATIC
        )
        self._inflight = False
        self._reset_pacing = True
        self._operation = _future() if single_step else None

    def snapshot(self) -> SourceControlSnapshot:
        with self._condition:
            return SourceControlSnapshot(self.generation, self._state)

    def _notify(self) -> None:
        self.changed.set()
        self._condition.notify_all()

    def pause(self) -> Future[None]:
        with self._condition:
            if self._state is SourceControlState.STOPPED:
                raise ApplicationStateError("Folder replay has stopped")
            if self._state in (SourceControlState.PAUSING, SourceControlState.STEPPING):
                assert self._operation is not None
                return self._operation
            completed = _future()
            self._reset_pacing = True
            if self._inflight:
                self._state = SourceControlState.PAUSING
                self._operation = completed
                self._notify()
                return completed
            self._state = SourceControlState.PAUSED
            self._notify()
        completed.set_result(None)
        return completed

    def resume(self) -> Future[None]:
        with self._condition:
            if self._state is not SourceControlState.PAUSED:
                raise ApplicationStateError("Continue requires paused Folder replay")
            self._state = SourceControlState.AUTOMATIC
            self._reset_pacing = True
            self._notify()
        completed = _future()
        completed.set_result(None)
        return completed

    def step(self) -> Future[None]:
        with self._condition:
            if self._state is SourceControlState.STEPPING:
                assert self._operation is not None
                return self._operation
            if self._state is not SourceControlState.PAUSED:
                raise ApplicationStateError("Step requires paused Folder replay")
            self._state = SourceControlState.STEPPING
            self._operation = _future()
            self._reset_pacing = True
            self._notify()
            return self._operation

    def wait_for_turn(self) -> bool:
        """Source-only wait, interruptible by shutdown, with no polling/spinning."""
        with self._condition:
            self._condition.wait_for(
                lambda: self._state is not SourceControlState.PAUSED
            )
            self.changed.clear()
            return self._state is not SourceControlState.STOPPED

    def pacing_start(self, previous: float | None) -> float | None:
        with self._condition:
            return (
                previous
                if self._state is SourceControlState.AUTOMATIC
                and not self._reset_pacing
                else None
            )

    def claim(self) -> bool:
        """Linearize Pause against the next source group's preparation/admission."""
        with self._condition:
            if self._inflight or self._state not in (
                SourceControlState.AUTOMATIC,
                SourceControlState.STEPPING,
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
            if self._state in (SourceControlState.PAUSING, SourceControlState.STEPPING):
                self._state = SourceControlState.PAUSED
                completed, self._operation = self._operation, None
            self._notify()
        if completed is not None:
            completed.set_result(None)

    def stop(self, error: BaseException | None = None) -> None:
        with self._condition:
            self._state = SourceControlState.STOPPED
            completed, self._operation = self._operation, None
            self._notify()
        if completed is not None:
            completed.set_exception(
                error or ApplicationStateError("Folder replay ended before completion")
            )


@runtime_checkable
class SourceControlCommands(Protocol):
    """Source admission commands; Folder pacing and Basler draining stay distinct."""

    @property
    def control_available(self) -> bool: ...
    def control_snapshot(self) -> SourceControlSnapshot | None: ...
    def pause_source(self) -> Future[None]: ...
    def resume_source(self) -> Future[None]: ...
    def step_source(self) -> Future[None]: ...
