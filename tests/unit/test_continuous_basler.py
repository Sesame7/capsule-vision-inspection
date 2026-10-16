"""Passive external fake queues, injected clocks and no Production PLC owner."""

from collections import deque
from concurrent.futures import Future
from dataclasses import replace
from threading import Event, Lock, Thread
from time import monotonic

import numpy as np
import pytest
from numpy.typing import NDArray
from tests.application_support import WAIT

from vimo_demo.application import (
    ApplicationCapacityError,
    ApplicationStateError,
    _basler_sdk,
)
from vimo_demo.application.basler import (
    AcquisitionMetadata,
    CameraCleanup,
    CameraReadiness,
)
from vimo_demo.application.basler_control import BaslerAdmissionControl
from vimo_demo.application.continuous_basler import ContinuousBaslerSource
from vimo_demo.application.local_replay import ReplayState
from vimo_demo.communication import FinsTcpClient, FinsUdpClient
from vimo_demo.inspection import CameraId, CycleId, Frame

SERIALS = tuple(str(1000 + i) for i in range(6))
CAMERAS = tuple(CameraId(f"CAM{i + 1}") for i in range(6))


class Backend:
    def __init__(self) -> None:
        self.lock = Lock()
        self.queues: dict[str, deque[tuple[AcquisitionMetadata, NDArray[np.uint8]]]] = {
            s: deque() for s in SERIALS
        }
        self.prepared: list[str] = []
        self.arms = 0
        self.cleaned = False
        self.unsafe = False
        self.waited, self.retrieving, self.release = Event(), Event(), Event()
        self.release.set()

    def inventory(self, serials: tuple[str, ...]) -> None:
        assert serials == SERIALS

    def prepare(self, serial: str) -> None:
        self.prepared.append(serial)

    def arm(self) -> tuple[CameraReadiness, ...]:
        self.arms += 1
        return tuple(CameraReadiness(s, True, True, monotonic()) for s in SERIALS)

    def available(self, serial: str, timeout_ms: int) -> bool:
        with self.lock:
            found = bool(self.queues[serial])
        if not found and timeout_ms:
            self.waited.set()
            Event().wait(timeout_ms / 1000)
        return found

    def retrieve(self, serial: str) -> tuple[AcquisitionMetadata, NDArray[np.uint8]]:
        self.retrieving.set()
        assert self.release.wait(WAIT)
        with self.lock:
            value = self.queues[serial].popleft()
            return value

    def expose(
        self,
        cycle: int,
        cameras: tuple[int, ...] = tuple(range(6)),
        *,
        change: dict[str, int | None] | None = None,
    ) -> None:
        with self.lock:
            for index in cameras:
                serial = SERIALS[index]
                pixels = np.full((20, 60, 3), cycle * 10 + index, np.uint8)
                pixels.setflags(write=False)
                row = AcquisitionMetadata(
                    serial,
                    monotonic(),
                    True,
                    0,
                    "",
                    60,
                    20,
                    1,
                    3600,
                    cycle - 1,
                    cycle,
                    cycle * 1000,
                    0,
                )
                if change:
                    row = replace(row, **change)
                self.queues[serial].append((row, pixels))

    def cleanup(self) -> tuple[CameraCleanup, ...]:
        self.cleaned = True
        return tuple(
            CameraCleanup(s, True, not self.unsafe, True, True) for s in SERIALS
        )


class Clock:
    def __init__(self) -> None:
        self.lock = Lock()
        self.now = 100.0

    def __call__(self) -> float:
        with self.lock:
            return self.now

    def advance(self, seconds: float) -> None:
        with self.lock:
            self.now += seconds


