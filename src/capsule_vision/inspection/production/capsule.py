"""Black-background two-view capsule screening; calibration remains external."""

import math
from collections.abc import Callable
from dataclasses import dataclass, field

import cv2
import numpy as np
from numpy.typing import NDArray

from capsule_vision.inference import BoundingRect

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
    target_length: int = 1
    length_tolerance: int = 0

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ValueError("Size enabled must be boolean")
        _integer(self.target_length, 1)
        _integer(self.length_tolerance, 0)


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
    """Size geometry in original full-image coordinates."""

    bounding_rect: BoundingRect
    length: float
    minor_length: float
    endpoints: tuple[tuple[float, float], tuple[float, float]]


@dataclass(frozen=True, slots=True)
class ColorMeasurement:
    """View-level evidence, independent of body geometry. No invented Hue."""

    hue: float | None
    usable_pixels: int
    minimum_pixels: int
    invalid_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ViewMeasurement:
    name: str
    body: BodyMeasurement | None = None
    invalid_reason: str | None = None
    color: ColorMeasurement | None = None


@dataclass(frozen=True, slots=True)
class CapsuleMeasurement:
    views: tuple[ViewMeasurement, ViewMeasurement]
    size_measured: bool
    color_measured: bool


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


# OpenCV Hue is an integer in 0..179. Reuse the exact per-bin vectors from
# median_hue(), without allocating/trig-reducing one complex value per pixel.
_HUE_VECTORS = np.exp(1j * (np.arange(180, dtype=np.float64) * (2 * np.pi / 180)))
_HUE_VECTORS.flags.writeable = False


def median_pixel_hue(values: NDArray[np.uint8]) -> float | None:
    """Same circular-center/median rule for integer OpenCV Hue samples."""
    if values.size == 0:
        return None
    counts = np.bincount(values, minlength=180)
    return _histogram_hue(counts, values.size, lambda: values)


def _histogram_hue(
    counts: NDArray[np.intp],
    count: int,
    samples: Callable[[], NDArray[np.uint8]],
) -> float | None:
    """Exact circular ranks; gather original-order pixels only for fallback."""
    vector = complex(np.dot(counts, _HUE_VECTORS)) / count
    magnitude = abs(vector)
    # Histogram reduction changes floating summation order. At the ambiguity
    # boundary or a rounding tie, preserve the original pixel-order computation.
    # These guards select an implementation; they do not relax judgment limits.
    if abs(magnitude - 0.5) <= 1e-12:
        return median_hue(samples().astype(np.float64))
    if magnitude < 0.5:
        return None
    angle = float(np.angle(vector)) * 180 / (2 * np.pi)
    if abs(angle - (np.floor(angle) + 0.5)) <= 1e-10:
        return median_hue(samples().astype(np.float64))
    center = round(angle) % 180
    # Search the tail then wrapped head of original-bin prefix sums. Average
    # unwrapped offsets, not Hue IDs, to preserve even medians across 179/0.
    start = (center - 90) % 180
    cumulative = np.cumsum(counts)
    base = int(cumulative[start - 1]) if start else 0
    tail = count - base
    lower_rank, upper_rank = (count - 1) // 2 + 1, count // 2 + 1
    lower = int(
        np.searchsorted(
            cumulative, lower_rank + base if lower_rank <= tail else lower_rank - tail
        )
    )
    upper = int(
        np.searchsorted(
            cumulative, upper_rank + base if upper_rank <= tail else upper_rank - tail
        )
    )
    lower, upper = (lower - start) % 180, (upper - start) % 180
    return float((center + (lower + upper) / 2 - 90) % 180)


def _masked_pixel_hue(
    pixels: NDArray[np.uint8], usable: NDArray[np.uint8], count: int
) -> float | None:
    if count == 0:
        return None

    def samples() -> NDArray[np.uint8]:
        return pixels[:, :, 0][usable != 0]

    # calcHist has float32 counts: integers through 2**24 are exact. Larger
    # samples keep the gathered/bincount path rather than lose count precision.
    if count > 2**24:
        return median_pixel_hue(samples())
    counts = (
        cv2.calcHist([pixels], [0], usable, [180], [0, 180]).ravel().astype(np.intp)
    )
    if int(counts.sum()) != count:
        # Do not silently discard out-of-range Hue bins or discrepant counts.
        return median_pixel_hue(samples())
    return _histogram_hue(counts, count, samples)


