"""Latest-only display copies; no Qt, masks, history or inspection decisions."""

from dataclasses import dataclass
from threading import Lock

import numpy as np
from numpy.typing import NDArray

from vimo_demo.inference._validation import validate_image
from vimo_demo.inspection import CameraId, Frame, FrameId
from vimo_demo.inspection.models import timestamp


@dataclass(frozen=True, slots=True, eq=False)
class DisplayFrame:
    frame_id: FrameId
    generation: int
    captured_at: float
    revision: int
    image: NDArray[np.uint8]
    source_key: str | None = None


@dataclass(frozen=True, slots=True)
class DisplaySnapshot:
    revision: int
    frames: tuple[DisplayFrame, ...]


class LatestDisplayFrameStore:
    """One immutable byte-backed copy per camera; callers never share producer pixels.

    Publish only accepted frames, with the admitting application's generation.
    Producer aliases must remain immutable during publish (and throughout their
    separate inference lease). Old generation/cycle/sequence publications are ignored.
    """

    def __init__(self, cameras: tuple[CameraId, ...]) -> None:
        if not cameras or len(set(cameras)) != len(cameras):
            raise ValueError("Expected unique camera identities")
        self.cameras = cameras
        self._lock = Lock()
        self._frames: dict[CameraId, DisplayFrame] = {}
        self._revision = 0

    def publish(
        self, frame: Frame, *, generation: int, source_key: str | None = None
    ) -> bool:
        return self.publish_pixels(
            frame.id,
            frame.captured_at,
            frame.image,
            generation=generation,
            source_key=source_key,
        )

    def publish_pixels(
        self,
        frame_id: FrameId,
        captured_at: float,
        pixels: NDArray[np.uint8],
        *,
        generation: int,
        source_key: str | None = None,
    ) -> bool:
        """Copy identified source pixels, including readonly borrowed cache views.

        This is display publication, not transfer into a domain Frame. All pixel
        aliases stay immutable for this call; the store keeps only its own copy.
        """
        timestamp(captured_at)
        pixels = validate_image(pixels)
        if frame_id.camera_id not in self.cameras:
            raise ValueError("Unknown display camera")
        if type(generation) is not int or generation < 0:
            raise ValueError("Invalid generation")
        # bytes backing prevents consumers from re-enabling ndarray writes.
        image = np.frombuffer(pixels.tobytes(), dtype=np.uint8).reshape(pixels.shape)
        with self._lock:
            previous = self._frames.get(frame_id.camera_id)
            if previous is not None and (
                generation,
                frame_id.cycle_id,
                frame_id.sequence,
            ) <= (
                previous.generation,
                previous.frame_id.cycle_id,
                previous.frame_id.sequence,
            ):
                return False
            self._revision += 1
            self._frames[frame_id.camera_id] = DisplayFrame(
                frame_id,
                generation,
                captured_at,
                self._revision,
                image,
                source_key,
            )
            return True

    def snapshot(self) -> DisplaySnapshot:
        with self._lock:
            return DisplaySnapshot(
                self._revision,
                tuple(self._frames[c] for c in self.cameras if c in self._frames),
            )
