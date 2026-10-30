"""Synthetic production evidence builders and their stepping test clock."""

from dataclasses import replace

import numpy as np

from capsule_vision.inference import (
    BoundingRect,
    DefectRegion,
    ImageResult,
    RotatedRect,
)
from capsule_vision.inspection import (
    CameraId,
    CycleId,
    Frame,
    FrameId,
    FrameInspectionResult,
    InspectionFailure,
)
from capsule_vision.inspection.production import (
    CameraLayout,
    ChannelId,
    ChannelROI,
    FindingSource,
    InspectionLayout,
    ProductJudgment,
    TechnicalStatus,
)
from tests.support.cycles import image_result

SUCCESS, ERROR, DISABLED = TechnicalStatus
OK, NG, UNKNOWN = ProductJudgment
TRADITIONAL = FindingSource.TRADITIONAL
VIMO = FindingSource.VIMO
CAM1 = CameraId("CAM1")


class Clock:
    def __init__(self, step: float = 0) -> None:
        self.value = 100.0
        self.step = step

    def __call__(self) -> float:
        current = self.value
        self.value += self.step
        return current


def layout(count: int = 6, *, mirrored: bool = False) -> InspectionLayout:
    return InspectionLayout(
        tuple(
            CameraLayout(
                CameraId(f"CAM{camera + 1}"),
                tuple(
                    ChannelROI(
                        slot,
                        ChannelId(camera * 6 + (6 - slot if mirrored else slot + 1)),
                        BoundingRect(slot * 10, 0, 8, 10),
                    )
                    for slot in range(6)
                ),
            )
            for camera in range(count)
        )
    )


def source_frame(camera: CameraId = CAM1, cycle: int = 1) -> Frame:
    return Frame(
        FrameId(CycleId(cycle), camera, 7), 0, np.zeros((20, 60, 3), dtype=np.uint8)
    )


def inferred(*rects: BoundingRect) -> ImageResult:
    base = image_result(np.zeros((20, 60, 3), dtype=np.uint8))
    return replace(
        base,
        regions=tuple(
            DefectRegion(
                f"label-{i}",
                rect.width * rect.height,
                rect,
                2,
                1,
                (),
                (),
                RotatedRect(0, 0, 2, 1, 0),
            )
            for i, rect in enumerate(rects)
        ),
    )


def outcome(
    camera: CameraId = CAM1,
    image: ImageResult | None = None,
    *,
    failed: bool = False,
) -> FrameInspectionResult:
    return FrameInspectionResult(
        FrameId(CycleId(1), camera, 7),
        0,
        1,
        (20, 60, 3),
        2,
        2.01,
        None if failed else image if image is not None else inferred(),
        InspectionFailure("FakeError", "diagnostic", "trace") if failed else None,
    )
