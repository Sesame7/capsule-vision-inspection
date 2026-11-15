"""Pure presentation mappings; judgments/counters remain domain-owned."""

import math
from dataclasses import dataclass
from enum import Enum, auto

from capsule_vision.application import (
    ApplicationState,
    ProductionApplicationSnapshot,
    StartBlocker,
)
from capsule_vision.application.source_control import SourceControlState
from capsule_vision.inference import BoundingRect
from capsule_vision.inspection.production import (
    Assessment,
    ChannelBusinessStatus,
    ChannelInspectionResult,
    ProductJudgment,
    TechnicalStatus,
)

_START_HINTS = {
    StartBlocker.CLOSING: "state_closing",
    StartBlocker.PERMISSION: "permission_operator",
    StartBlocker.RECIPE_PREPARING: "preparing",
    StartBlocker.RECIPE_REQUIRED: "recipe_reprepare_hint",
    StartBlocker.RECIPE_STALE: "recipe_reprepare_hint",
    StartBlocker.BATCH_PENDING: "batch_pending",
    StartBlocker.PROCESS_APPLYING: "process_applying",
    StartBlocker.PROCESS_UNVERIFIED: "process_apply_required",
    StartBlocker.FILE_MANAGEMENT: "identity_working",
    StartBlocker.MODEL_PREPARATION: "preparing",
    StartBlocker.SOURCE_CONNECTING: "source_connecting",
    StartBlocker.SOURCE_DISCONNECTING: "source_disconnecting",
    StartBlocker.SOURCE_RECOVERY: "source_connection_error",
    StartBlocker.REPORTING_SOURCE: "plc_group_hint",
    StartBlocker.REPORTING_CONFIGURATION: "startup_communication",
}


class StartInteractionBlocker(Enum):
    """Qt-local guards on the Start interaction."""

    CLOSING = auto()
    VISION_DRAFT = auto()
    EDITING = auto()


_INTERACTION_HINTS = {
    StartInteractionBlocker.CLOSING: "state_closing",
    StartInteractionBlocker.VISION_DRAFT: "start_vision_dirty",
    StartInteractionBlocker.EDITING: "identity_working",
}


def start_hint(
    snapshot: ProductionApplicationSnapshot,
    setup_guidance_key: str,
    interaction: StartInteractionBlocker | None = None,
) -> str | None:
    if interaction is StartInteractionBlocker.CLOSING:
        return _INTERACTION_HINTS[interaction]
    blocker = snapshot.start_blocker
    if blocker is StartBlocker.SETUP:
        return setup_guidance_key
    if blocker is StartBlocker.LIFECYCLE:
        return {
            ApplicationState.STARTING: "state_STARTING",
            ApplicationState.STOPPING: "state_STOPPING",
            ApplicationState.FAULT: "fault_note",
            ApplicationState.CLOSED: "CLOSED",
        }.get(snapshot.state)
    if blocker is not None:
        return _START_HINTS[blocker]
    if interaction is not None and snapshot.state is ApplicationState.READY:
        return _INTERACTION_HINTS[interaction]
    return None


@dataclass(frozen=True, slots=True)
class Controls:
    start: bool
    stop: bool
    reset: bool
    batch: bool


def controls(snapshot: ProductionApplicationSnapshot) -> Controls:
    state = snapshot.state
    return Controls(
        snapshot.start_blocker is None,
        state in (ApplicationState.STARTING, ApplicationState.RUNNING),
        state is ApplicationState.FAULT and not snapshot.runtime_owned,
        state is ApplicationState.READY,
    )


def production_actions(
    snapshot: ProductionApplicationSnapshot, replay: SourceControlState | None = None
) -> dict[str, str | None]:
    """Fixed player positions; None replay means pause/step are unsupported."""
    available = controls(snapshot)
    state = snapshot.state
    running = state is ApplicationState.RUNNING
    return {
        "operation": "start"
        if state in (ApplicationState.UNCONFIGURED, ApplicationState.READY)
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
