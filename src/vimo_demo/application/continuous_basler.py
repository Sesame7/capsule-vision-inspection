"""Passive continuous Basler acquisition, with no PLC connection or trigger writes.

One source thread owns pylon. Connection-lifetime acquisition ordinals associate the
six cameras under the verified no-loss external-trigger condition; they are NOT
hardware trigger IDs. Observed gaps/duplicates/late inputs fail closed.
"""

from collections.abc import Callable
from dataclasses import dataclass
from threading import Event, Lock
from time import monotonic

from vimo_demo.inspection import CameraId, CycleId, Frame, FrameId

from .basler import (
    AcquisitionMetadata,
    BaslerAcquisitionSource,
    CameraCleanup,
    CameraReadiness,
    CameraSettings,
)
from .basler_control import BaslerAdmissionControl


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
        self.received_frames = self.discarded_frames = 0
        self.control: BaslerAdmissionControl | None = None
        self.admission_ready: Callable[[], bool] = lambda: True
        self._backend = None
        self._identity_lock = Lock()
        self._ambiguous_from: int | None = None

    def attributable(self, cycle_id: CycleId) -> bool:
        """No observed association anomaly; not proof of a physical trigger ID."""
        with self._identity_lock:
            return self._ambiguous_from is None or cycle_id < self._ambiguous_from

    def _ambiguity(self, cycle: CycleId | None, message: str) -> RuntimeError:
        with self._identity_lock:
            self._ambiguous_from = int(cycle) if cycle is not None else 1
        return RuntimeError(message)

    def retry_cleanup(self) -> None:
        """Explicit retry on the original acquisition owner only."""
        if self._backend is not None:
            self.cleanup = self._backend.cleanup()
            self.cleanup_safe = all(row.safe for row in self.cleanup)

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
        on_prepared: Callable[[tuple[CameraSettings, ...]], None] = lambda _: None,
        on_poll: Callable[[Callable[[], bool]], bool] = lambda _: False,
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
        admitted = False

        def clean_boundary() -> bool:
            return (cycle is None or len(seen) == 6) and not any(
                backend.available(serial, 0) for serial in self.serials
            )

        def expire_capture() -> None:
            nonlocal admitted
            if stop.is_set():
                return
            if (
                admitted
                and cycle is not None
                and self._clock() >= deadline
                and len(seen) != 6
            ):
                if self.control is None or not self.control.settling:
                    raise TimeoutError(
                        "Missing camera at observed-cycle deadline; restart required"
                    )
                # The early runtime owns the attributable partial result/deadline.
                # Retain the SDK ordinal until all peers arrive, discarding late
                # pixels; a later ordinal cannot silently fill this missing peer.
                admitted = False
                self.control.capture_finished(cycle)

        try:
            backend.inventory(self.serials)
            settings: list[CameraSettings] = []
            for serial in self.serials:
                if stop.is_set():
                    return
                settings.append(backend.prepare(serial))
            on_prepared(tuple(settings))
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
                if on_poll(clean_boundary):
                    # A stopped/detached session cannot consume late peers. Keep
                    # SDK ordinal continuity, but discard the rest of that group.
                    admitted = False
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
                        raise self._ambiguity(cycle, f"Invalid camera result: {serial}")
                    ordinal, block = row.image_number, row.block_id
                    if (
                        ordinal is None
                        or ordinal < 1
                        or block is None
                        or not 0 <= block < 2**64 - 1
                    ):
                        raise self._ambiguity(
                            cycle,
                            f"Required acquisition counters unavailable: {serial}",
                        )
                    previous = sequence.get(serial)
                    if previous is not None and (block, ordinal) != (
                        previous[0] + 1,
                        previous[1] + 1,
                    ):
                        raise self._ambiguity(
                            cycle, f"Camera sequence discontinuity: {serial}"
                        )
                    expected = 1 if cycle is None else int(cycle) + (len(seen) == 6)
                    if ordinal != expected:
                        raise self._ambiguity(
                            cycle,
                            f"Unexpected acquisition ordinal {ordinal}, expected {expected}; association unsafe: {serial}",
                        )
                    if cycle is None or len(seen) == 6:
                        cycle = CycleId(ordinal)
                        # Admission occurs only after actual valid, independently owned
                        # pixels exist. Idle armed time creates no cycles/deadlines.
                        seen.clear()
                        admitted = self.admission_ready() and (
                            self.control is None or self.control.claim(cycle)
                        )
                        if admitted:
                            begin(cycle)
                            deadline = self._clock() + 0.100
                    else:
                        expire_capture()
                    if serial in seen:
                        raise self._ambiguity(
                            cycle, f"Duplicate camera frame: {serial}"
                        )
                    sequence[serial] = block, ordinal
                    seen.add(serial)
                    self.received_frames += 1
                    if self.control is not None:
                        self.control.received(discarded=not admitted)
                    if not admitted:
                        self.discarded_frames += 1
                        continue
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
                    if len(seen) == 6 and self.control is not None:
                        self.control.capture_finished(cycle)
                expire_capture()
                if not found:
                    if self.control is not None:
                        self.control.boundary(clean=cycle is None or len(seen) == 6)
                    backend.available(self.serials[cursor], 1)
                    cursor = (cursor + 1) % 6
        finally:
            try:
                self.cleanup = backend.cleanup()
                # Cleanup reports every device actually owned, including partial
                # initialization. Never require six fictional cleanup records.
                self.cleanup_safe = all(row.safe for row in self.cleanup)
            finally:
                if self.control is not None:
                    self.control.stop()
