"""Exact circular-Hue equivalence, including numerical and judgment boundaries."""

import numpy as np
import pytest

from vimo_demo.inspection.production.capsule import median_hue, median_pixel_hue


def test_all_two_hue_pairs_match_pixel_reference() -> None:
    # All bin pairs cover wraparound, ambiguous mixtures and center rounding ties.
    for left in range(180):
        for right in range(left, 180):
            values = np.array([left, right], dtype=np.uint8)
            assert median_pixel_hue(values) == median_hue(values.astype(np.float64))


@pytest.mark.parametrize("size", [0, 1, 2, 31, 100, 1001, 50000])
def test_weighted_distributions_match_reference_without_mutation(size: int) -> None:
    rng = np.random.default_rng(314)
    for bins in ((179, 0, 1), (0, 60, 120), (0, 90), (13, 14, 15), tuple(range(180))):
        values = rng.choice(np.array(bins, dtype=np.uint8), size=size)
        before = values.copy()
        assert median_pixel_hue(values) == median_hue(values.astype(np.float64))
        assert np.array_equal(values, before)


@pytest.mark.parametrize(
    "bins, counts",
    [
        ([0, 60], [20000, 20000]),  # Magnitude exactly at the 0.5 branch.
        ([0, 60], [20000, 20001]),
        ([0, 1], [10001, 10001]),  # Circular center exactly at a rounding tie.
        ([179, 0], [10001, 10001]),
        ([178, 179, 0, 1], [5000, 1, 1, 5000]),
        ([0, 60, 120], [1, 1, 1]),
    ],
)
def test_histogram_numerical_boundaries_preserve_original_result(
    bins: list[int], counts: list[int]
) -> None:
    values = np.repeat(np.array(bins, dtype=np.uint8), counts)
    # Pixel summation order is part of the fallback behavior near branch points.
    for selected in (
        values,
        values[::-1],
        np.random.default_rng(1).permutation(values),
    ):
        assert median_pixel_hue(selected) == median_hue(selected.astype(np.float64))
