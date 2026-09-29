"""Single-owner collection and synchronous inspection, with explicit clock ticks."""

import math
import traceback
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from time import monotonic

import numpy as np
from numpy.typing import NDArray

from vimo_demo.inference import ImageResult

from .models import (
    CameraId,
    CollectedCycle,
    CollectionStatus,
    CycleId,
    CycleStatus,
    Frame,
    FrameInspectionResult,
    InspectionCycleResult,
    InspectionFailure,
    ReceivedFrame,
    nonnegative_integer,
    timestamp,
)


class CycleStateError(RuntimeError):
    """Cycle is not active, its ID was used, or the collector is shut down."""


class CycleCapacityError(CycleStateError):
    """No active-cycle slot is available; nothing was accepted or evicted."""


class DuplicateFrameError(ValueError):
    """This camera already has a frame in this cycle, regardless of sequence."""


class UnknownCameraError(ValueError):
    """Camera is not in the configured expected camera tuple."""


class CycleDeadlineError(CycleStateError):
    """Frame arrived at/after deadline; call expire() to retrieve the timeout."""


@dataclass(slots=True)
class _ActiveCycle:
    started_at: float
    deadline: float
    frames: dict[CameraId, ReceivedFrame] = field(
        default_factory=lambda: dict[CameraId, ReceivedFrame]()
    )


