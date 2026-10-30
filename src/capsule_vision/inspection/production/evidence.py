"""Shared ViMo evidence extraction, ROI assignment, checks and timing.

No engine, worker, collection or scheduling ownership lives here. Consumers must
release their original cycle Future/result separately to release its masks.
"""

from collections.abc import Sequence

from capsule_vision.inference import ImageResult

from ..models import (
    FrameId,
    FrameInspectionResult,
    timestamp,
)
from .layout import CameraLayout, OverlapPolicy
from .models import (
    Assessment,
    CheckPolicy,
    CheckResult,
    FindingAssignment,
    FindingSource,
    ProductJudgment,
    Reason,
    TechnicalStatus,
    ViMoFinding,
)


def duration_ms(start: float, end: float) -> float:
    if timestamp(end) < timestamp(start):
        raise ValueError("Clock moved backwards")
    return (end - start) * 1000


def check_result(
    source: FindingSource,
    policy: CheckPolicy,
    reason: Reason | None = None,
    *,
    matched: Sequence[FindingAssignment] = (),
) -> CheckResult:
    reasons: tuple[Reason, ...]
    if not policy.enabled:
        status = TechnicalStatus.DISABLED
        judgment = ProductJudgment.UNKNOWN
        reasons = ()
    elif reason is not None:
        status = TechnicalStatus.ERROR
        judgment = ProductJudgment.UNKNOWN
        reasons = (reason,)
    else:
        status = TechnicalStatus.SUCCESS
        judgment = ProductJudgment.NG if matched else ProductJudgment.OK
        reasons = tuple(
            Reason(
                source, "defect_region", item.finding.label, item.finding.bounding_rect
            )
            for item in matched
        )
    return CheckResult(
        source, Assessment(status, judgment), reasons, required=policy.required
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


def camera_vimo_evidence(
    layout: CameraLayout,
    outcome: FrameInspectionResult | None,
    policy: CheckPolicy,
    overlap: OverlapPolicy | None = None,
) -> tuple[tuple[FindingAssignment, ...], tuple[ViMoFinding, ...], Reason | None]:
    """Validate and assign existing SDK evidence without constructing judgments."""
    if outcome is not None:
        if outcome.frame_id.camera_id != layout.camera_id:
            raise ValueError("Frame result camera mismatch")
        layout.validate_shape(outcome.image_shape)
        if outcome.result is not None and outcome.failure is not None:
            raise ValueError("Frame cannot contain both result and failure")
    findings: tuple[ViMoFinding, ...] = ()
    reason: Reason | None = Reason(FindingSource.VIMO, "result_unavailable")
    if policy.enabled and outcome is not None:
        if outcome.failure is not None:
            reason = Reason(
                FindingSource.VIMO,
                "execution_error",
                exception_type=outcome.failure.exception_type,
            )
        elif outcome.result is not None:
            try:
                if outcome.result.image_shape != outcome.image_shape:
                    raise ValueError("Inference shape differs from frame metadata")
                findings = extract_vimo_findings(outcome.frame_id, outcome.result)
                reason = None
            except ValueError as exc:
                reason = Reason(
                    FindingSource.VIMO,
                    "invalid_result",
                    exception_type=type(exc).__name__,
                )
    assignments, unassigned = assign_vimo_findings(layout, findings, overlap)
    return assignments, unassigned, reason
