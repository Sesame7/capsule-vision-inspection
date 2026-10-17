"""Session-owned best-effort original-frame archive, independent of result delivery."""

import os
import tempfile
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Condition, Thread
from typing import Protocol, runtime_checkable

import cv2

from vimo_demo.configuration import ImageArchiveSettings
from vimo_demo.inspection import CameraId, CycleId, Frame
from vimo_demo.inspection.production import (
    CameraInspectionResult,
    ChannelBusinessStatus,
    ProductionInspectionResult,
    TechnicalStatus,
)


def camera_archive_status(camera: CameraInspectionResult) -> int | None:
    """NG > ERROR > confirmed EMPTY > all OK; never absence of a frame."""
    if (
        len(camera.channels) != 6
        or camera.collection_issue is not None
        or camera.frame_id is None
        or camera.image_shape is None
    ):
        return None  # Missing/unfinished camera evidence is not an inspected image.
    states = tuple(row.business_status for row in camera.channels)
    if ChannelBusinessStatus.NG in states:
        return 1
    if any(
        row.assessment.technical_status is TechnicalStatus.ERROR
        for row in camera.channels
    ):
        return 2
    if ChannelBusinessStatus.EMPTY in states and all(
        state in (ChannelBusinessStatus.OK, ChannelBusinessStatus.EMPTY)
        for state in states
    ):
        return 3
    if all(state is ChannelBusinessStatus.OK for state in states):
        return 0
    return None


def _component(value: str) -> str:
    # Reversible byte escaping preserves arbitrary CameraId identity and prevents
    # traversal, Windows reserved names and collisions with literal '%' characters.
    encoded = "".join(
        chr(byte)
        if chr(byte).isascii() and (chr(byte).isalnum() or chr(byte) in "-_")
        else f"%{byte:02X}"
        for byte in value.encode("utf-8")
    )
    if encoded.upper() in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }:
        encoded = f"%{ord(encoded[0]):02X}" + encoded[1:]
    return encoded


@dataclass(frozen=True, slots=True)
class ArchiveImage:
    generation: int
    frame: Frame
    observed_at: datetime
    status: int


def write_png(root: Path, item: ArchiveImage) -> None:
    """Encode off the critical path, publish atomically without replacing evidence."""
    stamp = item.observed_at
    directory = root / stamp.strftime("%Y-%m-%d") / _component(item.frame.id.camera_id)
    directory.mkdir(parents=True, exist_ok=True)
    name = f"{item.status}_{stamp:%H%M%S}{stamp.microsecond // 1000:03d}_{item.frame.id.cycle_id}"
    ok, encoded = cv2.imencode(".png", item.frame.image)
    if not ok:
        raise OSError("PNG encoding failed")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=directory, prefix=".archive-", suffix=".part", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(encoded.tobytes())
            stream.flush()
            os.fsync(stream.fileno())
        for collision in range(1000):
            final = directory / f"{name}{'_' + str(collision) if collision else ''}.png"
            try:
                if os.name == "nt":
                    os.rename(
                        temporary, final
                    )  # Windows rename never replaces a destination.
                else:
                    os.link(
                        temporary, final
                    )  # Same-directory atomic no-replace publication.
                break
            except FileExistsError:
                continue
        else:
            raise FileExistsError("Archive filename collision limit reached")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@dataclass(frozen=True, slots=True)
class ArchiveSnapshot:
    generation: int
    written: int
    dropped: int
    failed: int
    retained_bytes: int
    pending: int
    collecting: int
    last_issue: str
    alive: bool


@runtime_checkable
class ImageArchiveCommands(Protocol):
    def archive_snapshot(self) -> ArchiveSnapshot | None: ...


