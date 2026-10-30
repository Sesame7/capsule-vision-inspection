"""Always-on evidence, explicit EMPTY skip, unchanged ViMo and traditional checks."""

from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from numpy.typing import NDArray
from tests.support.application import prepared
from tests.support.capsule_images import SETTINGS, bodies
from tests.support.production import inferred

from capsule_vision.application import ConfiguredProductionPipeline
from capsule_vision.configuration import ResolvedInspectionConfiguration
from capsule_vision.inference import BoundingRect, ImageResult
from capsule_vision.inspection import CycleCollector, CycleId, Frame, FrameId
from capsule_vision.inspection.production import (
    Assessment,
    OccupancyResult,
    OccupancyState,
    ProductJudgment,
    TechnicalStatus,
    TraditionalSkipReason,
    measure_occupancy,
)
from capsule_vision.inspection.production.capsule import (
    CapsuleSettings,
    judge_capsule,
    measure_capsule,
)


def configuration(
    tmp_path: Path, *, enabled_vimo: bool = True, count: int = 6, mirrored: bool = False
):
    config = prepared(tmp_path).configuration
    layout = replace(
        config.layout,
        cameras=tuple(
            replace(
                camera,
                slots=tuple(
                    replace(
                        slot,
                        rect=BoundingRect(
                            (5 - slot.slot if mirrored else slot.slot) * 80, 0, 80, 120
                        ),
                    )
                    for slot in camera.slots
                ),
            )
            for camera in config.layout.cameras[:count]
        ),
    )
    return replace(
        config,
        layout=layout,
        checks=replace(
            config.checks, vimo=replace(config.checks.vimo, enabled=enabled_vimo)
        ),
        capsule=SETTINGS,
    )


def scene() -> NDArray[np.uint8]:
    image = np.zeros((120, 480, 3), dtype=np.uint8)
    image[:, 80:160] = bodies()
    # Weak centered signal in both views (between 1% and 5%).
    image[10:20, 180:190] = (0, 180, 180)
    image[70:80, 180:190] = (0, 180, 180)
    # Mixed PRESENT/EMPTY is UNKNOWN, not a skip.
    image[:60, 240:320] = bodies()[:60]
    # Neighbor signal excluded by the center, not a modified saved ROI.
    image[:, 320:332] = 255
    image[:, 400:] = 20
    return image


def cycle(config: ResolvedInspectionConfiguration, image: NDArray[np.uint8]):
    collector = CycleCollector(config.layout.camera_ids, timeout=100)
    collector.begin(CycleId(1))
    result = None
    for camera in reversed(config.layout.camera_ids):
        result = collector.accept(
            Frame(FrameId(CycleId(1), camera, 7), 0, image.copy())
        )
    assert result is not None
    return result


def no_findings(image: NDArray[np.uint8]) -> ImageResult:
    return replace(inferred(), image_shape=(image.shape[0], image.shape[1], 3))


@pytest.mark.parametrize(
    "size,color", [(False, False), (True, False), (False, True), (True, True)]
)
@pytest.mark.parametrize("vimo", [False, True])
def test_always_on_and_selective_skip_preserves_original_traditional_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, size: bool, color: bool, vimo: bool
) -> None:
    config = configuration(tmp_path, enabled_vimo=vimo)
    settings = CapsuleSettings(
        replace(SETTINGS.size, enabled=size), replace(SETTINGS.color, enabled=color)
    )
    config = replace(config, capsule=settings)
    image = scene()
    snapshot = cycle(config, image)
    seen: list[BoundingRect] = []
    calls = 0

    def measure(
        pixels: NDArray[np.uint8], roi: BoundingRect, settings: CapsuleSettings
    ):
        assert pixels.shape == (120, 480, 3) and not pixels.flags.writeable
        seen.append(roi)
        return measure_capsule(pixels, roi, settings)

    def infer(pixels: NDArray[np.uint8]) -> ImageResult:
        nonlocal calls
        calls += 1
        assert pixels.shape == (120, 480, 3)
        return no_findings(pixels)

    monkeypatch.setattr("capsule_vision.application.pipeline.measure_capsule", measure)
    result = (ConfiguredProductionPipeline(config).inspect(snapshot, infer)).production
    assert calls == (6 if vimo else 0)
    assert len(seen) == (18 if settings.enabled else 0)
    assert result.layout is config.layout
    assert tuple(c.camera_id for c in result.cameras) == config.layout.camera_ids
    expected = (
        OccupancyState.EMPTY,
        OccupancyState.PRESENT,
        OccupancyState.UNKNOWN,
        OccupancyState.UNKNOWN,
        OccupancyState.EMPTY,
        OccupancyState.EMPTY,
    )
    for camera in result.cameras:
        assert camera.frame_id is not None and camera.frame_id.sequence == 7
        assert camera.timing.occupancy_ms is not None
        for row, state, slot in zip(
            camera.channels, expected, camera.layout.slots, strict=True
        ):
            assert row.slot == slot.slot and row.channel_id == slot.channel_id
            assert row.occupancy is not None and row.occupancy.state is state
            check = row.checks[0]
            if settings.enabled and state is not OccupancyState.EMPTY:
                assert check == judge_capsule(
                    measure_capsule(image, slot.rect, settings), settings
                )
                assert row.traditional_skip is None
            else:
                assert check.assessment == Assessment(
                    TechnicalStatus.DISABLED, ProductJudgment.UNKNOWN
                )
                assert check.measurements == check.reasons == ()
                assert check.required is settings.enabled
                assert row.traditional_skip is (
                    TraditionalSkipReason.CONFIRMED_EMPTY if settings.enabled else None
                )
                assert row.assessment == Assessment(
                    TechnicalStatus.SUCCESS if vimo else TechnicalStatus.DISABLED,
                    ProductJudgment.OK if vimo else ProductJudgment.UNKNOWN,
                )
    np.testing.assert_array_equal(image, scene())


