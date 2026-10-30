"""Production semantics use synthetic geometry, never calibrated assumptions."""

from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from weakref import ref

import pytest
from tests.support.application import prepared, production
from tests.support.production import (
    DISABLED,
    ERROR,
    NG,
    OK,
    SUCCESS,
    TRADITIONAL,
    UNKNOWN,
    VIMO,
    Clock,
    inferred,
    layout,
    outcome,
    source_frame,
)

from capsule_vision.application import ConfiguredProductionPipeline
from capsule_vision.inference import BoundingRect
from capsule_vision.inspection import (
    CameraId,
    CycleCollector,
    CycleId,
    FrameId,
)
from capsule_vision.inspection.production import (
    Assessment,
    CameraTiming,
    ChannelId,
    CheckPolicy,
    CheckResult,
    FindingSource,
    InspectionLayout,
    Measurement,
    OverlapPolicy,
    ProductionInspectionResult,
    ProductJudgment,
    Reason,
    TechnicalStatus,
    aggregate,
    assign_vimo_findings,
    extract_vimo_findings,
)
from capsule_vision.inspection.production.evidence import (
    camera_vimo_evidence,
    check_result,
    duration_ms,
)


def check(
    status: TechnicalStatus = SUCCESS,
    judgment: ProductJudgment = OK,
    *,
    source: FindingSource = TRADITIONAL,
    required: bool = True,
) -> CheckResult:
    reasons = (
        (Reason(source, "fake_ng" if judgment is NG else "fake_error"),)
        if judgment is NG or status is ERROR
        else ()
    )
    return CheckResult(source, Assessment(status, judgment), reasons, required=required)


def with_traditional(
    result: ProductionInspectionResult,
    status: TechnicalStatus = SUCCESS,
    judgment: ProductJudgment = OK,
) -> ProductionInspectionResult:
    """Exercise shared evidence aggregation directly, without an obsolete plugin."""
    return replace(
        result,
        cameras=tuple(
            replace(
                camera,
                channels=tuple(
                    replace(
                        row,
                        checks=(
                            check(status, judgment) if index == 0 else check(),
                            *(c for c in row.checks if c.source is VIMO),
                        ),
                    )
                    for index, row in enumerate(camera.channels)
                ),
            )
            for camera in result.cameras
        ),
    )


def test_mirrored_mapping_changes_assignment_without_changing_algorithm() -> None:
    camera = layout(1, mirrored=True).cameras[0]
    frame_id = outcome().frame_id
    findings = extract_vimo_findings(frame_id, inferred(BoundingRect(1, 1, 2, 2)))
    assigned, unmatched = assign_vimo_findings(camera, findings)
    assert tuple(item.channel_id for item in assigned) == (ChannelId(6),)
    assert assigned[0].finding.frame_id == frame_id and not unmatched
    assert camera.slots[0].channel_id == assigned[0].channel_id
    assert check_result(VIMO, CheckPolicy(), matched=assigned).assessment.judgment is NG


@pytest.mark.parametrize(
    "kind",
    [
        "duplicate_slot",
        "missing_slot",
        "duplicate_channel",
        "missing_channel",
        "duplicate_camera",
        "invalid_roi",
    ],
)
def test_invalid_layouts_fail_early(kind: str) -> None:
    config = layout(1)
    camera = config.cameras[0]
    slots = camera.slots
    with pytest.raises(ValueError):
        if kind == "duplicate_slot":
            replace(camera, slots=(slots[0], *slots[:-1]))
        elif kind == "missing_slot":
            replace(camera, slots=slots[:-1])
        elif kind == "duplicate_channel":
            replace(
                camera,
                slots=(*slots[:-1], replace(slots[-1], channel_id=slots[0].channel_id)),
            )
        elif kind == "missing_channel":
            InspectionLayout(
                (
                    replace(
                        camera,
                        slots=(
                            *slots[:-1],
                            replace(slots[-1], channel_id=ChannelId(7)),
                        ),
                    ),
                )
            )
        elif kind == "duplicate_camera":
            InspectionLayout((camera, camera))
        else:
            replace(slots[0], rect=BoundingRect(0, 0, 0, 1))


@pytest.mark.parametrize(
    "rect,ids",
    [
        (BoundingRect(1, 1, 2, 2), (1,)),
        (BoundingRect(7, 1, 5, 2), (1, 2)),
        (BoundingRect(1, 12, 2, 2), ()),
        (BoundingRect(8, 1, 2, 2), ()),  # Touching edges are not overlap.
    ],
)
def test_roi_assignment_and_unassigned_ng(
    rect: BoundingRect, ids: tuple[int, ...]
) -> None:
    camera = layout(1).cameras[0]
    assigned, unassigned, reason = camera_vimo_evidence(
        camera, outcome(image=inferred(rect)), CheckPolicy()
    )
    assert tuple(item.channel_id for item in assigned) == ids
    assert reason is None
    assert len(unassigned) == (0 if ids else 1)
    assert all(
        check_result(VIMO, CheckPolicy(), matched=(item,)).assessment
        == Assessment(SUCCESS, NG)
        for item in assigned
    )
    if len(ids) == 2:
        first, second = assigned
        assert first.finding is second.finding
        assert first.intersection_area == 2 and second.intersection_area == 4
        assert first.finding_bbox_fraction == pytest.approx(0.2)


