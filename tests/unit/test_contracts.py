from dataclasses import replace
from types import MappingProxyType

import numpy as np
import pytest

from vimo_demo.inference import BoundingRect, DefectRegion, ImageResult, RotatedRect
from vimo_demo.inference._validation import (
    normalize_mask,
    validate_image,
    validate_label_map,
)


def test_contiguous_bgr_is_not_copied_or_recolored() -> None:
    image = np.array([[[7, 19, 211], [3, 4, 5]]], dtype=np.uint8)
    assert validate_image(image) is image
    assert image[0, 0].tolist() == [7, 19, 211]


def test_strided_input_preserves_pixels_and_orientation() -> None:
    image = np.arange(36, dtype=np.uint8).reshape(3, 4, 3)[:, ::-1]
    normalized = validate_image(image)
    assert normalized.flags.c_contiguous
    np.testing.assert_array_equal(normalized, image)
    assert not np.shares_memory(normalized, image)


@pytest.mark.parametrize(
    "value", [None, [[[0, 0, 0]]], np.zeros((2, 2, 3), np.float32)]
)
def test_invalid_input_type(value: object) -> None:
    with pytest.raises(TypeError):
        validate_image(value)


@pytest.mark.parametrize(
    "shape", [(0, 2, 3), (2, 0, 3), (2, 3), (2, 3, 4), (2, 3, 1), (1, 2, 3, 3)]
)
def test_invalid_image_shape(shape: tuple[int, ...]) -> None:
    with pytest.raises(ValueError):
        validate_image(np.zeros(shape, np.uint8))


@pytest.mark.parametrize("shape", [(1, 3), (1, 3, 1), (3, 1), (3, 1, 1)])
def test_mask_normalization_preserves_spatial_singletons_and_owns_ids(
    shape: tuple[int, ...],
) -> None:
    original = np.array([0, 2, 255], dtype=np.uint8).reshape(shape)
    mask = normalize_mask(original, (shape[0], shape[1]))
    assert mask.shape == shape[:2]
    assert mask.ravel().tolist() == [0, 2, 255]
    assert mask.flags.c_contiguous and not mask.flags.writeable
    original[...] = 4
    assert mask.ravel().tolist() == [0, 2, 255]


@pytest.mark.parametrize(
    "shape", [(2, 3, 2), (2, 3, 3), (6,), (3, 2), (1, 2, 3, 1), (0, 3)]
)
def test_incompatible_mask_shape_is_not_reshaped(shape: tuple[int, ...]) -> None:
    with pytest.raises(ValueError):
        normalize_mask(np.zeros(shape, np.uint8), (2, 3))


def test_mask_float_ids_are_not_silently_converted() -> None:
    with pytest.raises(TypeError):
        normalize_mask(np.zeros((2, 3), np.float32), (2, 3))


def test_label_ids_are_distinct_from_region_counts_and_presence() -> None:
    rect = BoundingRect(2, 3, 4, 5)
    rotated = RotatedRect(4, 5.5, 4, 5, 0)
    region = DefectRegion("scratch", 20, rect, 5.0, 4.0, (), (), rotated)
    result = ImageResult(
        (2, 3, 3),
        MappingProxyType({"scratch": 7, "dent": 9}),
        (region, region),
        normalize_mask(np.zeros((2, 3), np.uint8), (2, 3)),
        rect,
        rotated,
        "2",
    )
    assert result.label_map == {"scratch": 7, "dent": 9}
    assert result.mask_id_to_label == {7: "scratch", 9: "dent"}
    assert result.regions_per_label == {"scratch": 2}
    assert result.has_defects
    assert not replace(result, regions=()).has_defects
    counts = result.regions_per_label
    counts.clear()
    assert result.regions_per_label == {"scratch": 2}


@pytest.mark.parametrize("labels", [{"a": 2, "b": 2}, {"a": -1}, {"a": 256}, {"": 1}])
def test_invalid_label_map(labels: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        validate_label_map(labels)
