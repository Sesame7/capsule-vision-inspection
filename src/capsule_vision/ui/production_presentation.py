"""Pure presentation mappings; judgments/counters remain domain-owned."""

import math
from dataclasses import dataclass
from enum import Enum

from capsule_vision.application import ApplicationState, ProductionApplicationSnapshot
from capsule_vision.application.source_control import SourceControlState
from capsule_vision.inference import BoundingRect
from capsule_vision.inspection.production import (
    Assessment,
    ChannelBusinessStatus,
    ChannelInspectionResult,
    ProductJudgment,
    TechnicalStatus,
)


@dataclass(frozen=True, slots=True)
class Controls:
    start: bool
    stop: bool
    reset: bool
    batch: bool


def controls(snapshot: ProductionApplicationSnapshot) -> Controls:
    state = snapshot.state
    return Controls(
        state is ApplicationState.READY,
        state in (ApplicationState.STARTING, ApplicationState.RUNNING),
        state is ApplicationState.FAULT and not snapshot.runtime_owned,
        state is ApplicationState.READY,
    )


def production_actions(
    snapshot: ProductionApplicationSnapshot, replay: SourceControlState | None = None
) -> dict[str, str | None]:
    """Fixed player positions; None replay means pause/step are unsupported."""
    available = controls(snapshot)
    running = snapshot.state is ApplicationState.RUNNING
    return {
        "operation": "start"
        if available.start
        else "replay_continue"
        if running and replay is SourceControlState.PAUSED
        else None,
        "pause": "replay_pause"
        if running and replay is SourceControlState.AUTOMATIC
        else "replay_step"
        if running and replay is SourceControlState.PAUSED
        else None,
        "stop": "stop" if available.stop else None,
        "secondary": "reset" if available.reset else None,
    }


class Indicator(Enum):
    PENDING = "pending"
    OK = "ok"
    NG = "ng"
    ERROR = "error"
    ERROR_NG = "error_ng"
    UNKNOWN = "unknown"
    DISABLED = "disabled"
    EMPTY = "empty"


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


def channel_indicator(result: ChannelInspectionResult | None) -> Indicator:
    """Present domain classification; keep independent technical error treatment."""
    if result is None:
        return Indicator.PENDING
    status = result.business_status
    error = result.assessment.technical_status is TechnicalStatus.ERROR
    if status is ChannelBusinessStatus.NG:
        return Indicator.ERROR_NG if error else Indicator.NG
    if status is ChannelBusinessStatus.EMPTY:
        return Indicator.EMPTY
    if status is ChannelBusinessStatus.OK:
        return Indicator.OK
    return Indicator.ERROR if error else Indicator.UNKNOWN


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