def test_explicit_overlap_policy_and_region_order_do_not_define_channels() -> None:
    camera = layout(1).cameras[0]
    raw = inferred(BoundingRect(21, 1, 2, 2), BoundingRect(1, 1, 2, 2))
    findings = extract_vimo_findings(outcome().frame_id, raw)
    assigned, unmatched = assign_vimo_findings(camera, findings)
    assert tuple(item.channel_id for item in assigned) == (3, 1) and not unmatched
    assigned, unmatched = assign_vimo_findings(camera, findings, OverlapPolicy(5))
    assert not assigned and unmatched == findings
    with pytest.raises(ValueError):
        OverlapPolicy(0)


def test_unassigned_defect_and_independent_error_preserve_overall_error_ng() -> None:
    base = production()
    camera = base.cameras[0]
    assert camera.frame_id is not None
    findings = extract_vimo_findings(
        camera.frame_id, inferred(BoundingRect(1, 12, 2, 2))
    )
    result = with_traditional(
        replace(
            base,
            cameras=(replace(camera, unassigned_findings=findings), *base.cameras[1:]),
        ),
        ERROR,
        UNKNOWN,
    )
    assert result.assessment == Assessment(ERROR, NG)
    assert len(result.unassigned_findings) == 1
    assert not any(channel.assessment.judgment is NG for channel in result.channels)


def test_disabled_channel_cannot_make_a_camera_completely_ok() -> None:
    result = production()
    first = result.channels[0]
    disabled = replace(
        first, checks=(check(DISABLED, UNKNOWN), check(DISABLED, UNKNOWN, source=VIMO))
    )
    camera = replace(
        result.cameras[0], channels=(disabled, *result.cameras[0].channels[1:])
    )
    assert camera.assessment == Assessment(SUCCESS, UNKNOWN)
    assert replace(
        result, cameras=(camera, *result.cameras[1:])
    ).assessment == Assessment(SUCCESS, UNKNOWN)


@pytest.mark.parametrize(
    "traditional_status,traditional_judgment,vimo_defect,vimo_error,expected",
    [
        (SUCCESS, OK, False, False, Assessment(SUCCESS, OK)),
        (SUCCESS, NG, False, False, Assessment(SUCCESS, NG)),
        (SUCCESS, OK, True, False, Assessment(SUCCESS, NG)),
        (SUCCESS, NG, True, False, Assessment(SUCCESS, NG)),
        (ERROR, UNKNOWN, False, False, Assessment(ERROR, UNKNOWN)),
        (SUCCESS, OK, False, True, Assessment(ERROR, UNKNOWN)),
        (SUCCESS, NG, False, True, Assessment(ERROR, NG)),
        (ERROR, UNKNOWN, True, False, Assessment(ERROR, NG)),
    ],
)
def test_independent_status_and_evidence_first_judgment(
    traditional_status: TechnicalStatus,
    traditional_judgment: ProductJudgment,
    vimo_defect: bool,
    vimo_error: bool,
    expected: Assessment,
) -> None:
    result = with_traditional(
        production(
            status=ERROR if vimo_error else SUCCESS,
            judgment=UNKNOWN if vimo_error else NG if vimo_defect else OK,
        ),
        traditional_status,
        traditional_judgment,
    )
    assert (
        result.channels[0].assessment
        == result.cameras[0].assessment
        == result.assessment
        == expected
    )
    if traditional_judgment is NG and vimo_defect:
        assert {reason.source for reason in result.channels[0].reasons} == {
            TRADITIONAL,
            VIMO,
        }
    if vimo_error:
        assert all(
            row.assessment.judgment is UNKNOWN
            for camera in result.cameras
            for row in camera.channels[1:]
        )


def test_disabled_vimo_is_explicit() -> None:
    result = check_result(VIMO, CheckPolicy(False), Reason(VIMO, "execution_error"))
    assert result.assessment == Assessment(DISABLED, UNKNOWN)
    assert result.reasons == ()
    assert production(status=DISABLED, judgment=UNKNOWN).assessment == Assessment(
        DISABLED, UNKNOWN
    )


