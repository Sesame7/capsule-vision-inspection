"""Center-region geometry, scalar evidence and binary presence semantics."""

from dataclasses import FrozenInstanceError, asdict

import numpy as np
import pytest

from capsule_vision.inference import BoundingRect
from capsule_vision.inspection.production import (
    OccupancyResult,
    OccupancyState,
    OccupancyView,
    measure_occupancy,
)
from capsule_vision.inspection.production.occupancy import occupancy_rect

EMPTY, PRESENT = OccupancyState


@pytest.mark.parametrize("width", [1, 2, 3, 7, 79, 80, 175, 176, 177])
def test_center_geometry_and_odd_height(width: int) -> None:
    roi = BoundingRect(2, 3, width, 5)
    image = np.full((10, width + 5, 3), 40, dtype=np.uint8)
    image.flags.writeable = False
    evidence = measure_occupancy(image, roi)
    expected_width = max(1, width * 70 // 100)
    assert evidence.measurement_rect == BoundingRect(
        2 + (width - expected_width) // 2, 3, expected_width, 5
    )
    assert evidence.upper == OccupancyView(0, expected_width * 2)
    assert evidence.lower == OccupancyView(0, expected_width * 3)
    assert evidence.state is EMPTY
    assert roi == BoundingRect(2, 3, width, 5)
    assert np.all(image == 40) and not image.flags.writeable


@pytest.mark.parametrize(
    "foreground,state",
    [(0, EMPTY), (4, EMPTY), (5, EMPTY), (6, PRESENT), (100, PRESENT)],
)
def test_presence_requires_strictly_more_than_five_percent(
    foreground: int, state: OccupancyState
) -> None:
    view = OccupancyView(foreground, 100)
    assert view.ratio == foreground / 100 and view.state is state


@pytest.mark.parametrize("component", [0, 1, 2])
def test_foreground_is_strictly_above_40_in_any_bgr_component(component: int) -> None:
    roi = BoundingRect(0, 0, 72, 4)
    rect = occupancy_rect(roi)
    image = np.full((4, 72, 3), 40, dtype=np.uint8)
    image[0, rect.x, component] = 39
    image[0, rect.x + 1, component] = 41
    image[2:, rect.x, component] = 41
    evidence = measure_occupancy(image, roi)
    assert evidence.upper == OccupancyView(1, 100)
    assert evidence.lower == OccupancyView(2, 100)
    assert evidence.state is EMPTY


@pytest.mark.parametrize(
    "upper,lower,expected",
    [(0, 0, EMPTY), (5, 5, EMPTY), (5, 6, PRESENT), (6, 5, PRESENT), (6, 6, PRESENT)],
)
def test_independent_views_and_all_fusion_pairs(
    upper: int, lower: int, expected: OccupancyState
) -> None:
    # Each measured view has exactly 100 pixels; no rounding in thresholds.
    image = np.zeros((4, 72, 3), dtype=np.uint8)
    roi = BoundingRect(0, 0, 72, 4)
    rect = occupancy_rect(roi)
    assert rect.width == 50
    for start, count in ((0, upper), (2, lower)):
        image[start, rect.x : rect.x + count, 2] = 41
    evidence = measure_occupancy(image, roi)
    assert evidence.upper == OccupancyView(upper, 100)
    assert evidence.lower == OccupancyView(lower, 100)
    assert evidence.state is expected
    assert OccupancyResult(evidence).state is expected


def test_neighbor_only_foreground_and_no_pixel_aliases() -> None:
    image = np.zeros((120, 80, 3), dtype=np.uint8)
    image[:, :12] = 255
    image.flags.writeable = False
    evidence = measure_occupancy(image, BoundingRect(0, 0, 80, 120))
    assert evidence.state is EMPTY and evidence.measurement_rect.x == 12
    assert np.any(image > 40)
    assert asdict(evidence) == {
        "measurement_rect": {"x": 12, "y": 0, "width": 56, "height": 120},
        "upper": {"foreground_pixels": 0, "total_pixels": 3360},
        "lower": {"foreground_pixels": 0, "total_pixels": 3360},
    }
    attribute = "foreground_pixels"
    with pytest.raises(FrozenInstanceError):
        setattr(evidence.upper, attribute, 1)
    assert not image.flags.writeable


@pytest.mark.parametrize("rect", [BoundingRect(0, 0, 80, 1), BoundingRect(1, 0, 80, 4)])
def test_original_roi_is_validated_before_center(rect: BoundingRect) -> None:
    with pytest.raises(ValueError, match="Original ROI"):
        measure_occupancy(np.zeros((4, 80, 3), dtype=np.uint8), rect)


def test_invalid_image_and_empty_evidence_never_assert_empty() -> None:
    with pytest.raises(ValueError, match="BGR"):
        measure_occupancy(
            np.zeros((4, 80, 1), dtype=np.uint8), BoundingRect(0, 0, 80, 4)
        )
    with pytest.raises(ValueError):
        OccupancyView(0, 0)
    with pytest.raises(ValueError):
        OccupancyView(101, 100)
    with pytest.raises(ValueError):
        OccupancyResult()
    for code in ("occupancy.execution_error", "occupancy.result_unavailable"):
        result = OccupancyResult(error_code=code)
        assert result.state is None and result.evidence is None


def test_row_strided_readonly_input_matches_contiguous_input() -> None:
    base = np.random.default_rng(7).integers(0, 51, (20, 90, 3), dtype=np.uint8)
    view = base[::2, :80]
    view.flags.writeable = False
    before = base.copy()
    roi = BoundingRect(1, 2, 77, 7)
    assert measure_occupancy(view, roi) == measure_occupancy(view.copy(), roi)
    np.testing.assert_array_equal(base, before)
