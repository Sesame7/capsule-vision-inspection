"""Center-region geometry, scalar evidence and conservative presence semantics."""

from dataclasses import FrozenInstanceError, asdict

import numpy as np
import pytest

from vimo_demo.inference import BoundingRect
from vimo_demo.inspection.production import (
    OccupancyResult,
    OccupancyState,
    OccupancyView,
    measure_occupancy,
)
from vimo_demo.inspection.production.occupancy import occupancy_rect

EMPTY, PRESENT, UNKNOWN = OccupancyState


@pytest.mark.parametrize("width", [1, 2, 3, 7, 79, 80, 175, 176, 177])
def test_center_geometry_and_odd_height(width: int) -> None:
    roi = BoundingRect(2, 3, width, 5)
    image = np.full((10, width + 5, 3), 20, dtype=np.uint8)
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
    assert np.all(image == 20) and not image.flags.writeable


@pytest.mark.parametrize(
    "foreground,state",
    [(0, EMPTY), (1, EMPTY), (2, UNKNOWN), (4, UNKNOWN), (5, PRESENT)],
)
def test_inclusive_ratio_boundaries(foreground: int, state: OccupancyState) -> None:
    view = OccupancyView(foreground, 100)
    assert view.ratio == foreground / 100 and view.state is state


@pytest.mark.parametrize("upper", [EMPTY, PRESENT, UNKNOWN])
@pytest.mark.parametrize("lower", [EMPTY, PRESENT, UNKNOWN])
def test_independent_views_and_all_fusion_pairs(
    upper: OccupancyState, lower: OccupancyState
) -> None:
    # Each measured view has exactly 100 pixels; no rounding in thresholds.
    image = np.zeros((4, 72, 3), dtype=np.uint8)
    rect = occupancy_rect(BoundingRect(0, 0, 72, 4))
    assert rect.width == 50
    counts = {EMPTY: 1, UNKNOWN: 2, PRESENT: 5}
    for start, state in ((0, upper), (2, lower)):
        image[start, rect.x : rect.x + counts[state], 2] = 21
    evidence = measure_occupancy(image, BoundingRect(0, 0, 72, 4))
    assert evidence.upper.state is upper and evidence.lower.state is lower
    expected = upper if upper is lower and upper is not UNKNOWN else UNKNOWN
    assert evidence.state is expected


def test_neighbor_only_foreground_and_no_pixel_aliases() -> None:
    image = np.zeros((120, 80, 3), dtype=np.uint8)
    image[:, :12] = 255
    image.flags.writeable = False
    evidence = measure_occupancy(image, BoundingRect(0, 0, 80, 120))
    assert evidence.state is EMPTY and evidence.measurement_rect.x == 12
    assert np.any(image > 20)
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
    result = OccupancyResult(error_code="occupancy.result_unavailable")
    assert result.state is UNKNOWN and result.evidence is None


def test_row_strided_readonly_input_matches_contiguous_input() -> None:
    base = np.random.default_rng(7).integers(0, 30, (20, 90, 3), dtype=np.uint8)
    view = base[::2, :80]
    view.flags.writeable = False
    before = base.copy()
    roi = BoundingRect(1, 2, 77, 7)
    assert measure_occupancy(view, roi) == measure_occupancy(view.copy(), roi)
    np.testing.assert_array_equal(base, before)
