"""One local application's decoded Folder dataset; no results or loading threads."""

import ctypes
import hashlib
import os
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from threading import Lock
from time import monotonic

import cv2
import numpy as np
from numpy.typing import NDArray

from capsule_vision.configuration import CameraSources
from capsule_vision.inference._validation import validate_image
from capsule_vision.inspection.production import InspectionLayout

from . import folder_replay
from .folder_replay import FolderReplay, FolderSourceError


@dataclass(frozen=True, slots=True)
class AvailableMemory:
    available: int
    total: int


class _MemoryStatus(ctypes.Structure):
    _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
        (name, ctypes.c_ulonglong)
        for name in (
            "total_physical",
            "available_physical",
            "total_pagefile",
            "available_pagefile",
            "total_virtual",
            "available_virtual",
            "extended",
        )
    ]


def available_memory() -> AvailableMemory | None:
    """Read current physical RAM; unsupported/unreadable means no safe full cache."""
    try:
        if os.name == "nt":
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(_MemoryStatus)]
            kernel.GlobalMemoryStatusEx.restype = ctypes.c_int
            status = _MemoryStatus()
            status.length = ctypes.sizeof(status)
            if kernel.GlobalMemoryStatusEx(ctypes.byref(status)):
                return AvailableMemory(status.available_physical, status.total_physical)
        elif Path("/proc/meminfo").is_file():
            entries = dict(
                line.split(":", 1)
                for line in Path("/proc/meminfo").read_text().splitlines()
            )
            return AvailableMemory(
                int(entries["MemAvailable"].split()[0]) * 1024,
                int(entries["MemTotal"].split()[0]) * 1024,
            )
    except OSError, ValueError, KeyError:
        pass
    return None


class CacheState(Enum):
    EMPTY = auto()
    LOADING = auto()
    READY = auto()
    HIT = auto()
    FALLBACK = auto()
    CANCELLED = auto()
    FAILED = auto()
    RELEASED = auto()


@dataclass(frozen=True, slots=True)
class FolderCacheStatus:
    state: CacheState = CacheState.EMPTY
    dataset_id: str | None = None
    image_count: int = 0
    loaded_images: int = 0
    decoded_bytes: int = 0
    load_seconds: float = 0.0
    reason: str | None = None


class FolderLoadCancelled(FolderSourceError):
    """Stop/Close observed at a safe preload boundary; no runtime was started."""


class _MemoryUnavailable(Exception):
    pass


# Paths are in configured camera/natural file order. Metadata is deliberately
# cheaper than hashing file contents on every Start; same-metadata edits can evade it.
type FileStamp = tuple[str, int, int, int, int, int, int]
type DatasetStamp = tuple[tuple[FileStamp, ...], ...]


def _stamp(
    paths: list[tuple[Path, ...]], cancelled: Callable[[], bool]
) -> DatasetStamp:
    cameras: list[tuple[FileStamp, ...]] = []
    for files in paths:
        rows: list[FileStamp] = []
        for path in files:
            _check_cancel(cancelled)
            try:
                stat = path.stat()
                rows.append(
                    (
                        str(path.resolve()),
                        stat.st_size,
                        stat.st_mtime_ns,
                        stat.st_ctime_ns,
                        stat.st_dev,
                        stat.st_ino,
                        stat.st_mode,
                    )
                )
            except OSError as exc:
                raise FolderSourceError(f"Stat {path}: {exc}") from exc
        cameras.append(tuple(rows))
    return tuple(cameras)


def _check_cancel(cancelled: Callable[[], bool]) -> None:
    if cancelled():
        raise FolderLoadCancelled("Folder preload cancelled by Stop/Close")


@dataclass(frozen=True, slots=True)
class _Dataset:
    sources: CameraSources
    stamp: DatasetStamp
    dataset_id: str
    paths: tuple[tuple[Path, ...], ...]
    images: tuple[tuple[NDArray[np.uint8], ...], ...]
    status: FolderCacheStatus


