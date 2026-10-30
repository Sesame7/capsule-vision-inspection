"""Latest metadata and bounded immutable JPEGs, encoded by the service publisher."""

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from threading import Lock
from typing import Protocol
from urllib.parse import quote
from uuid import uuid4

import cv2

from capsule_vision.application import ApplicationState, ProductionApplicationSnapshot
from capsule_vision.display import (
    DisplayFrame,
    LatestDisplayFrameStore,
    matching_camera,
)
from capsule_vision.inspection import CameraId


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
class _Delivery:
    key: str
    epoch: int
    scope: tuple[int, int, tuple[int, int, int], str | None]
    image: EncodedImage


@dataclass(frozen=True, slots=True)
class Publication:
    publisher_id: str
    revision: int
    metadata: bytes
    camera_ids: tuple[CameraId, ...]
    images: tuple[EncodedImage, ...]
    deliveries: tuple[_Delivery, ...]


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
    """One in-progress observation; current/previous encoded images per camera.

    publish() has one serial service-thread owner. HTTP reads only immutable bytes;
    it never retains domain results, input pixels or an inference reference.
    """

    def __init__(
        self,
        source: SnapshotSource,
        frames: LatestDisplayFrameStore,
        cameras: tuple[CameraId, ...] | None = None,
        *,
        refresh_interval_ms: float = 500,
    ) -> None:
        configured = cameras
        cameras = frames.cameras if cameras is None else cameras
        if (
            len(cameras) > 6
            or len(set(cameras)) != len(cameras)
            or not set(cameras) <= set(frames.cameras)
        ):
            raise ValueError("Expected at most six unique configured display cameras")
        self.source, self.frames, self._cameras = source, frames, configured
        self.refresh_interval_ms = refresh_interval_ms
        self.publisher_id = uuid4().hex
        self._lock = Lock()
        self._latest: Publication | None = None
        self._error: str | None = None
        self._image_epoch = 0

    @property
    def cameras(self) -> tuple[CameraId, ...]:
        return self.frames.cameras if self._cameras is None else self._cameras

    def latest(self) -> Publication | None:
        with self._lock:
            return self._latest

    def error(self) -> str | None:
        with self._lock:
            return self._error

    def failed(self, error: Exception) -> None:
        with self._lock:
            self._error = f"{type(error).__name__}: {error}"
            if self._latest is not None:
                self._latest = replace(self._latest, images=(), deliveries=())

    def clear(self) -> None:
        """Release encoded bytes only after the service confirms both owners exited."""
        with self._lock:
            self._latest = None

    def publish(self) -> None:
        snapshot = self.source.snapshot()
        display = self.frames.snapshot()
        cameras = self.cameras
        observed_at = datetime.now(UTC).isoformat()
        previous = self.latest()
        revision = previous.revision + 1 if previous else 1
        frames = {frame.frame_id.camera_id: frame for frame in display.frames}
        images: list[EncodedImage] = []
        deliveries: list[_Delivery] = []
        rows: list[dict[str, object]] = []
        for camera_id in cameras:
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
                identity: dict[str, object] = {
                    "generation": frame.generation,
                    "cycle_id": int(frame.frame_id.cycle_id),
                    "camera_id": str(frame.frame_id.camera_id),
                    "sequence": frame.frame_id.sequence,
                    "frame_revision": frame.revision,
                    "captured_at": frame.captured_at,
                    "shape": list(frame.image.shape),
                }
                row["image_identity"] = identity
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
                        shape = (
                            frame.image.shape[0],
                            frame.image.shape[1],
                            frame.image.shape[2],
                        )
                        scope = (
                            snapshot.generation,
                            frame.generation,
                            shape,
                            frame.source_key,
                        )
                        old = (
                            tuple(
                                delivery
                                for delivery in previous.deliveries
                                if delivery.image.camera_id == camera_id
                                and delivery.scope == scope
                            )
                            if previous is not None
                            else ()
                        )
                        if old:
                            epoch = old[0].epoch
                        else:
                            self._image_epoch += 1
                            epoch = self._image_epoch
                        key = f"{epoch}-{frame.generation}-{int(frame.frame_id.cycle_id)}-{frame.frame_id.sequence}-{frame.revision}-{shape[0]}-{shape[1]}"
                        if old and old[0].key == key:
                            current = old
                        else:
                            current = (
                                _Delivery(key, epoch, scope, encode_image(frame)),
                                *old[:1],
                            )
                        image = current[0].image
                    except Exception as exc:
                        row["image_status"] = "unavailable"
                        row["result_status"] = "unavailable"
                        row["channels"] = []
                        row["encoding_error"] = f"{type(exc).__name__}: {exc}"
                    else:
                        images.append(image)
                        deliveries.extend(current)
                        identity["image_epoch"] = epoch
                        row["image_url"] = (
                            f"/api/cameras/{quote(str(camera_id), safe='')}.jpg?publisher={self.publisher_id}&image={key}"
                        )
                        row["jpeg_dimensions"] = {
                            "width": image.width,
                            "height": image.height,
                        }
            rows.append(row)
        result = snapshot.latest_production_result
        value: dict[str, object] = {
            "schema_version": 1,
            "refresh_interval_ms": self.refresh_interval_ms,
            "publisher_id": self.publisher_id,
            "revision": revision,
            "observed_at": observed_at,
            "published_at": datetime.now(UTC).isoformat(),
            "application_state": snapshot.state.name,
            "generation": snapshot.generation,
            "latest_cycle_id": int(result.cycle_id) if result else None,
            "display_revision": display.revision,
            "configured_camera_ids": [str(camera) for camera in cameras],
            "cameras": rows,
        }
        publication = Publication(
            self.publisher_id,
            revision,
            json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            ),
            cameras,
            tuple(images),
            tuple(deliveries),
        )
        with self._lock:
            self._latest = publication
            self._error = None
