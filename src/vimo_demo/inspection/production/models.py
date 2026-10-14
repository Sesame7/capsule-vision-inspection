"""Immutable production evidence, independent technical and product judgments."""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum, auto

from vimo_demo.inference import BoundingRect

from ..models import (
    CameraId,
    CycleId,
    CycleStatus,
    FrameId,
    nonnegative_integer,
    timestamp,
)
from .layout import CameraLayout, ChannelId, InspectionLayout, valid_rect
from .occupancy import OccupancyResult, OccupancyState


class TechnicalStatus(Enum):
    SUCCESS = auto()
    ERROR = auto()
    DISABLED = auto()


class ProductJudgment(Enum):
    OK = auto()
    NG = auto()
    UNKNOWN = auto()


class ChannelBusinessStatus(Enum):
    OK = auto()
    NG = auto()
    EMPTY = auto()
    UNKNOWN = auto()


class FindingSource(Enum):
    VIMO = auto()
    TRADITIONAL = auto()
    COLLECTION = auto()


class TraditionalSkipReason(Enum):
    CONFIRMED_EMPTY = auto()


@dataclass(frozen=True, slots=True)
class Assessment:
    technical_status: TechnicalStatus
    judgment: ProductJudgment

    def __post_init__(self) -> None:
        if (
            self.technical_status is TechnicalStatus.DISABLED
            and self.judgment is not ProductJudgment.UNKNOWN
        ):
            raise ValueError("Disabled inspection has UNKNOWN judgment")
        if (
            self.technical_status is TechnicalStatus.ERROR
            and self.judgment is ProductJudgment.OK
        ):
            raise ValueError("Technical ERROR cannot assert OK")


@dataclass(frozen=True, slots=True)
class Reason:
    source: FindingSource
    code: str
    label: str | None = None
    bounding_rect: BoundingRect | None = None
    exception_type: str | None = None

    def __post_init__(self) -> None:
        if not self.code.strip():
            raise ValueError("Reason requires a stable code")
        if self.bounding_rect is not None:
            valid_rect(self.bounding_rect)


@dataclass(frozen=True, slots=True)
class Measurement:
    name: str
    value: float
    unit: str | None = None

    def __post_init__(self) -> None:
        if not self.name.strip() or not math.isfinite(self.value):
            raise ValueError("Measurement requires a name and finite value")


@dataclass(frozen=True, slots=True)
class CheckPolicy:
    enabled: bool = True
    required: bool = True


@dataclass(frozen=True, slots=True)
class CheckResult:
    source: FindingSource
    assessment: Assessment
    reasons: tuple[Reason, ...] = ()
    measurements: tuple[Measurement, ...] = ()
    required: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "reasons", tuple(self.reasons))
        object.__setattr__(self, "measurements", tuple(self.measurements))
        if self.assessment == Assessment(
            TechnicalStatus.SUCCESS, ProductJudgment.UNKNOWN
        ):
            raise ValueError("A successful individual check needs a definite judgment")
        if any(reason.source is not self.source for reason in self.reasons):
            raise ValueError("Check reasons must belong to its source")
        if (
            self.assessment.judgment is ProductJudgment.NG
            or self.assessment.technical_status is TechnicalStatus.ERROR
        ) and not self.reasons:
            raise ValueError("NG/error requires an explicit reason")
        if self.assessment.technical_status is TechnicalStatus.DISABLED and (
            self.reasons or self.measurements
        ):
            raise ValueError("Disabled check cannot carry executed evidence")


def aggregate(checks: Sequence[CheckResult]) -> Assessment:
    active = [
        check
        for check in checks
        if check.assessment.technical_status is not TechnicalStatus.DISABLED
    ]
    if not active:
        return Assessment(TechnicalStatus.DISABLED, ProductJudgment.UNKNOWN)
    successful = any(
        check.assessment.technical_status is TechnicalStatus.SUCCESS for check in active
    )
    error = (
        any(
            check.required
            and check.assessment.technical_status is TechnicalStatus.ERROR
            for check in active
        )
        or not successful
    )
    ng = any(check.assessment.judgment is ProductJudgment.NG for check in active)
    return Assessment(
        TechnicalStatus.ERROR if error else TechnicalStatus.SUCCESS,
        ProductJudgment.NG
        if ng
        else ProductJudgment.UNKNOWN
        if error
        else ProductJudgment.OK,
    )


