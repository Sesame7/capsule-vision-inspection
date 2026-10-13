"""Exact circular-Hue equivalence, including numerical and judgment boundaries."""

import cv2
import numpy as np
import pytest
from numpy.typing import NDArray

from vimo_demo.inspection.production import capsule
from vimo_demo.inspection.production.capsule import median_hue, median_pixel_hue


def masked_hue(values: NDArray[np.uint8]) -> float | None:
    pixels = np.full((1, values.size, 3), (0, 200, 200), dtype=np.uint8)
    pixels[0, :, 0] = values
    mask = np.full(pixels.shape[:2], 255, dtype=np.uint8)
    pixels.flags.writeable = mask.flags.writeable = False
    # White-box regression of the private sampling path; no public strategy API.
    return capsule._masked_pixel_hue(pixels, mask, values.size)  # pyright: ignore[reportPrivateUsage]


def test_all_two_hue_pairs_match_pixel_reference() -> None:
    # All bin pairs cover wraparound, ambiguous mixtures and center rounding ties.
    for left in range(180):
        for right in range(left, 180):
            values = np.array([left, right], dtype=np.uint8)
            assert median_pixel_hue(values) == median_hue(values.astype(np.float64))
            assert masked_hue(values) == median_hue(values.astype(np.float64))


@pytest.mark.parametrize("size", [0, 1, 2, 3, 11, 12, 31, 100, 1001, 50000])
def test_weighted_distributions_match_reference_without_mutation(size: int) -> None:
    rng = np.random.default_rng(314)
    for bins in ((179, 0, 1), (0, 60, 120), (0, 90), (13, 14, 15), tuple(range(180))):
        values = rng.choice(np.array(bins, dtype=np.uint8), size=size)
        before = values.copy()
        assert median_pixel_hue(values) == median_hue(values.astype(np.float64))
        assert masked_hue(values) == median_hue(values.astype(np.float64))
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
        ([0, 90], [30000, 10000]),
        ([0, 90], [30001, 10000]),
        ([0, 90], [29999, 10000]),
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
        assert masked_hue(selected) == median_hue(selected.astype(np.float64))


@pytest.mark.parametrize(
    "kind,offset,fallbacks,expected",
    [
        ("magnitude", -2e-12, 0, None),
        ("magnitude", -2e-13, 2, 1.0),
        ("magnitude", 0, 2, 1.0),
        ("magnitude", 2e-13, 2, 1.0),
        ("magnitude", 2e-12, 0, 1.0),
        ("rounding", -2e-10, 0, 1.0),
        ("rounding", -5e-11, 2, 1.0),
        ("rounding", 0, 2, 1.0),
        ("rounding", 5e-11, 2, 1.0),
        ("rounding", 2e-10, 0, 1.0),
    ],
)
def test_numerical_guards_preserve_pixel_order_fallback(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    offset: float,
    fallbacks: int,
    expected: float | None,
) -> None:
    values = np.array([0, 1, 13, 14, 179, 0, 1, 14], dtype=np.uint8)
    vector = (
        complex(0.5 + offset, 0)
        if kind == "magnitude"
        else complex(0.8 * np.exp(1j * (0.5 + offset) * (2 * np.pi / 180)))
    )
    original = capsule.median_hue
    observed: list[NDArray[np.float64]] = []

    def dot(*_: object) -> complex:
        return vector * values.size

    def floating(samples: NDArray[np.float64]) -> float | None:
        observed.append(samples.copy())
        return original(samples)

    monkeypatch.setattr(np, "dot", dot)
    monkeypatch.setattr(capsule, "median_hue", floating)
    assert median_pixel_hue(values) == expected
    assert masked_hue(values) == expected
    assert len(observed) == fallbacks
    assert all(np.array_equal(v, values.astype(np.float64)) for v in observed)


@pytest.mark.parametrize("count", [2**24, 2**24 + 1])
def test_masked_histogram_float32_precision_guard(
    monkeypatch: pytest.MonkeyPatch, count: int
) -> None:
    # Branch probe, not a claim to exercise a real >16M-pixel allocation.
    pixels = np.full((1, 12, 3), (30, 200, 200), dtype=np.uint8)
    mask = np.full((1, 12), 255, dtype=np.uint8)
    calls: list[str] = []
    original = capsule.median_pixel_hue

    def histogram(*args: object) -> NDArray[np.float32]:
        assert count == 2**24
        calls.append("histogram")
        result = np.zeros((180, 1), dtype=np.float32)
        result[30] = count
        return result

    def gathered(values: NDArray[np.uint8]) -> float | None:
        assert count > 2**24
        calls.append("gathered")
        assert np.array_equal(values, pixels[:, :, 0][mask != 0])
        return original(values)

    monkeypatch.setattr(cv2, "calcHist", histogram)
    monkeypatch.setattr(capsule, "median_pixel_hue", gathered)
    # Explicit count injection probes the private precision guard without 16M pixels.
    assert capsule._masked_pixel_hue(pixels, mask, count) == 30  # pyright: ignore[reportPrivateUsage]
    assert calls == (["histogram"] if count == 2**24 else ["gathered"])


def test_masked_histogram_discrepancy_uses_exact_gathered_samples(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = np.array([179, 0, 1, 178, 0, 1, 0, 1, 178, 179, 0, 1], dtype=np.uint8)
    original = capsule.median_pixel_hue
    observed: list[NDArray[np.uint8]] = []

    def histogram(*args: object) -> NDArray[np.float32]:
        return np.zeros((180, 1), dtype=np.float32)  # Deliberate count disagreement.

    def gathered(samples: NDArray[np.uint8]) -> float | None:
        observed.append(samples.copy())
        return original(samples)

    monkeypatch.setattr(cv2, "calcHist", histogram)
    monkeypatch.setattr(capsule, "median_pixel_hue", gathered)
    assert masked_hue(values) == original(values)
    assert len(observed) == 1 and np.array_equal(observed[0], values)


def test_regular_masked_path_needs_neither_gathered_median_nor_roll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail(
            "Normal masked histogram must not enter gathered/floating/roll paths"
        )

    monkeypatch.setattr(capsule, "median_pixel_hue", forbidden)
    monkeypatch.setattr(capsule, "median_hue", forbidden)
    monkeypatch.setattr(np, "roll", forbidden)
    assert masked_hue(np.full(100, 30, dtype=np.uint8)) == 30
