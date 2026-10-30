"""Camera source selection, separate from machine geometry and Recipes."""

import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from capsule_vision.inspection import CameraId

from .errors import ConfigurationError


@dataclass(frozen=True, slots=True)
class CameraFolder:
    """Optional Folder reference and hardware identity for one configured camera."""

    camera_id: CameraId
    folder: str = ""
    basler_serial: str = ""

    def __post_init__(self) -> None:
        if type(self.camera_id) is not str or self.camera_id not in tuple(
            f"CAM{i}" for i in range(1, 7)
        ):
            raise ConfigurationError("Camera identity must be CAM1..CAM6")
        if type(self.folder) is not str:
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
    cameras: tuple[CameraFolder, ...] = ()
    loop: bool = True
    target_fps: float = 8.0
    mode: Literal["folder", "basler"] = "folder"

    def __post_init__(self) -> None:
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
        if len(set(ids)) != len(ids):
            raise ConfigurationError("Expected unique camera-source identities")
        object.__setattr__(
            self, "cameras", tuple(sorted(self.cameras, key=lambda c: c.camera_id))
        )

    def validate_cameras(
        self, cameras: tuple[CameraId, ...], *, complete: bool = True
    ) -> None:
        """Check explicit references; runtime callers require the complete set."""
        assigned = {c.camera_id for c in self.cameras}
        if assigned - set(cameras) or (complete and set(cameras) != assigned):
            raise ConfigurationError(
                "Camera sources must match configured camera identities exactly"
            )

    def validate_mapping(self) -> None:
        """Connection/preparation boundary; saved definitions may be incomplete."""
        if self.mode == "basler":
            serials = tuple(c.basler_serial for c in self.cameras)
            if (
                len(serials) not in (5, 6)
                or any(not s for s in serials)
                or len(set(serials)) != len(serials)
            ):
                raise ConfigurationError(
                    "Basler mode requires five or six distinct nonempty serials"
                )

    def basler_serials(self, cameras: tuple[CameraId, ...]) -> tuple[str, ...]:
        self.validate_cameras(cameras)
        self.validate_mapping()
        if self.mode != "basler":
            raise ConfigurationError("Basler mapping requires Basler mode")
        return tuple(self.camera(c).basler_serial for c in cameras)

    def source_key(self, camera: CameraId, base: Path) -> str:
        row = self.camera(camera)
        return f"basler:{row.basler_serial}" if self.mode == "basler" else row.key(base)

    def camera(self, identity: CameraId) -> CameraFolder:
        return next(
            (c for c in self.cameras if c.camera_id == identity), CameraFolder(identity)
        )