@pytest.mark.parametrize("count", [5, 6])
@pytest.mark.parametrize("mirrored", [False, True])
def test_layout_slots_supply_occupancy_and_traditional_rois(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, count: int, mirrored: bool
) -> None:
    config = configuration(tmp_path, count=count, mirrored=mirrored)
    last = config.layout.cameras[-1]
    last = replace(
        last,
        slots=tuple(
            replace(slot, rect=replace(slot.rect, y=1, height=119))
            for slot in last.slots
        ),
    )
    config = replace(
        config,
        layout=replace(config.layout, cameras=(*config.layout.cameras[:-1], last)),
    )
    image = scene()
    occupancy_rois: list[BoundingRect] = []
    traditional_rois: list[BoundingRect] = []

    def occupancy(pixels: NDArray[np.uint8], roi: BoundingRect):
        occupancy_rois.append(roi)
        return measure_occupancy(pixels, roi)

    def traditional(
        pixels: NDArray[np.uint8], roi: BoundingRect, settings: CapsuleSettings
    ):
        traditional_rois.append(roi)
        return measure_capsule(pixels, roi, settings)

    monkeypatch.setattr(
        "capsule_vision.application.pipeline.measure_occupancy", occupancy
    )
    monkeypatch.setattr(
        "capsule_vision.application.pipeline.measure_capsule", traditional
    )
    result = (
        ConfiguredProductionPipeline(config).inspect(cycle(config, image), no_findings)
    ).production
    expected = [slot.rect for camera in config.layout.cameras for slot in camera.slots]
    assert occupancy_rois == expected
    assert traditional_rois == [
        roi
        for roi in expected
        if measure_occupancy(image, roi).state is not OccupancyState.EMPTY
    ]
    assert result.layout is config.layout
    assert [row.channel_id for row in result.channels] == list(range(1, count * 6 + 1))
    for camera, layout in zip(result.cameras, config.layout.cameras, strict=True):
        assert camera.layout is layout
        assert camera.frame_id == FrameId(CycleId(1), layout.camera_id, 7)
        for row, slot in zip(camera.channels, layout.slots, strict=True):
            assert (row.camera_id, row.channel_id, row.slot) == (
                layout.camera_id,
                slot.channel_id,
                slot.slot,
            )
            assert row.occupancy == OccupancyResult(measure_occupancy(image, slot.rect))


def test_empty_keeps_accepted_vimo_ng_and_unassigned_findings(tmp_path: Path) -> None:
    config = configuration(tmp_path)
    image = np.zeros((120, 481, 3), dtype=np.uint8)
    original = replace(
        inferred(BoundingRect(1, 1, 4, 4), BoundingRect(480, 1, 1, 2)),
        image_shape=(120, 481, 3),
    )
    result = (
        ConfiguredProductionPipeline(config).inspect(
            cycle(config, image), lambda _: original
        )
    ).production
    for camera in result.cameras:
        first = camera.channels[0]
        assert (
            first.occupancy is not None
            and first.occupancy.state is OccupancyState.EMPTY
        )
        assert first.traditional_skip is TraditionalSkipReason.CONFIRMED_EMPTY
        assert first.assessment == Assessment(
            TechnicalStatus.SUCCESS, ProductJudgment.NG
        )
        assert first.vimo_findings[0].finding.label == "label-0"
        assert first.checks[1].reasons[0].code == "defect_region"
        assert len(camera.unassigned_findings) == 1
        assert camera.unassigned_findings[0].label == "label-1"
        assert all(not row.vimo_findings for row in camera.channels[1:])
    assert result.assessment.judgment is ProductJudgment.NG
    assert len(original.regions) == 2


