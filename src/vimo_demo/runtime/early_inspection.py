"""Bounded, deadline-driven camera inspection for passive Basler acquisition.

The ordinary full-group RuntimeCoordinator is unchanged. One serial runner uses
the existing worker; a separate owner expires cycles even during a native call.
"""

from collections import deque
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field
from threading import Condition, Thread
from time import monotonic

import numpy as np
from numpy.typing import NDArray

from vimo_demo.inference import ImageResult
from vimo_demo.inspection import CameraId, CycleId, CycleStatus, Frame, FrameId
from vimo_demo.inspection.production import (
    CameraInspectionResult,
    CameraLayout,
    InspectionLayout,
    ProductionInspectionResult,
    TechnicalStatus,
)

from .coordinator import CoordinatorCapacityError, CoordinatorStateError
from .inference_worker import InferenceWorker


def promise[T](result: Future[T]) -> Future[T]:
    result.set_running_or_notify_cancel()
    return result


@dataclass(frozen=True, slots=True)
class EarlyAdmission:
    accepted: Future[None]
    result: Future[ProductionInspectionResult]


@dataclass(frozen=True, slots=True)
class EarlyRuntimeSnapshot:
    cycles: int
    queued_cameras: int
    active_frame: FrameId | None
    peak_cycles: int
    peak_queued_cameras: int
    late_results: int


class _DispatchFailure(BaseException):
    """Do not disguise rejected worker submission as an ordinary image error."""


@dataclass(slots=True)
class _Cycle:
    cycle_id: CycleId
    started: float
    deadline: float
    future: Future[ProductionInspectionResult]
    frames: dict[CameraId, FrameId] = field(default_factory=dict[CameraId, FrameId])
    shapes: dict[CameraId, tuple[int, int, int]] = field(
        default_factory=dict[CameraId, tuple[int, int, int]]
    )
    results: dict[CameraId, CameraInspectionResult] = field(
        default_factory=dict[CameraId, CameraInspectionResult]
    )
    inspecting_at: float | None = None


type CameraInspector = Callable[
    [Frame, Callable[[NDArray[np.uint8]], ImageResult]], CameraInspectionResult
]
type ForcedCamera = Callable[
    [CameraLayout, FrameId | None, tuple[int, int, int] | None, str],
    CameraInspectionResult,
]


