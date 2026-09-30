"""Event-driven coordinator checks: real worker threads, tiny fake images."""

from collections import Counter
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from threading import Event, Lock, current_thread, get_ident
from threading import enumerate as threads
from typing import cast

import numpy as np
import pytest
from numpy.typing import NDArray
from tests.unit.test_inspection_cycle import CAMERAS, frame, image_result

from vimo_demo.inference import ImageResult
from vimo_demo.inspection import (
    CameraId,
    CycleCapacityError,
    CycleDeadlineError,
    CycleId,
    CycleStateError,
    CycleStatus,
    DuplicateFrameError,
    UnknownCameraError,
)
from vimo_demo.runtime import (
    CoordinatedCycleResult,
    CoordinatorCapacityError,
    CoordinatorError,
    CoordinatorState,
    CoordinatorStateError,
    CoordinatorStoppedError,
    InferenceWorker,
    RuntimeCoordinator,
    WorkerQueueFullError,
)

WAIT = 5.0  # Failure guard only; synchronization never relies on elapsed sleep.


class Clock:
    def __init__(self) -> None:
        self._lock = Lock()
        self._value = 10.0
        self.block_owner = False
        self.entered = Event()
        self.release = Event()
        self.owner_threads: set[int] = set()

    def advance(self, delta: float) -> None:
        with self._lock:
            self._value += delta

    def __call__(self) -> float:
        if current_thread().name.startswith("CycleCoordinator-"):
            self.owner_threads.add(get_ident())
            if self.block_owner:
                self.entered.set()
                assert self.release.wait(WAIT)
        with self._lock:
            return self._value


class FakeEngine:
    def __init__(self) -> None:
        self.entered = Event()
        self.release = Event()
        self.release.set()
        self.closing = Event()
        self.close_release = Event()
        self.close_release.set()
        self.calls: list[int] = []
        self.trace: list[tuple[str, int]] = []
        self.error: BaseException | None = None
        self.error_call = 2

    def construct(self) -> FakeEngine:
        self.trace.append(("construct", get_ident()))
        return self

    def infer(self, image_bgr: NDArray[np.generic]) -> ImageResult:
        self.trace.append(("infer", get_ident()))
        code = int(image_bgr[0, 0, 0])
        self.calls.append(code)
        self.entered.set()
        assert self.release.wait(WAIT)
        if self.error is not None and len(self.calls) == self.error_call:
            raise self.error
        # Encode source pixels in output metadata to detect identity mixups.
        return replace(
            image_result(cast(NDArray[np.uint8], image_bgr)),
            label_map={"source": code},
        )

    def close(self) -> None:
        self.trace.append(("close", get_ident()))
        self.closing.set()
        assert self.close_release.wait(WAIT)


def runtime(
    engine: FakeEngine,
    clock: Clock | None = None,
    *,
    max_collecting: int = 2,
    max_ready: int = 2,
    max_commands: int = 32,
) -> RuntimeCoordinator:
    coordinator = RuntimeCoordinator(
        InferenceWorker(engine.construct),
        CAMERAS,
        timeout=5,
        clock=clock or Clock(),
        max_collecting=max_collecting,
        max_ready=max_ready,
        max_commands=max_commands,
    )
    coordinator.start().result(WAIT)
    return coordinator


def feed(coordinator: RuntimeCoordinator, cycle: int) -> None:
    for index in reversed(range(6)):
        coordinator.submit_frame(
            frame(cycle, CAMERAS[index], cycle * 6 + index)
        ).result(WAIT)


