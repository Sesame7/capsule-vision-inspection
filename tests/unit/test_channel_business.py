"""Additional classification without replacing original judgments or evidence."""

from dataclasses import asdict, replace

import pytest
from tests.support.application import production
from tests.support.business import channel, mixed_result
from tests.support.production import inferred

from capsule_vision.inference import BoundingRect
from capsule_vision.inspection.production import (
    ChannelBusinessStatus,
    OccupancyResult,
    OccupancyState,
    ProductJudgment,
    TechnicalStatus,
    extract_vimo_findings,
)


@pytest.mark.parametrize("occupancy", [None, *OccupancyState])
@pytest.mark.parametrize(
    "technical,judgment",
    [
        (TechnicalStatus.SUCCESS, ProductJudgment.OK),
        (TechnicalStatus.SUCCESS, ProductJudgment.NG),
        (TechnicalStatus.ERROR, ProductJudgment.UNKNOWN),
        (TechnicalStatus.ERROR, ProductJudgment.NG),
        (TechnicalStatus.DISABLED, ProductJudgment.UNKNOWN),
    ],
)
def test_business_precedence_preserves_evidence(
    occupancy: OccupancyState | None,
    technical: TechnicalStatus,
    judgment: ProductJudgment,
) -> None:
    row = channel(production().channels[0], occupancy, technical, judgment)
    before = asdict(row)
    expected = (
        ChannelBusinessStatus.NG
        if judgment is ProductJudgment.NG
        else ChannelBusinessStatus.EMPTY
        if occupancy is OccupancyState.EMPTY
        else ChannelBusinessStatus.OK
        if occupancy is OccupancyState.PRESENT
        and technical is TechnicalStatus.SUCCESS
        and judgment is ProductJudgment.OK
        else ChannelBusinessStatus.UNKNOWN
    )
    assert row.business_status is expected
    assert row.assessment.technical_status is technical
    assert row.assessment.judgment is judgment
    assert asdict(row) == before


@pytest.mark.parametrize("ng", [False, True])
def test_failed_occupancy_is_not_empty_or_ok(ng: bool) -> None:
    row = channel(
        production().channels[0],
        None,
        TechnicalStatus.SUCCESS,
        ProductJudgment.NG if ng else ProductJudgment.OK,
    )
    row = replace(
        row,
        occupancy=OccupancyResult(
            error_code="occupancy.execution_error", exception_type="RuntimeError"
        ),
    )
    assert row.occupancy is not None and row.occupancy.evidence is None
    assert row.business_status is (
        ChannelBusinessStatus.NG if ng else ChannelBusinessStatus.UNKNOWN
    )
    assert row.assessment.technical_status is TechnicalStatus.ERROR


def test_unassigned_camera_ng_never_fabricates_channel_ng() -> None:
    base = production()
    camera = base.cameras[0]
    assert camera.frame_id is not None
    findings = extract_vimo_findings(
        camera.frame_id, inferred(BoundingRect(58, 15, 2, 2))
    )
    base = replace(
        base,
        cameras=(replace(camera, unassigned_findings=findings), *base.cameras[1:]),
    )
    cameras = tuple(
        replace(
            camera,
            channels=tuple(
                channel(
                    row,
                    OccupancyState.PRESENT,
                    TechnicalStatus.SUCCESS,
                    ProductJudgment.OK,
                )
                for row in camera.channels
            ),
        )
        for camera in base.cameras
    )
    result = replace(base, cameras=cameras)
    assert (
        result.unassigned_findings and result.assessment.judgment is ProductJudgment.NG
    )
    assert all(
        row.business_status is ChannelBusinessStatus.OK for row in result.channels
    )
    assert all(not row.vimo_findings for row in result.channels)


def test_classification_is_available_on_compact_cycle_results() -> None:
    result = mixed_result()
    assert tuple(row.channel_id for row in result.channels) == tuple(range(1, 37))
    assert all(
        row.camera_id == camera.camera_id
        for camera in result.cameras
        for row in camera.channels
    )
    assert [row.business_status for row in result.cameras[0].channels] == [
        ChannelBusinessStatus.OK,
        ChannelBusinessStatus.EMPTY,
        ChannelBusinessStatus.UNKNOWN,
        ChannelBusinessStatus.NG,
        ChannelBusinessStatus.UNKNOWN,
        ChannelBusinessStatus.NG,
    ]
