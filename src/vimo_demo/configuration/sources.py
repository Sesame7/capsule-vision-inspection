"""Camera source selection, separate from machine geometry and Recipes."""

import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from vimo_demo.inspection import CameraId

from .errors import ConfigurationError


@dataclass(frozen=True, slots=True)
class CameraFolder:
    """Folder reference and optional hardware identity for one configured camera.

    source_type is retained for the v1 Python constructor; v2 persists global mode.
    """

    camera_id: CameraId
    folder: str
    source_type: str = "folder"
    basler_serial: str = ""

    def __post_init__(self) -> None:
        if type(self.camera_id) is not str or not self.camera_id.strip():
            raise ConfigurationError("Camera identity must be nonempty")
        if type(self.folder) is not str or self.source_type != "folder":
            raise ConfigurationError("Invalid Folder reference")
        if type(self.basler_serial) is not str or (
            self.basler_serial
            and not (self.basler_serial.isascii() and self.basler_serial.isdecimal())
        ):
            raise ConfigurationError(
                "Basler serial must be empty or numeric ASCII text"
            )

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
    schema_version: int = 2
    mode: Literal["folder", "basler"] = "folder"

    def __post_init__(self) -> None:
        if self.schema_version != 2 or type(self.schema_version) is not int:
            raise ConfigurationError("Unsupported camera-source schema")
        if self.mode not in ("folder", "basler"):
            raise ConfigurationError("Unsupported camera-source mode")
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
        self.validate_mapping()

    def validate_mapping(self) -> None:
        """Persisted Basler mappings must be complete; UI drafts may be incomplete."""
        if self.mode == "basler":
            serials = tuple(c.basler_serial for c in self.cameras)
            if (
                len(serials) != 6
                or any(not s for s in serials)
                or len(set(serials)) != 6
            ):
                raise ConfigurationError(
                    "Basler mode requires six distinct nonempty serials"
                )

    def basler_serials(self, cameras: tuple[CameraId, ...]) -> tuple[str, ...]:
        self.validate_cameras(cameras)
        if self.mode != "basler":
            raise ConfigurationError("Basler mapping requires Basler mode")
        return tuple(self.camera(c).basler_serial for c in cameras)

    def source_key(self, camera: CameraId, base: Path) -> str:
        row = self.camera(camera)
        return f"basler:{row.basler_serial}" if self.mode == "basler" else row.key(base)

    def camera(self, identity: CameraId) -> CameraFolder:
        return next(c for c in self.cameras if c.camera_id == identity)
