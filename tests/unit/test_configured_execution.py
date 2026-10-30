"""Resolved policies reach execution, without calibrated algorithms or GPU."""

from dataclasses import replace
from pathlib import Path
from weakref import ref

import numpy as np
import pytest
from numpy.typing import NDArray
from tests.support.application import collected, config
from tests.support.production import inferred

from capsule_vision.application import (
    ConfiguredProductionPipeline,
    ProductionCounters,
)
from capsule_vision.inference import BoundingRect, ImageResult
from capsule_vision.inspection import (
    CycleCollector,
    CycleId,
    CycleStatus,
    FrameId,
)
from capsule_vision.inspection.production import (
    Assessment,
    ConfiguredCycleCompletion,
    ProductJudgment,
    TechnicalStatus,
    extract_vimo_findings,
)
from capsule_vision.inspection.production.capsule import CapsuleSettings


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("excluded", [(), ("defect",), ("Defect",)])
@pytest.mark.parametrize("count", [5, 6])
def test_shared_vimo_policy_and_exact_label_filtering(
    tmp_path: Path, enabled: bool, excluded: tuple[str, ...], count: int
) -> None:
    configuration = config(tmp_path, count)
    configuration = replace(
        configuration,
        checks=replace(
            configuration.checks,
            vimo=replace(
                configuration.checks.vimo, enabled=enabled, disabled_labels=excluded
            ),
        ),
    )
    calls: list[int] = []
    raw = inferred(
        BoundingRect(1, 1, 2, 2), BoundingRect(11, 1, 2, 2), BoundingRect(21, 1, 2, 2)
    )
    raw = replace(
        raw,
        regions=(
            replace(raw.regions[0], label="defect"),
            replace(raw.regions[1], label="Defect"),
            replace(raw.regions[2], label="未知类别"),
        ),
    )
    mask, regions, label_map = raw.mask, raw.regions, raw.label_map

    def infer(image: NDArray[np.uint8]) -> ImageResult:
        calls.append(int(image[0, 0, 0]))
        return raw

    cycle = collected(configuration)
    findings = extract_vimo_findings(cycle.frames[0].frame.id, raw)
    assert tuple(f.label for f in findings) == ("defect", "Defect", "未知类别")
    assert tuple(f.region_index for f in findings) == (0, 1, 2)
    assert all(f.frame_id == cycle.frames[0].frame.id for f in findings)
    assert configuration.checks.vimo.label_enabled("未知类别") is enabled
    completion = ConfiguredProductionPipeline(configuration).inspect(cycle, infer)
    raw_cycle, result = completion.raw, completion.production
    assert calls == (list(range(count)) if enabled else [])
    assert (
        tuple(camera.camera_id for camera in result.cameras)
        == configuration.layout.camera_ids
    )
    assert tuple(row.channel_id for row in result.channels) == tuple(
        range(1, count * 6 + 1)
    )
    for index, camera in enumerate(result.cameras):
        assert tuple(row.channel_id for row in camera.channels) == tuple(
            range(index * 6 + 1, index * 6 + 7)
        )
        assert result.channels[index * 6] is camera.channels[0]
    for frame, camera in zip(raw_cycle.frames, result.cameras, strict=True):
        assert (
            frame.frame_id
            == camera.frame_id
            == FrameId(cycle.cycle_id, camera.camera_id, 0)
        )
        assert frame.result is (raw if enabled else None)
        assert tuple(item.finding for item in camera.assigned_findings) == tuple(
            replace(f, frame_id=frame.frame_id)
            for f in findings
            if configuration.checks.vimo.label_enabled(f.label)
        )
    for row in result.channels:
        label = ("defect", "Defect", "未知类别")[row.slot] if row.slot < 3 else None
        ng = enabled and label is not None and label not in excluded
        assert row.assessment.judgment is (
            ProductJudgment.NG
            if ng
            else ProductJudgment.OK
            if enabled
            else ProductJudgment.UNKNOWN
        )
        assert len(row.vimo_findings) == int(ng)
        assert row.checks[1].assessment.technical_status is (
            TechnicalStatus.SUCCESS if enabled else TechnicalStatus.DISABLED
        )
    assert raw.mask is mask and raw.regions is regions and raw.label_map is label_map


@pytest.mark.parametrize("accept", [False, True])
def test_unassigned_policy_and_cycle_ng_without_channel_ng(
    tmp_path: Path, accept: bool
) -> None:
    configuration = config(tmp_path)
    configuration = replace(
        configuration,
        checks=replace(
            configuration.checks,
            vimo=replace(
                configuration.checks.vimo, disabled_labels=() if accept else ("黑点",)
            ),
        ),
    )
    raw = inferred(BoundingRect(58, 15, 2, 2))
    raw = replace(raw, regions=(replace(raw.regions[0], label="黑点"),))
    result = (
        ConfiguredProductionPipeline(configuration).inspect(
            collected(configuration), lambda _: raw
        )
    ).production
    assert len(result.unassigned_findings) == (6 if accept else 0)
    assert {finding.frame_id.camera_id for finding in result.unassigned_findings} == (
        set(configuration.layout.camera_ids) if accept else set()
    )
    counters = ProductionCounters().admitted().completed(result)
    assert counters.production_ng_cycles == int(accept)
    assert counters.channel_ng == 0 and counters.channel_ok == 36


