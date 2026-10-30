"""Configured serial inspection using shared evidence assignment and judgment."""

from collections.abc import Callable
from time import monotonic

import numpy as np
from numpy.typing import NDArray

from capsule_vision.configuration import ResolvedInspectionConfiguration
from capsule_vision.inference import ImageResult
from capsule_vision.inspection import (
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
from capsule_vision.inspection.production import (
    CameraInspectionResult,
    CameraLayout,
    CameraTiming,
    ChannelInspectionResult,
    ChannelROI,
    CheckPolicy,
    CheckResult,
    ConfiguredCycleCompletion,
    FindingSource,
    OccupancyResult,
    OccupancyState,
    ProductionInspectionResult,
    Reason,
    TraditionalSkipReason,
    measure_occupancy,
)
from capsule_vision.inspection.production.capsule import judge_capsule, measure_capsule
from capsule_vision.inspection.production.evidence import (
    camera_vimo_evidence,
    check_result,
    duration_ms,
)


def _unavailable(
    source: FindingSource,
    policy: CheckPolicy,
    code: str = "result_unavailable",
    error: Exception | None = None,
) -> CheckResult:
    return check_result(
        source,
        policy,
        Reason(source, code, exception_type=type(error).__name__ if error else None)
        if policy.enabled
        else None,
    )


class ConfiguredProductionPipeline:
    def __init__(
        self,
        configuration: ResolvedInspectionConfiguration,
    ) -> None:
        self.configuration = configuration

    def _traditional_check(
        self,
        frame: Frame | None,
        channel: ChannelROI,
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
                measure_capsule(frame.image, channel.rect, capsule), capsule
            )
        except Exception as exc:
            return _unavailable(
                FindingSource.TRADITIONAL, policy, "capsule.execution_error", exc
            )

    def _occupancy(self, frame: Frame | None, channel: ChannelROI) -> OccupancyResult:
        if frame is None:
            return OccupancyResult(error_code="occupancy.result_unavailable")
        try:
            return OccupancyResult(measure_occupancy(frame.image, channel.rect))
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
        started = monotonic()
        if layout not in self.configuration.layout.cameras:
            raise ValueError("Camera layout is not configured in this pipeline")
        reason = Reason(FindingSource.COLLECTION, code)
        capsule = self.configuration.capsule
        checks = (
            _unavailable(
                FindingSource.TRADITIONAL, CheckPolicy(capsule.enabled, capsule.enabled)
            ),
            _unavailable(FindingSource.VIMO, self.configuration.checks.vimo.policy),
        )
        rows = tuple(
            ChannelInspectionResult(
                layout.camera_id,
                slot.channel_id,
                slot.slot,
                checks,
                forced_reject=reason,
            )
            for slot in layout.slots
        )
        return CameraInspectionResult(
            layout,
            frame_id,
            shape,
            rows,
            (),
            CameraTiming(None, None, (monotonic() - started) * 1000),
            reason,
        )

    def _camera(
        self,
        layout: CameraLayout,
        received: ReceivedFrame | None,
        infer: Callable[[NDArray[np.uint8]], ImageResult],
        ready: bool,
        status: CycleStatus,
    ) -> tuple[FrameInspectionResult | None, CameraInspectionResult, float]:
        frame = received.frame if received else None
        policy = self.configuration.checks.vimo
        occupancy_start = monotonic()
        occupancy = tuple(
            self._occupancy(frame if ready else None, slot) for slot in layout.slots
        )
        occupancy_ms = (monotonic() - occupancy_start) * 1000
        traditional_start = monotonic()
        traditional = tuple(
            self._traditional_check(frame if ready else None, slot, presence)
            for slot, presence in zip(layout.slots, occupancy, strict=True)
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
            if ready and policy.enabled:
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
        assignments, unassigned, reason = camera_vimo_evidence(
            layout, outcome, policy.policy
        )
        assignments = tuple(
            item for item in assignments if policy.label_enabled(item.finding.label)
        )
        rows: list[ChannelInspectionResult] = []
        for slot, traditional_check, presence in zip(
            layout.slots, traditional, occupancy, strict=True
        ):
            matched = tuple(
                item for item in assignments if item.channel_id == slot.channel_id
            )
            rows.append(
                ChannelInspectionResult(
                    layout.camera_id,
                    slot.channel_id,
                    slot.slot,
                    (
                        traditional_check,
                        check_result(
                            FindingSource.VIMO, policy.policy, reason, matched=matched
                        ),
                    ),
                    matched,
                    occupancy=presence,
                    traditional_skip=TraditionalSkipReason.CONFIRMED_EMPTY
                    if self.configuration.capsule.enabled
                    and presence.state is OccupancyState.EMPTY
                    else None,
                )
            )
        vimo_ms = (
            duration_ms(outcome.started_at, outcome.finished_at)
            if outcome is not None
            and outcome.started_at is not None
            and outcome.finished_at is not None
            else None
        )
        camera = CameraInspectionResult(
            layout,
            outcome.frame_id if outcome else None,
            outcome.image_shape if outcome else None,
            tuple(rows),
            tuple(f for f in unassigned if policy.label_enabled(f.label)),
            CameraTiming(
                traditional_ms if ready else None,
                vimo_ms,
                (monotonic() - mapping_started) * 1000,
                occupancy_ms if ready else None,
            ),
            issue,
        )
        return outcome, camera, (monotonic() - mapping_started) * 1000

    def inspect(
        self, cycle: CollectedCycle, infer: Callable[[NDArray[np.uint8]], ImageResult]
    ) -> ConfiguredCycleCompletion:
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
        return ConfiguredCycleCompletion(raw, production)