def combine(
    assessments: Sequence[Assessment], *, error: bool = False, ng: bool = False
) -> Assessment:
    """All child channels/cameras are required; disabled children are neutral."""
    active = [
        item
        for item in assessments
        if item.technical_status is not TechnicalStatus.DISABLED
    ]
    error = error or any(
        item.technical_status is TechnicalStatus.ERROR for item in active
    )
    ng = ng or any(item.judgment is ProductJudgment.NG for item in active)
    status = (
        TechnicalStatus.ERROR
        if error
        else TechnicalStatus.SUCCESS
        if active
        else TechnicalStatus.DISABLED
    )
    return Assessment(
        status,
        ProductJudgment.NG
        if ng
        else ProductJudgment.UNKNOWN
        if error
        or not active
        or any(item.judgment is ProductJudgment.UNKNOWN for item in assessments)
        else ProductJudgment.OK,
    )


@dataclass(frozen=True, slots=True)
class ViMoFinding:
    frame_id: FrameId
    region_index: int  # Source-local correlation, never a channel assignment.
    label: str
    area: int
    bounding_rect: BoundingRect
    module_id: str

    def __post_init__(self) -> None:
        nonnegative_integer(self.region_index, "region_index")
        nonnegative_integer(self.area, "area")
        if not self.label.strip():
            raise ValueError("Defect label must be nonempty")
        valid_rect(self.bounding_rect)


@dataclass(frozen=True, slots=True)
class FindingAssignment:
    finding: ViMoFinding
    channel_id: ChannelId
    intersection_area: int
    finding_bbox_fraction: float

    def __post_init__(self) -> None:
        rect = self.finding.bounding_rect
        if type(self.channel_id) is not int or self.channel_id < 1:
            raise ValueError("Assignment requires a positive channel ID")
        if (
            type(self.intersection_area) is not int
            or not 0 < self.intersection_area <= rect.width * rect.height
        ):
            raise ValueError("Invalid bounding-box intersection area")
        if self.finding_bbox_fraction != self.intersection_area / (
            rect.width * rect.height
        ):
            raise ValueError("Intersection area and bbox fraction disagree")


@dataclass(frozen=True, slots=True)
class ChannelInspectionResult:
    camera_id: CameraId
    channel_id: ChannelId
    slot: int
    checks: tuple[CheckResult, ...]
    vimo_findings: tuple[FindingAssignment, ...] = ()
    occupancy: OccupancyResult | None = None
    traditional_skip: TraditionalSkipReason | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "checks", tuple(self.checks))
        object.__setattr__(self, "vimo_findings", tuple(self.vimo_findings))
        if {check.source for check in self.checks} != {
            FindingSource.VIMO,
            FindingSource.TRADITIONAL,
        } or len(self.checks) != 2:
            raise ValueError("Channel requires one explicit result per check source")
        if any(
            item.channel_id != self.channel_id
            or item.finding.frame_id.camera_id != self.camera_id
            for item in self.vimo_findings
        ):
            raise ValueError("Finding assigned to wrong channel/camera")
        vimo = next(
            check for check in self.checks if check.source is FindingSource.VIMO
        )
        if self.vimo_findings and vimo.assessment.judgment is not ProductJudgment.NG:
            raise ValueError("Assigned ViMo defects require NG evidence")
        if self.traditional_skip is not None:
            traditional = next(
                check
                for check in self.checks
                if check.source is FindingSource.TRADITIONAL
            )
            if (
                self.traditional_skip is not TraditionalSkipReason.CONFIRMED_EMPTY
                or self.occupancy is None
                or self.occupancy.state is not OccupancyState.EMPTY
                or traditional.assessment.technical_status
                is not TechnicalStatus.DISABLED
                or not traditional.required
            ):
                raise ValueError(
                    "EMPTY skip needs confirmed evidence and a neutral check"
                )

    @property
    def assessment(self) -> Assessment:
        assessment = aggregate(self.checks)
        if self.occupancy is not None and self.occupancy.error_code is not None:
            return Assessment(
                TechnicalStatus.ERROR,
                ProductJudgment.NG
                if assessment.judgment is ProductJudgment.NG
                else ProductJudgment.UNKNOWN,
            )
        return assessment

    @property
    def business_status(self) -> ChannelBusinessStatus:
        """Additional interpretation; never replaces technical/product evidence."""
        assessment = self.assessment
        if assessment.judgment is ProductJudgment.NG:
            return ChannelBusinessStatus.NG
        if self.occupancy is not None and self.occupancy.evidence is not None:
            if self.occupancy.state is OccupancyState.EMPTY:
                return ChannelBusinessStatus.EMPTY
            if (
                self.occupancy.state is OccupancyState.PRESENT
                and assessment
                == Assessment(TechnicalStatus.SUCCESS, ProductJudgment.OK)
            ):
                return ChannelBusinessStatus.OK
        return ChannelBusinessStatus.UNKNOWN

    @property
    def reasons(self) -> tuple[Reason, ...]:
        return tuple(reason for check in self.checks for reason in check.reasons)


