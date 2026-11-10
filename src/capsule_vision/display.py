"""Shared Qt-independent display copies and result-identity matching."""

from dataclasses import dataclass, replace
from threading import Lock

import numpy as np
from numpy.typing import NDArray

from capsule_vision.application import ProductionApplicationSnapshot
from capsule_vision.communication.reporting import CompletedReportingBatch
from capsule_vision.inference._validation import validate_image
from capsule_vision.inspection import CameraId, CycleId, Frame, FrameId
from capsule_vision.inspection.models import timestamp
from capsule_vision.inspection.production import (
    CameraInspectionResult,
    ProductionInspectionResult,
)


@dataclass(frozen=True, slots=True, eq=False)
class DisplayFrame:
    frame_id: FrameId
    generation: int
    captured_at: float
    revision: int
    image: NDArray[np.uint8]
    source_key: str | None = None
    result: CameraInspectionResult | None = None


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
        if len(set(cameras)) != len(cameras):
            raise ValueError("Expected unique camera identities")
        self.cameras = cameras
        self._lock = Lock()
        self._frames: dict[CameraId, DisplayFrame] = {}
        self._revision = 0
        self._generation = -1
        self._four_cycle = False
        self._candidate_cycle: CycleId | None = None
        self._candidate_frames: dict[CameraId, DisplayFrame] = {}
        self._candidate_result: ProductionInspectionResult | None = None
        self._eligible_cycle: CycleId | None = None
        self._eligible_result: ProductionInspectionResult | None = None

    def begin_generation(self, generation: int, *, four_cycle: bool) -> None:
        with self._lock:
            if generation <= self._generation:
                return
            self._generation, self._four_cycle = generation, four_cycle
            self._candidate_cycle = self._eligible_cycle = None
            self._candidate_result = self._eligible_result = None
            self._candidate_frames.clear()
            if four_cycle:
                self._frames.clear()
            self._revision += 1

    def select_fourth(self, generation: int, cycle: CycleId) -> None:
        """Called only for the reporting owner's actual fourth admission."""
        with self._lock:
            if generation != self._generation or not self._four_cycle:
                return
            self._candidate_cycle = cycle
            self._candidate_frames.clear()
            self._candidate_result = None

    def retain_fourth_result(
        self, generation: int, result: ProductionInspectionResult
    ) -> None:
        with self._lock:
            if (
                generation == self._generation
                and result.cycle_id == self._candidate_cycle
            ):
                self._candidate_result = result

    def discard_candidate(self, generation: int) -> None:
        with self._lock:
            if generation == self._generation:
                self._candidate_frames.clear()
                self._candidate_result = None
                self._candidate_cycle = None

    def _matched_fourth(self, frame: DisplayFrame) -> DisplayFrame | None:
        result = self._eligible_result
        if (
            result is None
            or frame.frame_id.cycle_id != result.cycle_id
            or frame.generation != self._generation
        ):
            return None
        camera = next(
            (
                camera
                for camera in result.cameras
                if camera.frame_id == frame.frame_id
                and camera.image_shape == frame.image.shape
            ),
            None,
        )
        return replace(frame, result=camera) if camera is not None else None

    def complete_group(self, batch: CompletedReportingBatch) -> None:
        """Promote only matching fourth pixels. Missing cameras explicitly clear.

        A source's accepted-frame callback can finish after terminal delivery;
        that exact fourth identity may still arrive, never an earlier/later frame.
        """
        with self._lock:
            cycle = batch.reports[-1].identity.cycle_id
            if (
                batch.identity.generation != self._generation
                or not self._four_cycle
                or (self._eligible_cycle is not None and cycle <= self._eligible_cycle)
            ):
                return
            self._eligible_cycle = cycle
            self._eligible_result = (
                self._candidate_result if self._candidate_cycle == cycle else None
            )
            self._frames = {
                camera: matched
                for camera, frame in self._candidate_frames.items()
                if (matched := self._matched_fourth(frame)) is not None
            }
            self._candidate_frames.clear()
            self._candidate_result = None
            self._candidate_cycle = None
            self._revision += 1

    def reconfigure(self, cameras: tuple[CameraId, ...]) -> None:
        """Called by stopped Application configuration; invalidate former pixels."""
        if not cameras or len(set(cameras)) != len(cameras):
            raise ValueError("Expected unique camera identities")
        with self._lock:
            if self.cameras != cameras:
                self.cameras = cameras
                self._frames.clear()
                self._candidate_frames.clear()
                self._candidate_result = self._eligible_result = None
                self._candidate_cycle = self._eligible_cycle = None
                self._revision += 1

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
            if frame_id.camera_id not in self.cameras:
                raise ValueError("Unknown display camera")
            if generation < self._generation:
                return False
            target = self._frames
            if self._four_cycle and generation == self._generation:
                if frame_id.cycle_id == self._candidate_cycle:
                    target = self._candidate_frames
                elif frame_id.cycle_id != self._eligible_cycle:
                    return False
            previous = target.get(frame_id.camera_id)
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
            frame = DisplayFrame(
                frame_id,
                generation,
                captured_at,
                self._revision,
                image,
                source_key,
            )
            if self._four_cycle and target is self._frames:
                matched = self._matched_fourth(frame)
                if matched is None:
                    return False
                frame = matched
            target[frame_id.camera_id] = frame
            return True

    def snapshot(self) -> DisplaySnapshot:
        with self._lock:
            return DisplaySnapshot(
                self._revision,
                tuple(self._frames[c] for c in self.cameras if c in self._frames),
            )


def matching_camera(
    frame: DisplayFrame | None, snapshot: ProductionApplicationSnapshot
) -> CameraInspectionResult | None:
    if snapshot.four_cycle_display:
        return (
            frame.result
            if frame is not None
            and frame.generation == snapshot.generation
            and snapshot.latest_production_result is not None
            and frame.result is not None
            and frame.result.frame_id == frame.frame_id
            and frame.result.image_shape == frame.image.shape
            else None
        )
    result = snapshot.latest_production_result
    if (
        frame is None
        or result is None
        or frame.generation != snapshot.generation
        or frame.frame_id.cycle_id != result.cycle_id
    ):
        return None
    return next(
        (
            camera
            for camera in result.cameras
            if camera.frame_id == frame.frame_id
            and camera.image_shape == frame.image.shape
        ),
        None,
    )
