"""Event-coordinated ownership/lifecycle tests; no native SDK, GPU or sleeps."""

from collections.abc import Iterator
from concurrent.futures import CancelledError, Future, wait
from threading import Barrier, Event, Thread, get_ident
from threading import enumerate as threads

import numpy as np
import pytest
from numpy.typing import NDArray

from vimo_demo.inference import BoundingRect, ImageResult, RotatedRect
from vimo_demo.runtime import (
    InferenceWorker,
    WorkerQueueFullError,
    WorkerState,
    WorkerStateError,
)

TIMEOUT = 5.0


class Controls:
    def __init__(self) -> None:
        self.construct_entered, self.infer_entered, self.close_entered = (
            Event(),
            Event(),
            Event(),
        )
        self.construct_release, self.infer_release, self.close_release = (
            Event(),
            Event(),
            Event(),
        )
        self.construct_release.set()
        self.infer_release.set()
        self.close_release.set()
        self.trace: list[tuple[str, int]] = []
        self.images: list[NDArray[np.generic]] = []
        self.start_error: Exception | None = None
        self.infer_error: BaseException | None = None
        self.close_error: Exception | None = None
        self.image = np.zeros((2, 3, 3), dtype=np.uint8)
        self.result = ImageResult(
            (2, 3, 3),
            {},
            (),
            np.zeros((2, 3), dtype=np.uint8),
            BoundingRect(0, 0, 3, 2),
            RotatedRect(1.5, 1, 3, 2, 0),
            "fake",
        )

    def make_engine(self) -> FakeEngine:
        return FakeEngine(self)


class FakeEngine:
    def __init__(self, control: Controls) -> None:
        self.control = control
        control.trace.append(("construct", get_ident()))
        control.construct_entered.set()
        assert control.construct_release.wait(TIMEOUT), "Factory release timed out"
        if control.start_error is not None:
            raise control.start_error

    def infer(self, image_bgr: NDArray[np.generic]) -> ImageResult:
        c = self.control
        c.trace.append(("infer", get_ident()))
        c.images.append(image_bgr)
        c.infer_entered.set()
        assert c.infer_release.wait(TIMEOUT), "Inference release timed out"
        error, c.infer_error = c.infer_error, None
        if error is not None:
            raise error
        return c.result

    def close(self) -> None:
        c = self.control
        c.trace.append(("close", get_ident()))
        c.close_entered.set()
        assert c.close_release.wait(TIMEOUT), "Close release timed out"
        if c.close_error is not None:
            raise c.close_error


@pytest.fixture
def case() -> Iterator[tuple[Controls, InferenceWorker]]:
    control = Controls()
    worker = InferenceWorker(control.make_engine)
    try:
        yield control, worker
    finally:
        control.construct_release.set()
        control.infer_release.set()
        control.close_release.set()
        worker.request_stop()
        assert worker.join(TIMEOUT), "Worker leaked during test cleanup"
        assert not any(t.name == worker.thread_name for t in threads())


def test_lifecycle_ownership_and_no_copy(
    case: tuple[Controls, InferenceWorker],
) -> None:
    c, worker = case
    caller = get_ident()
    assert worker.state is WorkerState.NEW and not worker.is_alive
    assert not c.trace  # Construction of the worker does not construct an engine.
    with pytest.raises(WorkerStateError):
        worker.submit(c.image)
    c.construct_release.clear()
    startup = worker.start()
    assert c.construct_entered.wait(TIMEOUT)
    assert worker.state is WorkerState.STARTING and not startup.done()
    assert not next(t for t in threads() if t.name == worker.thread_name).daemon
    assert not startup.cancel()
    with pytest.raises(WorkerStateError):
        worker.submit(c.image)
    with pytest.raises(WorkerStateError):
        worker.start()
    c.construct_release.set()
    assert startup.result(TIMEOUT) is None
    assert worker.state is WorkerState.RUNNING
    c.infer_release.clear()
    future = worker.submit(c.image)
    assert c.infer_entered.wait(TIMEOUT)
    assert not future.cancel()  # Native work is never forcibly cancelled.
    c.infer_release.set()
    assert future.result(TIMEOUT) is c.result
    assert c.images[0] is c.image
    c.close_release.clear()
    worker.request_stop()
    assert c.close_entered.wait(TIMEOUT)
    assert worker.state is WorkerState.STOPPING and worker.is_alive
    assert not worker.join(0)
    c.close_release.set()
    assert worker.join(TIMEOUT)
    assert worker.state is WorkerState.STOPPED and not worker.is_alive
    assert worker.failure is None
    assert [action for action, _ in c.trace] == ["construct", "infer", "close"]
    assert len({ident for _, ident in c.trace}) == 1
    assert c.trace[0][1] != caller
    with pytest.raises(WorkerStateError):
        worker.start()