def test_optional_error_preserves_evidence_without_invalidating_successful_required_check() -> (
    None
):
    assert aggregate(
        (check(ERROR, UNKNOWN, required=False), check(source=VIMO))
    ) == Assessment(SUCCESS, OK)
    assert aggregate(
        (check(ERROR, NG, required=False), check(source=VIMO))
    ) == Assessment(SUCCESS, NG)
    assert aggregate((check(ERROR, UNKNOWN, required=False),)) == Assessment(
        ERROR, UNKNOWN
    )


def test_measurement_evidence_is_preserved() -> None:
    measured = replace(check(), measurements=(Measurement("upper.width", 7, "px"),))
    assert measured.measurements[0].value == 7


def test_result_identity_and_channel_mapping_mismatches_are_rejected() -> None:
    result = production()
    camera = result.cameras[0]
    with pytest.raises(ValueError, match="another cycle"):
        replace(result, cycle_id=CycleId(2))
    with pytest.raises(ValueError, match="camera mismatch"):
        camera_vimo_evidence(camera.layout, outcome(CameraId("CAM2")), CheckPolicy())
    with pytest.raises(ValueError, match="layout"):
        replace(camera, channels=tuple(reversed(camera.channels)))
    with pytest.raises(ValueError, match="another camera"):
        replace(camera, frame_id=FrameId(CycleId(1), CameraId("CAM2"), 7))


def test_mask_and_source_pixels_are_not_retained_and_collections_are_readonly() -> None:
    camera = layout(1).cameras[0]
    frame = source_frame()
    pixels = ref(frame.image)
    image = inferred(BoundingRect(1, 1, 2, 2))
    mask = ref(image.mask)
    raw = outcome(image=image)
    assigned, unassigned, reason = camera_vimo_evidence(camera, raw, CheckPolicy())
    del image, raw, frame
    assert mask() is None and pixels() is None
    assert not unassigned and reason is None
    assert check_result(VIMO, CheckPolicy(), matched=assigned).assessment.judgment is NG
    result = production()
    field_name = "slot"
    with pytest.raises(FrozenInstanceError):
        setattr(result.channels[0], field_name, 5)
    assert isinstance(result.channels, tuple) and isinstance(
        result.channels[0].reasons, tuple
    )


def test_timing_uses_injected_clock_and_existing_inference_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock(0.001)
    monkeypatch.setattr("capsule_vision.application.pipeline.monotonic", clock)
    configuration = replace(prepared(tmp_path).configuration, layout=layout(1))
    collector = CycleCollector(configuration.layout.camera_ids, timeout=10, clock=clock)
    collector.begin(CycleId(1))
    cycle = collector.accept(source_frame())
    assert cycle is not None
    completion = ConfiguredProductionPipeline(configuration).inspect(
        cycle, lambda _: inferred()
    )
    raw = completion.raw.frames[0]
    timing = completion.production.cameras[0].timing
    assert raw.started_at is not None and raw.finished_at is not None
    assert timing.vimo_ms == pytest.approx(duration_ms(raw.started_at, raw.finished_at))
    assert timing.vimo_ms == pytest.approx(1)
    assert timing.traditional_ms == pytest.approx(1)
    assert timing.mapping_ms == pytest.approx(1)
    assert timing.occupancy_ms == pytest.approx(1)
    assert timing.total_ms == pytest.approx(4)
    assert completion.production.aggregation_ms >= timing.mapping_ms
    assert completion.production.result_ready_at == completion.raw.finished_at
    assert completion.production.inspection_started_at is not None
    with pytest.raises(ValueError):
        CameraTiming(None, -1, 0)
    with pytest.raises(ValueError, match="backwards"):
        duration_ms(2, 1)


def test_invalid_geometry_and_out_of_image_configuration_are_explicit() -> None:
    camera = layout(1).cameras[0]
    raw = outcome(image=inferred(BoundingRect(59, 1, 5, 2)))
    assigned, unassigned, reason = camera_vimo_evidence(camera, raw, CheckPolicy())
    assert not assigned and not unassigned
    assert reason is not None and reason.code == "invalid_result"
    assert check_result(VIMO, CheckPolicy(), reason).assessment == Assessment(
        ERROR, UNKNOWN
    )
    slots = (
        *camera.slots[:-1],
        replace(camera.slots[-1], rect=BoundingRect(59, 0, 8, 10)),
    )
    with pytest.raises(ValueError, match="outside"):
        camera_vimo_evidence(replace(camera, slots=slots), outcome(), CheckPolicy())


@pytest.mark.parametrize(
    "status,judgment", [(ERROR, OK), (DISABLED, OK), (DISABLED, NG), (SUCCESS, UNKNOWN)]
)
def test_impossible_check_assessments_are_rejected(
    status: TechnicalStatus, judgment: ProductJudgment
) -> None:
    with pytest.raises(ValueError):
        check(status, judgment)
