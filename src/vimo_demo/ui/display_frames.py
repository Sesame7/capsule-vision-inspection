"""Latest-only display copies; no Qt, masks, history or inspection decisions."""

from dataclasses import dataclass
from threading import Lock

import numpy as np
from numpy.typing import NDArray

from vimo_demo.inspection import CameraId, Frame, FrameId


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
        if frame.id.camera_id not in self.cameras:
            raise ValueError("Unknown display camera")
        if type(generation) is not int or generation < 0:
            raise ValueError("Invalid generation")
        # bytes backing prevents consumers from re-enabling ndarray writes.
        image = np.frombuffer(frame.image.tobytes(), dtype=np.uint8).reshape(
            frame.image.shape
        )
        with self._lock:
            previous = self._frames.get(frame.id.camera_id)
            if previous is not None and (
                generation,
                frame.id.cycle_id,
                frame.id.sequence,
            ) <= (
                previous.generation,
                previous.frame_id.cycle_id,
                previous.frame_id.sequence,
            ):
                return False
            self._revision += 1
            self._frames[frame.id.camera_id] = DisplayFrame(
                frame.id,
                generation,
                frame.captured_at,
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
