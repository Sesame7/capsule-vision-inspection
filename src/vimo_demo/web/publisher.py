"""Latest-only immutable publication; encoding runs on the service's publisher."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock
from typing import Protocol
from urllib.parse import quote
from uuid import uuid4

import cv2

from vimo_demo.application import ApplicationState, ProductionApplicationSnapshot
from vimo_demo.inspection import CameraId
from vimo_demo.ui.display_frames import DisplayFrame, LatestDisplayFrameStore
from vimo_demo.ui.production_presentation import matching_camera


class SnapshotSource(Protocol):
    def snapshot(self) -> ProductionApplicationSnapshot: ...


@dataclass(frozen=True, slots=True)
class EncodedImage:
    camera_id: CameraId
    frame_revision: int
    width: int
    height: int
    jpeg: bytes


@dataclass(frozen=True, slots=True)
class Publication:
    publisher_id: str
    revision: int
    metadata: bytes
    images: tuple[EncodedImage, ...]


def encode_image(frame: DisplayFrame) -> EncodedImage:
    """OpenCV takes BGR directly; only the encoded copy is scaled."""
    height, width, _ = frame.image.shape
    scale = min(1.0, 640 / max(width, height))
    w, h = max(1, round(width * scale)), max(1, round(height * scale))
    pixels = (
        cv2.resize(frame.image, (w, h), interpolation=cv2.INTER_AREA)
        if scale < 1
        else frame.image
    )
    success, encoded = cv2.imencode(".jpg", pixels, [cv2.IMWRITE_JPEG_QUALITY, 70])
    if not success:
        raise ValueError("JPEG encoding failed")
    return EncodedImage(
        frame.frame_id.camera_id, frame.revision, w, h, encoded.tobytes()
    )


class SnapshotPublisher:
    """One in-progress observation and one cached publication, without a queue.

    publish() has one serial service-thread owner. HTTP reads only immutable bytes;
    it never retains domain results, input pixels or an inference reference.
    """

    def __init__(
        self,
        source: SnapshotSource,
        frames: LatestDisplayFrameStore,
        cameras: tuple[CameraId, ...] | None = None,
    ) -> None:
        cameras = frames.cameras if cameras is None else cameras
        if (
            len(cameras) > 6
            or len(set(cameras)) != len(cameras)
            or not set(cameras) <= set(frames.cameras)
        ):
            raise ValueError("Expected at most six unique configured display cameras")
        self.source, self.frames, self.cameras = source, frames, cameras
        self.publisher_id = uuid4().hex
        self._lock = Lock()
        self._latest: Publication | None = None
        self._error: str | None = None

    def latest(self) -> Publication | None:
        with self._lock:
            return self._latest

    def error(self) -> str | None:
        with self._lock:
            return self._error

    def failed(self, error: Exception) -> None:
        with self._lock:
            self._error = f"{type(error).__name__}: {error}"

    def publish(self) -> None:
        snapshot = self.source.snapshot()
        display = self.frames.snapshot()
        observed_at = datetime.now(UTC).isoformat()
        previous = self.latest()
        revision = previous.revision + 1 if previous else 1
        old_images = (
            {image.camera_id: image for image in previous.images} if previous else {}
        )
        frames = {frame.frame_id.camera_id: frame for frame in display.frames}
        images: list[EncodedImage] = []
        rows: list[dict[str, object]] = []
        for camera_id in self.cameras:
            frame = frames.get(camera_id)
            row: dict[str, object] = {
                "camera_id": str(camera_id),
                "image_status": "unavailable",
                "result_status": "unavailable",
                "image_identity": None,
                "image_url": None,
                "channels": [],
            }
            if frame is not None:
                row["image_identity"] = {
                    "generation": frame.generation,
                    "cycle_id": int(frame.frame_id.cycle_id),
                    "camera_id": str(frame.frame_id.camera_id),
                    "sequence": frame.frame_id.sequence,
                    "frame_revision": frame.revision,
                    "captured_at": frame.captured_at,
                    "shape": list(frame.image.shape),
                }
                stale = frame.generation != snapshot.generation
                hidden = stale and snapshot.state in (
                    ApplicationState.STARTING,
                    ApplicationState.RUNNING,
                    ApplicationState.STOPPING,
                )
                if frame.generation == 0:
                    row["image_status"] = "reference"
                elif hidden:
                    row["image_status"] = "stale"
                    row["result_status"] = "stale"
                else:
                    row["image_status"] = (
                        "current"
                        if snapshot.state is ApplicationState.RUNNING
                        else "retained"
                    )
                    camera = matching_camera(frame, snapshot)
                    row["result_status"] = (
                        "matched"
                        if camera
                        else "stale"
                        if stale
                        else "pending"
                        if snapshot.state is ApplicationState.RUNNING
                        else "unavailable"
                    )
                    if camera is not None:
                        row["channels"] = [
                            {
                                "channel_id": int(channel.channel_id),
                                "business_status": channel.business_status.name,
                                "technical_status": channel.assessment.technical_status.name,
                            }
                            for channel in camera.channels
                        ]
                    try:
                        old = old_images.get(camera_id)
                        image = (
                            old
                            if old is not None and old.frame_revision == frame.revision
                            else encode_image(frame)
                        )
                    except Exception as exc:
                        row["image_status"] = "unavailable"
                        row["result_status"] = "unavailable"
                        row["channels"] = []
                        row["encoding_error"] = f"{type(exc).__name__}: {exc}"
                    else:
                        images.append(image)
                        row["image_url"] = (
                            f"/api/cameras/{quote(str(camera_id), safe='')}.jpg?publisher={self.publisher_id}&revision={revision}"
                        )
                        row["jpeg_dimensions"] = {
                            "width": image.width,
                            "height": image.height,
                        }
            rows.append(row)
        result = snapshot.latest_production_result
        value: dict[str, object] = {
            "schema_version": 1,
            "publisher_id": self.publisher_id,
            "revision": revision,
            "observed_at": observed_at,
            "published_at": datetime.now(UTC).isoformat(),
            "application_state": snapshot.state.name,
            "generation": snapshot.generation,
            "latest_cycle_id": int(result.cycle_id) if result else None,
            "display_revision": display.revision,
            "configured_camera_ids": [str(camera) for camera in self.cameras],
            "cameras": rows,
        }
        publication = Publication(
            self.publisher_id,
            revision,
            json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            ),
            tuple(images),
        )
        with self._lock:
            self._latest = publication
            self._error = None
