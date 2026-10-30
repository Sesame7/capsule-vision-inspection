"""Explicit slot geometry; channel identity never follows image orientation."""

from dataclasses import dataclass
from typing import NewType

from capsule_vision.inference import BoundingRect

from ..models import CameraId

ChannelId = NewType("ChannelId", int)
SLOTS_PER_CAMERA = 6


def valid_rect(rect: BoundingRect) -> None:
    if any(type(n) is not int for n in (rect.x, rect.y, rect.width, rect.height)):
        raise ValueError("Rectangle coordinates must be integers")
    if min(rect.x, rect.y) < 0 or min(rect.width, rect.height) <= 0:
        raise ValueError("Rectangle must have nonnegative origin and positive size")


@dataclass(frozen=True, slots=True)
class ChannelROI:
    slot: int
    channel_id: ChannelId
    rect: BoundingRect

    def __post_init__(self) -> None:
        if type(self.slot) is not int or not 0 <= self.slot < SLOTS_PER_CAMERA:
            raise ValueError("Slot must be in 0..5")
        if type(self.channel_id) is not int or self.channel_id < 1:
            raise ValueError("ChannelId must be a positive integer")
        valid_rect(self.rect)


@dataclass(frozen=True, slots=True)
class CameraLayout:
    camera_id: CameraId
    slots: tuple[ChannelROI, ...]

    def __post_init__(self) -> None:
        if not self.camera_id.strip():
            raise ValueError("Camera identity must be nonempty")
        slots = tuple(sorted(self.slots, key=lambda item: item.slot))
        if tuple(item.slot for item in slots) != tuple(range(SLOTS_PER_CAMERA)):
            raise ValueError("Camera requires each slot 0..5 exactly once")
        if len({item.channel_id for item in slots}) != len(slots):
            raise ValueError("Duplicate channel mapping")
        object.__setattr__(self, "slots", slots)

    def validate_shape(self, shape: tuple[int, int, int]) -> None:
        height, width, channels = shape
        if min(height, width) <= 0 or channels != 3:
            raise ValueError("Expected nonempty H x W x 3 image shape")
        if any(
            item.rect.x + item.rect.width > width
            or item.rect.y + item.rect.height > height
            for item in self.slots
        ):
            raise ValueError("Configured ROI extends outside the camera image")


@dataclass(frozen=True, slots=True)
class InspectionLayout:
    cameras: tuple[CameraLayout, ...]

    def __post_init__(self) -> None:
        cameras = tuple(self.cameras)
        ids = tuple(camera.camera_id for camera in cameras)
        if not cameras or len(set(ids)) != len(ids):
            raise ValueError("Expected unique nonempty camera layout")
        channels = [slot.channel_id for camera in cameras for slot in camera.slots]
        if sorted(channels) != list(range(1, len(channels) + 1)):
            raise ValueError("Channels must map exactly once to contiguous IDs 1..N")
        object.__setattr__(self, "cameras", cameras)

    @property
    def camera_ids(self) -> tuple[CameraId, ...]:
        return tuple(camera.camera_id for camera in self.cameras)


@dataclass(frozen=True, slots=True)
class OverlapPolicy:
    """Integer bbox intersection in original pixels; touching edges do not match."""

    minimum_intersection_area: int = 1

    def __post_init__(self) -> None:
        if (
            type(self.minimum_intersection_area) is not int
            or self.minimum_intersection_area < 1
        ):
            raise ValueError("Minimum intersection area must be a positive integer")

    def intersection(self, finding: BoundingRect, roi: BoundingRect) -> int:
        width = max(
            0, min(finding.x + finding.width, roi.x + roi.width) - max(finding.x, roi.x)
        )
        height = max(
            0,
            min(finding.y + finding.height, roi.y + roi.height) - max(finding.y, roi.y),
        )
        return width * height