def verify(future: Future[CoordinatedCycleResult], cycle: int) -> None:
    envelope = future.result(WAIT)
    result = envelope.result
    assert result.status is CycleStatus.COMPLETE
    assert result.cycle_id == CycleId(cycle)
    assert tuple(row.frame_id.camera_id for row in result.frames) == CAMERAS
    for index, row in enumerate(result.frames):
        assert row.frame_id.cycle_id == CycleId(cycle)
        assert row.frame_id.sequence == cycle * 6 + index
        assert row.result is not None
        assert row.result.label_map == {"source": (cycle * 6 + index) % 256}
    assert envelope.inspection_queued_at is not None
    assert envelope.inspection_started_at is not None
    assert (
        result.started_at
        <= result.collection_finished_at
        <= envelope.inspection_queued_at
        <= envelope.inspection_started_at
        <= result.finished_at
        <= envelope.result_ready_at
    )


def stop(coordinator: RuntimeCoordinator, engine: FakeEngine) -> None:
    engine.release.set()
    engine.close_release.set()
    coordinator.request_stop()
    assert coordinator.join(WAIT)
    assert not set(coordinator.thread_names) & {thread.name for thread in threads()}


def test_normal_out_of_order_identity_timing_and_single_engine_owner() -> None:
    engine, clock = FakeEngine(), Clock()
    coordinator = runtime(engine, clock)
    try:
        future = coordinator.begin_cycle(CycleId(1))
        assert not future.cancel()
        feed(coordinator, 1)
        verify(future, 1)
        with pytest.raises(CycleStateError):
            coordinator.submit_frame(frame(1, CAMERAS[0])).result(WAIT)
        with pytest.raises(CycleStateError):
            coordinator.begin_cycle(CycleId(1)).result(WAIT)
    finally:
        stop(coordinator, engine)
    assert [event for event, _ in engine.trace] == [
        "construct",
        *["infer"] * 6,
        "close",
    ]
    assert len({ident for _, ident in engine.trace}) == 1
    assert engine.trace[0][1] != get_ident()
    assert len(clock.owner_threads) == 1
    assert not clock.owner_threads & {engine.trace[0][1], get_ident()}
    assert coordinator.snapshot.state is CoordinatorState.STOPPED


def test_overlapping_cycles_can_finish_out_of_id_order() -> None:
    engine = FakeEngine()
    coordinator = runtime(engine)
    completed: list[int] = []
    try:
        first = coordinator.begin_cycle(CycleId(1))
        second = coordinator.begin_cycle(CycleId(2))
        first.add_done_callback(lambda _: completed.append(1))
        second.add_done_callback(lambda _: completed.append(2))
        coordinator.submit_frame(frame(1, CAMERAS[0], 6)).result(WAIT)
        feed(coordinator, 2)
        verify(second, 2)
        assert not first.done()
        for index in reversed(range(1, 6)):
            coordinator.submit_frame(frame(1, CAMERAS[index], 6 + index)).result(WAIT)
        verify(first, 1)
    finally:
        stop(coordinator, engine)
    assert completed == [2, 1]


def test_concurrent_source_callbacks_serialize_through_one_collection_owner() -> None:
    engine, clock = FakeEngine(), Clock()
    coordinator = runtime(engine, clock)
    ready = Event()

    def submit(index: int) -> None:
        assert ready.wait(WAIT)
        coordinator.submit_frame(frame(1, CAMERAS[index], 6 + index)).result(WAIT)

    try:
        future = coordinator.begin_cycle(CycleId(1))
        with ThreadPoolExecutor(max_workers=6) as sources:
            submitted = [sources.submit(submit, index) for index in range(6)]
            ready.set()
            for ack in submitted:
                ack.result(WAIT)
        verify(future, 1)
    finally:
        ready.set()
        stop(coordinator, engine)
    assert len(clock.owner_threads) == 1


def test_duplicate_unknown_and_unopened_frames_do_not_replace_valid_data() -> None:
    engine = FakeEngine()
    coordinator = runtime(engine)
    try:
        future = coordinator.begin_cycle(CycleId(1))
        coordinator.submit_frame(frame(1, CAMERAS[0], 6)).result(WAIT)
        for invalid, error in (
            (frame(1, CAMERAS[0], 99), DuplicateFrameError),
            (frame(1, CameraId("unknown")), UnknownCameraError),
            (frame(2, CAMERAS[0]), CycleStateError),
        ):
            with pytest.raises(error):
                coordinator.submit_frame(invalid).result(WAIT)
        for index in range(1, 6):
            coordinator.submit_frame(frame(1, CAMERAS[index], 6 + index)).result(WAIT)
        verify(future, 1)
    finally:
        stop(coordinator, engine)


