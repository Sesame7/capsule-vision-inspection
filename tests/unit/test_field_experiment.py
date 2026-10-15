"""S1-E2A local-only fixed-bit protocol, release, admission and ambiguity checks."""

import json
import socket
import struct
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from threading import Event, Thread

import numpy as np
import pytest
from _field_basler_experiment import ConversionTimer
from _field_trigger_experiment import PHOTO, PhotoClient, PlcState, pulse
from _scheduling_experiment import Runner, Workload, json_default
from experiment_field_continuous import Arrival, Trial, Windows, ownership_probe
from tests.application_support import prepared

from vimo_demo.inspection import CameraId, CycleId, Frame, FrameId
from vimo_demo.runtime import InferenceWorker
from vimo_demo.runtime.inference_worker import InferenceEngine

AUTO = bytes.fromhex("46494e53 0000000c 00000000 00000000 00000000")
NODES = bytes.fromhex("46494e53 00000010 00000001 00000000 00000065 00000014")
SERIALS = tuple(f"serial-{i}" for i in range(6))


def exact(connection: socket.socket, count: int) -> bytes:
    result = bytearray()
    while len(result) < count:
        part = connection.recv(count - len(result))
        if not part:
            raise RuntimeError("Unexpected loopback EOF")
        result.extend(part)
    return bytes(result)


def packet(connection: socket.socket) -> bytes:
    prefix = exact(connection, 8)
    assert prefix[:4] == b"FINS"
    return prefix + exact(connection, int.from_bytes(prefix[4:], "big"))


@contextmanager
def bit_peer(value: int | None) -> Generator[int]:
    errors: list[BaseException] = []
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(2)

        def serve() -> None:
            try:
                connection, _ = listener.accept()
                with connection:
                    connection.settimeout(2)
                    assert packet(connection) == AUTO
                    connection.sendall(NODES)
                    expected = (
                        bytes.fromhex(
                            "46494e53 0000001a 00000002 00000000 80000200140000650001 0101 31000a070001"
                        )
                        if value is None
                        else bytes.fromhex(
                            "46494e53 0000001b 00000002 00000000 80000200140000650001 0102 31000a070001"
                        )
                        + bytes((value,))
                    )
                    assert packet(connection) == expected
                    response = bytes.fromhex("c0000200650000140001")
                    response += (
                        b"\x01\x01\x00\x00\x01"
                        if value is None
                        else b"\x01\x02\x00\x00"
                    )
                    connection.sendall(
                        b"FINS"
                        + struct.pack("!III", 8 + len(response), 2, 0)
                        + response
                    )
                    assert connection.recv(1) == b""
            except BaseException as exc:
                errors.append(exc)

        owner = Thread(target=serve, name="S1E2A-local-bit-peer", daemon=False)
        owner.start()
        try:
            yield listener.getsockname()[1]
        finally:
            owner.join(3)
            assert not owner.is_alive() and not errors


@pytest.mark.parametrize("value", [None, 0, 1])
def test_literal_bit_encoding_and_tcp_cleanup(value: int | None) -> None:
    assert PHOTO == bytes.fromhex("31 00 0a 07 00 01")
    with bit_peer(value) as port:
        client = PhotoClient("127.0.0.1", port=port)
        with client:
            if value is None:
                assert client.read_photo() == 1
            else:
                client.write_photo(value)
    assert client.closed


@pytest.mark.parametrize(
    "words",
    [
        (128, 0, 0, 0, 0, 1, 0, 0, 0),
        (0, 0, 0, 0, 0, 0, 0, 0, 0),
        (0, 0, 0, 0, 0, 1, 2, 0, 0),
        (0, 0, 0, 0, 0, 1, 4, 0, 0),
    ],
)
def test_no_activation_on_changed_manual_running_or_photo_state(
    words: tuple[int, ...],
) -> None:
    with pytest.raises(RuntimeError):
        PlcState(words).require_idle()


def test_unrelated_word_or_alarm_change_stops_trial() -> None:
    original = PlcState((0, 0, 0, 0, 0, 1, 0, 0, 0))
    original.require_idle()
    for index in (0, 5, 6, 7, 8):
        words = list(original.words)
        words[index] += 16
        with pytest.raises(RuntimeError, match="changed"):
            PlcState(tuple(words)).require_idle(original)


class FakePhoto:
    def __init__(self, *, fail_high: bool = False, fail_low: bool = False) -> None:
        self.closed = False
        self.values: list[int] = []
        self.reads = 0
        self.fail_high, self.fail_low = fail_high, fail_low

    def write_photo(self, value: int) -> None:
        self.values.append(value)
        if (value == 1 and self.fail_high) or (value == 0 and self.fail_low):
            self.closed = True
            raise TimeoutError("ACK missing; outcome unknown")

    def read_photo(self) -> int:
        self.reads += 1
        return self.values[-1]