class Run:
    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, *, controlled: bool = False
    ) -> None:
        self.backend, self.clock, self.stop, self.armed = (
            Backend(),
            Clock(),
            Event(),
            Event(),
        )
        self.starts: list[CycleId] = []
        self.frames: list[Frame] = []
        self.received = tuple(Event() for _ in range(18))
        self.done: Future[None] = Future()

        def factory(*, continuous: bool = False) -> Backend:
            assert continuous
            return self.backend

        monkeypatch.setattr(_basler_sdk, "PylonBackend", factory)
        self.source = ContinuousBaslerSource(CAMERAS, SERIALS, clock=self.clock)
        self.control = BaslerAdmissionControl(1) if controlled else None
        self.source.control = self.control
        self.raw_received = tuple(Event() for _ in range(36))
        if self.control is not None:
            original_received = self.control.received

            def received(*, discarded: bool) -> None:
                original_received(discarded=discarded)
                assert self.control is not None
                self.raw_received[self.control.snapshot().received_frames - 1].set()

            monkeypatch.setattr(self.control, "received", received)
        self.thread = Thread(target=self._run, name="TestPassiveCapture")

    def _run(self) -> None:
        def submit(frame: Frame) -> None:
            assert frame.image.flags.owndata and not frame.image.flags.writeable
            self.frames.append(frame)
            self.received[len(self.frames) - 1].set()
            if (
                self.control is not None
                and sum(f.id.cycle_id == frame.id.cycle_id for f in self.frames) == 6
            ):
                self.control.inspected(frame.id.cycle_id)
                self.control.reported(frame.id.cycle_id)

        try:
            self.source.run(
                self.stop,
                begin=self.starts.append,
                submit=submit,
                on_armed=lambda _: self.armed.set(),
            )
        except BaseException as exc:
            self.done.set_exception(exc)
        else:
            self.done.set_result(None)

    def start(self) -> None:
        self.thread.start()
        assert self.armed.wait(WAIT)

    def close(self) -> None:
        self.stop.set()
        self.backend.release.set()
        self.thread.join(WAIT)
        assert not self.thread.is_alive()
        assert self.backend.cleaned


@pytest.mark.parametrize("prepared", [0, 1, 5, 6])
@pytest.mark.parametrize("unsafe", [False, True])
def test_stop_during_preparation_checks_only_owned_devices(
    monkeypatch: pytest.MonkeyPatch, prepared: int, unsafe: bool
) -> None:
    stop = Event()

    class PartialBackend(Backend):
        def inventory(self, serials: tuple[str, ...]) -> None:
            super().inventory(serials)
            if prepared == 0:
                stop.set()

        def prepare(self, serial: str) -> None:
            super().prepare(serial)
            if len(self.prepared) == prepared:
                stop.set()

        def cleanup(self) -> tuple[CameraCleanup, ...]:
            # The SDK boundary returns one observation per actually owned device,
            # not six fictional devices when initialization stopped partway.
            self.cleaned = True
            return tuple(
                CameraCleanup(serial, True, not unsafe, True, True)
                for serial in self.prepared
            )

    backend = PartialBackend()

    def factory(*, continuous: bool = False) -> Backend:
        assert continuous
        return backend

    monkeypatch.setattr(_basler_sdk, "PylonBackend", factory)
    source = ContinuousBaslerSource(CAMERAS, SERIALS)
    starts: list[CycleId] = []
    frames: list[Frame] = []
    armed: list[object] = []
    source.run(stop, begin=starts.append, submit=frames.append, on_armed=armed.append)
    assert backend.cleaned and len(source.cleanup) == prepared
    assert source.cleanup_safe is (prepared == 0 or not unsafe)
    assert not starts and not frames and not armed and source.frames == 0


def test_idle_armed_stop_finishes_without_a_frame_or_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = Run(monkeypatch)
    try:
        run.start()
        assert run.backend.waited.wait(WAIT)
        assert not run.done.done() and not run.starts and not run.frames
    finally:
        run.close()
    run.done.result(WAIT)
    assert run.source.cleanup_safe and run.source.frames == 0
    assert run.backend.arms == 1


