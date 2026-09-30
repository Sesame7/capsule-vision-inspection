"""Bounded collection owner plus a serial cycle runner, never an engine owner."""

import math
from collections import deque
from collections.abc import Callable, Sequence
from concurrent.futures import Future
from dataclasses import dataclass
from enum import Enum, auto
from threading import Condition, Thread, current_thread
from time import monotonic

import numpy as np
from numpy.typing import NDArray

from vimo_demo.inference import ImageResult
from vimo_demo.inspection import (
    CameraId,
    CollectedCycle,
    CollectionStatus,
    CycleCollector,
    CycleId,
    Frame,
    InspectionCycleResult,
    inspect_cycle,
)

from .inference_worker import InferenceWorker, WorkerState


class CoordinatorError(RuntimeError):
    """Runtime failure, distinct from an individual image inspection failure."""


class CoordinatorStateError(CoordinatorError):
    """Operation is unavailable in this lifecycle state."""


class CoordinatorCapacityError(CoordinatorError):
    """Command or READY backlog is full; no silent eviction or retry."""


class CoordinatorStoppedError(CoordinatorStateError):
    """Work had not started when shutdown revoked admission."""


class CoordinatorState(Enum):
    NEW = auto()
    STARTING = auto()
    RUNNING = auto()
    STOPPING = auto()
    STOPPED = auto()
    FAILED = auto()


@dataclass(frozen=True, slots=True)
class CoordinatedCycleResult:
    result: InspectionCycleResult
    inspection_queued_at: float | None
    inspection_started_at: float | None
    result_ready_at: float


@dataclass(frozen=True, slots=True)
class CoordinatorSnapshot:
    state: CoordinatorState
    commands: int
    collecting: int
    ready: int
    inspecting: CycleId | None
    peak_commands: int
    peak_collecting: int
    peak_ready: int


@dataclass(frozen=True, slots=True)
class _Begin:
    cycle_id: CycleId
    future: Future[CoordinatedCycleResult]


@dataclass(frozen=True, slots=True)
class _Frame:
    frame: Frame
    future: Future[None]


@dataclass(frozen=True, slots=True)
class _Tick:
    future: Future[None]


@dataclass(frozen=True, slots=True)
class _Job:
    cycle: CollectedCycle
    future: Future[CoordinatedCycleResult]
    queued_at: float


class _DispatchFailure(BaseException):
    """Cross inspect_cycle's Exception handler without faking a model failure."""


def _running[T](future: Future[T]) -> Future[T]:
    # Cancellation would otherwise let a caller discard an admitted cycle result.
    future.set_running_or_notify_cancel()
    return future