class _CachedReplay(FolderReplay):
    cached = True

    def __init__(
        self, layout: InspectionLayout, sources: CameraSources, dataset: _Dataset
    ) -> None:
        self.layout, self.sources = layout, sources
        self.paths = list(dataset.paths)
        self.indices = [0] * len(layout.cameras)
        self._images = dataset.images

    def next_group(self) -> tuple[NDArray[np.uint8], ...]:
        group = tuple(
            images[index % len(images)]
            for images, index in zip(self._images, self.indices, strict=True)
        )
        for index in range(len(self.indices)):
            self.indices[index] += 1
        return group


class FolderReplayCache:
    """Loading has one lifecycle-thread owner. Snapshots/Close release are locked.

    One atomic six-camera dataset, retained across Stop, never a result cache.
    Replays borrow readonly pixels; ingress and display still copy independently.
    """

    def __init__(
        self,
        *,
        memory: Callable[[], AvailableMemory | None] = available_memory,
        notify: Callable[[FolderCacheStatus], None] = lambda _: None,
    ) -> None:
        self._memory, self._notify = memory, notify
        self._lock = Lock()
        self._dataset: _Dataset | None = None
        self._status = FolderCacheStatus()

    def snapshot(self) -> FolderCacheStatus:
        with self._lock:
            return self._status

    def clear(self) -> None:
        with self._lock:
            self._dataset = None
            self._status = FolderCacheStatus(CacheState.RELEASED)

    def _publish(self, status: FolderCacheStatus) -> None:
        with self._lock:
            self._status = status
        self._notify(status)  # Metadata only; consumers must be short/nonblocking.

    def _budget(self, next_bytes: int = 0) -> None:
        memory = self._memory()
        if memory is None:
            raise _MemoryUnavailable("Physical memory telemetry unavailable")
        reserve = max(2 * 1024**3, memory.total // 10)
        if memory.available - next_bytes < reserve:
            raise _MemoryUnavailable(
                f"Available RAM {memory.available} bytes cannot retain {reserve} bytes of system/runtime reserve"
            )

    def replay(
        self,
        layout: InspectionLayout,
        sources: CameraSources,
        base: Path,
        *,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> FolderReplay:
        """Resolve on every Start; publish only after full decode and metadata recheck."""
        started = monotonic()
        loaded = decoded_bytes = 0
        count = 0
        dataset_id: str | None = None
        groups: list[tuple[NDArray[np.uint8], ...]] = []
        images: list[NDArray[np.uint8]] = []
        image: NDArray[np.uint8] | None = None
        previous: _Dataset | None = None
        complete: _Dataset | None = None
        cached: tuple[NDArray[np.uint8], ...] = ()
        try:
            _check_cancel(cancelled)
            if not sources.loop:
                self.clear()
                return FolderReplay(layout, sources, base)
            replay = FolderReplay(layout, sources, base)
            count = sum(len(p) for p in replay.paths)
            stamp = _stamp(replay.paths, cancelled)
            dataset_id = hashlib.sha256(
                repr((sources, layout.camera_ids, stamp)).encode()
            ).hexdigest()
            with self._lock:
                previous = self._dataset
                self._dataset = None  # Never leave stale pixels after a failed rebuild.
            if previous is not None and previous.dataset_id == dataset_id:
                self._budget()
                for camera, cached in zip(layout.cameras, previous.images, strict=True):
                    try:
                        camera.validate_shape(
                            (cached[0].shape[0], cached[0].shape[1], 3)
                        )
                    except ValueError as exc:
                        raise FolderSourceError(f"{camera.camera_id}: {exc}") from exc
                _check_cancel(cancelled)
                with self._lock:
                    self._dataset = previous
                self._publish(
                    FolderCacheStatus(
                        CacheState.HIT,
                        dataset_id,
                        previous.status.image_count,
                        previous.status.loaded_images,
                        previous.status.decoded_bytes,
                        previous.status.load_seconds,
                    )
                )
                return _CachedReplay(layout, sources, previous)
            previous = None
            cached = ()
            self._publish(FolderCacheStatus(CacheState.LOADING, dataset_id, count))
            for camera, files in zip(layout.cameras, replay.paths, strict=True):
                images = []
                shape: tuple[int, ...] | None = None
                for path in files:
                    _check_cancel(cancelled)
                    self._budget(images[-1].nbytes if images else 0)
                    try:
                        image = validate_image(folder_replay.decode_image(path))
                        _check_cancel(cancelled)
                        camera.validate_shape((image.shape[0], image.shape[1], 3))
                        if shape is not None and image.shape != shape:
                            raise ValueError(
                                f"Resolution changed from {shape} to {image.shape}; no automatic resizing"
                            )
                    except FolderLoadCancelled:
                        raise
                    except (FolderSourceError, ValueError, TypeError) as exc:
                        cause = exc.__cause__
                        if (
                            isinstance(cause, cv2.error)
                            and cause.code == cv2.Error.StsNoMem
                        ):
                            raise _MemoryUnavailable(
                                f"OpenCV cannot allocate {path}"
                            ) from exc
                        raise FolderSourceError(
                            f"{camera.camera_id} / {path.name}: {exc}"
                        ) from exc
                    self._budget()
                    shape = image.shape
                    image.setflags(write=False)
                    # A readonly view over a readonly base prevents simply re-enabling
                    # writes on the consumer array; this copies no pixel storage.
                    images.append(image.view())
                    loaded += 1
                    decoded_bytes += image.nbytes
                    if loaded % 100 == 0:
                        self._publish(
                            FolderCacheStatus(
                                CacheState.LOADING,
                                dataset_id,
                                count,
                                loaded,
                                decoded_bytes,
                                monotonic() - started,
                            )
                        )
                groups.append(tuple(images))
            _check_cancel(cancelled)
            # Re-enumerate too: additions/removals during loading invalidate the snapshot.
            after = FolderReplay(layout, sources, base)
            if _stamp(after.paths, cancelled) != stamp:
                raise FolderSourceError(
                    "Folder dataset changed during preload; Start again explicitly"
                )
            status = FolderCacheStatus(
                CacheState.READY,
                dataset_id,
                count,
                loaded,
                decoded_bytes,
                monotonic() - started,
            )
            complete = _Dataset(
                sources, stamp, dataset_id, tuple(replay.paths), tuple(groups), status
            )
            _check_cancel(cancelled)
            with self._lock:
                self._dataset = complete
            self._publish(status)
            return _CachedReplay(layout, sources, complete)
        except (MemoryError, _MemoryUnavailable) as exc:
            # Release partial arrays BEFORE the on-demand first-group preflight.
            image = None
            images.clear()
            groups.clear()
            previous = complete = None
            cached = ()
            with self._lock:
                self._dataset = None
            if cancelled():
                self._publish(
                    FolderCacheStatus(
                        CacheState.CANCELLED,
                        dataset_id,
                        load_seconds=monotonic() - started,
                        reason="Stop/Close during memory fallback",
                    )
                )
                raise FolderLoadCancelled(
                    "Folder preload cancelled by Stop/Close"
                ) from exc
            self._publish(
                FolderCacheStatus(
                    CacheState.FALLBACK,
                    dataset_id,
                    image_count=count,
                    loaded_images=loaded,
                    load_seconds=monotonic() - started,
                    reason=str(exc),
                )
            )
            return FolderReplay(layout, sources, base)
        except BaseException as exc:
            retained_status: FolderCacheStatus | None = None
            if isinstance(exc, FolderLoadCancelled):
                with self._lock:
                    retained = self._dataset or previous
                    if (
                        retained is not None
                        and retained.sources == sources
                        and (dataset_id is None or dataset_id == retained.dataset_id)
                    ):
                        # Cancelling validation of an unchanged warm cache does not
                        # retire it. Every later Start must still validate it afresh.
                        self._dataset = retained
                        retained_status = retained.status
                    retained = None
            # Chained Future errors preserve traceback locals; discard bulk pixels
            # here too, not just the cache owner, on cancellation/data failure.
            image = None
            images.clear()
            groups.clear()
            previous = complete = None
            cached = ()
            with self._lock:
                if retained_status is None:
                    self._dataset = None
            self._publish(
                FolderCacheStatus(
                    CacheState.CANCELLED
                    if isinstance(exc, FolderLoadCancelled)
                    else CacheState.FAILED,
                    dataset_id,
                    image_count=retained_status.image_count if retained_status else 0,
                    loaded_images=retained_status.loaded_images
                    if retained_status
                    else 0,
                    decoded_bytes=retained_status.decoded_bytes
                    if retained_status
                    else 0,
                    load_seconds=monotonic() - started,
                    reason=str(exc),
                )
            )
            raise