@pytest.mark.parametrize("required", [False, True])
@pytest.mark.parametrize("enabled", [False, True])
def test_inference_failure_uses_shared_enabled_required_policy(
    tmp_path: Path, required: bool, enabled: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    configuration = config(tmp_path)
    configuration = replace(
        configuration,
        checks=replace(
            configuration.checks,
            vimo=replace(configuration.checks.vimo, enabled=enabled, required=required),
        ),
    )
    from tests.support.capsule_images import ROI, SETTINGS, bodies

    from capsule_vision.inspection.production.capsule import measure_capsule

    measured = measure_capsule(bodies(), ROI, SETTINGS)

    def measure(image: NDArray[np.uint8], roi: BoundingRect, settings: CapsuleSettings):
        return measured

    monkeypatch.setattr("capsule_vision.application.pipeline.measure_capsule", measure)
    configuration = replace(configuration, capsule=SETTINGS)
    calls = 0

    def infer(image: NDArray[np.uint8]) -> ImageResult:
        nonlocal calls
        calls += 1
        raise RuntimeError("ordinary inference failure")

    completion = ConfiguredProductionPipeline(configuration).inspect(
        collected(configuration), infer
    )
    raw, result = completion.raw, completion.production
    assert calls == (6 if enabled else 0)
    expected_status = CycleStatus.FAILED if enabled else CycleStatus.COMPLETE
    assert raw.status is result.cycle_status is expected_status
    assert result.assessment.technical_status is (
        TechnicalStatus.ERROR if enabled and required else TechnicalStatus.SUCCESS
    )
    assert result.assessment.judgment is (
        ProductJudgment.UNKNOWN if enabled and required else ProductJudgment.OK
    )
    assert all(
        row.checks[1].assessment.technical_status
        is (TechnicalStatus.ERROR if enabled else TechnicalStatus.DISABLED)
        for row in result.channels
    )


@pytest.mark.parametrize("invalid", ["shape", "bounds"])
def test_invalid_sdk_evidence_is_not_filtered_into_success(
    tmp_path: Path, invalid: str
) -> None:
    configuration = config(tmp_path)
    configuration = replace(
        configuration,
        checks=replace(
            configuration.checks,
            vimo=replace(configuration.checks.vimo, disabled_labels=("defect",)),
        ),
    )
    raw = inferred(BoundingRect(60, 1, 2, 2))
    raw = replace(raw, regions=(replace(raw.regions[0], label="defect"),))
    if invalid == "shape":
        raw = replace(raw, image_shape=(1, 1, 3))
    regions, mask, label_map = raw.regions, raw.mask, raw.label_map
    calls = 0

    def infer(image: NDArray[np.uint8]) -> ImageResult:
        nonlocal calls
        calls += 1
        return raw

    completion = ConfiguredProductionPipeline(configuration).inspect(
        collected(configuration), infer
    )
    observed, result = completion.raw, completion.production
    assert calls == 6 and len(observed.frames) == 6
    assert observed.status is (
        CycleStatus.FAILED if invalid == "shape" else CycleStatus.COMPLETE
    )
    for row in result.channels:
        assert row.checks[1].assessment == Assessment(
            TechnicalStatus.ERROR, ProductJudgment.UNKNOWN
        )
        assert row.checks[1].reasons[0].code == (
            "execution_error" if invalid == "shape" else "invalid_result"
        )
        assert not row.vimo_findings
    assert raw.regions is regions and raw.mask is mask and raw.label_map is label_map


def test_incomplete_cycle_never_executes_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configuration = config(tmp_path)
    now = [0.0]
    collector = CycleCollector(
        configuration.layout.camera_ids, timeout=1, clock=lambda: now[0]
    )
    collector.begin(CycleId(1))
    now[0] = 1
    cycle = collector.expire()[0]

    def forbidden(image: NDArray[np.uint8]) -> ImageResult:
        pytest.fail("Incomplete cycle must not infer")

    from tests.support.capsule_images import SETTINGS

    monkeypatch.setattr(
        "capsule_vision.application.pipeline.measure_capsule", forbidden
    )
    configuration = replace(configuration, capsule=SETTINGS)
    result = (
        ConfiguredProductionPipeline(configuration).inspect(cycle, forbidden)
    ).production
    assert result.cycle_status is CycleStatus.TIMED_OUT
    assert len(result.channels) == 36
    assert all(
        row.assessment == Assessment(TechnicalStatus.ERROR, ProductJudgment.UNKNOWN)
        for row in result.channels
    )
    assert all(camera.collection_issue is not None for camera in result.cameras)
    assert result.assessment == Assessment(
        TechnicalStatus.ERROR, ProductJudgment.UNKNOWN
    )


def test_completion_rejects_mismatched_raw_identity_or_status(tmp_path: Path) -> None:
    configuration = config(tmp_path)
    completion = ConfiguredProductionPipeline(configuration).inspect(
        collected(configuration), lambda _: inferred()
    )
    for raw in (
        replace(completion.raw, cycle_id=CycleId(2)),
        replace(completion.raw, status=CycleStatus.FAILED),
    ):
        with pytest.raises(ValueError, match="cycle identity/status"):
            ConfiguredCycleCompletion(raw, completion.production)


def test_production_retains_evidence_without_raw_masks_or_source_pixels(
    tmp_path: Path,
) -> None:
    configuration = config(tmp_path)
    cycle = collected(configuration)
    pixels = ref(cycle.frames[0].frame.image)
    image = inferred(BoundingRect(1, 1, 2, 2))
    mask = ref(image.mask)
    completion = ConfiguredProductionPipeline(configuration).inspect(
        cycle, lambda _, result=image: result
    )
    result = completion.production
    assert all(row.result is image for row in completion.raw.frames)
    del image, cycle, completion
    assert pixels() is None and mask() is None
    assert result.channels[0].assessment.judgment is ProductJudgment.NG