def test_stop_during_active_call_cancels_pending_and_is_idempotent(
    case: tuple[Controls, InferenceWorker],
) -> None:
    c, worker = case
    worker.start().result(TIMEOUT)
    c.infer_release.clear()
    active = worker.submit(c.image)
    assert c.infer_entered.wait(TIMEOUT)
    pending = worker.submit(c.image)
    with pytest.raises(WorkerQueueFullError):
        worker.submit(c.image)
    worker.request_stop()
    worker.request_stop()
    with pytest.raises(WorkerStateError):
        worker.submit(c.image)
    assert worker.state is WorkerState.STOPPING
    assert not worker.join(0) and worker.is_alive
    assert not active.done() and not c.close_entered.is_set()
    c.infer_release.set()
    assert active.result(TIMEOUT) is c.result
    assert worker.join(TIMEOUT)
    assert pending.cancelled()
    assert wait([pending], timeout=0).done == {pending}
    with pytest.raises(CancelledError):
        pending.result()
    worker.request_stop()
    assert worker.join(0) and worker.state is WorkerState.STOPPED
    assert [action for action, _ in c.trace] == ["construct", "infer", "close"]


def test_task_error_preserves_cause_and_worker_remains_usable(
    case: tuple[Controls, InferenceWorker],
) -> None:
    c, worker = case
    error = RuntimeError("inference failed")
    error.__cause__ = ValueError("native cause")
    c.infer_error = error
    worker.start().result(TIMEOUT)
    with pytest.raises(RuntimeError) as caught:
        worker.submit(c.image).result(TIMEOUT)
    assert caught.value is error and caught.value.__cause__ is error.__cause__
    assert worker.state is WorkerState.RUNNING and worker.failure is None
    assert worker.submit(c.image).result(TIMEOUT) is c.result
    worker.request_stop()  # Idle shutdown.
    assert worker.join(TIMEOUT)
    assert [action for action, _ in c.trace] == ["construct", "infer", "infer", "close"]


def test_initialization_failure_is_terminal_without_fake_cleanup(
    case: tuple[Controls, InferenceWorker],
) -> None:
    c, worker = case
    error = RuntimeError("initialization failed")
    error.__cause__ = ValueError("original constructor cause")
    c.start_error = error
    with pytest.raises(RuntimeError) as caught:
        worker.start().result(TIMEOUT)
    assert caught.value is error and caught.value.__cause__ is error.__cause__
    assert worker.join(TIMEOUT)
    assert worker.state is WorkerState.FAILED and worker.failure is error
    assert [action for action, _ in c.trace] == ["construct"]
    with pytest.raises(WorkerStateError):
        worker.submit(c.image)
    with pytest.raises(WorkerStateError):
        worker.start()


def test_stop_during_startup_never_admits_work(
    case: tuple[Controls, InferenceWorker],
) -> None:
    c, worker = case
    c.construct_release.clear()
    startup = worker.start()
    assert c.construct_entered.wait(TIMEOUT)
    worker.request_stop()
    assert worker.state is WorkerState.STOPPING
    assert not startup.done() and not worker.join(0)
    with pytest.raises(WorkerStateError):
        worker.submit(c.image)
    c.construct_release.set()
    with pytest.raises(WorkerStateError, match="before startup"):
        startup.result(TIMEOUT)
    assert worker.join(TIMEOUT)
    assert worker.state is WorkerState.STOPPED and worker.failure is None
    assert [action for action, _ in c.trace] == ["construct", "close"]