def test_idle_external_frames_early_admission_and_persistent_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_plc(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Passive Production must not create a PLC client")

    monkeypatch.setattr(FinsTcpClient, "__init__", no_plc)
    monkeypatch.setattr(FinsUdpClient, "__init__", no_plc)
    run = Run(monkeypatch)
    try:
        run.start()
        run.clock.advance(1000)
        assert run.backend.waited.wait(WAIT)
        assert not run.starts and not run.frames and not run.done.done()
        run.backend.expose(1, (4,))
        assert run.received[0].wait(WAIT)
        assert run.starts == [1] and run.frames[0].id.camera_id == CAMERAS[4]
        run.backend.expose(1, (5, 3, 0, 2, 1))
        assert run.received[5].wait(WAIT)
        run.clock.advance(1000)
        run.backend.waited.clear()
        assert run.backend.waited.wait(WAIT)
        assert not run.done.done() and run.starts == [1]
        for ordinal in (2, 3):
            run.backend.expose(ordinal)
            assert run.received[ordinal * 6 - 1].wait(WAIT)
        assert run.starts == [1, 2, 3]
        assert len({f.id for f in run.frames}) == 18
        assert all(
            int(f.image[0, 0, 0]) == f.id.cycle_id * 10 + CAMERAS.index(f.id.camera_id)
            for f in run.frames
        )
        assert run.backend.arms == 1 and run.backend.prepared == list(SERIALS)
    finally:
        run.close()
    run.done.result(WAIT)
    assert run.source.cleanup_safe and run.source.frames == 18


def test_missing_frame_times_out_only_after_observed_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = Run(monkeypatch)
    try:
        run.start()
        run.backend.expose(1, (0, 1, 3, 4, 5))
        assert run.received[4].wait(WAIT)
        run.clock.advance(0.101)
        with pytest.raises(TimeoutError, match="Missing camera"):
            run.done.result(WAIT)
        assert run.starts == [1] and len(run.frames) == 5
        assert run.source.attributable(CycleId(1))
    finally:
        run.close()


@pytest.mark.parametrize(
    "ordinal,change",
    [(1, None), (2, None), (2, {"block_id": 9}), (1, {"image_number": None})],
)
def test_duplicate_new_group_before_missing_peer_and_counter_anomalies_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    ordinal: int,
    change: dict[str, int | None] | None,
) -> None:
    run = Run(monkeypatch)
    try:
        run.start()
        run.backend.expose(1, (0,))
        assert run.received[0].wait(WAIT)
        run.backend.expose(ordinal, (0,), change=change)
        with pytest.raises(RuntimeError, match="sequence|ordinal|counters"):
            run.done.result(WAIT)
        assert run.starts == [1] and len(run.frames) == 1
        assert not run.source.attributable(CycleId(1))
    finally:
        run.close()


def test_late_completed_ordinal_never_becomes_a_new_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = Run(monkeypatch)
    try:
        run.start()
        run.backend.expose(1)
        assert run.received[5].wait(WAIT)
        run.clock.advance(1000)
        run.backend.expose(1, (0,))
        with pytest.raises(RuntimeError, match="sequence|ordinal"):
            run.done.result(WAIT)
        assert run.starts == [1] and len(run.frames) == 6
    finally:
        run.close()


def test_stop_waits_for_owned_retrieve_and_drops_late_pixels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = Run(monkeypatch)
    run.backend.release.clear()
    try:
        run.start()
        run.backend.expose(1, (0,))
        assert run.backend.retrieving.wait(WAIT)
        run.stop.set()
        assert run.thread.is_alive() and not run.backend.cleaned
        run.backend.release.set()
        run.done.result(WAIT)
        assert not run.starts and not run.frames
    finally:
        run.close()


def test_unsafe_cleanup_remains_visible(monkeypatch: pytest.MonkeyPatch) -> None:
    run = Run(monkeypatch)
    run.backend.unsafe = True
    run.start()
    run.close()
    assert not run.source.cleanup_safe