class ImageArchive:
    """One writer; bounds cover retained, queued AND actively written raw pixels.

    Encoding has additional temporary native/encoded storage for one image. This
    is a raw-pixel budget, not a process RSS guarantee. No completed image history.
    Caller transfers an immutable Frame reference until completion/drop/write.
    """

    def __init__(
        self,
        settings: ImageArchiveSettings,
        generation: int,
        cameras: tuple[CameraId, ...],
        *,
        max_pending: int = 12,
        max_bytes: int = 128 * 1024 * 1024,
        max_cycles: int = 8,
        max_frame_bytes: int = 32 * 1024 * 1024,
        writer: Callable[[Path, ArchiveImage], None] = write_png,
        now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    ) -> None:
        if min(max_pending, max_bytes, max_cycles, max_frame_bytes) < 1:
            raise ValueError("Archive limits must be positive")
        if not settings.enabled or len(cameras) != 6 or len(set(cameras)) != 6:
            raise ValueError("Enabled archive requires six distinct cameras")
        self.settings, self.generation, self.cameras = settings, generation, cameras
        self._max_pending, self._max_bytes = max_pending, max_bytes
        self._max_cycles, self._max_frame_bytes = max_cycles, max_frame_bytes
        self._writer, self._now = writer, now
        self._condition = Condition()
        self._cycles: dict[CycleId, dict[CameraId, tuple[Frame, datetime]]] = {}
        self._queue: deque[ArchiveImage] = deque()
        self._bytes = self._active = self._written = self._dropped = self._failed = 0
        self._issue = ""
        self._stopping = False
        self._thread = Thread(
            target=self._run, name=f"ImageArchive-{generation}", daemon=False
        )

    @property
    def thread_name(self) -> str:
        return self._thread.name

    def start(self) -> None:
        self._thread.start()

    def _drop(self, message: str) -> None:
        self._dropped += 1
        self._issue = message

    def begin(self, cycle: CycleId) -> bool:
        with self._condition:
            if (
                self._stopping
                or cycle in self._cycles
                or len(self._cycles) >= self._max_cycles
            ):
                self._drop("Archive cycle capacity/state rejection")
                return False
            self._cycles[cycle] = {}
            return True

    def retain(self, frame: Frame) -> None:
        with self._condition:
            rows = self._cycles.get(frame.id.cycle_id)
            if self._stopping or rows is None:
                return  # Terminal/rejected cycles cannot reopen through late frames.
            if frame.id.camera_id not in self.cameras or frame.id.camera_id in rows:
                self._drop("Archive unknown/duplicate camera frame")
                return
            size = frame.image.nbytes
            if size > self._max_frame_bytes or self._bytes + size > self._max_bytes:
                self._drop("Archive raw-pixel capacity exceeded")
                return
            rows[frame.id.camera_id] = frame, self._now()
            self._bytes += size

    def complete(
        self, cycle: CycleId, result: ProductionInspectionResult | None
    ) -> None:
        with self._condition:
            rows = self._cycles.pop(cycle, None)
            if rows is None:
                return
            matched = (
                result is not None
                and result.cycle_id == cycle
                and result.layout.camera_ids == self.cameras
            )
            if result is not None and not matched:
                self._drop("Archive result identity mismatch")
            outcomes = (
                {row.camera_id: row for row in result.cameras}
                if matched and result is not None
                else {}
            )
            for camera, (frame, stamp) in rows.items():
                outcome = outcomes.get(camera)
                status = camera_archive_status(outcome) if outcome is not None else None
                if outcome is not None and (
                    outcome.frame_id != frame.id
                    or outcome.image_shape != frame.image.shape
                ):
                    self._drop("Archive FrameId/image-shape mismatch")
                    status = None
                enabled = (
                    status is not None
                    and (
                        self.settings.save_ok,
                        self.settings.save_ng,
                        self.settings.save_error,
                        self.settings.save_empty,
                    )[status]
                )
                if enabled and not self._stopping:
                    if len(self._queue) + self._active < self._max_pending:
                        assert status is not None
                        self._queue.append(
                            ArchiveImage(self.generation, frame, stamp, status)
                        )
                        continue
                    self._drop("Archive write queue capacity exceeded")
                self._bytes -= frame.image.nbytes
            self._condition.notify_all()

    def request_stop(self) -> None:
        with self._condition:
            self._stopping = True
            for rows in self._cycles.values():
                self._bytes -= sum(frame.image.nbytes for frame, _ in rows.values())
            self._cycles.clear()
            self._condition.notify_all()  # Drain already accepted writes.

    def join(self, timeout: float | None = None) -> bool:
        if self._thread.ident is not None:
            self._thread.join(timeout)
        return not self._thread.is_alive()

    def snapshot(self) -> ArchiveSnapshot:
        with self._condition:
            return ArchiveSnapshot(
                self.generation,
                self._written,
                self._dropped,
                self._failed,
                self._bytes,
                len(self._queue) + self._active,
                len(self._cycles),
                self._issue,
                self._thread.is_alive(),
            )

    def _run(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(lambda: bool(self._queue) or self._stopping)
                if not self._queue:
                    return
                item = self._queue.popleft()
                self._active = 1
            try:
                self._writer(Path(self.settings.root_directory), item)
            except Exception as exc:
                with self._condition:
                    self._failed += 1
                    self._issue = f"{type(exc).__name__}: {exc}"[:500]
            else:
                with self._condition:
                    self._written += 1
            finally:
                with self._condition:
                    self._active = 0
                    self._bytes -= item.frame.image.nbytes
                del item
