"""Whole-component Size ownership and length-only judgments, without hardware."""

import json
import math
from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np
import pytest

from capsule_vision.configuration import ConfigurationError, load_recipe
from capsule_vision.inference import BoundingRect
from capsule_vision.inspection.production import ProductJudgment
from capsule_vision.inspection.production.capsule import (
    CapsuleSettings,
    ColorSettings,
    SizeSettings,
    judge_capsule,
    measure_capsule,
)

SIZE = CapsuleSettings(size=SizeSettings(True, 39, 0))
ROI = BoundingRect(7, 5, 100, 121)


@pytest.mark.parametrize("lower", [False, True])
@pytest.mark.parametrize("crossing", [0, 1, 7])
def test_complete_crossing_body_has_exactly_one_owner(
    lower: bool, crossing: int
) -> None:
    image = np.zeros((140, 120, 3), dtype=np.uint8)
    divider = ROI.y + ROI.height // 2
    start = divider - crossing if lower else divider - 40 + crossing
    image[start : start + 40, 25:35] = 21
    measured = measure_capsule(image, ROI, SIZE)
    owner, other = measured.views[int(lower)], measured.views[int(not lower)]
    assert owner.body is not None and owner.invalid_reason is None
    assert owner.body.bounding_rect == BoundingRect(25, start, 10, 40)
    assert owner.body.length == 39
    assert math.dist(*owner.body.endpoints) == pytest.approx(39)
    assert min(p[1] for p in owner.body.endpoints) == start
    assert max(p[1] for p in owner.body.endpoints) == start + 39
    assert other.body is None and other.invalid_reason == "missing_body"
    # The other logical view still independently rejects missing required Size.
    result = judge_capsule(measured, SIZE)
    assert result.assessment.judgment is ProductJudgment.NG
    assert [r.code for r in result.reasons] == [f"capsule.{other.name}.missing_body"]


@pytest.mark.parametrize("lower", [False, True])
@pytest.mark.parametrize("crossing", [8, 15, 20])
def test_significant_crossing_is_ambiguous_at_existing_quarter_rule(
    lower: bool, crossing: int
) -> None:
    image = np.zeros((140, 120, 3), dtype=np.uint8)
    divider = ROI.y + ROI.height // 2
    start = divider - crossing if lower else divider - 40 + crossing
    image[start : start + 40, 25:35] = 21
    measured = measure_capsule(image, ROI, SIZE)
    assert all(
        v.body is None and v.invalid_reason == "ambiguous_body" for v in measured.views
    )


def test_merged_views_and_disagreeing_centroid_are_not_assigned() -> None:
    image = np.zeros((120, 100, 3), dtype=np.uint8)
    roi = BoundingRect(0, 0, 100, 120)
    image[20:40, 20:40] = 255
    image[80:100, 20:40] = 255
    image[39:81, 29:31] = 255
    assert all(
        v.invalid_reason == "ambiguous_body"
        for v in measure_capsule(image, roi, SIZE).views
    )
    # A short wide region beside the divider with a very long thin tail: area
    # predominance and position disagree, even below the quarter-area rule.
    image[:] = 0
    image[58:60, 10:90] = 255
    image[60:90, 49:50] = 255
    assert all(
        v.invalid_reason == "ambiguous_body"
        for v in measure_capsule(image, roi, SIZE).views
    )


def test_two_bodies_crossing_in_opposite_directions_stay_independent() -> None:
    image = np.zeros((120, 100, 3), dtype=np.uint8)
    image[22:62, 20:30] = 255
    image[58:98, 60:70] = 255
    measured = measure_capsule(image, BoundingRect(0, 0, 100, 120), SIZE)
    assert [v.body.bounding_rect.y if v.body else None for v in measured.views] == [
        22,
        58,
    ]
    assert judge_capsule(measured, SIZE).assessment.judgment is ProductJudgment.OK


@pytest.mark.parametrize("area,valid", [(29, False), (30, True)])
def test_logical_view_minimum_area_is_preserved(area: int, valid: bool) -> None:
    image = np.zeros((120, 100, 3), dtype=np.uint8)
    image[10:13, 20:30] = 21
    if area == 29:
        image[10, 20] = 20
    view = measure_capsule(image, BoundingRect(0, 0, 100, 120), SIZE).views[0]
    assert (view.body is not None) is valid
    if not valid:
        assert view.invalid_reason == "missing_body"


def test_external_bottom_is_still_truncated() -> None:
    image = np.zeros((120, 100, 3), dtype=np.uint8)
    image[80:, 20:30] = 255
    view = measure_capsule(image, BoundingRect(0, 0, 100, 120), SIZE).views[1]
    assert view.body is None and view.invalid_reason == "truncated_body"


def test_one_segmentation_color_only_skips_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = np.zeros((120, 100, 3), dtype=np.uint8)
    image[10:40, 20:40] = (0, 180, 180)
    image[70:100, 20:40] = (0, 180, 180)
    calls = 0
    original = cv2.connectedComponentsWithStats

    def observed(
        foreground: np.ndarray[tuple[int, ...], np.dtype[np.uint8]],
        *,
        connectivity: int,
    ):
        nonlocal calls
        calls += 1
        return original(foreground, connectivity=connectivity)

    monkeypatch.setattr(cv2, "connectedComponentsWithStats", observed)
    roi = BoundingRect(0, 0, 100, 120)
    color = CapsuleSettings(color=ColorSettings(True, 30, 0))
    only = measure_capsule(image, roi, color)
    assert calls == 0 and all(v.body is None for v in only.views)
    both = measure_capsule(image, roi, replace(color, size=SIZE.size))
    assert calls == 1
    assert [v.color for v in both.views] == [v.color for v in only.views]


@pytest.mark.parametrize(
    "legacy", ["target_width", "target_height", "width_tolerance", "height_tolerance"]
)
@pytest.mark.parametrize("enabled", [False, True])
def test_legacy_size_rejected_without_rewriting(
    tmp_path: Path, legacy: str, enabled: bool
) -> None:
    path = tmp_path / "legacy.json"
    original = json.dumps(
        {
            "display_name": "Legacy",
            "capsule": {"size": {"enabled": enabled, legacy: 12}},
        }
    )
    path.write_text(original, encoding="utf-8")
    with pytest.raises(ConfigurationError, match="Legacy Size W/H.*target_length"):
        load_recipe(path)
    assert path.read_text(encoding="utf-8") == original
