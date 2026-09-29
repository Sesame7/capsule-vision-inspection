"""Pure input and mask contracts, independently testable without ViMo."""

from typing import cast

import numpy as np
from numpy.typing import NDArray


def uint8_array(value: object) -> NDArray[np.uint8]:
    if not isinstance(value, np.ndarray):
        raise TypeError("Expected a numpy.ndarray")
    array = cast(NDArray[np.generic], value)
    if array.dtype != np.uint8:
        raise TypeError(f"Expected uint8, got {array.dtype}")
    # NumPy's generic ndarray type does not narrow its dtype after this check.
    return cast(NDArray[np.uint8], value)


def validate_device_id(value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("device_id must be a nonnegative integer")


def validate_image(value: object) -> NDArray[np.uint8]:
    """BGR is a caller contract: never infer channel meaning or convert colors."""
    image = uint8_array(value)
    if image.ndim != 3 or image.shape[2] != 3 or min(image.shape[:2]) <= 0:
        raise ValueError(f"Expected nonempty H x W x 3 BGR image, got {image.shape}")
    return np.ascontiguousarray(image)


def normalize_mask(value: object, shape: tuple[int, int]) -> NDArray[np.uint8]:
    mask = uint8_array(value)
    if mask.ndim == 3 and mask.shape[-1] == 1:
        mask = np.squeeze(mask, axis=-1)
    if mask.ndim != 2 or mask.shape != shape or mask.size == 0:
        raise ValueError(f"Expected full-image mask {shape}, got {mask.shape}")
    # Response storage may be reused/released by the SDK on the next call/close.
    owned = mask.copy(order="C")
    owned.setflags(write=False)
    return owned


def validate_label_map(labels: dict[str, int]) -> None:
    if any(not label or not 0 <= code <= 255 for label, code in labels.items()):
        raise ValueError(f"Invalid label -> uint8 mask ID mapping: {labels}")
    if len(set(labels.values())) != len(labels):
        raise ValueError(f"Ambiguous label -> mask ID mapping: {labels}")
