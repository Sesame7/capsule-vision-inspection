"""Passive continuous Basler acquisition, with no PLC connection or trigger writes.

One source thread owns pylon. Session-local acquisition ordinals associate the
six cameras under the verified no-loss external-trigger condition; they are NOT
hardware trigger IDs. Observed gaps/duplicates/late inputs fail closed.
"""

from collections.abc import Callable
from dataclasses import dataclass
from threading import Event
from time import monotonic

from vimo_demo.inspection import CameraId, CycleId, Frame, FrameId

from .basler import (
    AcquisitionMetadata,
    BaslerAcquisitionSource,
    CameraCleanup,
    CameraReadiness,
)


@dataclass(frozen=True, slots=True)
class ContinuousArmedAcquisition:
    serials: tuple[str, ...]
    armed_at: float
    readiness: tuple[CameraReadiness, ...]


class ContinuousBaslerSource:
    def __init__(
        self,
        cameras: tuple[CameraId, ...],
        serials: tuple[str, ...],
        *,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._clock = clock
        self.cameras = cameras
        self.serials = BaslerAcquisitionSource(serials).serials
        if len(cameras) != 6 or len(set(cameras)) != 6:
            raise ValueError("Six configured camera identities required")
        self.cleanup: tuple[CameraCleanup, ...] = ()
        self.cleanup_safe = True
        self.frames = 0
        self._backend = None

    def run(
        self,
        stop: Event,
        *,
        begin: Callable[[CycleId], None],
        submit: Callable[[Frame], None],
        on_armed: Callable[[ContinuousArmedAcquisition], None],
        on_frame: Callable[
            [CycleId, AcquisitionMetadata], None
        ] = lambda _cycle, _event: None,
    ) -> None:
        from ._basler_sdk import PylonBackend

        if stop.is_set():
            return
        backend = self._backend = PylonBackend(continuous=True)
        self.cleanup_safe = False
        cycle: CycleId | None = None
        deadline = 0.0
        seen: set[str] = set()
        sequence: dict[str, tuple[int, int]] = {}
        try:
            backend.inventory(self.serials)
            for serial in self.serials:
                if stop.is_set():
                    return
                backend.prepare(serial)
            if stop.is_set():
                return
            readiness = backend.arm()
            if tuple(row.serial for row in readiness) != self.serials or any(
                row.supported and row.ready is not True for row in readiness
            ):
                raise RuntimeError("Incomplete or unsuccessful camera readiness")
            if any(backend.available(serial, 0) for serial in self.serials):
                raise RuntimeError(
                    "Pre-ARMED images make acquisition ordinals ambiguous"
                )
            on_armed(ContinuousArmedAcquisition(self.serials, self._clock(), readiness))
            cursor = 0
            while not stop.is_set():
                found = False
                for index, serial in enumerate(self.serials):
                    if stop.is_set():
                        return
                    if not backend.available(serial, 0):
                        continue
                    found = True
                    row, pixels = backend.retrieve(serial)
                    if stop.is_set():
                        return
                    if (
                        not row.succeeded
                        or pixels is None
                        or row.serial != serial
                        or row.skipped_images not in (0, None)
                    ):
                        raise RuntimeError(f"Invalid camera result: {serial}")
                    ordinal, block = row.image_number, row.block_id
                    if (
                        ordinal is None
                        or ordinal < 1
                        or block is None
                        or not 0 <= block < 2**64 - 1
                    ):
                        raise RuntimeError(
                            f"Required acquisition counters unavailable: {serial}"
                        )
                    previous = sequence.get(serial)
                    if previous is not None and (block, ordinal) != (
                        previous[0] + 1,
                        previous[1] + 1,
                    ):
                        raise RuntimeError(f"Camera sequence discontinuity: {serial}")
                    expected = 1 if cycle is None else int(cycle) + (len(seen) == 6)
                    if ordinal != expected:
                        raise RuntimeError(
                            f"Unexpected acquisition ordinal {ordinal}, expected {expected}; association unsafe: {serial}"
                        )
                    if cycle is None or len(seen) == 6:
                        cycle = CycleId(ordinal)
                        # Admission occurs only after actual valid, independently owned
                        # pixels exist. Idle armed time creates no cycles/deadlines.
                        begin(cycle)
                        deadline = self._clock() + 0.100
                        seen.clear()
                    elif self._clock() >= deadline:
                        raise TimeoutError(
                            "Late frame after observed-cycle deadline; never reassociate"
                        )
                    if serial in seen:
                        raise RuntimeError(f"Duplicate camera frame: {serial}")
                    sequence[serial] = block, ordinal
                    seen.add(serial)
                    frame = Frame(
                        FrameId(cycle, self.cameras[index], ordinal),
                        row.received_at,
                        pixels,
                    )
                    submit(
                        frame
                    )  # Eligible camera inspection need not wait for its peers.
                    self.frames += 1
                    on_frame(cycle, row)
                if cycle is not None and len(seen) != 6 and self._clock() >= deadline:
                    raise TimeoutError(
                        "Missing camera at observed-cycle deadline; restart required"
                    )
                if not found:
                    backend.available(self.serials[cursor], 1)
                    cursor = (cursor + 1) % 6
        finally:
            self.cleanup = backend.cleanup()
            self.cleanup_safe = len(self.cleanup) == 6 and all(
                row.safe for row in self.cleanup
            )
