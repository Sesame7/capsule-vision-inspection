"""JSON -> resolved layout/policies -> existing Phase 2A domain, without GPU."""

from dataclasses import replace
from pathlib import Path

from tests.unit.test_production_inspection import inferred, outcome

from vimo_demo.configuration import (
    ModelRegistry,
    ViMoCheckConfig,
    load_machine_layout,
    load_model_definition,
    load_recipe,
    resolve_configuration,
)
from vimo_demo.inference import BoundingRect
from vimo_demo.inspection import CameraId, CycleId, FrameId
from vimo_demo.inspection.production import (
    Assessment,
    CheckPolicy,
    CheckResult,
    FindingSource,
    ProductionPipeline,
    ProductJudgment,
    Reason,
    TechnicalStatus,
    aggregate,
    extract_vimo_findings,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "configuration"


def test_json_resolution_supplies_layout_and_exact_policies() -> None:
    registry = ModelRegistry(
        tuple(
            load_model_definition(FIXTURES / name)
            for name in ("special.json", "general.json")
        )
    )
    base = load_machine_layout(FIXTURES / "machine.json")
    defaults = resolve_configuration(
        base, load_recipe(FIXTURES / "defaults.json"), registry, "general"
    )
    overrides = resolve_configuration(
        base, load_recipe(FIXTURES / "overrides.json"), registry, "general"
    )
    assert (
        defaults.model.model_id == "general" and overrides.model.model_id == "special"
    )
    assert defaults.layout == base.layout
    assert len(overrides.channels) == 36
    ordinary, camera_override, special = (
        overrides.channels[0],
        overrides.channels[12],
        overrides.channels[16],
    )
    assert (
        ordinary.checks.vimo.required,
        camera_override.checks.vimo.required,
        special.checks.vimo.required,
    ) == (True, False, True)
    assert special.roi == BoundingRect(40, 0, 8, 10)
    assert special.checks.vimo.policy == CheckPolicy(True, True)
    assert not special.checks.vimo.label_enabled("黑点")
    assert ordinary.checks.vimo.label_enabled("黑点")
    # For uniform policies this is already directly consumable by Phase 2A.
    # Per-channel heterogeneous composition is intentionally not activated here.
    pipeline = ProductionPipeline(
        defaults.layout,
        vimo=ordinary.checks.vimo.policy,
    )
    camera = pipeline.camera_result(
        defaults.layout.cameras[0], outcome(image=inferred(BoundingRect(1, 1, 2, 2)))
    )
    assert camera.assessment == Assessment(TechnicalStatus.SUCCESS, ProductJudgment.NG)
    assert camera.channels[0].assessment.judgment is ProductJudgment.NG
    assert len(camera.assigned_findings) == 1


def test_label_filter_is_explicit_and_does_not_mutate_inference_evidence() -> None:
    raw = inferred(BoundingRect(1, 1, 2, 2), BoundingRect(11, 1, 2, 2))
    raw = replace(
        raw,
        regions=(
            replace(raw.regions[0], label="黑点"),
            replace(raw.regions[1], label="未知类别"),
        ),
    )
    regions, mask, label_map = raw.regions, raw.mask, raw.label_map
    findings = extract_vimo_findings(FrameId(CycleId(1), CameraId("CAM1"), 0), raw)
    no_filter = ViMoCheckConfig()
    filtered = ViMoCheckConfig(disabled_labels=("黑点",))
    assert tuple(f for f in findings if no_filter.label_enabled(f.label)) == findings
    assert tuple(f.label for f in findings if filtered.label_enabled(f.label)) == (
        "未知类别",
    )
    assert not ViMoCheckConfig(enabled=False).label_enabled("未知类别")
    assert raw.regions is regions and raw.mask is mask and raw.label_map is label_map
    assert [f.label for f in findings] == ["黑点", "未知类别"]


def test_resolved_optional_policy_preserves_existing_aggregation() -> None:
    registry = ModelRegistry((load_model_definition(FIXTURES / "general.json"),))
    config = resolve_configuration(
        load_machine_layout(FIXTURES / "machine.json"),
        load_recipe(FIXTURES / "defaults.json"),
        registry,
        "general",
    )
    policy = replace(config.channels[0].checks.vimo, required=False).policy
    failed = CheckResult(
        FindingSource.VIMO,
        Assessment(TechnicalStatus.ERROR, ProductJudgment.UNKNOWN),
        (Reason(FindingSource.VIMO, "execution_error"),),
        required=policy.required,
    )
    successful = CheckResult(
        FindingSource.TRADITIONAL,
        Assessment(TechnicalStatus.SUCCESS, ProductJudgment.OK),
    )
    assert aggregate((failed, successful)) == Assessment(
        TechnicalStatus.SUCCESS, ProductJudgment.OK
    )
    ng = replace(
        failed,
        assessment=Assessment(TechnicalStatus.SUCCESS, ProductJudgment.NG),
        reasons=(Reason(FindingSource.VIMO, "defect_region", "黑点"),),
    )
    assert aggregate((ng, successful)) == Assessment(
        TechnicalStatus.SUCCESS, ProductJudgment.NG
    )
