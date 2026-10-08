"""Provisional black-background, two-view capsule screening; not calibration."""

from dataclasses import dataclass, field

import cv2
import numpy as np
from numpy.typing import NDArray

from vimo_demo.inference import BoundingRect

from .layout import valid_rect
from .models import (
    Assessment,
    CheckResult,
    FindingSource,
    Measurement,
    ProductJudgment,
    Reason,
    TechnicalStatus,
)


def _integer(value: int, minimum: int, maximum: int | None = None) -> None:
    if (
        type(value) is not int
        or value < minimum
        or (maximum is not None and value > maximum)
    ):
        raise ValueError(f"Expected integer in [{minimum}, {maximum}]")


@dataclass(frozen=True, slots=True)
class SizeSettings:
    enabled: bool = False
    target_width: int = 1
    width_tolerance: int = 0
    target_height: int = 1
    height_tolerance: int = 0

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ValueError("Size enabled must be boolean")
        for value in (self.target_width, self.target_height):
            _integer(value, 1)
        for value in (self.width_tolerance, self.height_tolerance):
            _integer(value, 0)


@dataclass(frozen=True, slots=True)
class ColorSettings:
    enabled: bool = False
    target_hue: int = 0
    hue_tolerance: int = 0

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ValueError("Color enabled must be boolean")
        _integer(self.target_hue, 0, 179)
        _integer(self.hue_tolerance, 0, 90)


@dataclass(frozen=True, slots=True)
class CapsuleSettings:
    size: SizeSettings = field(default_factory=SizeSettings)
    color: ColorSettings = field(default_factory=ColorSettings)

    @property
    def enabled(self) -> bool:
        return self.size.enabled or self.color.enabled


@dataclass(frozen=True, slots=True)
class BodyMeasurement:
    """Original full-image coordinates. Missing hue is unusable chroma, not zero."""

    bounding_rect: BoundingRect
    hue: float | None


@dataclass(frozen=True, slots=True)
class ViewMeasurement:
    name: str
    body: BodyMeasurement | None
    invalid_reason: str | None = None


@dataclass(frozen=True, slots=True)
class CapsuleMeasurement:
    views: tuple[ViewMeasurement, ViewMeasurement]


def hue_distance(left: float, right: float) -> float:
    distance = abs(left - right) % 180
    return min(distance, 180 - distance)


def median_hue(values: NDArray[np.float64]) -> float | None:
    """Unwrap about the circular mean before median; reject ambiguous mixtures."""
    if values.size == 0:
        return None
    angles = values * (2 * np.pi / 180)
    vector = np.mean(np.exp(1j * angles))
    if abs(vector) < 0.5:
        return None
    center = round(float(np.angle(vector)) * 180 / (2 * np.pi)) % 180
    unwrapped = (values - center + 90) % 180 - 90
    return float((center + np.median(unwrapped)) % 180)


