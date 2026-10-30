"""Bounded passive-camera admission; SDK draining stays on the acquisition owner."""

from concurrent.futures import Future
from dataclasses import dataclass
from threading import Lock

from capsule_vision.inspection import CycleId

from .models import ApplicationCapacityError, ApplicationStateError
from .source_control import SourceControlSnapshot, SourceControlState


def _promise() -> Future[None]:
    future: Future[None] = Future()
    future.set_running_or_notify_cancel()
    return future


@dataclass(frozen=True, slots=True)
class BaslerControlSnapshot(SourceControlSnapshot):
    """Player state plus scalar diagnostics; no image/result history."""

    boundary_ready: bool
    admission_armed: bool
    pending_cycles: int
    received_frames: int
    discarded_frames: int


class BaslerAdmissionControl:
    """One SDK owner claims captures; terminal callbacks settle bounded tokens.

    SourceControlState is the existing player vocabulary, not Folder's pacing/permit
    implementation. PAUSED additionally waits for inspection and accepted reports.
    """

    def __init__(self, generation: int, *, capacity: int = 16) -> None:
        if capacity < 1:
            raise ValueError("Admission capacity must be positive")
        self.generation = generation
        self._lock = Lock()
        self._capacity = capacity
        self._state = SourceControlState.AUTOMATIC
        self._ready = False
        self._boundary_ready = False
        self._armed = True
        self._capture: CycleId | None = None
        self._step_cycle: CycleId | None = None
        self._pending: dict[CycleId, tuple[bool, bool]] = {}
        self._operation: Future[None] | None = None
        self._received = self._discarded = 0

    def snapshot(self) -> BaslerControlSnapshot:
        with self._lock:
            return BaslerControlSnapshot(
                self.generation,
                self._state,
                self._boundary_ready,
                self._armed,
                len(self._pending),
                self._received,
                self._discarded,
            )

    @property
    def settling(self) -> bool:
        with self._lock:
            return self._state in (
                SourceControlState.PAUSING,
                SourceControlState.STEPPING,
                SourceControlState.PAUSED,
            )

    def pause(self) -> Future[None]:
        with self._lock:
            if self._state is SourceControlState.PAUSED:
                completed = _promise()
            elif self._state is SourceControlState.PAUSING:
                assert self._operation is not None
                return self._operation
            elif self._state is SourceControlState.AUTOMATIC:
                self._state = SourceControlState.PAUSING
                self._armed = False
                self._operation = _promise()
                return self._operation
            else:
                raise ApplicationStateError(
                    "Basler Pause requires continuous admission"
                )
        completed.set_result(None)
        return completed

    def _request(self, state: SourceControlState) -> Future[None]:
        with self._lock:
            if self._state is state and self._operation is not None:
                return self._operation
            if self._state is not SourceControlState.PAUSED:
                raise ApplicationStateError("Basler Step/Resume requires PAUSED")
            self._state, self._armed = state, False
            self._step_cycle = None
            self._operation = _promise()
            return self._operation

    def step(self) -> Future[None]:
        return self._request(SourceControlState.STEPPING)

    def resume(self) -> Future[None]:
        return self._request(SourceControlState.RESUMING)

    def boundary(self, *, clean: bool) -> None:
        """SDK owner only, after a sweep with no available result on any camera.

        Existing queued groups were consumed with admission disabled. An incomplete
        ordinal cannot be used as a fence; reject the transition rather than shift.
        """
        completed = None
        error = None
        with self._lock:
            self._ready = True
            self._boundary_ready = clean
            if (
                self._state
                in (SourceControlState.RESUMING, SourceControlState.STEPPING)
                and not self._armed
                and self._step_cycle is None
            ):
                if clean:
                    self._armed = True
                    if self._state is SourceControlState.RESUMING:
                        self._state = SourceControlState.AUTOMATIC
                        completed, self._operation = self._operation, None
                else:
                    self._state = SourceControlState.PAUSED
                    completed, self._operation = self._operation, None
                    error = ApplicationStateError(
                        "Incomplete SDK capture boundary; Step/Resume rejected"
                    )
            settled = self._settle_locked()
            completed = completed or settled
        self._resolve(completed, error)

    def claim(self, cycle: CycleId) -> bool:
        with self._lock:
            if not self._armed or self._state not in (
                SourceControlState.AUTOMATIC,
                SourceControlState.STEPPING,
            ):
                return False
            if (
                self._state is SourceControlState.STEPPING
                and self._step_cycle is not None
            ):
                return False
            if len(self._pending) >= self._capacity:
                raise ApplicationCapacityError(
                    "Basler unsettled-cycle capacity reached"
                )
            if cycle in self._pending or self._capture is not None:
                raise ApplicationStateError("Basler capture already admitted")
            self._capture = cycle
            self._pending[cycle] = False, False
            self._boundary_ready = False
            if self._state is SourceControlState.STEPPING:
                self._step_cycle = cycle
            return True

    def received(self, *, discarded: bool) -> None:
        with self._lock:
            self._received += 1
            self._discarded += int(discarded)

    def capture_finished(self, cycle: CycleId) -> None:
        with self._lock:
            if self._capture == cycle:
                self._capture = None
            completed = self._settle_locked()
        self._resolve(completed)

    def inspected(self, cycle: CycleId) -> None:
        self._settled(cycle, inspection=True)

    def reported(self, cycle: CycleId) -> None:
        self._settled(cycle, inspection=False)

    def _settled(self, cycle: CycleId, *, inspection: bool) -> None:
        with self._lock:
            pending = self._pending.get(cycle)
            if pending is not None:
                value = (pending[0] or inspection, pending[1] or not inspection)
                if all(value):
                    del self._pending[cycle]
                else:
                    self._pending[cycle] = value
            completed = self._settle_locked()
        self._resolve(completed)

    def _settle_locked(self) -> Future[None] | None:
        if (
            self._ready
            and self._capture is None
            and not self._pending
            and (
                self._state is SourceControlState.PAUSING
                or self._state is SourceControlState.STEPPING
                and self._step_cycle is not None
            )
        ):
            self._state, self._armed = SourceControlState.PAUSED, False
            completed, self._operation = self._operation, None
            return completed
        return None

    @staticmethod
    def _resolve(
        completed: Future[None] | None, error: BaseException | None = None
    ) -> None:
        if completed is not None:
            if error is None:
                completed.set_result(None)
            else:
                completed.set_exception(error)

    def stop(self, error: BaseException | None = None) -> None:
        with self._lock:
            self._state, self._armed = SourceControlState.STOPPED, False
            completed, self._operation = self._operation, None
            self._pending.clear()
        self._resolve(
            completed, error or ApplicationStateError("Basler admission stopped")
        )
