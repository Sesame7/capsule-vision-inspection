"""Owned tiny frames and synthetic inference results shared by cycle tests."""

from collections.abc import Callable

import numpy as np
from numpy.typing import NDArray

from capsule_vision.inference import (
    BoundingRect,
    DefectRegion,
    ImageResult,
    RotatedRect,
)
from capsule_vision.inspection import (
    CameraId,
    CollectedCycle,
    CycleCollector,
    CycleId,
    Frame,
    FrameId,
)

CAMERAS = tuple(CameraId(f"camera-{i}") for i in (3, 1, 6, 2, 5, 4))


def frame(cycle: int, camera: CameraId, sequence: int = 0) -> Frame:
    return Frame(
        FrameId(CycleId(cycle), camera, sequence),
        2000.0,  # Camera clock deliberately differs from host monotonic time.
        np.full((2, 3, 3), sequence % 256, dtype=np.uint8),
    )


def image_result(image: NDArray[np.uint8], defect: bool = False) -> ImageResult:
    height, width, channels = image.shape
    rect = BoundingRect(0, 0, width, height)
    rotated = RotatedRect(width / 2, height / 2, width, height, 0)
    regions = (
        (DefectRegion("fake-defect", 6, rect, 3, 2, (), (), rotated),) if defect else ()
    )
    return ImageResult(
        (height, width, channels),
        {"fake-defect": 1},
        regions,
        np.zeros((height, width), dtype=np.uint8),
        rect,
        rotated,
        "fake",
    )


def ready_cycle(clock: Callable[[], float], cycle_id: int = 1) -> CollectedCycle:
    collector = CycleCollector(CAMERAS, timeout=2, clock=clock)
    collector.begin(CycleId(cycle_id))
    snapshot: CollectedCycle | None = None
    for i, camera in enumerate(reversed(CAMERAS)):
        snapshot = collector.accept(frame(cycle_id, camera, i))
    assert snapshot is not None
    assert collector.active_cycle_ids == ()
    return snapshot
