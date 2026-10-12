"""Deterministic demonstration checks, not industrial calibration."""

from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from numpy.typing import NDArray
from tests.capsule_support import ROI, SETTINGS, bodies
from tests.production_support import inferred
from tests.unit.test_configuration import machine, registry
from tests.unit.test_configured_execution import collected, config

from vimo_demo.application import ConfiguredProductionPipeline
from vimo_demo.configuration import (
    RecipeDefinition,
    load_recipe,
    resolve_configuration,
    save_recipe,
)
from vimo_demo.inference import BoundingRect
from vimo_demo.inspection.production import ProductJudgment, TechnicalStatus
from vimo_demo.inspection.production.capsule import (
    CapsuleSettings,
    ColorSettings,
    SizeSettings,
    hue_distance,
    judge_capsule,
    measure_capsule,
    median_hue,
)
from vimo_demo.ui.recipe_editor import RecipeDraft


def test_two_bodies_dimensions_coordinates_ownership() -> None:
    image = bodies()
    before = image.copy()
    padded = np.pad(image, ((4, 6), (3, 5), (0, 0)))
    measured = measure_capsule(padded, BoundingRect(3, 4, 80, 120), SETTINGS)
    assert [v.body.bounding_rect if v.body else None for v in measured.views] == [
        BoundingRect(23, 14, 20, 30),
        BoundingRect(23, 74, 20, 30),
    ]
    assert [v.color.hue if v.color else None for v in measured.views] == [30, 30]
    assert judge_capsule(measured, SETTINGS).assessment.judgment is ProductJudgment.OK
    assert np.array_equal(image, before)


@pytest.mark.parametrize(
    "kind, reason",
    [
        ("missing", "missing_body"),
        ("competing", "competing_bodies"),
        ("truncated", "truncated_body"),
    ],
)
def test_content_rejection_is_ng_not_error(kind: str, reason: str) -> None:
    image = bodies()
    if kind == "missing":
        image[60:] = 0
    elif kind == "competing":
        image[10:40, 50:70] = (0, 180, 180)
    else:
        image[0:10, 20:40] = (0, 180, 180)
    measured = measure_capsule(image, ROI, SETTINGS)
    invalid = [v for v in measured.views if v.body is None]
    assert len(invalid) == 1 and invalid[0].invalid_reason == reason
    result = judge_capsule(measured, SETTINGS)
    assert result.assessment.technical_status is TechnicalStatus.SUCCESS
    assert result.assessment.judgment is ProductJudgment.NG
    assert len([m for m in result.measurements if m.name.endswith(".width")]) == 1


def test_particles_and_dark_edges_do_not_distort_body() -> None:
    image = bodies(brightness=90)
    image[10:40, 20] = (0, 30, 30)
    image[5:7, 50:52] = (255, 255, 255)
    image[75:77, 60:62] = (255, 255, 255)
    result = judge_capsule(measure_capsule(image, ROI, SETTINGS), SETTINGS)
    assert result.assessment.judgment is ProductJudgment.OK


@pytest.mark.parametrize(
    "target,tolerance,ok", [(19, 1, True), (21, 1, True), (18, 1, False), (20, 0, True)]
)
def test_size_inclusive_limits(target: int, tolerance: int, ok: bool) -> None:
    settings = CapsuleSettings(SizeSettings(True, target, tolerance, 30, 0))
    result = judge_capsule(measure_capsule(bodies(), ROI, SETTINGS), settings)
    assert (result.assessment.judgment is ProductJudgment.OK) == ok
    tall = replace(
        settings, size=replace(settings.size, target_height=28, height_tolerance=1)
    )
    assert (
        judge_capsule(
            measure_capsule(bodies(), ROI, SETTINGS), tall
        ).assessment.judgment
        is ProductJudgment.NG
    )


def test_circular_hue_and_color_unusable() -> None:
    assert hue_distance(179, 1) == 2
    assert median_hue(np.array([179, 0, 1], dtype=np.float64)) == pytest.approx(
        0, abs=1e-10
    )
    measured = measure_capsule(bodies(179), ROI, SETTINGS)
    for tolerance, expected in [(2, ProductJudgment.OK), (1, ProductJudgment.NG)]:
        settings = CapsuleSettings(color=ColorSettings(True, 1, tolerance))
        assert judge_capsule(measured, settings).assessment.judgment is expected
    gray = bodies()
    gray[gray.max(axis=2) > 0] = 100
    measured = measure_capsule(gray, ROI, SETTINGS)
    assert all(v.body and v.color and v.color.hue is None for v in measured.views)
    assert judge_capsule(measured, SETTINGS).assessment.judgment is ProductJudgment.NG
    assert (
        judge_capsule(measured, CapsuleSettings(size=SETTINGS.size)).assessment.judgment
        is ProductJudgment.OK
    )


@pytest.mark.parametrize(
    "size,color", [(False, False), (True, False), (False, True), (True, True)]
)
def test_independent_enabled(size: bool, color: bool) -> None:
    settings = CapsuleSettings(
        replace(SETTINGS.size, enabled=size), replace(SETTINGS.color, enabled=color)
    )
    result = judge_capsule(measure_capsule(bodies(), ROI, SETTINGS), settings)
    assert result.assessment.technical_status is (
        TechnicalStatus.SUCCESS if size or color else TechnicalStatus.DISABLED
    )


