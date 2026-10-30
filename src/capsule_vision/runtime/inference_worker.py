"""One engine, one owning thread, one lifecycle, bounded pending work."""

from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from enum import Enum, auto
from queue import Empty, Full, Queue
from threading import Condition, Thread, current_thread
from typing import Protocol

import numpy as np
from numpy.typing import NDArray

from capsule_vision.inference import ImageResult


class InferenceEngine(Protocol):
    def infer(self, image_bgr: NDArray[np.generic]) -> ImageResult: ...

    def close(self) -> None: ...


class WorkerState(Enum):
    NEW = auto()
    STARTING = auto()
    RUNNING = auto()
    STOPPING = auto()
    STOPPED = auto()
    FAILED = auto()


class WorkerStateError(RuntimeError):
    """The operation is unavailable in the worker's current lifecycle state."""


class WorkerQueueFullError(RuntimeError):
    """No pending slot is available; the submitted image was not accepted."""


@dataclass(frozen=True, slots=True)
class _Task:
    image: NDArray[np.generic]
    future: Future[ImageResult]


class InferenceWorker:
    """The factory, infer and close execute on the same non-daemon thread.

    Inputs are borrowed without copying. After successful submit, callers must
    keep every alias immutable until the returned Future completes/cancels.
    max_pending excludes the active task. Returned Futures are result handles:
    callers may wait/cancel tasks, but must not set results or execution states.
    """

    def __init__(
        self, engine_factory: Callable[[], InferenceEngine], *, max_pending: int = 1
    ) -> None:
        if type(max_pending) is not int or max_pending < 1:
            raise ValueError("max_pending must be a positive integer")
        self._factory = engine_factory
        self._pending: Queue[_Task] = Queue(maxsize=max_pending)
        # Admission, stop and task claiming share one lock. The condition wakes
        # an idle worker on work/stop without polling or a queue-full sentinel.
        self._condition = Condition()
        self._state = WorkerState.NEW
        self._failure: BaseException | None = None
        self._startup: Future[None] = Future()
        self._started = False
        self._thread = Thread(
            target=self._run, name=f"VimoInferenceWorker-{id(self):x}", daemon=False
        )

    @property
    def state(self) -> WorkerState:
        with self._condition:
            return self._state

    @property
    def failure(self) -> BaseException | None:
        """Original worker-level failure, including cleanup; task errors stay on Futures."""
        with self._condition:
            return self._failure

    @property
    def is_alive(self) -> bool:
        return self._thread.is_alive()

    @property
    def thread_name(self) -> str:
        return self._thread.name

    def start(self) -> Future[None]:
        """Start promptly; readiness reports construction success/failure asynchronously."""
        error: Exception | None = None
        with self._condition:
            if self._state is not WorkerState.NEW:
                raise WorkerStateError(f"Cannot start worker in {self._state.name}")
            self._state = WorkerState.STARTING
            # Readiness cannot cancel native initialization. Use request_stop().
            self._startup.set_running_or_notify_cancel()
            try:
                self._thread.start()
                self._started = True
            except Exception as exc:
                self._failure = error = exc
                self._state = WorkerState.FAILED
        if error is not None:
            self._startup.set_exception(error)
        return self._startup

    def submit(self, image_bgr: NDArray[np.generic]) -> Future[ImageResult]:
        """Accept only while RUNNING; reject a full queue immediately, without copying."""
        with self._condition:
            if self._state is not WorkerState.RUNNING:
                raise WorkerStateError(f"Cannot submit in {self._state.name}")
            future: Future[ImageResult] = Future()
            try:
                self._pending.put_nowait(_Task(image_bgr, future))
            except Full as exc:
                raise WorkerQueueFullError("Pending inference queue is full") from exc
            self._condition.notify()
            return future

    def request_stop(self) -> None:
        """Revoke admission and wake the worker; never wait for native work/cleanup."""
        with self._condition:
            if self._state is WorkerState.NEW:
                self._state = WorkerState.STOPPED
            elif self._state in (WorkerState.STARTING, WorkerState.RUNNING):
                self._state = WorkerState.STOPPING
            self._condition.notify_all()

    def join(self, timeout: float | None = None) -> bool:
        """True means the thread terminated (possibly FAILED); False means timeout.

        This does not request stop. NEW workers must be started or stopped first.
        Never call join from a completion callback running on this worker.
        """
        with self._condition:
            if self._state is WorkerState.NEW:
                raise WorkerStateError("Cannot join a NEW worker")
            started = self._started
        if current_thread() is self._thread:
            raise WorkerStateError("A worker cannot join its own thread")
        if started:
            self._thread.join(timeout)
        return not self._thread.is_alive()

    def _serve(self, engine: InferenceEngine) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(
                    lambda: (
                        self._state is not WorkerState.RUNNING
                        or not self._pending.empty()
                    )
                )
                if self._state is not WorkerState.RUNNING:
                    return
                task = self._pending.get_nowait()
                # Claiming under the admission lock is the task's start boundary.
                run = task.future.set_running_or_notify_cancel()
            try:
                if run:
                    self._execute(engine, task)
            finally:
                self._pending.task_done()
                del task  # Do not retain the last image/result while idle.

    @staticmethod
    def _execute(engine: InferenceEngine, task: _Task) -> None:
        try:
            result = engine.infer(task.image)
        except BaseException as exc:
            task.future.set_exception(exc)
            if not isinstance(exc, Exception):
                raise  # Fatal control-flow exceptions terminate the worker.
        else:
            task.future.set_result(result)

    def _run(self) -> None:
        engine: InferenceEngine | None = None
        failure: BaseException | None = None
        try:
            engine = self._factory()
            with self._condition:
                ready = self._state is WorkerState.STARTING
                if ready:
                    self._state = WorkerState.RUNNING
            # Future completion can invoke user callbacks: never hold our lock.
            if ready:
                self._startup.set_result(None)
                self._serve(engine)
            else:
                self._startup.set_exception(
                    WorkerStateError("Stop requested before startup became ready")
                )
        except BaseException as exc:
            failure = exc
            with self._condition:
                self._failure = exc
                self._state = WorkerState.FAILED
            if not self._startup.done():
                self._startup.set_exception(exc)
        finally:
            with self._condition:
                if self._state is not WorkerState.FAILED:
                    self._state = WorkerState.STOPPING
            # Cancel only after active inference has returned. No user callbacks
            # run in request_stop(), so stop cannot wait on cancellation handlers.
            while True:
                try:
                    task = self._pending.get_nowait()
                except Empty:
                    break
                try:
                    task.future.cancel()
                    task.future.set_running_or_notify_cancel()  # Notify wait()/as_completed().
                except BaseException as exc:
                    if failure is None:
                        failure = exc
                finally:
                    self._pending.task_done()
                    del task
            if engine is not None:
                try:
                    engine.close()
                except BaseException as exc:
                    if failure is None:
                        failure = exc
                    else:
                        failure.add_note(f"Engine cleanup also failed: {exc!r}")
            with self._condition:
                self._failure = failure
                self._state = (
                    WorkerState.FAILED if failure is not None else WorkerState.STOPPED
                )
            # Terminal state records cleanup outcome; join confirms actual exit.
