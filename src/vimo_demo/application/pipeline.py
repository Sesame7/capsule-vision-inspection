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
    FrameId,
    FrameInspectionResult,
    InspectionCycleResult,
    InspectionFailure,
    ReceivedFrame,
)
from vimo_demo.inspection.production import (
    Assessment,
    CameraInspectionResult,
    CameraLayout,
    ChannelInspectionResult,
    CheckPolicy,
    CheckResult,
    FindingSource,
    OccupancyResult,
    OccupancyState,
    ProductionInspectionResult,
    ProductionPipeline,
    ProductJudgment,
    Reason,
    TechnicalStatus,
    TraditionalSkipReason,
    measure_occupancy,
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
        self,
        frame: Frame | None,
        channel: ResolvedChannelConfiguration,
        occupancy: OccupancyResult,
    ) -> CheckResult:
        capsule = self.configuration.capsule
        policy = CheckPolicy(capsule.enabled, capsule.enabled)
        if not capsule.enabled or frame is None:
            return _unavailable(FindingSource.TRADITIONAL, policy)
        if occupancy.state is OccupancyState.EMPTY:
            return _unavailable(FindingSource.TRADITIONAL, CheckPolicy(False, True))
        try:
            return judge_capsule(
                measure_capsule(frame.image, channel.roi, capsule), capsule
            )
        except Exception as exc:
            return _unavailable(
                FindingSource.TRADITIONAL, policy, "capsule.execution_error", exc
            )

    def _occupancy(
        self, frame: Frame | None, channel: ResolvedChannelConfiguration
    ) -> OccupancyResult:
        if frame is None:
            return OccupancyResult(error_code="occupancy.result_unavailable")
        try:
            return OccupancyResult(measure_occupancy(frame.image, channel.roi))
        except Exception as exc:
            return OccupancyResult(
                error_code="occupancy.execution_error",
                exception_type=type(exc).__name__,
                message=str(exc),
            )

    def inspect_frame(
        self, frame: Frame, infer: Callable[[NDArray[np.uint8]], ImageResult]
    ) -> CameraInspectionResult:
        """Configured full-camera execution, retaining global camera/channel IDs."""
        layout = next(
            c
            for c in self.configuration.layout.cameras
            if c.camera_id == frame.id.camera_id
        )
        layout.validate_shape(tuple(frame.image.shape))
        _, result, _ = self._camera(
            layout, ReceivedFrame(frame, monotonic()), infer, True, CycleStatus.COMPLETE
        )
        return result

    def forced_camera(
        self,
        layout: CameraLayout,
        frame_id: FrameId | None,
        shape: tuple[int, int, int] | None,
        code: str,
    ) -> CameraInspectionResult:
        """Explicit fail-to-reject policy, separate from executed model evidence."""
        reason = Reason(FindingSource.COLLECTION, code)
        camera = self._base.camera_result(layout, None, collection_issue=reason)
        rows = tuple(
            replace(
                row,
                checks=(
                    _unavailable(
                        FindingSource.TRADITIONAL,
                        CheckPolicy(
                            self.configuration.capsule.enabled,
                            self.configuration.capsule.enabled,
                        ),
                    ),
                    _unavailable(
                        FindingSource.VIMO,
                        self.configuration.channels[
                            row.channel_id - 1
                        ].checks.vimo.policy,
                    ),
                ),
                forced_reject=reason,
            )
            for row in camera.channels
        )
        return replace(camera, frame_id=frame_id, image_shape=shape, channels=rows)

    def _camera(
        self,
        layout: CameraLayout,
        received: ReceivedFrame | None,
        infer: Callable[[NDArray[np.uint8]], ImageResult],
        ready: bool,
        status: CycleStatus,
    ) -> tuple[FrameInspectionResult | None, CameraInspectionResult, float]:
        frame = received.frame if received else None
        channels = tuple(
            self.configuration.channels[slot.channel_id - 1] for slot in layout.slots
        )
        occupancy_start = monotonic()
        occupancy = tuple(
            self._occupancy(frame if ready else None, channel) for channel in channels
        )
        occupancy_ms = (monotonic() - occupancy_start) * 1000
        traditional_start = monotonic()
        traditional = tuple(
            self._traditional_check(frame if ready else None, channel, presence)
            for channel, presence in zip(channels, occupancy, strict=True)
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
        issue = (
            None
            if ready
            else Reason(FindingSource.COLLECTION, f"cycle_{status.name.lower()}")
        )
        mapping_started = monotonic()
        camera = self._base.camera_result(layout, outcome, collection_issue=issue)
        rows: list[ChannelInspectionResult] = []
        for row, channel, traditional_check, presence in zip(
            camera.channels, channels, traditional, occupancy, strict=True
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
                    row,
                    checks=(traditional_check, vimo),
                    vimo_findings=evidence,
                    occupancy=presence,
                    traditional_skip=TraditionalSkipReason.CONFIRMED_EMPTY
                    if self.configuration.capsule.enabled
                    and presence.state is OccupancyState.EMPTY
                    else None,
                )
            )
        unassigned = tuple(
            f
            for f in camera.unassigned_findings
            if any(channel.checks.vimo.label_enabled(f.label) for channel in channels)
        )
        camera = replace(
            camera,
            channels=tuple(rows),
            unassigned_findings=unassigned,
            timing=replace(
                camera.timing,
                traditional_ms=traditional_ms if ready else None,
                occupancy_ms=occupancy_ms if ready else None,
                mapping_ms=(monotonic() - mapping_started) * 1000,
            ),
        )
        return outcome, camera, (monotonic() - mapping_started) * 1000

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
            outcome, camera, elapsed = self._camera(
                layout, by_camera.get(layout.camera_id), infer, ready, status
            )
            if outcome is not None:
                outcomes.append(outcome)
                if outcome.failure is not None:
                    status = CycleStatus.FAILED
            cameras.append(camera)
            aggregation_ms += elapsed
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
