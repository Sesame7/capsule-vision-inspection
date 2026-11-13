"""Scalar channel-business evidence shared by domain, accounting and Qt tests."""

from dataclasses import replace
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from capsule_vision.application import ConfiguredProductionPipeline
from capsule_vision.inference import BoundingRect
from capsule_vision.inspection import CycleStatus, Frame
from capsule_vision.inspection.production import (
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
from tests.support.application import prepared, production
from tests.support.capsule_images import SETTINGS, bodies
from tests.support.cycles import image_result
from tests.support.production import inferred


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


def mixed_result(
    cycle: int = 1, *, camera_count: int = 6
) -> ProductionInspectionResult:
    """Per camera: one OK, two NG, one EMPTY, two UNKNOWN; not physical products."""
    base = production(cycle)
    cameras = base.cameras[:camera_count]
    states = (
        (OccupancyState.PRESENT, TechnicalStatus.SUCCESS, ProductJudgment.OK),
        (OccupancyState.EMPTY, TechnicalStatus.SUCCESS, ProductJudgment.OK),
        (OccupancyState.UNKNOWN, TechnicalStatus.SUCCESS, ProductJudgment.OK),
        (OccupancyState.PRESENT, TechnicalStatus.ERROR, ProductJudgment.NG),
        (OccupancyState.PRESENT, TechnicalStatus.ERROR, ProductJudgment.UNKNOWN),
        (OccupancyState.UNKNOWN, TechnicalStatus.SUCCESS, ProductJudgment.NG),
    )
    return replace(
        base,
        layout=replace(base.layout, cameras=tuple(camera.layout for camera in cameras)),
        result_ready_at=2.025,  # Explicit fake clock: 25 ms, not live monotonic time.
        cameras=tuple(
            replace(
                camera,
                channels=tuple(
                    channel(row, *state)
                    for row, state in zip(camera.channels, states, strict=True)
                ),
            )
            for camera in cameras
        ),
    )


def history_review(
    tmp_path: Path, *, dent_label: str = "Dent"
) -> tuple[ProductionInspectionResult, NDArray[np.uint8]]:
    """One real pipeline camera with synthetic regions, plus an unfinished peer."""
    base = mixed_result()
    camera = base.cameras[1]
    layout = replace(
        camera.layout,
        slots=tuple(
            replace(slot, rect=BoundingRect(slot.slot * 80, 0, 80, 120))
            for slot in camera.layout.slots
        ),
    )
    machine = replace(
        base.layout, cameras=(base.layout.cameras[0], layout, *base.layout.cameras[2:])
    )
    config = prepared(tmp_path).configuration
    pipeline = ConfiguredProductionPipeline(
        replace(
            config,
            layout=machine,
            capsule=SETTINGS,
            checks=replace(
                config.checks,
                vimo=replace(config.checks.vimo, disabled_labels=("Filtered",)),
            ),
        )
    )
    image = np.zeros((120, 504, 3), dtype=np.uint8)
    image[:, 80:160] = bodies(hue=20)
    image[10:15, 100:120] = 0  # Length and Hue evidence on CH08.
    image[:, 160:240] = bodies()
    boxes = (
        (
            "Scratch",
            BoundingRect(75, 17, 90, 12),
        ),  # CH07 is EMPTY, CH08/09 are present.
        (dent_label, BoundingRect(103, 22, 9, 12)),
        (dent_label, BoundingRect(109, 80, 6, 9)),
        (
            dent_label,
            BoundingRect(103, 22, 9, 12),
        ),  # A distinct SDK region with identical geometry.
        ("Filtered", BoundingRect(106, 74, 4, 4)),
        ("Empty-only", BoundingRect(12, 25, 7, 7)),
        ("Background", BoundingRect(485, 88, 7, 8)),
    )
    raw = replace(
        image_result(image),
        regions=tuple(
            replace(region, label=label)
            for region, (label, _) in zip(
                inferred(*(rect for _, rect in boxes)).regions, boxes, strict=True
            )
        ),
    )
    assert camera.frame_id is not None
    reviewed = pipeline.inspect_frame(Frame(camera.frame_id, 0, image), lambda _: raw)
    forced = pipeline.forced_camera(
        machine.cameras[2], None, None, "basler.missing_camera"
    )
    return (
        replace(
            base,
            layout=machine,
            cycle_status=CycleStatus.FAILED,
            cameras=(base.cameras[0], reviewed, forced, *base.cameras[3:]),
        ),
        image,
    )