@dataclass(frozen=True, slots=True)
class CameraTiming:
    traditional_ms: float | None
    vimo_ms: float | None
    mapping_ms: float
    occupancy_ms: float | None = None

    def __post_init__(self) -> None:
        for value in (
            self.traditional_ms,
            self.vimo_ms,
            self.mapping_ms,
            self.occupancy_ms,
        ):
            if value is not None:
                timestamp(value)

    @property
    def total_ms(self) -> float:
        """Sum of measured stages, not collection/queue latency."""
        return (
            (self.occupancy_ms or 0)
            + (self.traditional_ms or 0)
            + (self.vimo_ms or 0)
            + self.mapping_ms
        )


@dataclass(frozen=True, slots=True)
class CameraInspectionResult:
    layout: CameraLayout
    frame_id: FrameId | None
    image_shape: tuple[int, int, int] | None
    channels: tuple[ChannelInspectionResult, ...]
    unassigned_findings: tuple[ViMoFinding, ...]
    timing: CameraTiming
    collection_issue: Reason | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "channels", tuple(self.channels))
        object.__setattr__(self, "unassigned_findings", tuple(self.unassigned_findings))
        if self.frame_id is not None and self.frame_id.camera_id != self.camera_id:
            raise ValueError("Frame belongs to another camera")
        if tuple(
            (row.camera_id, row.slot, row.channel_id) for row in self.channels
        ) != tuple(
            (self.camera_id, item.slot, item.channel_id) for item in self.layout.slots
        ):
            raise ValueError("Channel results differ from configured camera layout")
        findings = (
            *self.unassigned_findings,
            *(item.finding for item in self.assigned_findings),
        )
        if any(finding.frame_id != self.frame_id for finding in findings):
            raise ValueError("Finding belongs to another frame")
        if self.image_shape is not None:
            object.__setattr__(self, "image_shape", tuple(self.image_shape))
            self.layout.validate_shape(self.image_shape)
        if (
            self.collection_issue is not None
            and self.collection_issue.source is not FindingSource.COLLECTION
        ):
            raise ValueError("Collection issue requires COLLECTION source")

    @property
    def camera_id(self) -> CameraId:
        return self.layout.camera_id

    @property
    def assigned_findings(self) -> tuple[FindingAssignment, ...]:
        return tuple(
            item for channel in self.channels for item in channel.vimo_findings
        )

    @property
    def assessment(self) -> Assessment:
        return combine(
            tuple(row.assessment for row in self.channels),
            error=self.collection_issue is not None,
            ng=bool(self.unassigned_findings),
        )


@dataclass(frozen=True, slots=True)
class ProductionInspectionResult:
    cycle_id: CycleId
    cycle_status: CycleStatus
    layout: InspectionLayout
    cameras: tuple[CameraInspectionResult, ...]
    inspection_started_at: float | None
    result_ready_at: float
    aggregation_ms: float
    frame_failures_in_checks: bool = False

    def __post_init__(self) -> None:
        nonnegative_integer(self.cycle_id, "cycle_id")
        object.__setattr__(self, "cameras", tuple(self.cameras))
        if tuple(camera.layout for camera in self.cameras) != self.layout.cameras:
            raise ValueError("Camera results differ from configured layout/order")
        if any(
            camera.frame_id is not None and camera.frame_id.cycle_id != self.cycle_id
            for camera in self.cameras
        ):
            raise ValueError("Camera result belongs to another cycle")
        timestamp(self.result_ready_at)
        timestamp(self.aggregation_ms)
        if (
            self.inspection_started_at is not None
            and timestamp(self.inspection_started_at) > self.result_ready_at
        ):
            raise ValueError("Inspection starts after result readiness")

    @property
    def channels(self) -> tuple[ChannelInspectionResult, ...]:
        return tuple(
            sorted(
                (channel for camera in self.cameras for channel in camera.channels),
                key=lambda channel: channel.channel_id,
            )
        )

    @property
    def unassigned_findings(self) -> tuple[ViMoFinding, ...]:
        return tuple(
            finding for camera in self.cameras for finding in camera.unassigned_findings
        )

    @property
    def assessment(self) -> Assessment:
        return combine(
            tuple(camera.assessment for camera in self.cameras),
            # Configured composition can account for every frame failure using
            # enabled/required check policies. Legacy cycle consumers retain the
            # original unconditional FAILED-cycle error by default.
            error=self.cycle_status is not CycleStatus.COMPLETE
            and not (
                self.cycle_status is CycleStatus.FAILED
                and self.frame_failures_in_checks
            ),
        )
