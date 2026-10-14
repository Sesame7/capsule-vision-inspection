"""Always-on evidence, explicit EMPTY skip, unchanged ViMo and traditional checks."""

from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from numpy.typing import NDArray
from tests.application_support import prepared
from tests.capsule_support import SETTINGS, bodies
from tests.production_support import inferred

from vimo_demo.application import ConfiguredProductionPipeline
from vimo_demo.configuration import ResolvedInspectionConfiguration
from vimo_demo.inference import BoundingRect, ImageResult
from vimo_demo.inspection import CycleCollector, CycleId, Frame, FrameId
from vimo_demo.inspection.production import (
    Assessment,
    OccupancyResult,
    OccupancyState,
    ProductJudgment,
    TechnicalStatus,
    TraditionalSkipReason,
    measure_occupancy,
)
from vimo_demo.inspection.production.capsule import (
    CapsuleSettings,
    judge_capsule,
    measure_capsule,
)


def configuration(tmp_path: Path, *, enabled_vimo: bool = True):
    config = prepared(tmp_path).configuration
    layout = replace(
        config.layout,
        cameras=tuple(
            replace(
                camera,
                slots=tuple(
                    replace(slot, rect=BoundingRect(slot.slot * 80, 0, 80, 120))
                    for slot in camera.slots
                ),
            )
            for camera in config.layout.cameras
        ),
    )
    return replace(
        config,
        layout=layout,
        channels=tuple(
            replace(
                channel,
                roi=layout.cameras[(channel.channel_id - 1) // 6]
                .slots[channel.slot]
                .rect,
                checks=replace(
                    channel.checks,
                    vimo=replace(channel.checks.vimo, enabled=enabled_vimo),
                ),
            )
            for channel in config.channels
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

    monkeypatch.setattr("vimo_demo.application.pipeline.measure_capsule", measure)
    _, result = ConfiguredProductionPipeline(config).inspect(snapshot, infer)
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


def test_empty_keeps_accepted_vimo_ng_and_unassigned_findings(tmp_path: Path) -> None:
    config = configuration(tmp_path)
    image = np.zeros((120, 481, 3), dtype=np.uint8)
    original = replace(
        inferred(BoundingRect(1, 1, 4, 4), BoundingRect(480, 1, 1, 2)),
        image_shape=(120, 481, 3),
    )
    _, result = ConfiguredProductionPipeline(config).inspect(
        cycle(config, image), lambda _: original
    )
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

    monkeypatch.setattr("vimo_demo.application.pipeline.measure_occupancy", measure)
    image = scene()
    raw = replace(
        inferred(*((BoundingRect(1, 1, 4, 4),) if ng else ())),
        image_shape=(120, 480, 3),
    )
    _, result = ConfiguredProductionPipeline(config).inspect(
        cycle(config, image), lambda _: raw
    )
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

    monkeypatch.setattr("vimo_demo.application.pipeline.measure_occupancy", forbidden)
    monkeypatch.setattr("vimo_demo.application.pipeline.measure_capsule", forbidden)
    _, result = ConfiguredProductionPipeline(config).inspect(snapshot, forbidden)
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
    _, result = ConfiguredProductionPipeline(config).inspect(
        cycle(config, scene()), no_findings
    )
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

    monkeypatch.setattr("vimo_demo.application.pipeline.measure_capsule", measure)
    _, result = ConfiguredProductionPipeline(config).inspect(
        cycle(config, scene()), no_findings
    )
    assert seen == 18
    row = result.channels[1]
    assert row.assessment == Assessment(TechnicalStatus.ERROR, ProductJudgment.UNKNOWN)
    assert row.checks[0].reasons[0].code == "capsule.execution_error"
    assert (
        result.cameras[-1].channels[1].checks[0].assessment.technical_status
        is TechnicalStatus.SUCCESS
    )