@pytest.mark.parametrize("full", [True, False])
def test_exactly_one_high_and_low_with_measured_trace(full: bool) -> None:
    client = FakePhoto()
    row = pulse(
        client,
        hold_ms=20,
        full_readback=full,
        stop=Event(),
        recovery=FakePhoto,
        clock=lambda: 1,
        wait=lambda _d, _s: None,
    )
    assert client.values == [1, 0] and client.reads == (2 if full else 0)
    assert row["activation_send"] == row["deactivation_ack"] == 1


def test_lost_activation_ack_is_not_retried_release_uses_separate_connection() -> None:
    client, recovery = FakePhoto(fail_high=True), FakePhoto()
    row: dict[str, object] = {}
    with pytest.raises(TimeoutError, match="unknown"):
        pulse(
            client,
            hold_ms=20,
            full_readback=True,
            stop=Event(),
            recovery=lambda: recovery,
            clock=lambda: 1,
            wait=lambda _d, _s: None,
            trace=row,
        )
    assert client.values == [1] and recovery.values == [0]
    assert row["release_readback"] == 0 and "activation_error" in row


def test_failed_deactivation_remains_explicit_and_stop_before_high_sends_nothing() -> (
    None
):
    client = FakePhoto(fail_low=True)
    row: dict[str, object] = {}
    with pytest.raises(TimeoutError):
        pulse(
            client,
            hold_ms=20,
            full_readback=False,
            stop=Event(),
            recovery=FakePhoto,
            clock=lambda: 1,
            wait=lambda _d, _s: None,
            trace=row,
        )
    assert client.values == [1, 0] and "release_error" in row
    stop = Event()
    stop.set()
    untouched = FakePhoto()
    with pytest.raises(RuntimeError, match="before activation"):
        pulse(untouched, hold_ms=20, full_readback=False, stop=stop, recovery=FakePhoto)
    assert not untouched.values


def test_missing_frame_blocks_next_pulse_and_no_late_frame_reassociation() -> None:
    windows = Windows(SERIALS)
    windows.open(0)
    for serial in SERIALS[:5]:
        assert windows.accept(serial, 1) == 0
    windows.empty_sweep()
    with pytest.raises(RuntimeError, match="Unresolved"):
        windows.open(1)
    assert windows.accept(SERIALS[5], 1) == 0
    with pytest.raises(RuntimeError, match="Unresolved"):
        windows.open(1)  # All received is not enough; queued extras must be swept.
    windows.empty_sweep()
    windows.open(1)
    assert windows.accept(SERIALS[0], 2) == 1


def test_extra_duplicate_and_sequence_gap_are_not_accepted() -> None:
    windows = Windows(SERIALS)
    with pytest.raises(RuntimeError, match="pretrigger"):
        windows.accept(SERIALS[0], 1)
    windows.open(0)
    windows.accept(SERIALS[0], 1)
    with pytest.raises(RuntimeError, match="duplicate"):
        windows.accept(SERIALS[0], 2)
    with pytest.raises(RuntimeError, match="Nonconsecutive"):
        windows.accept(SERIALS[1], 2)


def test_same_pixel_ownership_probe_is_serializable_without_starting_worker(
    tmp_path: Path,
) -> None:
    def unused_factory() -> InferenceEngine:
        raise AssertionError("GPU must not start")

    workload = Workload(prepared(tmp_path).configuration)
    runner = Runner(workload, InferenceWorker(unused_factory))
    trial = Trial(SERIALS, runner, 1)
    frame = Frame(
        FrameId(CycleId(0), CameraId("CAM1"), 1),
        0,
        np.zeros((20, 60, 3), dtype=np.uint8),
    )
    trial.pool[0] = {frame.id.camera_id: Arrival(frame, 0)}
    result = ownership_probe(trial)
    assert "retained_pixel_sha256" in result
    assert not frame.image.flags.writeable
    assert (
        json.loads(json.dumps(result, default=json_default))[
            "direct_owned_transfer_ms"
        ]["calls"]
        == 3
    )


def test_conversion_timer_forwards_the_identical_buffer_without_copying() -> None:
    marker = object()
    result = np.zeros((2, 3, 3), dtype=np.uint8)

    class Converter:
        def ConvertToArray(self, value: object) -> object:
            assert value is marker
            return result

    timer = ConversionTimer(Converter())
    assert timer.ConvertToArray(marker) is result
    assert timer.elapsed_ms >= 0