class CycleCollector:
    """Explicitly begin increasing IDs; accept frames in any arrival order.

    Call all methods in one owning context. No threads, callbacks or scheduler
    are created. Poll deadlines through expire() from the application's clock
    events, not sleeps. No finalized snapshots are retained internally.
    """

    def __init__(
        self,
        expected_cameras: Sequence[CameraId],
        *,
        timeout: float,
        max_active: int = 2,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        cameras = tuple(expected_cameras)
        if not cameras or any(not camera.strip() for camera in cameras):
            raise ValueError("Expected cameras must be nonempty identities")
        if len(set(cameras)) != len(cameras):
            raise ValueError("Expected cameras must be unique")
        if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        if type(max_active) is not int or max_active < 1:
            raise ValueError("max_active must be a positive integer")
        self._cameras = cameras
        self._timeout = timeout
        self._max_active = max_active
        self._clock = clock
        self._last_time = 0.0
        self._last_id = -1
        self._active: dict[CycleId, _ActiveCycle] = {}
        self._closed = False

    @property
    def active_cycle_ids(self) -> tuple[CycleId, ...]:
        return tuple(sorted(self._active))

    def _now(self) -> float:
        now = timestamp(self._clock())
        if now < self._last_time:
            raise ValueError("Collection clock moved backwards")
        self._last_time = now
        return now

    def _check_open(self) -> None:
        if self._closed:
            raise CycleStateError("Collector is shut down")

    def begin(self, cycle_id: CycleId) -> None:
        self._check_open()
        nonnegative_integer(cycle_id, "cycle_id")
        if cycle_id <= self._last_id:
            raise CycleStateError(
                "Cycle IDs must increase; IDs cannot be reused in this session"
            )
        if len(self._active) >= self._max_active:
            raise CycleCapacityError(
                "Active-cycle capacity reached; expire/finalize a cycle first"
            )
        now = self._now()
        deadline = timestamp(now + self._timeout)
        if deadline <= now:
            raise ValueError("timeout is smaller than the clock's numeric resolution")
        self._active[cycle_id] = _ActiveCycle(now, deadline)
        self._last_id = cycle_id

    def accept(self, frame: Frame) -> CollectedCycle | None:
        self._check_open()
        cycle_id, camera = frame.id.cycle_id, frame.id.camera_id
        active = self._active.get(cycle_id)
        if active is None:
            raise CycleStateError(
                f"Cycle {cycle_id} is not active (unopened or finalized)"
            )
        now = self._now()
        if now >= active.deadline:
            raise CycleDeadlineError(
                f"Cycle {cycle_id} deadline reached; frame rejected"
            )
        if camera not in self._cameras:
            raise UnknownCameraError(f"Unexpected camera {camera!r}")
        if camera in active.frames:
            raise DuplicateFrameError(
                f"Cycle {cycle_id} already contains camera {camera!r}"
            )
        active.frames[camera] = ReceivedFrame(frame, now)
        if len(active.frames) == len(self._cameras):
            return self._finish(cycle_id, CollectionStatus.READY, now)
        return None

    def _finish(
        self, cycle_id: CycleId, status: CollectionStatus, now: float
    ) -> CollectedCycle:
        active = self._active.pop(cycle_id)
        return CollectedCycle(
            cycle_id,
            self._cameras,
            active.started_at,
            active.deadline,
            now,
            status,
            tuple(
                active.frames[camera]
                for camera in self._cameras
                if camera in active.frames
            ),
        )

    def expire(self) -> tuple[CollectedCycle, ...]:
        """Finalize all due cycles in ID order. Boundary is now >= deadline."""
        if self._closed:
            return ()
        now = self._now()
        return tuple(
            self._finish(cycle_id, CollectionStatus.TIMED_OUT, now)
            for cycle_id in self.active_cycle_ids
            if now >= self._active[cycle_id].deadline
        )

    def shutdown(self) -> tuple[CollectedCycle, ...]:
        """Finalize partial cycles once. Already-expired cycles keep timeout status."""
        if self._closed:
            return ()
        now = self._now()
        self._closed = True
        return tuple(
            self._finish(
                cycle_id,
                CollectionStatus.TIMED_OUT
                if now >= self._active[cycle_id].deadline
                else CollectionStatus.SHUTDOWN,
                now,
            )
            for cycle_id in self.active_cycle_ids
        )


def inspect_cycle(
    cycle: CollectedCycle,
    inspect: Callable[[NDArray[np.uint8]], ImageResult],
    *,
    clock: Callable[[], float] = monotonic,
) -> InspectionCycleResult:
    """Inspect READY snapshots serially; incomplete snapshots never invoke inspect.

    Execute outside the GUI thread. The injected callable owns its engine/worker
    integration; this function constructs neither. Each Exception becomes a
    compact failure with its formatted cause chain, not a retained traceback.
    """
    last_time = cycle.finalized_at

    def now() -> float:
        nonlocal last_time
        value = timestamp(clock())
        if value < last_time:
            raise ValueError("Inspection clock predates collection or moved backwards")
        last_time = value
        return value

    status = {
        CollectionStatus.READY: CycleStatus.COMPLETE,
        CollectionStatus.TIMED_OUT: CycleStatus.TIMED_OUT,
        CollectionStatus.SHUTDOWN: CycleStatus.SHUTDOWN,
    }[cycle.status]
    outcomes: list[FrameInspectionResult] = []
    for received in cycle.frames:
        frame = received.frame
        height, width, channels = frame.image.shape
        started = finished = None
        result: ImageResult | None = None
        failure: InspectionFailure | None = None
        if cycle.status is CollectionStatus.READY:
            started = now()
            try:
                result = inspect(frame.image)
                if result.image_shape != (height, width, channels):
                    raise ValueError("Inspection result shape differs from its frame")
            except Exception as exc:
                result = None
                failure = InspectionFailure(
                    f"{type(exc).__module__}.{type(exc).__qualname__}",
                    str(exc),
                    "".join(traceback.format_exception(exc)),
                )
                status = CycleStatus.FAILED
            finished = now()
        outcomes.append(
            FrameInspectionResult(
                frame.id,
                frame.captured_at,
                received.arrived_at,
                (height, width, channels),
                started,
                finished,
                result,
                failure,
            )
        )
    return InspectionCycleResult(
        cycle.cycle_id,
        cycle.expected_cameras,
        cycle.started_at,
        cycle.deadline,
        cycle.finalized_at,
        last_time,
        status,
        tuple(outcomes),
        cycle.missing_cameras,
    )
