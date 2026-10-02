"""Serial traditional boundary and lightweight consumption of Phase 1 outcomes.

No engine, worker, collection or scheduling ownership lives here. Consumers must
release their original cycle Future/result separately to release its masks.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from time import monotonic
from typing import Protocol

from vimo_demo.inference import ImageResult

from ..models import (
    CycleStatus,
    Frame,
    FrameId,
    FrameInspectionResult,
    InspectionCycleResult,
    timestamp,
)
from .layout import CameraLayout, ChannelId, InspectionLayout, OverlapPolicy
from .models import (
    Assessment,
    CameraInspectionResult,
    CameraTiming,
    ChannelInspectionResult,
    CheckPolicy,
    CheckResult,
    FindingAssignment,
    FindingSource,
    ProductionInspectionResult,
    ProductJudgment,
    Reason,
    TechnicalStatus,
    ViMoFinding,
)


@dataclass(frozen=True, slots=True)
class TraditionalChannelCheck:
    channel_id: ChannelId
    result: CheckResult

    def __post_init__(self) -> None:
        if self.result.source is not FindingSource.TRADITIONAL:
            raise ValueError("Traditional inspector must return traditional checks")


class TraditionalInspector(Protocol):
    def inspect(
        self, frame: Frame, layout: CameraLayout
    ) -> tuple[TraditionalChannelCheck, ...]:
        """Read immutable full-image pixels; return each configured channel once."""
        ...


@dataclass(frozen=True, slots=True)
class TraditionalCameraResult:
    frame_id: FrameId
    channels: tuple[TraditionalChannelCheck, ...]
    started_at: float
    finished_at: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "channels", tuple(self.channels))
        ids = tuple(item.channel_id for item in self.channels)
        if len(set(ids)) != len(ids):
            raise ValueError("Duplicate traditional channel result")
        duration_ms(self.started_at, self.finished_at)


def duration_ms(start: float, end: float) -> float:
    if timestamp(end) < timestamp(start):
        raise ValueError("Clock moved backwards")
    return (end - start) * 1000


def _check(
    source: FindingSource, policy: CheckPolicy, reason: Reason | None = None
) -> CheckResult:
    if not policy.enabled:
        return CheckResult(
            source,
            Assessment(TechnicalStatus.DISABLED, ProductJudgment.UNKNOWN),
            required=policy.required,
        )
    if reason is not None:
        return CheckResult(
            source,
            Assessment(TechnicalStatus.ERROR, ProductJudgment.UNKNOWN),
            (reason,),
            required=policy.required,
        )
    return CheckResult(
        source,
        Assessment(TechnicalStatus.SUCCESS, ProductJudgment.OK),
        required=policy.required,
    )


def extract_vimo_findings(
    frame_id: FrameId, result: ImageResult
) -> tuple[ViMoFinding, ...]:
    """Preserve original-coordinate bbox evidence; do not retain mask or contours."""
    height, width, _ = result.image_shape
    findings = tuple(
        ViMoFinding(
            frame_id,
            index,
            region.label,
            region.area,
            region.bounding_rect,
            result.module_id,
        )
        for index, region in enumerate(result.regions)
    )
    if any(
        f.bounding_rect.x + f.bounding_rect.width > width
        or f.bounding_rect.y + f.bounding_rect.height > height
        for f in findings
    ):
        raise ValueError("Finding bounding rectangle is outside the image")
    return findings


def assign_vimo_findings(
    layout: CameraLayout,
    findings: Sequence[ViMoFinding],
    policy: OverlapPolicy | None = None,
) -> tuple[tuple[FindingAssignment, ...], tuple[ViMoFinding, ...]]:
    policy = policy or OverlapPolicy()
    assigned: list[FindingAssignment] = []
    unassigned: list[ViMoFinding] = []
    for finding in findings:
        if finding.frame_id.camera_id != layout.camera_id:
            raise ValueError("Finding belongs to another camera")
        matched = False
        for slot in layout.slots:
            area = policy.intersection(finding.bounding_rect, slot.rect)
            if area >= policy.minimum_intersection_area:
                assigned.append(
                    FindingAssignment(
                        finding,
                        slot.channel_id,
                        area,
                        area
                        / (finding.bounding_rect.width * finding.bounding_rect.height),
                    )
                )
                matched = True
        if not matched:
            unassigned.append(finding)
    return tuple(assigned), tuple(unassigned)


class ProductionPipeline:
    """Stateless per-cycle aggregation; no source images or results are cached.

    Both sources are required/enabled by default. Without a calibrated traditional
    inspector, supply explicit disabled policy or receive ERROR/UNKNOWN evidence.
    Policies govern consumption, not the unchanged coordinator's inference calls.
    """

    def __init__(
        self,
        layout: InspectionLayout,
        *,
        traditional: CheckPolicy | None = None,
        vimo: CheckPolicy | None = None,
        overlap: OverlapPolicy | None = None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.layout = layout
        self.traditional = traditional or CheckPolicy()
        self.vimo = vimo or CheckPolicy()
        self.overlap = overlap or OverlapPolicy()
        self.clock = clock

    def inspect_traditional(
        self, frame: Frame, inspector: TraditionalInspector | None
    ) -> TraditionalCameraResult:
        layout = next(
            (
                item
                for item in self.layout.cameras
                if item.camera_id == frame.id.camera_id
            ),
            None,
        )
        if layout is None:
            raise ValueError("Frame camera is not configured")
        height, width, channels = frame.image.shape
        layout.validate_shape((height, width, channels))
        started = timestamp(self.clock())
        reason: Reason | None = None
        results: tuple[TraditionalChannelCheck, ...] | None = None
        if self.traditional.enabled:
            if inspector is None:
                reason = Reason(FindingSource.TRADITIONAL, "check_unavailable")
            else:
                try:
                    results = inspector.inspect(frame, layout)
                except Exception as exc:
                    reason = Reason(
                        FindingSource.TRADITIONAL,
                        "execution_error",
                        exception_type=f"{type(exc).__module__}.{type(exc).__qualname__}",
                    )
        if results is None:
            results = tuple(
                TraditionalChannelCheck(
                    item.channel_id,
                    _check(FindingSource.TRADITIONAL, self.traditional, reason),
                )
                for item in layout.slots
            )
        output = TraditionalCameraResult(
            frame.id, results, started, timestamp(self.clock())
        )
        self._validate_traditional(layout, output)
        return output

    @staticmethod
    def _validate_traditional(
        layout: CameraLayout, result: TraditionalCameraResult
    ) -> None:
        if result.frame_id.camera_id != layout.camera_id or {
            row.channel_id for row in result.channels
        } != {slot.channel_id for slot in layout.slots}:
            raise ValueError(
                "Traditional result differs from configured camera/channels"
            )

    def camera_result(
        self,
        layout: CameraLayout,
        outcome: FrameInspectionResult | None,
        traditional: TraditionalCameraResult | None = None,
        *,
        collection_issue: Reason | None = None,
    ) -> CameraInspectionResult:
        """Consume one existing frame outcome, with optional independent evidence."""
        start = timestamp(self.clock())
        if layout not in self.layout.cameras:
            raise ValueError("Camera layout is not configured in this pipeline")
        if outcome is not None:
            if outcome.frame_id.camera_id != layout.camera_id:
                raise ValueError("Frame result camera mismatch")
            layout.validate_shape(outcome.image_shape)
            if outcome.result is not None and outcome.failure is not None:
                raise ValueError("Frame cannot contain both result and failure")
        if traditional is not None:
            self._validate_traditional(layout, traditional)
            if outcome is None or traditional.frame_id != outcome.frame_id:
                raise ValueError("Traditional and ViMo frame identity mismatch")
        findings: tuple[ViMoFinding, ...] = ()
        reason = Reason(FindingSource.VIMO, "result_unavailable")
        vimo_check = _check(FindingSource.VIMO, self.vimo, reason)
        if self.vimo.enabled and outcome is not None:
            if outcome.failure is not None:
                vimo_check = _check(
                    FindingSource.VIMO,
                    self.vimo,
                    Reason(
                        FindingSource.VIMO,
                        "execution_error",
                        exception_type=outcome.failure.exception_type,
                    ),
                )
            elif outcome.result is not None:
                try:
                    if outcome.result.image_shape != outcome.image_shape:
                        raise ValueError("Inference shape differs from frame metadata")
                    findings = extract_vimo_findings(outcome.frame_id, outcome.result)
                    vimo_check = _check(FindingSource.VIMO, self.vimo)
                except ValueError as exc:
                    vimo_check = _check(
                        FindingSource.VIMO,
                        self.vimo,
                        Reason(
                            FindingSource.VIMO,
                            "invalid_result",
                            exception_type=type(exc).__name__,
                        ),
                    )
        assignments, unassigned = assign_vimo_findings(layout, findings, self.overlap)
        traditional_by_id = (
            {row.channel_id: row.result for row in traditional.channels}
            if traditional is not None
            else {}
        )
        channels: list[ChannelInspectionResult] = []
        for slot in layout.slots:
            matched = tuple(
                item for item in assignments if item.channel_id == slot.channel_id
            )
            check = vimo_check
            if matched:
                check = CheckResult(
                    FindingSource.VIMO,
                    Assessment(TechnicalStatus.SUCCESS, ProductJudgment.NG),
                    tuple(
                        Reason(
                            FindingSource.VIMO,
                            "defect_region",
                            item.finding.label,
                            item.finding.bounding_rect,
                        )
                        for item in matched
                    ),
                    required=self.vimo.required,
                )
            traditional_check = _check(
                FindingSource.TRADITIONAL,
                self.traditional,
                Reason(FindingSource.TRADITIONAL, "result_unavailable"),
            )
            if self.traditional.enabled and slot.channel_id in traditional_by_id:
                traditional_check = replace(
                    traditional_by_id[slot.channel_id],
                    required=self.traditional.required,
                )
                if (
                    traditional_check.assessment.technical_status
                    is TechnicalStatus.DISABLED
                ):
                    # A plugin cannot silently disable a check enabled by policy.
                    traditional_check = _check(
                        FindingSource.TRADITIONAL,
                        self.traditional,
                        Reason(FindingSource.TRADITIONAL, "result_unavailable"),
                    )
            channels.append(
                ChannelInspectionResult(
                    layout.camera_id,
                    slot.channel_id,
                    slot.slot,
                    (traditional_check, check),
                    matched,
                )
            )
        elapsed = duration_ms(start, timestamp(self.clock()))
        vimo_ms = None
        if outcome is not None and self.vimo.enabled:
            if (outcome.started_at is None) != (outcome.finished_at is None):
                raise ValueError("Incomplete inference timing pair")
            if outcome.started_at is not None and outcome.finished_at is not None:
                vimo_ms = duration_ms(outcome.started_at, outcome.finished_at)
        return CameraInspectionResult(
            layout,
            outcome.frame_id if outcome else None,
            outcome.image_shape if outcome else None,
            tuple(channels),
            unassigned,
            CameraTiming(
                duration_ms(traditional.started_at, traditional.finished_at)
                if traditional is not None and self.traditional.enabled
                else None,
                vimo_ms,
                elapsed,
            ),
            collection_issue,
        )

    def from_cycle(
        self,
        cycle: InspectionCycleResult,
        traditional: Sequence[TraditionalCameraResult] = (),
    ) -> ProductionInspectionResult:
        """Consume coordinator envelope.result or inspect_cycle() output unchanged."""
        start = timestamp(self.clock())
        if set(cycle.expected_cameras) != set(self.layout.camera_ids) or len(
            cycle.expected_cameras
        ) != len(self.layout.cameras):
            raise ValueError("Cycle cameras differ from production layout")
        by_camera = {row.frame_id.camera_id: row for row in cycle.frames}
        if (
            len(by_camera) != len(cycle.frames)
            or not set(by_camera) <= set(self.layout.camera_ids)
            or any(row.frame_id.cycle_id != cycle.cycle_id for row in cycle.frames)
        ):
            raise ValueError("Duplicate, unknown or mixed-cycle frame results")
        missing = set(self.layout.camera_ids) - set(by_camera)
        if missing != set(cycle.missing_cameras) or (
            missing and cycle.status in (CycleStatus.COMPLETE, CycleStatus.FAILED)
        ):
            raise ValueError("Cycle missing-camera metadata is inconsistent")
        trad_by_camera = {row.frame_id.camera_id: row for row in traditional}
        if len(trad_by_camera) != len(traditional) or not set(trad_by_camera) <= set(
            by_camera
        ):
            raise ValueError("Duplicate or unknown traditional camera results")
        issue = None
        if cycle.status in (CycleStatus.TIMED_OUT, CycleStatus.SHUTDOWN):
            issue = Reason(
                FindingSource.COLLECTION, f"cycle_{cycle.status.name.lower()}"
            )
        cameras = tuple(
            self.camera_result(
                layout,
                by_camera.get(layout.camera_id),
                trad_by_camera.get(layout.camera_id),
                collection_issue=issue,
            )
            for layout in self.layout.cameras
        )
        ready = timestamp(self.clock())
        if ready < max(
            cycle.finished_at, *(row.finished_at for row in traditional), start
        ):
            raise ValueError("Aggregation clock predates inspection outcome")
        starts = [row.started_at for row in cycle.frames if row.started_at is not None]
        starts.extend(row.started_at for row in traditional)
        return ProductionInspectionResult(
            cycle.cycle_id,
            cycle.status,
            self.layout,
            cameras,
            min(starts) if starts else None,
            ready,
            duration_ms(start, ready),
        )