class EarlyInspectionCoordinator:
    """Nonblocking bounded admission; no pixel copies or completed history.

    Frames remain immutable, including after deadline delivery while an active
    call still borrows them. Only actual join establishes native termination.
    """

    def __init__(
        self,
        worker: InferenceWorker,
        layout: InspectionLayout,
        inspect: CameraInspector,
        forced: ForcedCamera,
        *,
        timeout: float = 0.100,
        capacity: int = 4,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if capacity < 1 or timeout <= 0:
            raise ValueError("Positive capacity/timeout required")
        self.worker, self.layout = worker, layout
        self._inspect, self._forced, self._clock = inspect, forced, clock
        self._timeout, self._capacity = timeout, capacity
        self._condition = Condition()
        self._cycles: dict[CycleId, _Cycle] = {}
        self._jobs: deque[Frame] = deque()
        self._completed: deque[
            tuple[FrameId, CameraInspectionResult | BaseException, float]
        ] = deque()
        self._stop = False
        self._started = False
        self._ready = False
        self._last_id = -1
        self._active: FrameId | None = None
        self._failure: BaseException | None = None
        self._settlement: dict[CycleId, Future[None]] = {}
        self.peak_cycles = self.peak_jobs = self.late_results = 0
        self.finished: Future[None] = promise(Future())
        self._startup: Future[None] = promise(Future())
        self._runner = Thread(
            target=self._run_camera, name=f"EarlyCameraInspector-{id(self):x}"
        )
        self._owner = Thread(target=self._run, name=f"EarlyCycleOwner-{id(self):x}")

    @property
    def failure(self) -> BaseException | None:
        with self._condition:
            return self._failure or self.worker.failure

    @property
    def thread_names(self) -> tuple[str, ...]:
        return self._owner.name, self._runner.name, self.worker.thread_name

    def snapshot(self) -> EarlyRuntimeSnapshot:
        with self._condition:
            return EarlyRuntimeSnapshot(
                len(self._cycles),
                len(self._jobs),
                self._active,
                self.peak_cycles,
                self.peak_jobs,
                self.late_results,
            )

    def start(self) -> Future[None]:
        with self._condition:
            if self._started:
                raise CoordinatorStateError("Early coordinator has one lifecycle")
            self._started = True
            self._owner.start()
        return self._startup

    def begin(self, cycle_id: CycleId) -> EarlyAdmission:
        with self._condition:
            self._require_running()
            if type(cycle_id) is not int or cycle_id <= self._last_id:
                raise ValueError("Cycle IDs must increase within the session")
            if len(self._cycles) >= self._capacity:
                raise CoordinatorCapacityError(
                    "Early inspection cycle capacity reached"
                )
            now = self._clock()
            result: Future[ProductionInspectionResult] = promise(Future())
            self._cycles[cycle_id] = _Cycle(cycle_id, now, now + self._timeout, result)
            self._last_id = cycle_id
            self.peak_cycles = max(self.peak_cycles, len(self._cycles))
            self._condition.notify_all()
        accepted: Future[None] = promise(Future())
        accepted.set_result(None)
        return EarlyAdmission(accepted, result)

    def submit_frame(self, frame: Frame) -> Future[None]:
        with self._condition:
            self._require_running()
            cycle = self._cycles.get(frame.id.cycle_id)
            if cycle is None or self._clock() >= cycle.deadline:
                raise ValueError("Late or unknown cycle; never reopen")
            camera = next(
                (c for c in self.layout.cameras if c.camera_id == frame.id.camera_id),
                None,
            )
            if camera is None:
                raise ValueError("Unknown camera")
            if frame.id.camera_id in cycle.frames:
                raise ValueError("Duplicate camera frame")
            shape = (frame.image.shape[0], frame.image.shape[1], frame.image.shape[2])
            camera.validate_shape(shape)
            cycle.frames[frame.id.camera_id] = frame.id
            cycle.shapes[frame.id.camera_id] = shape
            self._jobs.append(frame)
            self.peak_jobs = max(self.peak_jobs, len(self._jobs))
            self._condition.notify_all()
        accepted: Future[None] = promise(Future())
        accepted.set_result(None)
        return accepted

    def expire(self) -> Future[None]:
        # Wakes explicit fake-clock tests; real deadlines have an owner wait.
        with self._condition:
            self._condition.notify_all()
        acknowledged: Future[None] = promise(Future())
        acknowledged.set_result(None)
        return acknowledged

    def when_settled(self, cycle_id: CycleId) -> Future[None]:
        """Observe terminal delivery AND actual camera-task return, not thread exit.

        A deadline result may precede the native return. No completed history is
        retained; only existing live cycles or the one active lease need a waiter.
        """
        with self._condition:
            if cycle_id > self._last_id or cycle_id < 1:
                raise CoordinatorStateError("Unknown early cycle")
            if self._busy_cycle(cycle_id):
                if cycle_id not in self._settlement:
                    self._settlement[cycle_id] = promise(Future())
                return self._settlement[cycle_id]
        completed: Future[None] = promise(Future())
        completed.set_result(None)
        return completed

    def _busy_cycle(self, cycle_id: CycleId) -> bool:
        return (
            cycle_id in self._cycles
            or (self._active is not None and self._active.cycle_id == cycle_id)
            or any(frame.id.cycle_id == cycle_id for frame in self._jobs)
        )

    def request_stop(self) -> None:
        with self._condition:
            self._stop = True
            self._condition.notify_all()

    def join(self, timeout: float | None = None) -> bool:
        if not self._started:
            return True
        self._owner.join(timeout)
        return not self._owner.is_alive()

    def _require_running(self) -> None:
        if not self._ready or self._stop:
            raise CoordinatorStateError("Early coordinator is not admitting work")

    def _infer(self, image: NDArray[np.uint8]) -> ImageResult:
        # Submission failures are infrastructure errors, not ordinary model NG.
        try:
            task = self.worker.submit(image)
        except Exception as exc:
            raise _DispatchFailure("Worker rejected early-camera task") from exc
        return task.result()

    def _run_camera(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._jobs or self._stop)
                if self._stop:
                    return
                frame = self._jobs.popleft()
                cycle = self._cycles.get(frame.id.cycle_id)
                if cycle is None or self._clock() >= cycle.deadline:
                    continue
                if cycle.inspecting_at is None:
                    cycle.inspecting_at = self._clock()
                self._active = frame.id
            try:
                result: CameraInspectionResult | BaseException = self._inspect(
                    frame, self._infer
                )
            except BaseException as exc:
                result = exc
            finished = self._clock()
            with self._condition:
                self._active = None
                self._completed.append((frame.id, result, finished))
                self._condition.notify_all()
            del frame, result, cycle  # Do not retain the last source frame while idle.

    def _finalize(
        self, cycle: _Cycle, status: CycleStatus, now: float
    ) -> ProductionInspectionResult:
        cameras = tuple(
            cycle.results.get(camera.camera_id)
            or self._forced(
                camera,
                cycle.frames.get(camera.camera_id),
                cycle.shapes.get(camera.camera_id),
                "basler.missing_camera"
                if camera.camera_id not in cycle.frames
                else "basler.inspection_deadline"
                if status is CycleStatus.TIMED_OUT
                else "basler.shutdown_unfinished",
            )
            for camera in self.layout.cameras
        )
        return ProductionInspectionResult(
            cycle.cycle_id,
            status,
            self.layout,
            cameras,
            cycle.inspecting_at,
            now,
            0.0,
            frame_failures_in_checks=True,
        )

    def _run(self) -> None:
        try:
            self.worker.start().result()
            self._runner.start()
            with self._condition:
                self._ready = True
            self._startup.set_result(None)
            while True:
                deliver: list[
                    tuple[
                        Future[ProductionInspectionResult], ProductionInspectionResult
                    ]
                ] = []
                with self._condition:
                    while self._completed:
                        identity, result, finished = self._completed.popleft()
                        if isinstance(result, BaseException):
                            raise result
                        cycle = self._cycles.get(identity.cycle_id)
                        if cycle is None or finished >= cycle.deadline:
                            self.late_results += 1
                        else:
                            if result.frame_id != identity:
                                raise RuntimeError(
                                    "Inspection returned another frame identity"
                                )
                            cycle.results[identity.camera_id] = result
                    now = self._clock()
                    for cycle in tuple(self._cycles.values()):
                        complete = len(cycle.results) == len(self.layout.cameras)
                        if complete or self._stop or now >= cycle.deadline:
                            status = (
                                (
                                    CycleStatus.COMPLETE
                                    if all(
                                        c.assessment.technical_status
                                        is not TechnicalStatus.ERROR
                                        for c in cycle.results.values()
                                    )
                                    else CycleStatus.FAILED
                                )
                                if complete
                                else CycleStatus.TIMED_OUT
                                if now >= cycle.deadline
                                else CycleStatus.SHUTDOWN
                            )
                            deliver.append(
                                (cycle.future, self._finalize(cycle, status, now))
                            )
                            del self._cycles[cycle.cycle_id]
                    self._jobs = deque(
                        f for f in self._jobs if f.id.cycle_id in self._cycles
                    )
                    settled = [
                        self._settlement.pop(identity)
                        for identity in tuple(self._settlement)
                        if not self._busy_cycle(identity)
                    ]
                    if not deliver and not settled and not self._stop:
                        deadline = min(
                            (c.deadline for c in self._cycles.values()), default=None
                        )
                        self._condition.wait(
                            None if deadline is None else max(0, deadline - now)
                        )
                        continue
                    stop = self._stop
                for future, result in deliver:
                    future.set_result(
                        result
                    )  # No admission lock held during callbacks.
                for completed in settled:
                    completed.set_result(None)
                if stop:
                    break
        except BaseException as exc:
            with self._condition:
                self._failure = exc
            if not self._startup.done():
                self._startup.set_exception(exc)
        finally:
            self.request_stop()
            if self._runner.ident is not None:
                self._runner.join()  # Never abandon an active native-call lease.
            self.worker.request_stop()
            self.worker.join()
            with self._condition:
                pending = tuple(self._cycles.values())
                self._cycles.clear()
                self._jobs.clear()
                self._completed.clear()
                settled = tuple(self._settlement.values())
                self._settlement.clear()
            for cycle in pending:
                cycle.future.set_exception(
                    self.failure or CoordinatorStateError("Early runtime stopped")
                )
            for completed in settled:
                completed.set_result(None)
            self.finished.set_result(None)
