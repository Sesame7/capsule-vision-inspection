"""Owned, typed results without vendor objects or an OK/NG acceptance rule."""

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

type Point = tuple[float, float]
type Contour = tuple[Point, ...]


@dataclass(frozen=True, slots=True)
class BoundingRect:
    """Axis-aligned rectangle in original image coordinates, x/y/width/height."""

    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class RotatedRect:
    """SDK center/size/angle convention, preserved without transforming geometry."""

    center_x: float
    center_y: float
    width: float
    height: float
    angle: float


@dataclass(frozen=True, slots=True)
class DefectRegion:
    label: str
    area: int
    bounding_rect: BoundingRect
    long_side: float
    short_side: float
    outer_contour: Contour
    inner_contours: tuple[Contour, ...]
    min_area_rect: RotatedRect


@dataclass(frozen=True, slots=True)
class EngineInfo:
    model_sha256: str
    module_id: str
    module_name: str
    model_version: str
    labels: tuple[str, ...]
    device_id: int
    area_thresholds: Mapping[str, int]


@dataclass(frozen=True, slots=True, eq=False)
class ImageResult:
    """Mask owns its pixels and is read-only by default; maps/regions are snapshots.

    label_map means label -> mask ID, NOT region count. has_defects means only
    that the SDK returned regions; quality acceptance belongs to the caller.
    """

    image_shape: tuple[int, int, int]
    label_map: Mapping[str, int]
    regions: tuple[DefectRegion, ...]
    mask: NDArray[np.uint8]
    roi: BoundingRect
    rotated_roi: RotatedRect
    module_id: str

    @property
    def has_defects(self) -> bool:
        return bool(self.regions)

    @property
    def regions_per_label(self) -> dict[str, int]:
        return dict(sorted(Counter(region.label for region in self.regions).items()))

    @property
    def mask_id_to_label(self) -> dict[int, str]:
        """Only actual SDK mappings; no fabricated background/class names."""
        return {code: label for label, code in self.label_map.items()}
