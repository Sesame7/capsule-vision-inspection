"""Pure presentation mappings; judgments/counters remain domain-owned."""

import math
from dataclasses import dataclass
from enum import Enum

from vimo_demo.application import ApplicationState, ProductionApplicationSnapshot
from vimo_demo.inference import BoundingRect
from vimo_demo.inspection.production import (
    Assessment,
    CameraInspectionResult,
    ProductJudgment,
    TechnicalStatus,
)

from .display_frames import DisplayFrame


@dataclass(frozen=True, slots=True)
class Controls:
    start: bool
    stop: bool
    reset: bool
    recipe: bool
    batch: bool


def controls(snapshot: ProductionApplicationSnapshot) -> Controls:
    state = snapshot.state
    return Controls(
        state is ApplicationState.READY,
        state in (ApplicationState.STARTING, ApplicationState.RUNNING),
        state is ApplicationState.FAULT and not snapshot.runtime_owned,
        state in (ApplicationState.UNCONFIGURED, ApplicationState.READY),
        state is ApplicationState.READY,
    )


class Indicator(Enum):
    PENDING = "pending"
    OK = "ok"
    NG = "ng"
    ERROR = "error"
    ERROR_NG = "error_ng"
    UNKNOWN = "unknown"
    DISABLED = "disabled"


def indicator(assessment: Assessment | None) -> Indicator:
    if assessment is None:
        return Indicator.PENDING
    if assessment.technical_status is TechnicalStatus.DISABLED:
        return Indicator.DISABLED
    if assessment.technical_status is TechnicalStatus.ERROR:
        return (
            Indicator.ERROR_NG
            if assessment.judgment is ProductJudgment.NG
            else Indicator.ERROR
        )
    return {
        ProductJudgment.OK: Indicator.OK,
        ProductJudgment.NG: Indicator.NG,
        ProductJudgment.UNKNOWN: Indicator.UNKNOWN,
    }[assessment.judgment]


def grid_columns(width: int, height: int) -> int:
    return 3 if width >= height * 1.2 else 2


@dataclass(frozen=True, slots=True)
class ImageTransform:
    scale: float
    x: float
    y: float
    width: float
    height: float

    def source_point(self, x: float, y: float) -> tuple[float, float]:
        """Inverse in logical widget pixels (Qt already accounts for device DPI)."""
        return (x - self.x) / self.scale, (y - self.y) / self.scale

    def rectangle(self, rect: BoundingRect) -> tuple[float, float, float, float]:
        return (
            self.x + rect.x * self.scale,
            self.y + rect.y * self.scale,
            rect.width * self.scale,
            rect.height * self.scale,
        )


def image_transform(
    source_width: int,
    source_height: int,
    width: float,
    height: float,
    *,
    scale: float | None = None,
    pan: tuple[float, float] = (0, 0),
) -> ImageTransform:
    if any(
        not math.isfinite(v) or v <= 0
        for v in (source_width, source_height, width, height)
    ):
        raise ValueError("Image and viewport dimensions must be positive and finite")
    if scale is not None and (not math.isfinite(scale) or scale <= 0):
        raise ValueError("Scale must be positive and finite")
    if not all(math.isfinite(v) for v in pan):
        raise ValueError("Pan must be finite")
    scale = (
        min(width / source_width, height / source_height) if scale is None else scale
    )
    w, h = source_width * scale, source_height * scale
    return ImageTransform(
        scale, (width - w) / 2 + pan[0], (height - h) / 2 + pan[1], w, h
    )


def matching_camera(
    frame: DisplayFrame | None, snapshot: ProductionApplicationSnapshot
) -> CameraInspectionResult | None:
    result = snapshot.latest_production_result
    if (
        frame is None
        or result is None
        or frame.generation != snapshot.generation
        or frame.frame_id.cycle_id != result.cycle_id
    ):
        return None
    return next(
        (
            camera
            for camera in result.cameras
            if camera.frame_id == frame.frame_id
            and camera.image_shape == frame.image.shape
        ),
        None,
    )
