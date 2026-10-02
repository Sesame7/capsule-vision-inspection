"""Production semantics use synthetic geometry, never calibrated assumptions."""

from dataclasses import FrozenInstanceError, replace
from weakref import ref

import numpy as np
import pytest
from tests.unit.test_inspection_cycle import image_result

from vimo_demo.inference import BoundingRect, DefectRegion, ImageResult, RotatedRect
from vimo_demo.inspection import (
    CameraId,
    CycleId,
    CycleStatus,
    Frame,
    FrameId,
    FrameInspectionResult,
    InspectionCycleResult,
    InspectionFailure,
)
from vimo_demo.inspection.production import (
    Assessment,
    CameraLayout,
    CameraTiming,
    ChannelId,
    ChannelROI,
    CheckPolicy,
    CheckResult,
    FindingSource,
    InspectionLayout,
    Measurement,
    OverlapPolicy,
    ProductionPipeline,
    ProductJudgment,
    Reason,
    TechnicalStatus,
    TraditionalCameraResult,
    TraditionalChannelCheck,
    aggregate,
    assign_vimo_findings,
    extract_vimo_findings,
)

SUCCESS, ERROR, DISABLED = TechnicalStatus
OK, NG, UNKNOWN = ProductJudgment
TRADITIONAL = FindingSource.TRADITIONAL
VIMO = FindingSource.VIMO
CAM1 = CameraId("CAM1")


class Clock:
    def __init__(self, step: float = 0) -> None:
        self.value = 100.0
        self.step = step

    def __call__(self) -> float:
        current = self.value
        self.value += self.step
        return current


def layout(count: int = 6, *, mirrored: bool = False) -> InspectionLayout:
    return InspectionLayout(
        tuple(
            CameraLayout(
                CameraId(f"CAM{camera + 1}"),
                tuple(
                    ChannelROI(
                        slot,
                        ChannelId(camera * 6 + (6 - slot if mirrored else slot + 1)),
                        BoundingRect(slot * 10, 0, 8, 10),
                    )
                    for slot in range(6)
                ),
            )
            for camera in range(count)
        )
    )


def source_frame(camera: CameraId = CAM1, cycle: int = 1) -> Frame:
    return Frame(
        FrameId(CycleId(cycle), camera, 7), 0, np.zeros((20, 60, 3), dtype=np.uint8)
    )


def inferred(*rects: BoundingRect) -> ImageResult:
    base = image_result(np.zeros((20, 60, 3), dtype=np.uint8))
    return replace(
        base,
        regions=tuple(
            DefectRegion(
                f"label-{i}",
                rect.width * rect.height,
                rect,
                2,
                1,
                (),
                (),
                RotatedRect(0, 0, 2, 1, 0),
            )
            for i, rect in enumerate(rects)
        ),
    )


def outcome(
    camera: CameraId = CAM1,
    image: ImageResult | None = None,
    *,
    failed: bool = False,
) -> FrameInspectionResult:
    return FrameInspectionResult(
        FrameId(CycleId(1), camera, 7),
        0,
        1,
        (20, 60, 3),
        2,
        2.01,
        None if failed else image if image is not None else inferred(),
        InspectionFailure("FakeError", "diagnostic", "trace") if failed else None,
    )


