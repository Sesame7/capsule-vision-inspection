"""Exact E0-R geometry, scalar evidence and conservative presence semantics."""

import csv
import hashlib
from dataclasses import FrozenInstanceError, asdict
from pathlib import Path

import cv2
import numpy as np
import pytest

from vimo_demo.inference import BoundingRect
from vimo_demo.inspection.production import (
    OccupancyEvidence,
    OccupancyResult,
    OccupancyState,
    OccupancyView,
    measure_occupancy,
)
from vimo_demo.inspection.production.occupancy import occupancy_rect

EMPTY, PRESENT, UNKNOWN = OccupancyState
ROOT = Path(__file__).resolve().parents[2]


def labeled_evidence(row: dict[str, str]) -> OccupancyEvidence:
    path = Path(row["source_path"])
    if not path.is_file():
        pytest.skip("Local E0-R challenge image unavailable")
    assert hashlib.sha256(path.read_bytes()).hexdigest() == row["image_sha256"]
    image = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert image is not None
    assert hashlib.sha256(image.tobytes()).hexdigest() == row["pixel_sha256"]
    pixels = np.asarray(image, dtype=np.uint8)
    pixels.flags.writeable = False
    roi = BoundingRect(*(int(row[f"roi_{k}"]) for k in ("x", "y", "width", "height")))
    return measure_occupancy(pixels, roi)


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


@pytest.mark.parametrize(
    "sample_id",
    [
        "E0-CAM1-000198-CH05",
        "E0-CAM6-000082-CH31",
        "E0-CAM6-000082-CH33",
        "E0-CAM6-000083-CH31",
        "E0-CAM6-000083-CH33",
        "E0-CAM6-000084-CH31",
        "E0-CAM6-000085-CH31",
    ],
)
def test_available_user_confirmed_neighbor_cases(sample_id: str) -> None:
    """Optional local challenge evidence, never a generic accuracy oracle."""
    annotations = ROOT / "outputs/occupancy_e0r/confirmed_annotations.csv"
    if not annotations.is_file():
        pytest.skip("Local E0-R external-label evidence unavailable")
    with annotations.open(encoding="utf-8-sig", newline="") as stream:
        row = next(r for r in csv.DictReader(stream) if r["sample_id"] == sample_id)
    assert row["manual_label"] == "EMPTY" and row["reviewer"] == "user"
    assert row["provenance"] == "Phase E0-R user request, section 3"
    evidence = labeled_evidence(row)
    assert evidence.state is EMPTY
    assert evidence.upper.foreground_pixels == evidence.lower.foreground_pixels == 0


def test_available_reviewed_shortlist_without_threshold_fitting() -> None:
    annotations = ROOT / "outputs/occupancy_e0r/review_shortlist.csv"
    if not annotations.is_file():
        pytest.skip("Local E0-R reviewed shortlist unavailable")
    with annotations.open(encoding="utf-8-sig", newline="") as stream:
        rows = [
            row
            for row in csv.DictReader(stream)
            if row["manual_label"] in ("EMPTY", "PRESENT")
        ]
    if not rows:
        pytest.skip("Shortlist has no independently entered labels")
    for row in rows:
        assert row["split"] in ("selection", "evaluation")
        evidence = labeled_evidence(row)
        assert evidence.state.name == row["manual_label"], row["sample_id"]
