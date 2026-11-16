"""Whole-component Size ownership and length-only judgments, without hardware."""

import json
from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np
import pytest
from numpy.typing import NDArray

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
    image[start : start + 40, 25:35] = 41
    measured = measure_capsule(image, ROI, SIZE)
    owner, other = measured.views[int(lower)], measured.views[int(not lower)]
    assert owner.body is not None and owner.invalid_reason is None
    assert owner.body.bounding_rect == BoundingRect(25, start, 10, 40)
    assert owner.body.length == 39
    assert owner.body.endpoints == ((29.5, start), (29.5, start + 39))
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
    image[start : start + 40, 25:35] = 41
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
    image[10:13, 20:30] = 41
    if area == 29:
        image[10, 20] = 40
    view = measure_capsule(image, BoundingRect(0, 0, 100, 120), SIZE).views[0]
    assert (view.body is not None) is valid
    if not valid:
        assert view.invalid_reason == "missing_body"


def test_external_bottom_is_still_truncated() -> None:
    image = np.zeros((120, 100, 3), dtype=np.uint8)
    image[80:, 20:30] = 255
    view = measure_capsule(image, BoundingRect(0, 0, 100, 120), SIZE).views[1]
    assert view.body is None and view.invalid_reason == "truncated_body"


@pytest.mark.parametrize("bridge_value", [40, 41])
def test_body_is_truncated_only_when_edge_connection_is_foreground(
    bridge_value: int,
) -> None:
    image = np.zeros((140, 120, 3), dtype=np.uint8)
    image[15:55, 28:38] = (0, 0, 180)
    image[30, ROI.x : 28] = (0, 0, bridge_value)
    view = measure_capsule(image, ROI, SIZE).views[0]
    if bridge_value == 40:
        assert view.invalid_reason is None and view.body is not None
        assert view.body.bounding_rect == BoundingRect(28, 15, 10, 40)
        assert view.body.endpoints == ((32.5, 15), (32.5, 54))
    else:
        assert view.body is None and view.invalid_reason == "truncated_body"


@pytest.mark.parametrize("direction", [-1, 1])
def test_diagonal_line_is_degenerate_and_noncollinear_body_is_valid(
    direction: int,
) -> None:
    image = np.zeros((140, 120, 3), dtype=np.uint8)
    cv2.line(image, (50, 20), (50 + 29 * direction, 49), (41, 0, 0), 1)
    line = measure_capsule(image, ROI, SIZE).views[0]
    assert line.body is None and line.invalid_reason == "invalid_geometry"
    image[30, 51 + 10 * direction] = (41, 0, 0)
    view = measure_capsule(image, ROI, SIZE).views[0]
    assert view.invalid_reason is None and view.body is not None
    assert view.body.bounding_rect == BoundingRect(
        min(50, 50 + 29 * direction), 20, 30, 30
    )
    assert view.body.length == 29


@pytest.mark.parametrize(
    "target, tolerance, expected",
    [
        (29, 0, ProductJudgment.OK),
        (28, 1, ProductJudgment.OK),
        (30, 1, ProductJudgment.OK),
        (31, 1, ProductJudgment.NG),
    ],
)
def test_saved_size_values_apply_unchanged_to_tilted_bodies(
    tmp_path: Path, target: int, tolerance: int, expected: ProductJudgment
) -> None:
    path = tmp_path / "capsule.json"
    original = json.dumps(
        {
            "display_name": "Capsule",
            "capsule": {
                "size": {
                    "enabled": True,
                    "target_length": target,
                    "length_tolerance": tolerance,
                }
            },
        }
    ).encode()
    path.write_bytes(original)
    settings = load_recipe(path).capsule
    assert settings.size == SizeSettings(True, target, tolerance)
    image = np.zeros((140, 120, 3), dtype=np.uint8)
    for y in (15, 75):
        points = np.array(
            [(28, y), (37, y), (45, y + 29), (36, y + 29)], dtype=np.int32
        )
        cv2.fillConvexPoly(image, points, (41, 0, 0))
        image[y - 4 : y, 28:38] = (40, 0, 0)  # Dim ends lie outside the T40 body.
    before = image.copy()
    measured = measure_capsule(image, ROI, settings)
    for view, y in zip(measured.views, (15, 75), strict=True):
        assert view.body is not None and view.invalid_reason is None
        assert view.body.bounding_rect == BoundingRect(28, y, 18, 30)
        assert view.body.endpoints == ((36.5, y), (36.5, y + 29))
        assert view.body.length == 29
    result = judge_capsule(measured, settings)
    assert result.assessment.judgment is expected
    assert [(m.name, m.value, m.unit) for m in result.measurements] == [
        ("capsule.upper.length", 29.0, "px"),
        ("capsule.lower.length", 29.0, "px"),
    ]
    assert path.read_bytes() == original
    np.testing.assert_array_equal(image, before)


def test_size_paths_label_the_same_strict_t40_mask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = np.zeros((120, 100, 3), dtype=np.uint8)
    image[10:40, 20:40] = (0, 180, 180)
    image[70:100, 20:40] = (0, 180, 180)
    image[3, :6] = [
        (21, 0, 0),
        (40, 40, 40),
        (41, 0, 0),
        (0, 41, 0),
        (0, 0, 41),
        (0, 40, 0),
    ]
    before = image.copy()
    image.setflags(write=False)
    masks: list[NDArray[np.uint8]] = []
    original = cv2.connectedComponentsWithStats

    def observed(
        foreground: NDArray[np.uint8],
        *,
        connectivity: int,
    ):
        masks.append(foreground.copy())
        return original(foreground, connectivity=connectivity)

    def forbidden(*args: object) -> None:
        pytest.fail("Size must use component statistics for body geometry")

    monkeypatch.setattr(cv2, "findContours", forbidden)
    monkeypatch.setattr(cv2, "minAreaRect", forbidden)
    monkeypatch.setattr(cv2, "connectedComponentsWithStats", observed)
    roi = BoundingRect(0, 0, 100, 120)
    color = CapsuleSettings(color=ColorSettings(True, 30, 0))
    only = measure_capsule(image, roi, color)
    assert not masks and all(v.body is None for v in only.views)
    both = measure_capsule(image, roi, replace(color, size=SIZE.size))
    assert len(masks) == 1
    assert [v.color for v in both.views] == [v.color for v in only.views]
    size_only = measure_capsule(image, roi, SIZE)
    assert len(masks) == 2
    assert [v.body for v in both.views] == [v.body for v in size_only.views]
    expected = np.where(np.any(before > 40, axis=2), 255, 0).astype(np.uint8)
    for mask in masks:
        np.testing.assert_array_equal(mask, expected)
    np.testing.assert_array_equal(image, before)


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