@pytest.mark.parametrize("ng", [False, True])
def test_occupancy_exception_is_explicit_required_error_and_later_channels_continue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ng: bool
) -> None:
    config = replace(configuration(tmp_path), capsule=CapsuleSettings())
    count = 0

    def measure(image: NDArray[np.uint8], roi: BoundingRect):
        nonlocal count
        count += 1
        if count == 1:
            raise RuntimeError("one occupancy call failed")
        return measure_occupancy(image, roi)

    monkeypatch.setattr(
        "capsule_vision.application.pipeline.measure_occupancy", measure
    )
    image = scene()
    raw = replace(
        inferred(*((BoundingRect(1, 1, 4, 4),) if ng else ())),
        image_shape=(120, 480, 3),
    )
    result = (
        ConfiguredProductionPipeline(config).inspect(
            cycle(config, image), lambda _: raw
        )
    ).production
    assert count == 36
    first = result.channels[0]
    assert first.occupancy == OccupancyResult(
        error_code="occupancy.execution_error",
        exception_type="RuntimeError",
        message="one occupancy call failed",
    )
    assert first.occupancy is not None
    assert first.occupancy.state is OccupancyState.UNKNOWN
    assert first.traditional_skip is None
    assert first.assessment == Assessment(
        TechnicalStatus.ERROR, ProductJudgment.NG if ng else ProductJudgment.UNKNOWN
    )
    assert all(
        row.occupancy is not None and row.occupancy.evidence is not None
        for row in result.channels[1:]
    )


@pytest.mark.parametrize("shutdown", [False, True])
def test_incomplete_cycles_have_unavailable_occupancy_without_any_algorithm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shutdown: bool
) -> None:
    config = configuration(tmp_path)
    now = [0.0]
    collector = CycleCollector(
        config.layout.camera_ids, timeout=1, clock=lambda: now[0]
    )
    collector.begin(CycleId(1))
    collector.accept(
        Frame(FrameId(CycleId(1), config.layout.camera_ids[0], 0), 0, scene())
    )
    if shutdown:
        snapshot = collector.shutdown()[0]
    else:
        now[0] = 1
        snapshot = collector.expire()[0]

    def forbidden(*args: object) -> ImageResult:
        pytest.fail("Incomplete cycles must not run algorithms")

    monkeypatch.setattr(
        "capsule_vision.application.pipeline.measure_occupancy", forbidden
    )
    monkeypatch.setattr(
        "capsule_vision.application.pipeline.measure_capsule", forbidden
    )
    result = (
        ConfiguredProductionPipeline(config).inspect(snapshot, forbidden)
    ).production
    assert result.assessment == Assessment(
        TechnicalStatus.ERROR, ProductJudgment.UNKNOWN
    )
    assert len(result.channels) == 36
    assert all(
        row.occupancy == OccupancyResult(error_code="occupancy.result_unavailable")
        for row in result.channels
    )
    assert all(row.traditional_skip is None for row in result.channels)
    assert all(camera.timing.occupancy_ms is None for camera in result.cameras)


def test_skip_provenance_cannot_mask_success_or_missing_evidence(
    tmp_path: Path,
) -> None:
    config = configuration(tmp_path)
    result = (
        ConfiguredProductionPipeline(config).inspect(
            cycle(config, scene()), no_findings
        )
    ).production
    empty = result.channels[0]
    with pytest.raises(ValueError, match="EMPTY skip"):
        replace(empty, occupancy=None)
    with pytest.raises(ValueError, match="EMPTY skip"):
        replace(empty, checks=result.channels[1].checks)


def test_ordinary_traditional_failure_remains_error_and_does_not_stop_later_channels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = configuration(tmp_path)
    seen = 0

    def measure(image: NDArray[np.uint8], roi: BoundingRect, settings: CapsuleSettings):
        nonlocal seen
        seen += 1
        if seen == 1:
            raise ValueError("ordinary capsule failure")
        return measure_capsule(image, roi, settings)

    monkeypatch.setattr("capsule_vision.application.pipeline.measure_capsule", measure)
    result = (
        ConfiguredProductionPipeline(config).inspect(
            cycle(config, scene()), no_findings
        )
    ).production
    assert seen == 18
    row = result.channels[1]
    assert row.assessment == Assessment(TechnicalStatus.ERROR, ProductJudgment.UNKNOWN)
    assert row.checks[0].reasons[0].code == "capsule.execution_error"
    assert (
        result.cameras[-1].channels[1].checks[0].assessment.technical_status
        is TechnicalStatus.SUCCESS
    )