def measure_capsule(image: NDArray[np.uint8], roi: BoundingRect) -> CapsuleMeasurement:
    """Borrow pixels; never mutate, resize or retain source data.

    Provisional constants: V > 20; particles below max(12px, 0.5% of view);
    competing bodies >= 25% of dominant area. Boundary contact means truncated.
    """
    if image.dtype != np.uint8 or image.ndim != 3 or image.shape[2] != 3:
        raise ValueError("Expected uint8 H x W x 3 BGR image")
    valid_rect(roi)
    if roi.x + roi.width > image.shape[1] or roi.y + roi.height > image.shape[0]:
        raise ValueError("Capsule ROI exceeds image bounds")
    if roi.height < 2:
        raise ValueError("Capsule ROI needs two nonempty views")
    crop = image[roi.y : roi.y + roi.height, roi.x : roi.x + roi.width]
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    middle = roi.height // 2

    def view(name: str, start: int, end: int) -> ViewMeasurement:
        pixels = hsv[start:end]
        foreground = (pixels[:, :, 2] > 20).astype(np.uint8)
        count, labels, stats, _ = cv2.connectedComponentsWithStats(
            foreground, connectivity=8
        )
        minimum = max(12, int(foreground.size * 0.005))
        candidates = sorted(
            (i for i in range(1, count) if int(stats[i, cv2.CC_STAT_AREA]) >= minimum),
            key=lambda i: (-int(stats[i, cv2.CC_STAT_AREA]), i),
        )
        if not candidates:
            return ViewMeasurement(name, None, "missing_body")
        dominant = candidates[0]
        area = int(stats[dominant, cv2.CC_STAT_AREA])
        if (
            len(candidates) > 1
            and int(stats[candidates[1], cv2.CC_STAT_AREA]) >= area * 0.25
        ):
            return ViewMeasurement(name, None, "competing_bodies")
        x, y, width, height = (int(v) for v in stats[dominant, :4])
        if x == 0 or y == 0 or x + width == roi.width or y + height == end - start:
            return ViewMeasurement(name, None, "truncated_body")
        if min(width, height) < 3:
            return ViewMeasurement(name, None, "invalid_geometry")
        body_mask = (labels == dominant).astype(np.uint8)
        # Erosion is only an interior sampling mask, never body/size morphology.
        interior = cv2.erode(body_mask, np.ones((3, 3), dtype=np.uint8)) != 0
        suitable = interior & (pixels[:, :, 1] >= 40) & (pixels[:, :, 2] >= 40)
        enough = np.count_nonzero(suitable) >= max(
            5, int(np.count_nonzero(interior) * 0.1)
        )
        hue = (
            median_hue(pixels[:, :, 0][suitable].astype(np.float64)) if enough else None
        )
        return ViewMeasurement(
            name,
            BodyMeasurement(
                BoundingRect(roi.x + x, roi.y + start + y, width, height), hue
            ),
        )

    return CapsuleMeasurement(
        (view("upper", 0, middle), view("lower", middle, roi.height))
    )


def judge_capsule(
    measured: CapsuleMeasurement, settings: CapsuleSettings
) -> CheckResult:
    """Content rejection is SUCCESS/NG; execution exceptions belong to the caller."""
    source = FindingSource.TRADITIONAL
    if not settings.enabled:
        return CheckResult(
            source, Assessment(TechnicalStatus.DISABLED, ProductJudgment.UNKNOWN)
        )
    reasons: list[Reason] = []
    values: list[Measurement] = []
    for view in measured.views:
        body = view.body
        if body is None:
            reasons.append(Reason(source, f"capsule.{view.name}.{view.invalid_reason}"))
            continue
        rect = body.bounding_rect
        values.extend(
            (
                Measurement(f"capsule.{view.name}.width", rect.width, "px"),
                Measurement(f"capsule.{view.name}.height", rect.height, "px"),
            )
        )
        if body.hue is not None:
            values.append(
                Measurement(f"capsule.{view.name}.hue", body.hue, "opencv_hue")
            )
        size, color = settings.size, settings.color
        if size.enabled:
            for name, actual, target, tolerance in (
                ("width", rect.width, size.target_width, size.width_tolerance),
                ("height", rect.height, size.target_height, size.height_tolerance),
            ):
                if abs(actual - target) > tolerance:
                    reasons.append(
                        Reason(
                            source,
                            f"capsule.size.{view.name}.{name}",
                            bounding_rect=rect,
                        )
                    )
        if color.enabled and (
            body.hue is None
            or hue_distance(body.hue, color.target_hue) > color.hue_tolerance
        ):
            reasons.append(
                Reason(
                    source,
                    f"capsule.color.{view.name}.{'unusable' if body.hue is None else 'tolerance'}",
                    bounding_rect=rect,
                )
            )
    return CheckResult(
        source,
        Assessment(
            TechnicalStatus.SUCCESS,
            ProductJudgment.NG if reasons else ProductJudgment.OK,
        ),
        tuple(reasons),
        tuple(values),
        required=True,
    )
