"""Scalar center-region presence evidence for dark-background capsule inspection."""

from dataclasses import dataclass
from enum import Enum, auto

import cv2
import numpy as np
from numpy.typing import NDArray

from capsule_vision.inference import BoundingRect

from .layout import valid_rect


class OccupancyState(Enum):
    EMPTY = auto()
    PRESENT = auto()
    UNKNOWN = auto()


@dataclass(frozen=True, slots=True)
class OccupancyView:
    foreground_pixels: int
    total_pixels: int

    def __post_init__(self) -> None:
        if (
            type(self.foreground_pixels) is not int
            or type(self.total_pixels) is not int
            or self.total_pixels <= 0
            or not 0 <= self.foreground_pixels <= self.total_pixels
        ):
            raise ValueError("Occupancy requires valid counts from a nonempty view")

    @property
    def ratio(self) -> float:
        return self.foreground_pixels / self.total_pixels

    @property
    def state(self) -> OccupancyState:
        if self.ratio <= 0.01:
            return OccupancyState.EMPTY
        if self.ratio >= 0.05:
            return OccupancyState.PRESENT
        return OccupancyState.UNKNOWN


@dataclass(frozen=True, slots=True)
class OccupancyEvidence:
    measurement_rect: BoundingRect
    upper: OccupancyView
    lower: OccupancyView

    def __post_init__(self) -> None:
        valid_rect(self.measurement_rect)
        rect = self.measurement_rect
        if rect.height < 2 or (
            self.upper.total_pixels != rect.width * (rect.height // 2)
            or self.lower.total_pixels != rect.width * (rect.height - rect.height // 2)
        ):
            raise ValueError("Occupancy counts must match the two measurement views")

    @property
    def state(self) -> OccupancyState:
        upper, lower = self.upper.state, self.lower.state
        if upper is lower and upper in (OccupancyState.EMPTY, OccupancyState.PRESENT):
            return upper
        return OccupancyState.UNKNOWN


@dataclass(frozen=True, slots=True)
class OccupancyResult:
    """Evidence or explicit failure; unavailable data never asserts EMPTY."""

    evidence: OccupancyEvidence | None = None
    error_code: str | None = None
    exception_type: str | None = None
    message: str | None = None

    def __post_init__(self) -> None:
        if (self.evidence is None) == (self.error_code is None):
            raise ValueError("Occupancy requires either evidence or a failure code")
        if self.error_code is not None and not self.error_code.strip():
            raise ValueError("Occupancy failure requires a nonempty code")
        if self.evidence is not None and (
            self.exception_type is not None or self.message is not None
        ):
            raise ValueError("Successful Occupancy cannot carry failure details")

    @property
    def state(self) -> OccupancyState:
        return (
            self.evidence.state if self.evidence is not None else OccupancyState.UNKNOWN
        )


def occupancy_rect(roi: BoundingRect) -> BoundingRect:
    """E0-R rounding: floor(70% width), minimum one pixel, extra margin on right."""
    valid_rect(roi)
    width = max(1, roi.width * 70 // 100)
    return BoundingRect(roi.x + (roi.width - width) // 2, roi.y, width, roi.height)


def measure_occupancy(image: NDArray[np.uint8], roi: BoundingRect) -> OccupancyEvidence:
    """Borrow pixels; count every pixel, retain only immutable scalar evidence."""
    if (
        image.dtype != np.uint8
        or image.ndim != 3
        or image.shape[2] != 3
        or min(image.shape[:2]) <= 0
    ):
        raise ValueError("Expected nonempty uint8 H x W x 3 BGR")
    valid_rect(roi)
    # Check the original ROI first: a valid center cannot conceal invalid bounds.
    if (
        roi.height < 2
        or roi.x + roi.width > image.shape[1]
        or roi.y + roi.height > image.shape[0]
    ):
        raise ValueError("Original ROI needs two nonempty in-bounds views")
    rect = occupancy_rect(roi)
    pixels = image[rect.y : rect.y + rect.height, rect.x : rect.x + rect.width]
    middle = rect.height // 2

    def count(view: NDArray[np.uint8]) -> OccupancyView:
        # Same E0-R kernel: complement of inclusive BGR <=20, without inversion.
        background = cv2.inRange(view, (0, 0, 0), (20, 20, 20))
        total = int(background.size)
        return OccupancyView(total - cv2.countNonZero(background), total)

    return OccupancyEvidence(rect, count(pixels[:middle]), count(pixels[middle:]))
