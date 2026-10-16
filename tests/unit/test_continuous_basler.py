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

from vimo_demo.application import _basler_sdk
from vimo_demo.application.basler import (
    AcquisitionMetadata,
    CameraCleanup,
    CameraReadiness,
)
from vimo_demo.application.continuous_basler import ContinuousBaslerSource
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
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
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
        self.thread = Thread(target=self._run, name="TestPassiveCapture")

    def _run(self) -> None:
        def submit(frame: Frame) -> None:
            assert frame.image.flags.owndata and not frame.image.flags.writeable
            self.frames.append(frame)
            self.received[len(self.frames) - 1].set()

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
