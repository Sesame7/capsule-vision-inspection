"""Folder-only camera assignments, separate from machine geometry and Recipes."""

import math
import os
from dataclasses import dataclass
from pathlib import Path

from vimo_demo.inspection import CameraId

from .errors import ConfigurationError


@dataclass(frozen=True, slots=True)
class CameraFolder:
    camera_id: CameraId
    folder: str
    source_type: str = "folder"

    def __post_init__(self) -> None:
        if type(self.camera_id) is not str or not self.camera_id.strip():
            raise ConfigurationError("Camera identity must be nonempty")
        if type(self.folder) is not str or self.source_type != "folder":
            raise ConfigurationError("Only Folder sources are supported")

    def path(self, base: Path) -> Path:
        path = Path(self.folder)
        return path if path.is_absolute() else base / path

    def key(self, base: Path) -> str:
        # Lexical identity, no filesystem traversal on Qt (including UNC paths).
        return os.path.normcase(os.path.abspath(self.path(base))) if self.folder else ""


@dataclass(frozen=True, slots=True)
class CameraSources:
    cameras: tuple[CameraFolder, ...]
    loop: bool = True
    target_fps: float = 8.0
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1 or type(self.schema_version) is not int:
            raise ConfigurationError("Unsupported camera-source schema")
        if type(self.loop) is not bool:
            raise ConfigurationError("Loop must be boolean")
        if (
            type(self.target_fps) not in (int, float)
            or not math.isfinite(self.target_fps)
            or self.target_fps <= 0
        ):
            raise ConfigurationError("Target FPS must be positive and finite")
        ids = tuple(c.camera_id for c in self.cameras)
        if not ids or len(set(ids)) != len(ids):
            raise ConfigurationError("Expected unique camera-source identities")

    def validate_cameras(self, cameras: tuple[CameraId, ...]) -> None:
        if set(cameras) != {c.camera_id for c in self.cameras}:
            raise ConfigurationError(
                "Camera sources must match configured camera identities exactly"
            )

    def camera(self, identity: CameraId) -> CameraFolder:
        return next(c for c in self.cameras if c.camera_id == identity)