def cycle_result(
    config: InspectionLayout, first: ImageResult | None = None, *, failed: bool = False
) -> InspectionCycleResult:
    return InspectionCycleResult(
        CycleId(1),
        config.camera_ids,
        0,
        10,
        1,
        2.01,
        CycleStatus.FAILED if failed else CycleStatus.COMPLETE,
        tuple(
            outcome(
                camera.camera_id,
                first if index == 0 else None,
                failed=failed and index == 0,
            )
            for index, camera in enumerate(config.cameras)
        ),
        (),
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


class FakeTraditional:
    def __init__(
        self, status: TechnicalStatus = SUCCESS, judgment: ProductJudgment = OK
    ) -> None:
        self.status, self.judgment = status, judgment
        self.calls = 0

    def inspect(
        self, frame: Frame, layout: CameraLayout
    ) -> tuple[TraditionalChannelCheck, ...]:
        self.calls += 1
        assert not frame.image.flags.writeable
        return tuple(
            TraditionalChannelCheck(
                slot.channel_id,
                check(self.status, self.judgment) if index == 0 else check(),
            )
            for index, slot in enumerate(layout.slots)
        )


def traditional_results(
    pipeline: ProductionPipeline, inspector: FakeTraditional | None = None
) -> tuple[TraditionalCameraResult, ...]:
    return tuple(
        pipeline.inspect_traditional(
            source_frame(camera.camera_id), inspector or FakeTraditional()
        )
        for camera in pipeline.layout.cameras
    )


def test_36_channels_order_identity_and_success_with_reversed_results() -> None:
    config = layout()
    pipeline = ProductionPipeline(config, clock=Clock())
    raw = cycle_result(config)
    raw = replace(raw, frames=tuple(reversed(raw.frames)))
    result = pipeline.from_cycle(raw, tuple(reversed(traditional_results(pipeline))))
    assert result.assessment == Assessment(SUCCESS, OK)
    assert tuple(camera.camera_id for camera in result.cameras) == config.camera_ids
    assert [channel.channel_id for channel in result.channels] == list(range(1, 37))
    for index, camera in enumerate(result.cameras):
        assert [row.channel_id for row in camera.channels] == list(
            range(index * 6 + 1, index * 6 + 7)
        )
        assert result.channels[index * 6] is camera.channels[0]


def test_mirrored_mapping_changes_assignment_without_changing_algorithm() -> None:
    config = layout(1, mirrored=True)
    pipeline = ProductionPipeline(
        config, traditional=CheckPolicy(enabled=False), clock=Clock()
    )
    result = pipeline.from_cycle(
        cycle_result(config, inferred(BoundingRect(1, 1, 2, 2)))
    )
    assert result.channels[-1].channel_id == ChannelId(6)
    assert result.channels[-1].assessment.judgment is NG
    assert result.cameras[0].channels[0] is result.channels[-1]
    assert all(row.assessment.judgment is OK for row in result.channels[:-1])


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
    config = layout(1)
    pipeline = ProductionPipeline(
        config, traditional=CheckPolicy(enabled=False), clock=Clock()
    )
    result = pipeline.from_cycle(cycle_result(config, inferred(rect)))
    camera = result.cameras[0]
    assert tuple(item.channel_id for item in camera.assigned_findings) == ids
    assert (
        tuple(
            row.channel_id for row in result.channels if row.assessment.judgment is NG
        )
        == ids
    )
    assert len(result.unassigned_findings) == (0 if ids else 1)
    assert result.assessment == camera.assessment == Assessment(SUCCESS, NG)
    if len(ids) == 2:
        first, second = camera.assigned_findings
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
    config = layout(1)
    pipeline = ProductionPipeline(config, clock=Clock())
    result = pipeline.from_cycle(
        cycle_result(config, inferred(BoundingRect(1, 12, 2, 2))),
        traditional_results(pipeline, FakeTraditional(ERROR, UNKNOWN)),
    )
    assert result.assessment == Assessment(ERROR, NG)
    assert len(result.unassigned_findings) == 1
    assert not any(channel.assessment.judgment is NG for channel in result.channels)


def test_disabled_channel_cannot_make_a_camera_completely_ok() -> None:
    config = layout(1)
    pipeline = ProductionPipeline(config, clock=Clock())
    result = pipeline.from_cycle(cycle_result(config), traditional_results(pipeline))
    first = result.channels[0]
    disabled = replace(
        first, checks=(check(DISABLED, UNKNOWN), check(DISABLED, UNKNOWN, source=VIMO))
    )
    camera = replace(result.cameras[0], channels=(disabled, *result.channels[1:]))
    assert camera.assessment == Assessment(SUCCESS, UNKNOWN)
    assert replace(result, cameras=(camera,)).assessment == Assessment(SUCCESS, UNKNOWN)


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
    config = layout(1)
    pipeline = ProductionPipeline(config, clock=Clock())
    raw = cycle_result(
        config,
        inferred(BoundingRect(1, 1, 2, 2)) if vimo_defect else None,
        failed=vimo_error,
    )
    trad = traditional_results(
        pipeline, FakeTraditional(traditional_status, traditional_judgment)
    )
    result = pipeline.from_cycle(raw, trad)
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
        assert all(row.assessment.judgment is UNKNOWN for row in result.channels[1:])


def test_disabled_checks_and_missing_required_traditional_are_explicit() -> None:
    config = layout(1)
    raw = cycle_result(config)
    both = ProductionPipeline(
        config, traditional=CheckPolicy(False), vimo=CheckPolicy(False), clock=Clock()
    )
    assert both.from_cycle(raw).assessment == Assessment(DISABLED, UNKNOWN)
    optional = ProductionPipeline(
        config, traditional=CheckPolicy(False, False), clock=Clock()
    )
    assert optional.from_cycle(raw).assessment == Assessment(SUCCESS, OK)
    required = ProductionPipeline(config, clock=Clock())
    result = required.from_cycle(raw)
    assert result.assessment == Assessment(ERROR, UNKNOWN)
    assert result.channels[0].reasons[0].code == "result_unavailable"


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


def test_traditional_protocol_errors_measurements_and_disabled_invocation() -> None:
    config = layout(1)
    pipeline = ProductionPipeline(config, clock=Clock())

    class Broken:
        def inspect(
            self, frame: Frame, layout: CameraLayout
        ) -> tuple[TraditionalChannelCheck, ...]:
            raise RuntimeError("not a product defect")

    traditional = pipeline.inspect_traditional(source_frame(), Broken())
    assert all(
        row.result.assessment == Assessment(ERROR, UNKNOWN)
        for row in traditional.channels
    )
    assert (
        traditional.channels[0].result.reasons[0].exception_type
        == "builtins.RuntimeError"
    )
    unavailable = pipeline.inspect_traditional(source_frame(), None)
    assert unavailable.channels[0].result.reasons[0].code == "check_unavailable"
    inspector = FakeTraditional()
    disabled = ProductionPipeline(config, traditional=CheckPolicy(False), clock=Clock())
    disabled.inspect_traditional(source_frame(), inspector)
    assert inspector.calls == 0
    measured = replace(check(), measurements=(Measurement("blob_area", 7, "px2"),))
    assert measured.measurements[0].value == 7


def test_result_identity_and_channel_mapping_mismatches_are_rejected() -> None:
    config = layout(1)
    pipeline = ProductionPipeline(config, clock=Clock())
    raw = cycle_result(config)
    trad = traditional_results(pipeline)[0]
    with pytest.raises(ValueError, match="identity"):
        pipeline.from_cycle(
            raw, (replace(trad, frame_id=FrameId(CycleId(2), CameraId("CAM1"), 7)),)
        )
    with pytest.raises(ValueError, match="Duplicate"):
        pipeline.from_cycle(raw, (trad, trad))
    with pytest.raises(ValueError, match="channels"):
        pipeline.from_cycle(raw, (replace(trad, channels=trad.channels[:-1]),))
    with pytest.raises(ValueError, match="mixed-cycle"):
        pipeline.from_cycle(replace(raw, frames=(outcome(CameraId("CAM2")),)))
    result = pipeline.from_cycle(raw, (trad,))
    with pytest.raises(ValueError, match="layout"):
        replace(result.cameras[0], channels=tuple(reversed(result.channels)))
    with pytest.raises(ValueError, match="another camera"):
        replace(result.cameras[0], frame_id=FrameId(CycleId(1), CameraId("CAM2"), 7))


def test_timeout_results_preserve_missing_cameras_without_fake_product_ng() -> None:
    config = layout()
    pipeline = ProductionPipeline(config, clock=Clock())
    raw = InspectionCycleResult(
        CycleId(1),
        config.camera_ids,
        0,
        10,
        10,
        10,
        CycleStatus.TIMED_OUT,
        (),
        config.camera_ids,
    )
    result = pipeline.from_cycle(raw)
    assert result.cycle_status is CycleStatus.TIMED_OUT
    assert result.assessment == Assessment(ERROR, UNKNOWN)
    assert len(result.channels) == 36
    assert all(
        channel.assessment == Assessment(ERROR, UNKNOWN) for channel in result.channels
    )
    assert all(camera.collection_issue is not None for camera in result.cameras)


def test_mask_and_source_pixels_are_not_retained_and_collections_are_readonly() -> None:
    config = layout(1)
    pipeline = ProductionPipeline(config, clock=Clock())
    frame = source_frame()
    pixels = ref(frame.image)
    traditional = pipeline.inspect_traditional(frame, FakeTraditional())
    image = inferred(BoundingRect(1, 1, 2, 2))
    mask = ref(image.mask)
    raw = cycle_result(config, image)
    result = pipeline.from_cycle(raw, (traditional,))
    del image, raw, frame, traditional
    assert mask() is None and pixels() is None
    assert result.channels[0].assessment.judgment is NG
    field_name = "slot"
    with pytest.raises(FrozenInstanceError):
        setattr(result.channels[0], field_name, 5)
    assert isinstance(result.channels, tuple) and isinstance(
        result.channels[0].reasons, tuple
    )


def test_timing_uses_injected_clock_and_existing_inference_interval() -> None:
    config = layout(1)
    clock = Clock(0.001)
    pipeline = ProductionPipeline(config, clock=clock)
    trad = traditional_results(pipeline)
    result = pipeline.from_cycle(cycle_result(config), trad)
    timing = result.cameras[0].timing
    assert timing.traditional_ms == pytest.approx(1)
    assert timing.vimo_ms == pytest.approx(10)
    assert timing.mapping_ms == pytest.approx(1)
    assert timing.total_ms == pytest.approx(12)
    assert result.aggregation_ms == pytest.approx(3)
    assert result.inspection_started_at == 2
    with pytest.raises(ValueError):
        CameraTiming(None, -1, 0)
    with pytest.raises(ValueError):
        replace(trad[0], finished_at=trad[0].started_at - 1)


def test_invalid_geometry_and_out_of_image_configuration_are_explicit() -> None:
    config = layout(1)
    pipeline = ProductionPipeline(config, traditional=CheckPolicy(False), clock=Clock())
    raw = cycle_result(config, inferred(BoundingRect(59, 1, 5, 2)))
    result = pipeline.from_cycle(raw)
    assert result.assessment == Assessment(ERROR, UNKNOWN)
    assert result.channels[0].reasons[0].code == "invalid_result"
    camera = config.cameras[0]
    slots = (
        *camera.slots[:-1],
        replace(camera.slots[-1], rect=BoundingRect(59, 0, 8, 10)),
    )
    invalid = ProductionPipeline(
        InspectionLayout((replace(camera, slots=slots),)), clock=Clock()
    )
    with pytest.raises(ValueError, match="outside"):
        invalid.from_cycle(cycle_result(invalid.layout))


@pytest.mark.parametrize(
    "status,judgment", [(ERROR, OK), (DISABLED, OK), (DISABLED, NG), (SUCCESS, UNKNOWN)]
)
def test_impossible_check_assessments_are_rejected(
    status: TechnicalStatus, judgment: ProductJudgment
) -> None:
    with pytest.raises(ValueError):
        check(status, judgment)