def test_pause_drains_frames_step_once_and_resume_preserve_sdk_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = Run(monkeypatch, controlled=True)
    control = run.control
    assert control is not None
    try:
        run.start()
        control.pause().result(WAIT)
        assert control.snapshot().state is ReplayState.PAUSED
        for ordinal in (1, 2, 3):
            run.backend.expose(ordinal)
            assert run.raw_received[ordinal * 6 - 1].wait(WAIT)
        assert run.source.frames == 0 and not run.starts
        assert control.snapshot().discarded_frames == 18
        step = control.step()
        # Wait for the source's empty-queue fence, not a time-based trigger guess.
        run.backend.waited.clear()
        assert run.backend.waited.wait(WAIT)
        assert control.snapshot().admission_armed
        run.backend.expose(4)
        step.result(WAIT)
        assert run.starts == [4] and [f.id.sequence for f in run.frames] == [4] * 6
        assert control.snapshot().state is ReplayState.PAUSED
        run.backend.expose(5)
        assert run.raw_received[29].wait(WAIT)
        assert run.starts == [4] and run.source.frames == 6
        control.resume().result(WAIT)
        run.backend.expose(6)
        assert run.received[11].wait(WAIT)
        assert run.starts == [4, 6]
        assert run.backend.arms == 1
    finally:
        run.close()
    run.done.result(WAIT)


def test_step_discards_already_buffered_capture_before_arming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = Run(monkeypatch, controlled=True)
    control = run.control
    assert control is not None
    try:
        run.start()
        control.pause().result(WAIT)
        run.backend.release.clear()
        run.backend.expose(1)
        assert run.backend.retrieving.wait(WAIT)
        step = control.step()
        assert control.step() is step
        run.backend.release.set()
        assert run.raw_received[5].wait(WAIT)
        run.backend.waited.clear()
        assert run.backend.waited.wait(WAIT)
        assert not step.done() and run.starts == []
        run.backend.expose(2)
        step.result(WAIT)
        assert run.starts == [2] and len(run.frames) == 6
    finally:
        run.close()


def test_pause_finishes_admitted_capture_and_step_partial_never_reassigns_peers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = Run(monkeypatch, controlled=True)
    control = run.control
    assert control is not None
    try:
        run.start()
        run.backend.expose(1, (0,))
        assert run.received[0].wait(WAIT)
        pause = control.pause()
        assert not pause.done()
        run.backend.expose(1, (1, 2, 3, 4, 5))
        pause.result(WAIT)
        step = control.step()
        run.backend.waited.clear()
        assert run.backend.waited.wait(WAIT)
        run.backend.expose(2, (0,))
        assert run.received[6].wait(WAIT)
        run.clock.advance(0.101)
        control.inspected(CycleId(2))
        control.reported(CycleId(2))
        step.result(WAIT)
        assert control.snapshot().state is ReplayState.PAUSED
        # The partial result is settled, but its missing SDK peers cannot be
        # guessed away. A new admission transition is explicitly rejected.
        with pytest.raises(ApplicationStateError, match="Incomplete SDK"):
            control.resume().result(WAIT)
        run.backend.expose(2, (1, 2, 3, 4, 5))
        assert run.raw_received[11].wait(WAIT)
        assert len(run.frames) == 7 and run.starts == [1, 2]
        control.resume().result(WAIT)
        run.backend.expose(3)
        assert run.received[12].wait(WAIT)
        assert run.starts == [1, 2, 3]
    finally:
        run.close()
    run.done.result(WAIT)


def test_admission_settlement_is_bounded_and_waits_for_report_and_stop() -> None:
    control = BaslerAdmissionControl(9, capacity=1)
    control.boundary(clean=True)
    assert control.claim(CycleId(1))
    control.capture_finished(CycleId(1))
    with pytest.raises(ApplicationCapacityError):
        control.claim(CycleId(2))
    paused = control.pause()
    control.inspected(CycleId(1))
    assert not paused.done() and control.snapshot().state is ReplayState.PAUSING
    control.reported(CycleId(1))
    paused.result(0)
    step = control.step()
    assert control.step() is step and not step.cancel()
    control.stop()
    with pytest.raises(ApplicationStateError, match="stopped"):
        step.result(0)
    control.stop()
    assert control.snapshot().pending_cycles == 0