class RuntimeCoordinator:
    """Exclusive lifecycle ownership of a NEW InferenceWorker.

    Public admission methods are thread-safe and nonblocking. Returned Futures
    are observation handles, not caller-settable promises. Completion callbacks
    must be short/nonblocking and never wait on this runtime or mutate widgets.
    """

    def __init__(
        self,
        worker: InferenceWorker,
        expected_cameras: Sequence[CameraId],
        *,
        timeout: float,
        max_collecting: int = 2,
        max_ready: int = 2,
        max_commands: int = 32,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if worker.state is not WorkerState.NEW:
            raise CoordinatorStateError(
                "Coordinator requires exclusive use of a NEW worker"
            )
        if any(type(n) is not int or n < 1 for n in (max_ready, max_commands)):
            raise ValueError("Queue capacities must be positive integers")
        self._collector = CycleCollector(
            expected_cameras, timeout=timeout, max_active=max_collecting, clock=clock
        )
        self._worker = worker
        self._clock = clock
        self._max_ready = max_ready
        self._max_commands = max_commands
        self._condition = Condition()
        self._commands: deque[_Begin | _Frame | _Tick] = deque()
        self._ready: deque[_Job] = deque()
        self._collecting: dict[CycleId, Future[CoordinatedCycleResult]] = {}
        self._inspecting: CycleId | None = None
        self._peak_commands = self._peak_collecting = self._peak_ready = 0
        self._state = CoordinatorState.NEW
        self._failure: BaseException | None = None
        self._stop = False
        self._startup: Future[None] = _running(Future())
        self._owner = Thread(
            target=self._run, name=f"CycleCoordinator-{id(self):x}", daemon=False
        )
        self._runner = Thread(
            target=self._inspect, name=f"CycleInspector-{id(self):x}", daemon=False
        )
        self._owner_started = self._runner_started = False

    @property
    def snapshot(self) -> CoordinatorSnapshot:
        with self._condition:
            return CoordinatorSnapshot(
                self._state,
                len(self._commands),
                len(self._collecting),
                len(self._ready),
                self._inspecting,
                self._peak_commands,
                self._peak_collecting,
                self._peak_ready,
            )

    @property
    def failure(self) -> BaseException | None:
        with self._condition:
            return self._failure

    @property
    def thread_names(self) -> tuple[str, ...]:
        return self._owner.name, self._runner.name, self._worker.thread_name

    def start(self) -> Future[None]:
        error: BaseException | None = None
        with self._condition:
            if self._state is not CoordinatorState.NEW:
                raise CoordinatorStateError(
                    "Coordinator has one lifecycle; cannot restart"
                )
            self._state = CoordinatorState.STARTING
            try:
                self._owner.start()
                self._owner_started = True
            except Exception as exc:
                self._failure = error = exc
                self._state = CoordinatorState.FAILED
        if error is not None:
            self._worker.request_stop()
            self._reject(self._startup, error)
        return self._startup

    def _enqueue(self, command: _Begin | _Frame | _Tick) -> None:
        with self._condition:
            if self._state is not CoordinatorState.RUNNING:
                raise CoordinatorStateError(
                    f"Cannot admit commands in {self._state.name}"
                )
            if len(self._commands) >= self._max_commands:
                raise CoordinatorCapacityError(
                    "Command queue is full; command not admitted"
                )
            self._commands.append(command)
            self._peak_commands = max(self._peak_commands, len(self._commands))
            self._condition.notify_all()

    def begin_cycle(self, cycle_id: CycleId) -> Future[CoordinatedCycleResult]:
        future: Future[CoordinatedCycleResult] = _running(Future())
        self._enqueue(_Begin(cycle_id, future))
        return future

    def submit_frame(self, frame: Frame) -> Future[None]:
        """Acknowledges collector acceptance, not eventual cycle inspection."""
        future: Future[None] = _running(Future())
        self._enqueue(_Frame(frame, future))
        return future

    def expire(self) -> Future[None]:
        """Explicit runtime clock tick, serialized with frame admission."""
        future: Future[None] = _running(Future())
        self._enqueue(_Tick(future))
        return future

    def request_stop(self) -> None:
        stop_new = False
        with self._condition:
            self._stop = True
            if self._state is CoordinatorState.NEW:
                self._state = CoordinatorState.STOPPED
                stop_new = True
            elif self._state in (CoordinatorState.STARTING, CoordinatorState.RUNNING):
                self._state = CoordinatorState.STOPPING
            self._condition.notify_all()
        if stop_new:
            self._worker.request_stop()

    def join(self, timeout: float | None = None) -> bool:
        """True confirms all three threads exited, even if cleanup failed."""
        if current_thread().name in self.thread_names:
            raise CoordinatorStateError(
                "Cannot join the runtime from one of its own threads"
            )
        with self._condition:
            if self._state is CoordinatorState.NEW:
                raise CoordinatorStateError("Start or stop the coordinator before join")
            started = self._owner_started
        if started:
            self._owner.join(timeout)
        return not (
            self._owner.is_alive() or self._runner.is_alive() or self._worker.is_alive
        )

    def _fail(self, error: BaseException) -> None:
        with self._condition:
            if self._failure is None:
                self._failure = error
        self.request_stop()

    def _resolve[T](self, future: Future[T], value: T) -> None:
        try:
            future.set_result(value)
        except BaseException as exc:
            # A callback raising BaseException must not abandon other promises.
            self._fail(exc)

    def _reject(
        self,
        future: Future[None] | Future[CoordinatedCycleResult],
        error: BaseException,
    ) -> None:
        try:
            future.set_exception(error)
        except BaseException as exc:
            self._fail(exc)

    def _now(self, floor: float) -> float:
        now = self._clock()
        if isinstance(now, bool) or not math.isfinite(now) or now < floor:
            raise ValueError("Runtime clock must be finite and monotonic")
        return now

    def _run(self) -> None:
        try:
            self._worker.start().result()  # Backend startup never blocks callers.
            with self._condition:
                if self._stop:
                    raise CoordinatorStoppedError("Stopped during startup")
                self._runner.start()
                self._runner_started = True
                self._state = CoordinatorState.RUNNING
            self._resolve(self._startup, None)
            while True:
                with self._condition:
                    self._condition.wait_for(lambda: self._stop or bool(self._commands))
                    if self._stop:
                        break
                    command = self._commands.popleft()
                try:
                    self._execute(command)
                except BaseException as exc:
                    self._reject(command.future, exc)
                    raise
                finally:
                    del command
        except BaseException as exc:
            if not isinstance(exc, CoordinatorStoppedError):
                self._fail(exc)
            if not self._startup.done():
                self._reject(self._startup, exc)
        finally:
            self._shutdown()

    def _execute(self, command: _Begin | _Frame | _Tick) -> None:
        if isinstance(command, _Begin):
            try:
                self._collector.begin(command.cycle_id)
            except Exception as exc:
                self._reject(command.future, exc)
            else:
                with self._condition:
                    self._collecting[command.cycle_id] = command.future
                    self._peak_collecting = max(
                        self._peak_collecting, len(self._collecting)
                    )
        elif isinstance(command, _Frame):
            try:
                cycle = self._collector.accept(command.frame)
            except Exception as exc:
                self._reject(command.future, exc)
            else:
                if cycle is not None:
                    self._dispatch(cycle)
                self._resolve(command.future, None)
        else:
            for cycle in self._collector.expire():
                self._dispatch(cycle)
            self._resolve(command.future, None)

    def _dispatch(self, cycle: CollectedCycle) -> None:
        with self._condition:
            future = self._collecting.pop(cycle.cycle_id)
        try:
            if cycle.status is not CollectionStatus.READY:
                result = inspect_cycle(cycle, self._infer, clock=self._clock)
                self._resolve(
                    future,
                    CoordinatedCycleResult(
                        result, None, None, self._now(result.finished_at)
                    ),
                )
                return
            queued = self._now(cycle.finalized_at)
            with self._condition:
                if self._stop:
                    raise CoordinatorStoppedError(
                        "READY cycle had not started before stop"
                    )
                if len(self._ready) >= self._max_ready:
                    raise CoordinatorCapacityError("READY inspection backlog is full")
                self._ready.append(_Job(cycle, future, queued))
                self._peak_ready = max(self._peak_ready, len(self._ready))
                self._condition.notify_all()
        except BaseException as exc:
            self._reject(future, exc)
            if not isinstance(exc, (CoordinatorCapacityError, CoordinatorStoppedError)):
                raise

    def _infer(self, image: NDArray[np.uint8]) -> ImageResult:
        try:
            future = self._worker.submit(image)
        except Exception as exc:
            raise _DispatchFailure("InferenceWorker rejected frame dispatch") from exc
        return future.result()  # Exactly one worker task outstanding, no retries.

    def _inspect(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._stop or bool(self._ready))
                if self._stop:
                    return
                job = self._ready.popleft()  # Claim and stop share this lock.
                self._inspecting = job.cycle.cycle_id
            try:
                started = self._now(job.queued_at)
                result = inspect_cycle(job.cycle, self._infer, clock=self._clock)
                self._resolve(
                    job.future,
                    CoordinatedCycleResult(
                        result, job.queued_at, started, self._now(result.finished_at)
                    ),
                )
                del result
            except BaseException as exc:
                error: BaseException = exc
                if isinstance(exc, _DispatchFailure):
                    error = CoordinatorError(str(exc))
                    error.__cause__ = exc.__cause__
                self._fail(error)
                self._reject(job.future, error)
            finally:
                with self._condition:
                    self._inspecting = None
                del job

    def _shutdown(self) -> None:
        self.request_stop()
        while True:
            with self._condition:
                if not self._commands:
                    break
                command = self._commands.popleft()
            self._reject(
                command.future,
                CoordinatorStoppedError("Command not processed before stop"),
            )
            del command
        try:
            for cycle in self._collector.shutdown():
                self._dispatch(cycle)
                del cycle
        except BaseException as exc:
            self._fail(exc)
        while True:
            with self._condition:
                if not self._collecting:
                    break
                _, future = self._collecting.popitem()
            self._reject(
                future, self.failure or CoordinatorStoppedError("Collection aborted")
            )
        while True:
            with self._condition:
                if not self._ready:
                    break
                job = self._ready.popleft()
            self._reject(
                job.future,
                CoordinatorStoppedError("Queued cycle not inspected during shutdown"),
            )
            del job
        if self._runner_started:
            self._runner.join()  # Finish the whole active cycle before stopping worker.
        self._worker.request_stop()
        self._worker.join()
        if self._worker.failure is not None:
            self._fail(self._worker.failure)
        with self._condition:
            self._state = (
                CoordinatorState.FAILED
                if self._failure is not None
                else CoordinatorState.STOPPED
            )