def test_timeout_and_late_frame_while_another_cycle_is_inspecting() -> None:
    engine, clock = FakeEngine(), Clock()
    engine.release.clear()
    coordinator = runtime(engine, clock)
    try:
        active = coordinator.begin_cycle(CycleId(1))
        feed(coordinator, 1)
        assert engine.entered.wait(WAIT)
        missing = coordinator.begin_cycle(CycleId(2))
        coordinator.submit_frame(frame(2, CAMERAS[0])).result(WAIT)
        clock.advance(5)
        with pytest.raises(CycleDeadlineError):
            coordinator.submit_frame(frame(2, CAMERAS[1])).result(WAIT)
        coordinator.expire().result(WAIT)
        envelope = missing.result(WAIT)
        assert envelope.result.status is CycleStatus.TIMED_OUT
        assert envelope.result.missing_cameras == CAMERAS[1:]
        assert envelope.inspection_queued_at is envelope.inspection_started_at is None
        assert len(engine.calls) == 1 and not active.done()
        coordinator.expire().result(WAIT)  # No second completion.
        with pytest.raises(CycleStateError):
            coordinator.submit_frame(frame(2, CAMERAS[1])).result(WAIT)
        engine.release.set()
        verify(active, 1)  # Collection deadline never cancels active inspection.
    finally:
        stop(coordinator, engine)


def test_normal_inspection_exception_keeps_later_frames_and_cycles_running() -> None:
    engine = FakeEngine()
    engine.error = RuntimeError("fake image failure")
    coordinator = runtime(engine)
    try:
        first = coordinator.begin_cycle(CycleId(1))
        feed(coordinator, 1)
        result = first.result(WAIT).result
        assert result.status is CycleStatus.FAILED
        assert result.frames[1].failure is not None
        assert result.frames[1].failure.message == "fake image failure"
        assert sum(row.result is not None for row in result.frames) == 5
        second = coordinator.begin_cycle(CycleId(2))
        feed(coordinator, 2)
        verify(second, 2)
    finally:
        stop(coordinator, engine)
    assert len(engine.calls) == 12 and coordinator.failure is None


def test_collector_capacity_rejection_preserves_admitted_cycle() -> None:
    engine = FakeEngine()
    coordinator = runtime(engine, max_collecting=1)
    try:
        first = coordinator.begin_cycle(CycleId(1))
        rejected = coordinator.begin_cycle(CycleId(2))
        with pytest.raises(CycleCapacityError):
            rejected.result(WAIT)
        feed(coordinator, 1)
        verify(first, 1)
        second = coordinator.begin_cycle(CycleId(2))
        feed(coordinator, 2)
        verify(second, 2)
        assert coordinator.snapshot.peak_collecting == 1
    finally:
        stop(coordinator, engine)