def _body(
    candidates: list[int],
    labels: NDArray[np.int32],
    stats: NDArray[np.int32],
    roi: BoundingRect,
) -> tuple[BodyMeasurement | None, str | None]:
    candidates.sort(key=lambda i: (-int(stats[i, cv2.CC_STAT_AREA]), i))
    if not candidates:
        return None, "missing_body"
    dominant = candidates[0]
    area = int(stats[dominant, cv2.CC_STAT_AREA])
    if (
        len(candidates) > 1
        and int(stats[candidates[1], cv2.CC_STAT_AREA]) >= area * 0.25
    ):
        return None, "competing_bodies"
    x, y, width, height = (int(v) for v in stats[dominant, :4])
    if x == 0 or y == 0 or x + width == roi.width or y + height == roi.height:
        return None, "truncated_body"
    if min(width, height) < 3:
        return None, "invalid_geometry"
    # Reuse the full-ROI labels. Only boundary vertices enter minAreaRect; no
    # per-pixel Python coordinates or visualization contours survive this call.
    selected = cv2.inRange(labels[y : y + height, x : x + width], dominant, dominant)
    contours, _ = cv2.findContours(selected, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    points = contours[0] if len(contours) == 1 else np.concatenate(contours)
    (cx, cy), (w, h), degrees = cv2.minAreaRect(points)
    if min(w, h) <= 1e-6:
        return None, "invalid_geometry"
    angle = math.radians(degrees if w >= h else degrees + 90)
    length = max(w, h)
    dx, dy = math.cos(angle) * length / 2, math.sin(angle) * length / 2
    cx, cy = cx + roi.x + x, cy + roi.y + y
    first, second = sorted(((cx - dx, cy - dy), (cx + dx, cy + dy)))
    return BodyMeasurement(
        BoundingRect(roi.x + x, roi.y + y, width, height),
        length,
        min(w, h),
        (first, second),
    ), None


def _geometry(
    foreground: NDArray[np.uint8], roi: BoundingRect
) -> tuple[tuple[BodyMeasurement | None, str | None], ...]:
    """Assign whole components once; a significant rival view is ambiguous.

    The existing quarter-dominant competition rule also guards cross-divider
    ownership. A significant minority (meeting its view's minimum area) cannot
    be discarded as a crossing tail. No clipped-body reconstruction is used.
    """
    _, labels, stats, centers = cv2.connectedComponentsWithStats(
        foreground, connectivity=8
    )
    labels, stats = (
        labels.astype(np.int32, copy=False),
        stats.astype(np.int32, copy=False),
    )
    middle = roi.height // 2
    minima = tuple(
        max(12, int(roi.width * height * 0.005))
        for height in (middle, roi.height - middle)
    )
    candidates: tuple[list[int], list[int]] = ([], [])
    ambiguous = [False, False]
    # Filter small specks in native array operations before extracting Python
    # geometry. Images may contain many below-minimum connected components.
    eligible = np.flatnonzero(stats[1:, cv2.CC_STAT_AREA] >= min(minima)) + 1
    for label in eligible:
        i = int(label)
        x, y, width, height, area = (int(v) for v in stats[i])
        upper = (
            area
            if y + height <= middle
            else 0
            if y >= middle
            else cv2.countNonZero(cv2.inRange(labels[y:middle, x : x + width], i, i))
        )
        portions = (upper, area - upper)
        owner = int(portions[1] > portions[0])
        rival = 1 - owner
        if portions[owner] < minima[owner]:
            continue
        # Centroid and foreground predominance must agree. Significant support
        # on both sides includes merged upper/lower bodies and exact ties.
        if (
            portions[rival] >= minima[rival] and portions[rival] * 4 >= portions[owner]
        ) or int(centers[i, 1] >= middle - 0.5) != owner:
            ambiguous[0] = ambiguous[1] = True
            continue
        candidates[owner].append(i)
    return tuple(
        (None, "ambiguous_body")
        if ambiguous[index]
        else _body(rows, labels, stats, roi)
        for index, rows in enumerate(candidates)
    )


def _color(pixels: NDArray[np.uint8], usable: NDArray[np.uint8]) -> ColorMeasurement:
    # S/V requirements imply V > 20 foreground; no component or erosion needed.
    count = cv2.countNonZero(usable)
    # Provisional: >=ceil(5% of view area), with a 12-pixel floor. Not presence.
    minimum = max(12, (usable.size + 19) // 20)
    if count < minimum:
        return ColorMeasurement(None, count, minimum, "insufficient_foreground")
    hue = _masked_pixel_hue(pixels, usable, count)
    return ColorMeasurement(hue, count, minimum, "unusable" if hue is None else None)


def measure_capsule(
    image: NDArray[np.uint8], roi: BoundingRect, settings: CapsuleSettings
) -> CapsuleMeasurement:
    """Borrow pixels; selectively measure geometry and/or independent view color.

    Targets/tolerances do not affect measurement. No resizing or retained arrays.
    """
    if image.dtype != np.uint8 or image.ndim != 3 or image.shape[2] != 3:
        raise ValueError("Expected uint8 H x W x 3 BGR image")
    valid_rect(roi)
    if roi.x + roi.width > image.shape[1] or roi.y + roi.height > image.shape[0]:
        raise ValueError("Capsule ROI exceeds image bounds")
    if roi.height < 2:
        raise ValueError("Capsule ROI needs two nonempty views")
    size, color = settings.size.enabled, settings.color.enabled
    if not settings.enabled:
        return CapsuleMeasurement(
            (ViewMeasurement("upper"), ViewMeasurement("lower")), False, False
        )
    crop = image[roi.y : roi.y + roi.height, roi.x : roi.x + roi.width]
    hsv = (
        cv2.cvtColor(crop, cv2.COLOR_BGR2HSV).astype(np.uint8, copy=False)
        if color
        else None
    )
    foreground = None
    if size:
        # Reuse Color's HSV Value when available. Size-only never converts to HSV.
        foreground = (
            cv2.threshold(cv2.extractChannel(hsv, 2), 20, 255, cv2.THRESH_BINARY)[1]
            if hsv is not None
            else cv2.bitwise_not(cv2.inRange(crop, (0, 0, 0), (20, 20, 20)))
        ).astype(np.uint8, copy=False)
    usable = (
        cv2.inRange(hsv, (0, 40, 40), (179, 255, 255)).astype(np.uint8, copy=False)
        if hsv is not None
        else None
    )
    middle = roi.height // 2
    geometry = (
        _geometry(foreground, roi)
        if foreground is not None
        else ((None, None), (None, None))
    )

    def view(name: str, start: int, end: int) -> ViewMeasurement:
        body, reason = geometry[int(start != 0)]
        evidence = (
            _color(hsv[start:end], usable[start:end])
            if hsv is not None and usable is not None
            else None
        )
        return ViewMeasurement(name, body, reason, evidence)

    return CapsuleMeasurement(
        (view("upper", 0, middle), view("lower", middle, roi.height)), size, color
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
    if (settings.size.enabled and not measured.size_measured) or (
        settings.color.enabled and not measured.color_measured
    ):
        raise ValueError("Enabled inspection has no measured evidence")
    reasons: list[Reason] = []
    values: list[Measurement] = []
    for view in measured.views:
        body, size, color = view.body, settings.size, settings.color
        if size.enabled:
            if body is None:
                if view.invalid_reason is None:
                    raise ValueError("Size evidence missing")
                reasons.append(
                    Reason(source, f"capsule.{view.name}.{view.invalid_reason}")
                )
            else:
                rect = body.bounding_rect
                values.append(
                    Measurement(f"capsule.{view.name}.length", body.length, "px")
                )
                if abs(body.length - size.target_length) > size.length_tolerance:
                    reasons.append(
                        Reason(
                            source,
                            f"capsule.size.{view.name}.length",
                            bounding_rect=rect,
                        )
                    )
        if color.enabled:
            evidence = view.color
            if evidence is None:
                raise ValueError("Color evidence missing")
            values.append(
                Measurement(
                    f"capsule.{view.name}.usable_pixels", evidence.usable_pixels, "px"
                )
            )
            if evidence.hue is None:
                reasons.append(
                    Reason(
                        source,
                        f"capsule.color.{view.name}.{evidence.invalid_reason or 'unusable'}",
                    )
                )
            else:
                values.append(
                    Measurement(f"capsule.{view.name}.hue", evidence.hue, "opencv_hue")
                )
                if hue_distance(evidence.hue, color.target_hue) > color.hue_tolerance:
                    reasons.append(
                        Reason(source, f"capsule.color.{view.name}.tolerance")
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
