"""Synthetic production evidence builders and their stepping test clock."""

from dataclasses import replace

import numpy as np

from tests.unit.test_inspection_cycle import image_result
from vimo_demo.inference import BoundingRect, DefectRegion, ImageResult, RotatedRect
from vimo_demo.inspection import (
    CameraId,
    CycleId,
    CycleStatus,
    Frame,
    FrameId,
    FrameInspectionResult,
    InspectionCycleResult,
    InspectionFailure,
)
from vimo_demo.inspection.production import (
    CameraLayout,
    ChannelId,
    ChannelROI,
    FindingSource,
    InspectionLayout,
    ProductJudgment,
    TechnicalStatus,
)

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


def cycle_result(
    config: InspectionLayout, first: ImageResult | None = None, *, failed: bool = False
) -> InspectionCycleResult:
    return InspectionCycleResult(
        CycleId(1),
        config.camera_ids,
        0,
        10,
        1,
        2.01,
        CycleStatus.FAILED if failed else CycleStatus.COMPLETE,
        tuple(
            outcome(
                camera.camera_id,
                first if index == 0 else None,
                failed=failed and index == 0,
            )
            for index, camera in enumerate(config.cameras)
        ),
        (),
    )
