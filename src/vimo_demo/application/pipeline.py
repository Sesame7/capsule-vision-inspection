"""Configured serial execution; reuse Phase 2A assignment and aggregation."""

from collections.abc import Callable
from dataclasses import replace
from time import monotonic

import numpy as np
from numpy.typing import NDArray

from vimo_demo.configuration import (
    ResolvedChannelConfiguration,
    ResolvedInspectionConfiguration,
)
from vimo_demo.inference import ImageResult
from vimo_demo.inspection import (
    CollectedCycle,
    CollectionStatus,
    CycleStatus,
    Frame,
    FrameInspectionResult,
    InspectionCycleResult,
    InspectionFailure,
)
from vimo_demo.inspection.production import (
    Assessment,
    CameraInspectionResult,
    ChannelInspectionResult,
    CheckPolicy,
    CheckResult,
    FindingSource,
    ProductionInspectionResult,
    ProductionPipeline,
    ProductJudgment,
    Reason,
    TechnicalStatus,
)
from vimo_demo.inspection.production.capsule import judge_capsule, measure_capsule


def _unavailable(
    source: FindingSource,
    policy: CheckPolicy,
    code: str = "result_unavailable",
    error: Exception | None = None,
) -> CheckResult:
    return CheckResult(
        source,
        Assessment(
            TechnicalStatus.ERROR if policy.enabled else TechnicalStatus.DISABLED,
            ProductJudgment.UNKNOWN,
        ),
        (Reason(source, code, exception_type=type(error).__name__ if error else None),)
        if policy.enabled
        else (),
        required=policy.required,
    )


class ConfiguredProductionPipeline:
    def __init__(
        self,
        configuration: ResolvedInspectionConfiguration,
    ) -> None:
        self.configuration = configuration
        self._base = ProductionPipeline(configuration.layout)

    def _traditional_check(
        self, frame: Frame | None, channel: ResolvedChannelConfiguration
    ) -> CheckResult:
        capsule = self.configuration.capsule
        policy = CheckPolicy(capsule.enabled, capsule.enabled)
        if not capsule.enabled or frame is None:
            return _unavailable(FindingSource.TRADITIONAL, policy)
        try:
            return judge_capsule(measure_capsule(frame.image, channel.roi), capsule)
        except Exception as exc:
            return _unavailable(
                FindingSource.TRADITIONAL, policy, "capsule.execution_error", exc
            )

    def inspect(
        self, cycle: CollectedCycle, infer: Callable[[NDArray[np.uint8]], ImageResult]
    ) -> tuple[InspectionCycleResult, ProductionInspectionResult]:
        started = monotonic()
        ready = cycle.status is CollectionStatus.READY
        status = {
            CollectionStatus.READY: CycleStatus.COMPLETE,
            CollectionStatus.TIMED_OUT: CycleStatus.TIMED_OUT,
            CollectionStatus.SHUTDOWN: CycleStatus.SHUTDOWN,
        }[cycle.status]
        by_camera = {entry.frame.id.camera_id: entry for entry in cycle.frames}
        outcomes: list[FrameInspectionResult] = []
        cameras: list[CameraInspectionResult] = []
        aggregation_ms = 0.0
        for layout in self.configuration.layout.cameras:
            received = by_camera.get(layout.camera_id)
            frame = received.frame if received else None
            channels = tuple(
                self.configuration.channels[slot.channel_id - 1]
                for slot in layout.slots
            )
            traditional_start = monotonic()
            traditional = tuple(
                self._traditional_check(frame if ready else None, channel)
                for channel in channels
            )
            traditional_ms = (monotonic() - traditional_start) * 1000
            outcome = None
            if frame is not None and received is not None:
                shape = (
                    frame.image.shape[0],
                    frame.image.shape[1],
                    frame.image.shape[2],
                )
                result = None
                failure = None
                begin = end = None
                if ready and any(channel.checks.vimo.enabled for channel in channels):
                    begin = monotonic()
                    try:
                        result = infer(frame.image)
                        if result.image_shape != shape:
                            raise ValueError("Inference shape differs from frame")
                    except Exception as exc:
                        result = None
                        failure = InspectionFailure(type(exc).__name__, str(exc), "")
                        status = CycleStatus.FAILED
                    end = monotonic()
                outcome = FrameInspectionResult(
                    frame.id,
                    frame.captured_at,
                    received.arrived_at,
                    shape,
                    begin,
                    end,
                    result,
                    failure,
                )
                outcomes.append(outcome)
            issue = (
                None
                if ready
                else Reason(FindingSource.COLLECTION, f"cycle_{status.name.lower()}")
            )
            mapping_started = monotonic()
            camera = self._base.camera_result(layout, outcome, collection_issue=issue)
            rows: list[ChannelInspectionResult] = []
            for row, channel, traditional_check in zip(
                camera.channels, channels, traditional, strict=True
            ):
                policy = channel.checks.vimo
                evidence = tuple(
                    item
                    for item in row.vimo_findings
                    if policy.label_enabled(item.finding.label)
                )
                base_check = next(
                    check for check in row.checks if check.source is FindingSource.VIMO
                )
                if not policy.enabled:
                    vimo = _unavailable(FindingSource.VIMO, policy.policy)
                elif base_check.assessment.technical_status is TechnicalStatus.ERROR:
                    vimo = replace(base_check, required=policy.required)
                else:
                    vimo = CheckResult(
                        FindingSource.VIMO,
                        Assessment(
                            TechnicalStatus.SUCCESS,
                            ProductJudgment.NG if evidence else ProductJudgment.OK,
                        ),
                        tuple(
                            Reason(
                                FindingSource.VIMO,
                                "defect_region",
                                item.finding.label,
                                item.finding.bounding_rect,
                            )
                            for item in evidence
                        ),
                        required=policy.required,
                    )
                rows.append(
                    replace(
                        row, checks=(traditional_check, vimo), vimo_findings=evidence
                    )
                )
            unassigned = tuple(
                f
                for f in camera.unassigned_findings
                if any(
                    channel.checks.vimo.label_enabled(f.label) for channel in channels
                )
            )
            cameras.append(
                replace(
                    camera,
                    channels=tuple(rows),
                    unassigned_findings=unassigned,
                    timing=replace(
                        camera.timing,
                        traditional_ms=traditional_ms if ready else None,
                        mapping_ms=(monotonic() - mapping_started) * 1000,
                    ),
                )
            )
            aggregation_ms += (monotonic() - mapping_started) * 1000
        finished = monotonic()
        raw = InspectionCycleResult(
            cycle.cycle_id,
            cycle.expected_cameras,
            cycle.started_at,
            cycle.deadline,
            cycle.finalized_at,
            finished,
            status,
            tuple(outcomes),
            cycle.missing_cameras,
        )
        production = ProductionInspectionResult(
            cycle.cycle_id,
            status,
            self.configuration.layout,
            tuple(cameras),
            started if ready else None,
            finished,
            aggregation_ms,
            frame_failures_in_checks=True,
        )
        return raw, production
