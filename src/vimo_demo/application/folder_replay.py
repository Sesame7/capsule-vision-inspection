"""Deterministic, on-demand folder replay; no threads, queues or camera SDK."""

import re
from pathlib import Path

import cv2
import numpy as np
from numpy.typing import NDArray

from vimo_demo.configuration.sources import CameraSources
from vimo_demo.inference._validation import uint8_array
from vimo_demo.inspection.production import InspectionLayout

EXTENSIONS = frozenset((".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"))


class FolderSourceError(RuntimeError):
    """Source data/IO failure, never a product judgment."""


def natural_key(path: Path) -> tuple[tuple[tuple[int, str | int], ...], str]:
    # Tagged segments compare safely; original spelling breaks case/numeric ties.
    parts = tuple(
        (1, int(s)) if s.isdigit() else (0, s.casefold())
        for s in re.split(r"([0-9]+)", path.name)
    )
    return parts, path.name


def folder_images(folder: Path) -> tuple[Path, ...]:
    try:
        if not folder.is_dir():
            raise FolderSourceError(f"Image folder unavailable: {folder}")
        paths = tuple(
            sorted(
                (
                    p
                    for p in folder.iterdir()
                    if p.is_file() and p.suffix.casefold() in EXTENSIONS
                ),
                key=natural_key,
            )
        )
        if not paths:
            raise FolderSourceError(f"Image folder is empty: {folder}")
        return paths
    except OSError as exc:
        raise FolderSourceError(f"Enumerate {folder}: {exc}") from exc


def decode_image(path: Path) -> NDArray[np.uint8]:
    try:
        image = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("Image cannot be decoded")
        return uint8_array(image)
    except (OSError, ValueError, cv2.error) as exc:
        raise FolderSourceError(f"Decode {path}: {type(exc).__name__}: {exc}") from exc


class FolderReplay:
    """One serial owner. Paths are snapshotted; only one decoded group is live."""

    cached = False

    def __init__(
        self, layout: InspectionLayout, sources: CameraSources, base: Path
    ) -> None:
        sources.validate_cameras(layout.camera_ids)
        self.layout, self.sources = layout, sources
        self.paths: list[tuple[Path, ...]] = []
        for camera in layout.cameras:
            source = sources.camera(camera.camera_id)
            if not source.folder.strip():
                raise FolderSourceError(
                    f"{camera.camera_id}: no Folder source configured"
                )
            try:
                self.paths.append(folder_images(source.path(base)))
            except FolderSourceError as exc:
                raise FolderSourceError(f"{camera.camera_id}: {exc}") from exc
        self.indices = [0] * len(layout.cameras)
        self._shapes: list[tuple[int, ...] | None] = [None] * len(layout.cameras)

    def next_group(self) -> tuple[NDArray[np.uint8], ...] | None:
        if not self.sources.loop and any(
            i >= len(paths) for i, paths in zip(self.indices, self.paths, strict=True)
        ):
            return None
        images: list[NDArray[np.uint8]] = []
        for index, (camera, paths) in enumerate(
            zip(self.layout.cameras, self.paths, strict=True)
        ):
            path = paths[self.indices[index] % len(paths)]
            try:
                image = decode_image(path)
                camera.validate_shape((image.shape[0], image.shape[1], image.shape[2]))
                previous = self._shapes[index]
                if previous is not None and image.shape != previous:
                    raise ValueError(
                        f"Resolution changed from {previous} to {image.shape}; no automatic resizing"
                    )
                image.setflags(write=False)
                images.append(image)
            except (FolderSourceError, ValueError) as exc:
                raise FolderSourceError(
                    f"{camera.camera_id} / {path.name}: {exc}"
                ) from exc
        # Advance only after a complete, valid group; never submit partial groups.
        for index, image in enumerate(images):
            self.indices[index] += 1
            self._shapes[index] = image.shape
        return tuple(images)


def start_interval_wait(started: float, now: float, target_fps: float) -> float:
    """Only remaining start-to-start budget, no fixed post-result delay."""
    return max(0.0, started + 1 / target_fps - now)