def test_backlog_overload_and_stop_resolve_every_cycle_exactly_once() -> None:
    engine = FakeEngine()
    engine.release.clear()
    coordinator = runtime(engine, max_ready=1)
    completions: Counter[int] = Counter()
    futures: dict[int, Future[CoordinatedCycleResult]] = {}

    def record(future: Future[CoordinatedCycleResult], cycle: int) -> None:
        completions[cycle] += 1

    try:
        for cycle in range(1, 5):
            future = coordinator.begin_cycle(CycleId(cycle))
            futures[cycle] = future
            # Bind the current ID, not the mutable loop variable.
            future.add_done_callback(lambda f, c=cycle: record(f, c))
            if cycle < 4:
                feed(coordinator, cycle)
            else:
                coordinator.submit_frame(frame(cycle, CAMERAS[0])).result(WAIT)
            if cycle == 1:
                assert engine.entered.wait(WAIT)
        with pytest.raises(CoordinatorCapacityError, match="backlog"):
            futures[3].result(WAIT)
        assert coordinator.snapshot.ready == coordinator.snapshot.peak_ready == 1
        coordinator.request_stop()
        coordinator.request_stop()
        with pytest.raises(CoordinatorStoppedError):
            futures[2].result(WAIT)
        assert futures[4].result(WAIT).result.status is CycleStatus.SHUTDOWN
        assert not coordinator.join(0) and not futures[1].done()
        with pytest.raises(CoordinatorStateError):
            coordinator.begin_cycle(CycleId(5))
        with pytest.raises(CoordinatorStateError):
            coordinator.submit_frame(frame(4, CAMERAS[1]))
        engine.release.set()
        verify(futures[1], 1)
    finally:
        stop(coordinator, engine)
    assert len(engine.calls) == 6  # Finish active cycle, never start queued cycle.
    assert completions == Counter({1: 1, 2: 1, 3: 1, 4: 1})
    snapshot = coordinator.snapshot
    assert (snapshot.commands, snapshot.collecting, snapshot.ready) == (0, 0, 0)


def test_bounded_command_queue_and_stop_do_not_need_queue_space() -> None:
    engine, clock = FakeEngine(), Clock()
    coordinator = runtime(engine, clock, max_commands=2)
    try:
        clock.block_owner = True
        claimed = coordinator.begin_cycle(CycleId(1))
        assert clock.entered.wait(WAIT)
        queued = [coordinator.expire(), coordinator.expire()]
        with pytest.raises(CoordinatorCapacityError, match="Command queue"):
            coordinator.submit_frame(frame(1, CAMERAS[0]))
        coordinator.request_stop()
        assert not coordinator.join(0)
        clock.release.set()
        assert claimed.result(WAIT).result.status is CycleStatus.SHUTDOWN
        for ack in queued:
            with pytest.raises(CoordinatorStoppedError):
                ack.result(WAIT)
        assert coordinator.snapshot.peak_commands == 2
    finally:
        clock.release.set()
        stop(coordinator, engine)


@pytest.mark.parametrize("start_first", [False, True])
def test_idle_repeated_stop_and_one_lifecycle(start_first: bool) -> None:
    engine = FakeEngine()
    coordinator = RuntimeCoordinator(
        InferenceWorker(engine.construct), CAMERAS, timeout=1
    )
    with pytest.raises(CoordinatorStateError):
        coordinator.join(0)
    with pytest.raises(CoordinatorStateError):
        coordinator.begin_cycle(CycleId(1))
    if start_first:
        coordinator.start().result(WAIT)
    stop(coordinator, engine)
    coordinator.request_stop()
    assert coordinator.join(0)
    with pytest.raises(CoordinatorStateError):
        coordinator.start()
    assert coordinator.snapshot.state is CoordinatorState.STOPPED


def test_blocked_cleanup_remains_observable_until_actual_worker_exit() -> None:
    engine = FakeEngine()
    engine.close_release.clear()
    coordinator = runtime(engine)
    try:
        coordinator.request_stop()
        assert engine.closing.wait(WAIT)
        assert not coordinator.join(0)
        assert coordinator.snapshot.state is CoordinatorState.STOPPING
        assert coordinator.thread_names[-1] in {t.name for t in threads()}
    finally:
        stop(coordinator, engine)


