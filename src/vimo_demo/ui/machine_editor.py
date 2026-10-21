"""Qt-free machine geometry working copy; no Recipe inheritance or activation."""

from dataclasses import replace
from pathlib import Path

from vimo_demo.configuration import MachineLayoutDefinition, save_machine_layout
from vimo_demo.inference import BoundingRect
from vimo_demo.inspection import CameraId
from vimo_demo.inspection.production import (
    CameraLayout,
    ChannelId,
    ChannelROI,
    InspectionLayout,
)

from .roi_geometry import fits, pixel


class MachineLayoutDraft:
    def __init__(self, machine: MachineLayoutDefinition, path: Path | None) -> None:
        self.saved = self.definition = machine
        self.path = path
        self.bounds: dict[CameraId, tuple[int, int]] = {}

    @property
    def dirty(self) -> bool:
        return self.definition != self.saved

    def camera(self, identity: CameraId) -> CameraLayout:
        return next(
            c for c in self.definition.layout.cameras if c.camera_id == identity
        )

    def replace_rects(self, camera: CameraId, rects: tuple[BoundingRect, ...]) -> None:
        current = self.camera(camera)
        size = self.bounds.get(camera)
        if len(rects) != len(current.slots):
            raise ValueError("Every configured slot is required")
        if size is not None and any(not fits(r, size) for r in rects):
            raise ValueError("ROI exceeds reference bounds; operation rejected")
        updated = replace(
            current,
            slots=tuple(
                replace(slot, rect=rect)
                for slot, rect in zip(current.slots, rects, strict=True)
            ),
        )
        self.definition = replace(
            self.definition,
            layout=InspectionLayout(
                tuple(
                    updated if c.camera_id == camera else c
                    for c in self.definition.layout.cameras
                )
            ),
        )

    def remove_camera_six(self) -> None:
        """Explicit draft edit; callers confirm loss before invoking this operation."""
        if len(self.definition.layout.cameras) != 6:
            raise ValueError("CAM6 is not configured")
        self.definition = replace(
            self.definition,
            layout=InspectionLayout(
                tuple(
                    row
                    for row in self.definition.layout.cameras
                    if row.camera_id != CameraId("CAM6")
                )
            ),
        )

    def restore_camera_six(self) -> None:
        """Initialize an editable, uncalibrated CAM6 template from current CAM5."""
        if len(self.definition.layout.cameras) != 5:
            raise ValueError("CAM6 is already configured")
        camera = CameraLayout(
            CameraId("CAM6"),
            tuple(
                ChannelROI(slot.slot, ChannelId(31 + slot.slot), replace(slot.rect))
                for slot in self.camera(CameraId("CAM5")).slots
            ),
        )
        self.definition = replace(
            self.definition,
            layout=InspectionLayout((*self.definition.layout.cameras, camera)),
        )

    def common_size(self, camera: CameraId, width: int, height: int) -> None:
        self.replace_rects(
            camera,
            tuple(
                BoundingRect(
                    pixel(s.rect.x + (s.rect.width - width) / 2),
                    pixel(s.rect.y + (s.rect.height - height) / 2),
                    width,
                    height,
                )
                for s in self.camera(camera).slots
            ),
        )

    def distribute(self, camera: CameraId) -> None:
        rects = [s.rect for s in self.camera(camera).slots]
        first, last = rects[0], rects[-1]
        ax, ay = first.x + first.width / 2, first.y + first.height / 2
        bx, by = last.x + last.width / 2, last.y + last.height / 2
        for index in range(1, len(rects) - 1):
            r = rects[index]
            t = index / (len(rects) - 1)
            rects[index] = BoundingRect(
                pixel(ax + (bx - ax) * t - r.width / 2),
                pixel(ay + (by - ay) * t - r.height / 2),
                r.width,
                r.height,
            )
        self.replace_rects(camera, tuple(rects))

    def save(self) -> None:
        if self.path is None:
            raise ValueError("No machine layout persistence path configured")
        for camera in self.definition.layout.cameras:
            size = self.bounds.get(camera.camera_id)
            if size is not None:
                camera.validate_shape((size[1], size[0], 3))
        save_machine_layout(self.path, self.definition)
        self.saved = self.definition

    def discard(self) -> None:
        self.definition = self.saved
