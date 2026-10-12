"""Resolved policies reach execution, without calibrated algorithms or GPU."""

from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from numpy.typing import NDArray
from tests.application_support import FIXTURES, prepared
from tests.production_support import inferred

from vimo_demo.application import (
    ConfiguredProductionPipeline,
    ProductionCounters,
)
from vimo_demo.configuration import (
    ModelRegistry,
    ResolvedInspectionConfiguration,
    load_machine_layout,
    load_recipe,
    resolve_configuration,
)
from vimo_demo.inference import BoundingRect, ImageResult
from vimo_demo.inspection import (
    CollectedCycle,
    CycleCollector,
    CycleId,
    CycleStatus,
    Frame,
    FrameId,
)
from vimo_demo.inspection.production import (
    Assessment,
    ProductJudgment,
    TechnicalStatus,
)
from vimo_demo.inspection.production.capsule import CapsuleSettings


def config(tmp_path: Path) -> ResolvedInspectionConfiguration:
    metadata = prepared(tmp_path).configuration.model
    registry = ModelRegistry((metadata, replace(metadata, model_id="special")))
    return resolve_configuration(
        load_machine_layout(FIXTURES / "machine.json"),
        load_recipe(FIXTURES / "overrides.json"),
        registry,
        "general",
    )


def collected(configuration: ResolvedInspectionConfiguration) -> CollectedCycle:
    collector = CycleCollector(configuration.layout.camera_ids, timeout=100)
    collector.begin(CycleId(1))
    cycle = None
    for index, camera in enumerate(configuration.layout.cameras):
        cycle = collector.accept(
            Frame(
                FrameId(CycleId(1), camera.camera_id, 0),
                0,
                np.full((20, 60, 3), index, dtype=np.uint8),
            )
        )
    assert cycle is not None
    return cycle


def test_vimo_skip_camera_and_same_finding_filtered_per_channel(tmp_path: Path) -> None:
    configuration = config(tmp_path)
    rows = tuple(
        replace(
            row,
            checks=replace(row.checks, vimo=replace(row.checks.vimo, enabled=False)),
        )
        if row.camera_id == "CAM1"
        else row
        for row in configuration.channels
    )
    configuration = replace(configuration, channels=rows)
    calls: list[int] = []
    raw = inferred(BoundingRect(41, 1, 16, 5))
    raw = replace(raw, regions=(replace(raw.regions[0], label="黑点"),))
    mask, regions = raw.mask, raw.regions

    def infer(image: NDArray[np.uint8]) -> ImageResult:
        calls.append(int(image[0, 0, 0]))
        return raw if image[0, 0, 0] == 2 else inferred()

    _, result = ConfiguredProductionPipeline(configuration).inspect(
        collected(configuration), infer
    )
    assert calls == [1, 2, 3, 4, 5]
    assert result.channels[16].assessment.judgment is ProductJudgment.OK
    assert result.channels[16].vimo_findings == ()
    assert result.channels[17].assessment.judgment is ProductJudgment.NG
    assert len(result.channels[17].vimo_findings) == 1
    assert raw.mask is mask and raw.regions is regions
    assert all(
        row.checks[1].assessment.technical_status is TechnicalStatus.DISABLED
        for row in result.channels[:6]
    )


@pytest.mark.parametrize("accept", [False, True])
def test_unassigned_policy_and_cycle_ng_without_channel_ng(
    tmp_path: Path, accept: bool
) -> None:
    configuration = config(tmp_path)
    rows = tuple(
        replace(
            row,
            checks=replace(
                row.checks,
                vimo=replace(
                    row.checks.vimo,
                    disabled_labels=() if accept and row.channel_id == 1 else ("黑点",),
                ),
            ),
        )
        for row in configuration.channels
    )
    configuration = replace(configuration, channels=rows)
    raw = inferred(BoundingRect(58, 15, 2, 2))
    raw = replace(raw, regions=(replace(raw.regions[0], label="黑点"),))
    _, result = ConfiguredProductionPipeline(configuration).inspect(
        collected(configuration), lambda _: raw
    )
    assert len(result.unassigned_findings) == int(accept)
    counters = ProductionCounters().admitted().completed(result)
    assert counters.production_ng_cycles == int(accept)
    assert counters.channel_ng == 0 and counters.channel_ok == 36


