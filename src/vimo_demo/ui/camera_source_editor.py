"""Single-owner camera-source draft, independent of machine and Recipe persistence."""

from dataclasses import replace
from pathlib import Path
from typing import Literal

from vimo_demo.configuration import CameraSources, save_camera_sources
from vimo_demo.inspection import CameraId


class CameraSourceDraft:
    def __init__(self, sources: CameraSources, path: Path) -> None:
        self.saved = self.definition = sources
        self.path = path

    @property
    def dirty(self) -> bool:
        return self.definition != self.saved

    def set_folder(self, camera: CameraId, folder: str) -> None:
        self.definition = replace(
            self.definition,
            cameras=tuple(
                replace(c, folder=folder) if c.camera_id == camera else c
                for c in self.definition.cameras
            ),
        )

    def set_loop(self, enabled: bool) -> None:
        self.definition = replace(self.definition, loop=enabled)

    def set_fps(self, value: float) -> None:
        self.definition = replace(self.definition, target_fps=value)

    def set_mode(self, mode: Literal["folder", "basler"]) -> None:
        self.definition = replace(self.definition, mode=mode)

    def set_serial(self, camera: CameraId, serial: str) -> None:
        self.definition = replace(
            self.definition,
            cameras=tuple(
                replace(c, basler_serial=serial) if c.camera_id == camera else c
                for c in self.definition.cameras
            ),
        )

    def save(self) -> None:
        save_camera_sources(self.path, self.definition)
        self.saved = self.definition

    def discard(self) -> None:
        self.definition = self.saved