def test_invalid_roi_is_execution_failure() -> None:
    with pytest.raises(ValueError, match="bounds"):
        measure_capsule(bodies(), BoundingRect(60, 0, 80, 120), SETTINGS)


def test_recipe_roundtrip_preserves_vimo(tmp_path: Path) -> None:
    from vimo_demo.configuration import CheckConfiguration, ViMoCheckConfig

    recipe = RecipeDefinition(
        "capsule",
        "Capsule",
        defaults=CheckConfiguration(vimo=ViMoCheckConfig(True, False, ("黑点",))),
        capsule=SETTINGS,
    )
    draft = RecipeDraft.from_recipe(recipe)
    assert draft.to_recipe() == recipe
    path = tmp_path / "capsule.recipe.json"
    save_recipe(path, recipe)
    assert load_recipe(path) == recipe
    resolved = resolve_configuration(
        machine(), load_recipe(path), registry(), "general"
    )
    assert resolved.capsule == SETTINGS
    assert all(row.checks.vimo == recipe.defaults.vimo for row in resolved.channels)


@pytest.mark.parametrize(
    "size,color", [(False, False), (True, False), (False, True), (True, True)]
)
def test_production_shared_measurement_and_disabled_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, size: bool, color: bool
) -> None:
    import vimo_demo.application.pipeline as pipeline

    configuration = config(tmp_path)
    measured = measure_capsule(bodies(), ROI, SETTINGS)
    calls: list[BoundingRect] = []

    def measure(image: NDArray[np.uint8], roi: BoundingRect, settings: CapsuleSettings):
        assert settings.size.enabled is size and settings.color.enabled is color
        calls.append(roi)
        return measured

    monkeypatch.setattr(pipeline, "measure_capsule", measure)
    settings = CapsuleSettings(
        replace(SETTINGS.size, enabled=size), replace(SETTINGS.color, enabled=color)
    )
    selected = replace(configuration, capsule=settings)
    _, result = ConfiguredProductionPipeline(selected).inspect(
        collected(selected), lambda _: inferred()
    )
    assert result.assessment.judgment is ProductJudgment.OK
    assert len(calls) == (36 if size or color else 0)
    expected = judge_capsule(measured, settings)
    if not settings.enabled:
        expected = replace(expected, required=False)
    assert all(channel.checks[0] == expected for channel in result.channels)


@pytest.mark.parametrize("size,color", [(True, False), (False, True), (True, True)])
def test_production_content_ng_and_execution_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, size: bool, color: bool
) -> None:
    import vimo_demo.application.pipeline as pipeline

    selected = replace(
        config(tmp_path),
        capsule=CapsuleSettings(
            replace(SETTINGS.size, enabled=size), replace(SETTINGS.color, enabled=color)
        ),
    )
    _, result = ConfiguredProductionPipeline(selected).inspect(
        collected(selected), lambda _: inferred()
    )
    assert result.assessment.technical_status is TechnicalStatus.SUCCESS
    assert result.assessment.judgment is ProductJudgment.NG

    def broken(image: NDArray[np.uint8], roi: BoundingRect, settings: CapsuleSettings):
        raise RuntimeError("algorithm failed")

    monkeypatch.setattr(pipeline, "measure_capsule", broken)
    _, result = ConfiguredProductionPipeline(selected).inspect(
        collected(selected), lambda _: inferred()
    )
    assert result.assessment.technical_status is TechnicalStatus.ERROR
    assert result.assessment.judgment is ProductJudgment.UNKNOWN
    assert result.channels[0].checks[0].reasons[0].code == "capsule.execution_error"


@pytest.mark.parametrize(
    "field,value",
    [
        ("target_width", 0),
        ("target_width", True),
        ("width_tolerance", -1),
        ("target_height", 1.5),
    ],
)
def test_invalid_size_recipe_fields(tmp_path: Path, field: str, value: object) -> None:
    import json

    from vimo_demo.configuration import ConfigurationError

    path = tmp_path / "invalid.recipe.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "recipe_id": "a",
                "display_name": "A",
                "defaults": {},
                "capsule": {"size": {field: value}},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError):
        load_recipe(path)


@pytest.mark.parametrize(
    "field,value",
    [("target_hue", 180), ("hue_tolerance", 91), ("enabled", 1), ("required", False)],
)
def test_invalid_color_recipe_fields(tmp_path: Path, field: str, value: object) -> None:
    import json

    from vimo_demo.configuration import ConfigurationError

    path = tmp_path / "invalid.recipe.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "recipe_id": "a",
                "display_name": "A",
                "defaults": {},
                "capsule": {"color": {field: value}},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError):
        load_recipe(path)


def test_lower_view_independently_rejects_size_and_color() -> None:
    image = bodies()
    image[70:100, 40:42] = (0, 180, 180)
    measured = measure_capsule(image, ROI, SETTINGS)
    result = judge_capsule(measured, SETTINGS)
    assert [r.code for r in result.reasons] == ["capsule.size.lower.width"]
    image = bodies()
    image[60:] = bodies(40)[60:]
    result = judge_capsule(measure_capsule(image, ROI, SETTINGS), SETTINGS)
    assert [r.code for r in result.reasons] == ["capsule.color.lower.tolerance"]