def test_stop_before_start_is_terminal(case: tuple[Controls, InferenceWorker]) -> None:
    c, worker = case
    with pytest.raises(WorkerStateError, match="NEW"):
        worker.join(0)
    worker.request_stop()
    worker.request_stop()
    assert worker.join(0) and worker.state is WorkerState.STOPPED
    assert not c.trace
    with pytest.raises(WorkerStateError):
        worker.start()


def test_close_error_is_observable_and_close_is_not_retried(
    case: tuple[Controls, InferenceWorker],
) -> None:
    c, worker = case
    error = RuntimeError("cleanup failed")
    c.close_error = error
    worker.start().result(TIMEOUT)
    worker.request_stop()
    assert worker.join(TIMEOUT)
    worker.request_stop()
    assert worker.join(0)
    assert worker.state is WorkerState.FAILED and worker.failure is error
    assert [action for action, _ in c.trace] == ["construct", "close"]


def test_caller_cancelled_pending_task_is_not_executed(
    case: tuple[Controls, InferenceWorker],
) -> None:
    c, worker = case
    worker.start().result(TIMEOUT)
    c.infer_release.clear()
    active = worker.submit(c.image)
    assert c.infer_entered.wait(TIMEOUT)
    cancelled = worker.submit(c.image)
    assert cancelled.cancel()
    c.infer_release.set()
    active.result(TIMEOUT)
    # Executor acknowledgement also makes wait()/as_completed() usable.
    assert wait([cancelled], timeout=TIMEOUT).done == {cancelled}
    assert worker.submit(c.image).result(TIMEOUT) is c.result
    assert len(c.images) == 2


def test_concurrent_producers_observe_one_pending_slot(
    case: tuple[Controls, InferenceWorker],
) -> None:
    c, worker = case
    worker.start().result(TIMEOUT)
    c.infer_release.clear()
    active = worker.submit(c.image)
    assert c.infer_entered.wait(TIMEOUT)
    barrier = Barrier(3, timeout=TIMEOUT)
    outcomes: list[Future[ImageResult] | WorkerQueueFullError | None] = [None, None]

    def produce(index: int) -> None:
        barrier.wait()
        try:
            outcomes[index] = worker.submit(c.image)
        except WorkerQueueFullError as exc:
            outcomes[index] = exc

    producers = [Thread(target=produce, args=(index,)) for index in range(2)]
    for thread in producers:
        thread.start()
    try:
        barrier.wait()
    finally:
        for thread in producers:
            thread.join(TIMEOUT)
            assert not thread.is_alive()
    assert sum(isinstance(outcome, Future) for outcome in outcomes) == 1
    assert sum(isinstance(outcome, WorkerQueueFullError) for outcome in outcomes) == 1
    worker.request_stop()
    c.infer_release.set()
    active.result(TIMEOUT)
    assert worker.join(TIMEOUT)
    assert len(c.images) == 1


def test_fatal_task_error_settles_future_and_still_closes(
    case: tuple[Controls, InferenceWorker],
) -> None:
    c, worker = case
    error = SystemExit("fatal fake engine failure")
    c.infer_error = error
    worker.start().result(TIMEOUT)
    c.infer_release.clear()
    active = worker.submit(c.image)
    assert c.infer_entered.wait(TIMEOUT)
    pending = worker.submit(c.image)
    c.infer_release.set()
    with pytest.raises(SystemExit) as caught:
        active.result(TIMEOUT)
    assert caught.value is error
    assert worker.join(TIMEOUT)
    assert worker.state is WorkerState.FAILED and worker.failure is error
    assert pending.cancelled()
    assert [action for action, _ in c.trace] == ["construct", "infer", "close"]


@pytest.mark.parametrize("capacity", [0, -1, True])
def test_queue_capacity_must_be_positive(capacity: int) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        InferenceWorker(Controls().make_engine, max_pending=capacity)
