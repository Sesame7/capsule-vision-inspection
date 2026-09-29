"""Frame ownership and technical cycle results, without acceptance policy."""

import math
from dataclasses import dataclass
from enum import Enum, auto
from typing import NewType

import numpy as np
from numpy.typing import NDArray

from vimo_demo.inference import ImageResult

CycleId = NewType("CycleId", int)
CameraId = NewType("CameraId", str)


def nonnegative_integer(value: int, name: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")


def timestamp(value: float) -> float:
    if isinstance(value, bool) or not math.isfinite(value) or value < 0:
        raise ValueError("Timestamp must be finite and nonnegative")
    return value


@dataclass(frozen=True, slots=True)
class FrameId:
    cycle_id: CycleId
    camera_id: CameraId
    sequence: int

    def __post_init__(self) -> None:
        nonnegative_integer(self.cycle_id, "cycle_id")
        nonnegative_integer(self.sequence, "sequence")
        if not self.camera_id or not self.camera_id.strip():
            raise ValueError("camera_id must not be empty")


@dataclass(frozen=True, slots=True, eq=False)
class Frame:
    """Transfer an exclusively owned contiguous uint8 BGR array; never copy.

    The producer must not mutate any alias while this frame is in use. Views and
    borrowed buffers require an explicit producer copy before construction.
    captured_at is source-clock metadata, not the collection deadline clock.
    """

    id: FrameId
    captured_at: float
    image: NDArray[np.uint8]

    def __post_init__(self) -> None:
        timestamp(self.captured_at)
        image = self.image
        if image.dtype != np.uint8:
            raise TypeError("Frame image must be uint8")
        if image.ndim != 3 or image.shape[2] != 3 or min(image.shape[:2]) <= 0:
            raise ValueError("Frame image must be nonempty H x W x 3 BGR")
        if not image.flags.owndata or not image.flags.c_contiguous:
            raise ValueError(
                "Frame image must own contiguous pixels; copy borrowed buffers explicitly"
            )
        image.setflags(write=False)


@dataclass(frozen=True, slots=True, eq=False)
class ReceivedFrame:
    frame: Frame
    arrived_at: float


class CollectionStatus(Enum):
    READY = auto()
    TIMED_OUT = auto()
    SHUTDOWN = auto()


@dataclass(frozen=True, slots=True, eq=False)
class CollectedCycle:
    """Terminal collection snapshot; READY does not mean inspection succeeded."""

    cycle_id: CycleId
    expected_cameras: tuple[CameraId, ...]
    started_at: float
    deadline: float
    finalized_at: float
    status: CollectionStatus
    frames: tuple[ReceivedFrame, ...]

    def __post_init__(self) -> None:
        nonnegative_integer(self.cycle_id, "cycle_id")
        cameras = self.expected_cameras
        if (
            not cameras
            or len(set(cameras)) != len(cameras)
            or any(not c.strip() for c in cameras)
        ):
            raise ValueError("Cycle must have unique nonempty expected cameras")
        present = tuple(entry.frame.id.camera_id for entry in self.frames)
        if (
            len(set(present)) != len(present)
            or present != tuple(camera for camera in cameras if camera in present)
            or any(entry.frame.id.cycle_id != self.cycle_id for entry in self.frames)
        ):
            raise ValueError(
                "Frames must belong to this cycle in expected camera order"
            )
        start, deadline, end = map(
            timestamp, (self.started_at, self.deadline, self.finalized_at)
        )
        if deadline <= start or end < start:
            raise ValueError("Invalid collection time interval")
        for entry in self.frames:
            arrival = timestamp(entry.arrived_at)
            if arrival < start or arrival >= deadline or arrival > end:
                raise ValueError("Frame arrival is outside its collection interval")
        if self.status is CollectionStatus.READY:
            if self.missing_cameras or end >= deadline:
                raise ValueError("READY requires every camera before the deadline")
        elif not self.missing_cameras:
            raise ValueError("Incomplete collection must have missing cameras")
        elif self.status is CollectionStatus.TIMED_OUT and end < deadline:
            raise ValueError("TIMED_OUT requires reaching the deadline")
        elif self.status is CollectionStatus.SHUTDOWN and end >= deadline:
            raise ValueError("Expired collection must be TIMED_OUT, not SHUTDOWN")

    @property
    def missing_cameras(self) -> tuple[CameraId, ...]:
        present = {entry.frame.id.camera_id for entry in self.frames}
        return tuple(
            camera for camera in self.expected_cameras if camera not in present
        )


@dataclass(frozen=True, slots=True)
class InspectionFailure:
    exception_type: str
    message: str
    traceback: str


@dataclass(frozen=True, slots=True, eq=False)
class FrameInspectionResult:
    """No source pixels retained. None/None result/failure means not inspected."""

    frame_id: FrameId
    captured_at: float
    arrived_at: float
    image_shape: tuple[int, int, int]
    started_at: float | None
    finished_at: float | None
    result: ImageResult | None
    failure: InspectionFailure | None


class CycleStatus(Enum):
    COMPLETE = auto()
    FAILED = auto()
    TIMED_OUT = auto()
    SHUTDOWN = auto()


@dataclass(frozen=True, slots=True, eq=False)
class InspectionCycleResult:
    cycle_id: CycleId
    expected_cameras: tuple[CameraId, ...]
    started_at: float
    deadline: float
    collection_finished_at: float
    finished_at: float
    status: CycleStatus
    frames: tuple[FrameInspectionResult, ...]
    missing_cameras: tuple[CameraId, ...]

    @property
    def is_complete(self) -> bool:
        """All required frames were inspected successfully, independent of defects."""
        return self.status is CycleStatus.COMPLETE
