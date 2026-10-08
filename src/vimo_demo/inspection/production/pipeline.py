"""Lightweight ViMo evidence assignment and consumption of cycle outcomes.

No engine, worker, collection or scheduling ownership lives here. Consumers must
release their original cycle Future/result separately to release its masks.
"""

from collections.abc import Callable, Sequence
from time import monotonic

from vimo_demo.inference import ImageResult

from ..models import (
    CycleStatus,
    FrameId,
    FrameInspectionResult,
    InspectionCycleResult,
    timestamp,
)
from .layout import CameraLayout, InspectionLayout, OverlapPolicy
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

    Configured capsule execution adds its CheckResult to these mapped results.
    This layer owns neither algorithm dispatch nor runtime scheduling.
    """

    def __init__(
        self,
        layout: InspectionLayout,
        *,
        vimo: CheckPolicy | None = None,
        overlap: OverlapPolicy | None = None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.layout = layout
        self.vimo = vimo or CheckPolicy()
        self.overlap = overlap or OverlapPolicy()
        self.clock = clock

    def camera_result(
        self,
        layout: CameraLayout,
        outcome: FrameInspectionResult | None,
        *,
        collection_issue: Reason | None = None,
    ) -> CameraInspectionResult:
        """Consume one existing full-camera ViMo outcome."""
        start = timestamp(self.clock())
        if layout not in self.layout.cameras:
            raise ValueError("Camera layout is not configured in this pipeline")
        if outcome is not None:
            if outcome.frame_id.camera_id != layout.camera_id:
                raise ValueError("Frame result camera mismatch")
            layout.validate_shape(outcome.image_shape)
            if outcome.result is not None and outcome.failure is not None:
                raise ValueError("Frame cannot contain both result and failure")
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
            channels.append(
                ChannelInspectionResult(
                    layout.camera_id,
                    slot.channel_id,
                    slot.slot,
                    (
                        _check(FindingSource.TRADITIONAL, CheckPolicy(False)),
                        check,
                    ),
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
                None,
                vimo_ms,
                elapsed,
            ),
            collection_issue,
        )

    def from_cycle(
        self,
        cycle: InspectionCycleResult,
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
        issue = None
        if cycle.status in (CycleStatus.TIMED_OUT, CycleStatus.SHUTDOWN):
            issue = Reason(
                FindingSource.COLLECTION, f"cycle_{cycle.status.name.lower()}"
            )
        cameras = tuple(
            self.camera_result(
                layout,
                by_camera.get(layout.camera_id),
                collection_issue=issue,
            )
            for layout in self.layout.cameras
        )
        ready = timestamp(self.clock())
        if ready < max(cycle.finished_at, start):
            raise ValueError("Aggregation clock predates inspection outcome")
        starts = [row.started_at for row in cycle.frames if row.started_at is not None]
        return ProductionInspectionResult(
            cycle.cycle_id,
            cycle.status,
            self.layout,
            cameras,
            min(starts) if starts else None,
            ready,
            duration_ms(start, ready),
        )