@pytest.mark.parametrize("required", [False, True])
def test_inference_failure_uses_each_enabled_required_policy(
    tmp_path: Path, required: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    configuration = config(tmp_path)
    rows = tuple(
        replace(
            row,
            checks=replace(
                row.checks,
                vimo=replace(
                    row.checks.vimo, enabled=row.channel_id != 1, required=required
                ),
            ),
        )
        for row in configuration.channels
    )
    configuration = replace(configuration, channels=rows)
    from tests.capsule_support import ROI, SETTINGS, bodies

    from vimo_demo.inspection.production.capsule import measure_capsule

    measured = measure_capsule(bodies(), ROI, SETTINGS)

    def measure(image: NDArray[np.uint8], roi: BoundingRect, settings: CapsuleSettings):
        return measured

    monkeypatch.setattr("vimo_demo.application.pipeline.measure_capsule", measure)
    configuration = replace(configuration, capsule=SETTINGS)
    calls = 0

    def infer(image: NDArray[np.uint8]) -> ImageResult:
        nonlocal calls
        calls += 1
        raise RuntimeError("ordinary inference failure")

    raw, result = ConfiguredProductionPipeline(configuration).inspect(
        collected(configuration), infer
    )
    assert calls == 6 and raw.status is CycleStatus.FAILED
    assert result.cycle_status is CycleStatus.FAILED
    assert result.channels[0].assessment == Assessment(
        TechnicalStatus.SUCCESS, ProductJudgment.OK
    )
    assert result.assessment.technical_status is (
        TechnicalStatus.ERROR if required else TechnicalStatus.SUCCESS
    )
    assert result.assessment.judgment is (
        ProductJudgment.UNKNOWN if required else ProductJudgment.OK
    )
    assert all(
        row.checks[1].assessment.technical_status is TechnicalStatus.ERROR
        for row in result.channels[1:]
    )


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

    from tests.capsule_support import SETTINGS

    monkeypatch.setattr("vimo_demo.application.pipeline.measure_capsule", forbidden)
    configuration = replace(configuration, capsule=SETTINGS)
    _, result = ConfiguredProductionPipeline(configuration).inspect(cycle, forbidden)
    assert result.cycle_status is CycleStatus.TIMED_OUT
    assert result.assessment == Assessment(
        TechnicalStatus.ERROR, ProductJudgment.UNKNOWN
    )


def test_resolved_vimo_required_overrides_drive_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.capsule_support import ROI, SETTINGS, bodies

    from vimo_demo.inspection.production.capsule import measure_capsule

    measured = measure_capsule(bodies(), ROI, SETTINGS)

    def measure(image: NDArray[np.uint8], roi: BoundingRect, settings: CapsuleSettings):
        return measured

    monkeypatch.setattr("vimo_demo.application.pipeline.measure_capsule", measure)
    configuration = replace(config(tmp_path), capsule=SETTINGS)
    calls = 0

    def infer(image: NDArray[np.uint8]) -> ImageResult:
        nonlocal calls
        calls += 1
        raise RuntimeError("synthetic inference failure")

    _, result = ConfiguredProductionPipeline(configuration).inspect(
        collected(configuration), infer
    )
    assert calls == 6
    assert result.channels[0].assessment == Assessment(
        TechnicalStatus.ERROR, ProductJudgment.UNKNOWN
    )
    assert result.channels[12].assessment == Assessment(
        TechnicalStatus.SUCCESS, ProductJudgment.OK
    )
    assert result.channels[16].assessment == Assessment(
        TechnicalStatus.ERROR, ProductJudgment.UNKNOWN
    )
    assert [result.channels[i].checks[1].required for i in (0, 12, 16)] == [
        True,
        False,
        True,
    ]
