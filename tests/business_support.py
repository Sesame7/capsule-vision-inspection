"""Scalar E2 evidence builders shared by domain, accounting and Qt tests."""

from dataclasses import replace

from tests.application_support import production
from vimo_demo.inference import BoundingRect
from vimo_demo.inspection.production import (
    Assessment,
    ChannelInspectionResult,
    CheckResult,
    FindingSource,
    OccupancyEvidence,
    OccupancyResult,
    OccupancyState,
    OccupancyView,
    ProductionInspectionResult,
    ProductJudgment,
    Reason,
    TechnicalStatus,
    TraditionalSkipReason,
)


def channel(
    row: ChannelInspectionResult,
    occupancy: OccupancyState | None,
    technical: TechnicalStatus,
    judgment: ProductJudgment,
) -> ChannelInspectionResult:
    counts = {
        OccupancyState.EMPTY: 0,
        OccupancyState.PRESENT: 2,
        OccupancyState.UNKNOWN: 1,
    }
    evidence = None
    if occupancy is not None:
        view = OccupancyView(counts[occupancy], 25)
        evidence = OccupancyResult(
            OccupancyEvidence(BoundingRect(row.slot * 10 + 1, 0, 5, 10), view, view)
        )
    reasons = (
        *(
            (Reason(FindingSource.VIMO, "defect_region"),)
            if judgment is ProductJudgment.NG
            else ()
        ),
        *(
            (
                Reason(
                    FindingSource.VIMO, "execution_error", exception_type="RuntimeError"
                ),
            )
            if technical is TechnicalStatus.ERROR
            else ()
        ),
    )
    return replace(
        row,
        occupancy=evidence,
        traditional_skip=TraditionalSkipReason.CONFIRMED_EMPTY
        if occupancy is OccupancyState.EMPTY
        else None,
        checks=(
            row.checks[0],
            CheckResult(FindingSource.VIMO, Assessment(technical, judgment), reasons),
        ),
    )


def mixed_result(cycle: int = 1) -> ProductionInspectionResult:
    """Per camera: one OK, two NG, one EMPTY, two UNKNOWN; not physical products."""
    base = production(cycle)
    states = (
        (OccupancyState.PRESENT, TechnicalStatus.SUCCESS, ProductJudgment.OK),
        (OccupancyState.EMPTY, TechnicalStatus.SUCCESS, ProductJudgment.OK),
        (OccupancyState.UNKNOWN, TechnicalStatus.SUCCESS, ProductJudgment.OK),
        (OccupancyState.EMPTY, TechnicalStatus.ERROR, ProductJudgment.NG),
        (OccupancyState.PRESENT, TechnicalStatus.ERROR, ProductJudgment.UNKNOWN),
        (OccupancyState.UNKNOWN, TechnicalStatus.SUCCESS, ProductJudgment.NG),
    )
    return replace(
        base,
        result_ready_at=2.025,  # Explicit fake clock: 25 ms, not live monotonic time.
        cameras=tuple(
            replace(
                camera,
                channels=tuple(
                    channel(row, *state)
                    for row, state in zip(camera.channels, states, strict=True)
                ),
            )
            for camera in base.cameras
        ),
    )
