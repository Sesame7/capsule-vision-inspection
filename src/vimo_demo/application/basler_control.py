"""Bounded passive-camera admission; SDK draining stays on the acquisition owner."""

from concurrent.futures import Future
from dataclasses import dataclass
from threading import Lock

from vimo_demo.inspection import CycleId

from .local_replay import ReplaySnapshot, ReplayState
from .models import ApplicationCapacityError, ApplicationStateError


def _promise() -> Future[None]:
    future: Future[None] = Future()
    future.set_running_or_notify_cancel()
    return future


@dataclass(frozen=True, slots=True)
class BaslerControlSnapshot(ReplaySnapshot):
    """Player state plus scalar diagnostics; no image/result history."""

    boundary_ready: bool
    admission_armed: bool
    pending_cycles: int
    received_frames: int
    discarded_frames: int


class BaslerAdmissionControl:
    """One SDK owner claims captures; terminal callbacks settle bounded tokens.

    ReplayState is the existing player vocabulary, not Folder's pacing/permit
    implementation. PAUSED additionally waits for inspection and accepted reports.
    """

    def __init__(self, generation: int, *, capacity: int = 16) -> None:
        if capacity < 1:
            raise ValueError("Admission capacity must be positive")
        self.generation = generation
        self._lock = Lock()
        self._capacity = capacity
        self._state = ReplayState.AUTOMATIC
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
                ReplayState.PAUSING,
                ReplayState.STEPPING,
                ReplayState.PAUSED,
            )

    def pause(self) -> Future[None]:
        with self._lock:
            if self._state is ReplayState.PAUSED:
                completed = _promise()
            elif self._state is ReplayState.PAUSING:
                assert self._operation is not None
                return self._operation
            elif self._state is ReplayState.AUTOMATIC:
                self._state = ReplayState.PAUSING
                self._armed = False
                self._operation = _promise()
                return self._operation
            else:
                raise ApplicationStateError(
                    "Basler Pause requires continuous admission"
                )
        completed.set_result(None)
        return completed

    def _request(self, state: ReplayState) -> Future[None]:
        with self._lock:
            if self._state is state and self._operation is not None:
                return self._operation
            if self._state is not ReplayState.PAUSED:
                raise ApplicationStateError("Basler Step/Resume requires PAUSED")
            self._state, self._armed = state, False
            self._step_cycle = None
            self._operation = _promise()
            return self._operation

    def step(self) -> Future[None]:
        return self._request(ReplayState.STEPPING)

    def resume(self) -> Future[None]:
        return self._request(ReplayState.RESUMING)

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
                self._state in (ReplayState.RESUMING, ReplayState.STEPPING)
                and not self._armed
                and self._step_cycle is None
            ):
                if clean:
                    self._armed = True
                    if self._state is ReplayState.RESUMING:
                        self._state = ReplayState.AUTOMATIC
                        completed, self._operation = self._operation, None
                else:
                    self._state = ReplayState.PAUSED
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
                ReplayState.AUTOMATIC,
                ReplayState.STEPPING,
            ):
                return False
            if self._state is ReplayState.STEPPING and self._step_cycle is not None:
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
            if self._state is ReplayState.STEPPING:
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
                self._state is ReplayState.PAUSING
                or self._state is ReplayState.STEPPING
                and self._step_cycle is not None
            )
        ):
            self._state, self._armed = ReplayState.PAUSED, False
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
            self._state, self._armed = ReplayState.STOPPED, False
            completed, self._operation = self._operation, None
            self._pending.clear()
        self._resolve(
            completed, error or ApplicationStateError("Basler admission stopped")
        )