def test_stop_during_startup_waits_for_engine_construction_and_cleanup() -> None:
    engine = FakeEngine()
    entered, release = Event(), Event()

    def factory() -> FakeEngine:
        entered.set()
        assert release.wait(WAIT)
        return engine.construct()

    coordinator = RuntimeCoordinator(InferenceWorker(factory), CAMERAS, timeout=1)
    startup = coordinator.start()
    try:
        assert entered.wait(WAIT)
        coordinator.request_stop()
        assert not coordinator.join(0)
        release.set()
        with pytest.raises(CoordinatorStoppedError):
            startup.result(WAIT)
    finally:
        release.set()
        stop(coordinator, engine)
    assert [event for event, _ in engine.trace] == ["construct", "close"]


def test_startup_failure_is_observable_and_threads_exit() -> None:
    def broken() -> FakeEngine:
        raise RuntimeError("factory failed")

    coordinator = RuntimeCoordinator(InferenceWorker(broken), CAMERAS, timeout=1)
    with pytest.raises(RuntimeError, match="factory failed"):
        coordinator.start().result(WAIT)
    assert coordinator.join(WAIT)
    assert coordinator.snapshot.state is CoordinatorState.FAILED
    assert coordinator.failure is not None
    assert not set(coordinator.thread_names) & {thread.name for thread in threads()}


def test_fatal_worker_error_does_not_disguise_runtime_failure() -> None:
    engine = FakeEngine()
    engine.release.clear()
    engine.error = SystemExit("fatal fake engine")
    engine.error_call = 1
    coordinator = runtime(engine)
    try:
        active = coordinator.begin_cycle(CycleId(1))
        feed(coordinator, 1)
        assert engine.entered.wait(WAIT)
        waiting = coordinator.begin_cycle(CycleId(2))
        feed(coordinator, 2)
        engine.release.set()
        with pytest.raises(SystemExit, match="fatal fake engine"):
            active.result(WAIT)
        with pytest.raises(CoordinatorStoppedError):
            waiting.result(WAIT)
    finally:
        stop(coordinator, engine)
    assert coordinator.snapshot.state is CoordinatorState.FAILED
    assert len(engine.calls) == 1


def test_worker_dispatch_rejection_is_runtime_error_not_model_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = FakeEngine()
    worker = InferenceWorker(engine.construct)
    coordinator = RuntimeCoordinator(worker, CAMERAS, timeout=5)
    coordinator.start().result(WAIT)

    def reject(image_bgr: NDArray[np.generic]) -> Future[ImageResult]:
        raise WorkerQueueFullError("unexpected shared worker")

    monkeypatch.setattr(worker, "submit", reject)
    try:
        future = coordinator.begin_cycle(CycleId(1))
        feed(coordinator, 1)
        with pytest.raises(CoordinatorError) as failure:
            future.result(WAIT)
        assert isinstance(failure.value.__cause__, WorkerQueueFullError)
    finally:
        stop(coordinator, engine)
    assert not engine.calls and coordinator.snapshot.state is CoordinatorState.FAILED


def test_simulated_200_cycles_bounded_overlap_no_identity_mixing_or_history() -> None:
    engine = FakeEngine()
    coordinator = runtime(engine, max_collecting=2, max_ready=2)
    completions: Counter[int] = Counter()

    def record(future: Future[CoordinatedCycleResult]) -> None:
        completions[int(future.result().result.cycle_id)] += 1

    try:
        for cycle in range(1, 201, 2):
            first = coordinator.begin_cycle(CycleId(cycle))
            second = coordinator.begin_cycle(CycleId(cycle + 1))
            first.add_done_callback(record)
            second.add_done_callback(record)
            feed(coordinator, cycle + 1)
            feed(coordinator, cycle)
            verify(first, cycle)
            verify(second, cycle + 1)
        snapshot = coordinator.snapshot
        assert snapshot.peak_collecting == 2
        assert snapshot.peak_ready <= 2 and snapshot.peak_commands <= 32
        assert (snapshot.commands, snapshot.collecting, snapshot.ready) == (0, 0, 0)
    finally:
        stop(coordinator, engine)
    assert completions == Counter(dict.fromkeys(range(1, 201), 1))
    assert len(engine.calls) == 1200
    assert coordinator.failure is None
