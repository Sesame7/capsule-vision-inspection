"""Identity projection and two-stage commits over actual loopback UDP only."""

import socket
from collections.abc import Generator
from contextlib import contextmanager
from threading import Event, Thread
from typing import Literal, cast

import pytest
from tests.business_support import mixed_result

from vimo_demo.communication import (
    FinsAddress,
    FinsMemoryArea,
    FinsPlcError,
    FinsProtocolError,
    FinsTimeoutError,
    FinsTransportError,
    FinsUdpClient,
    FirstStationReporter,
    FirstStationSubmissionError,
    SubmissionStage,
    first_station_reject_words,
)
from vimo_demo.communication.simulator import FinsUdpSimulator
from vimo_demo.inspection.production import ChannelBusinessStatus as Status
from vimo_demo.inspection.production import ChannelId

type Statuses = tuple[tuple[ChannelId, Status], ...]
type FaultMode = Literal["plc", "drop_before", "drop_after", "malformed_after"]


def statuses(default: Status = Status.OK, count: int = 36) -> Statuses:
    return tuple((ChannelId(i), default) for i in range(1, count + 1))


@pytest.mark.parametrize("channel_id", range(1, 37))
def test_every_reject_bit_uses_channel_identity(channel_id: int) -> None:
    inputs = tuple(
        (i, Status.NG if i == channel_id else state)
        for i, state in reversed(statuses())
    )
    expected = [0, 0, 0]
    word, bit = divmod(channel_id - 1, 16)
    expected[word] = 1 << bit
    assert first_station_reject_words(inputs) == tuple(expected)


@pytest.mark.parametrize("status", list(Status))
@pytest.mark.parametrize("count", [30, 36])
def test_uniform_status_and_reserved_bits(status: Status, count: int) -> None:
    words = first_station_reject_words(statuses(status, count))
    expected = (65535, 16383) if count == 30 else (65535, 65535, 15)
    assert words == (tuple(0 for _ in expected) if status is Status.OK else expected)
    assert words[-1] & (0xC000 if count == 30 else 0xFFF0) == 0


def test_real_business_results_are_projected_without_mutation() -> None:
    result = mixed_result()
    channels = result.channels
    # Deliberately shuffle across cameras and slots, using the actual derived enum.
    shuffled = channels[::2] + channels[1::2]
    words = first_station_reject_words(
        (row.channel_id, row.business_status) for row in shuffled
    )
    assert words == (0xEFBE, 0xBEFB, 0x000F)
    assert result.channels == channels
    assert sum(row.business_status is Status.OK for row in channels) == 6


def test_thirty_channel_bits_use_identity_and_reject_holes() -> None:
    for channel in range(1, 31):
        inputs = tuple(
            (i, Status.NG if i == channel else state)
            for i, state in reversed(statuses(count=30))
        )
        expected = [0, 0]
        word, bit = divmod(channel - 1, 16)
        expected[word] = 1 << bit
        assert first_station_reject_words(inputs) == tuple(expected), channel
    for invalid in (
        statuses(count=29),
        statuses(count=31),
        statuses(count=30)[:-1] + ((ChannelId(36), Status.OK),),
    ):
        with pytest.raises(ValueError):
            first_station_reject_words(invalid)


@pytest.mark.parametrize(
    "inputs",
    [
        (),
        statuses()[:-1],
        statuses() + ((ChannelId(1), Status.OK),),
        statuses()[:-1] + ((ChannelId(35), Status.OK),),
        statuses()[:-1] + ((ChannelId(0), Status.OK),),
        statuses()[:-1] + ((ChannelId(37), Status.OK),),
        statuses()[:-1] + ((ChannelId(True), Status.OK),),
        statuses()[:-1] + ((cast(ChannelId, "36"), Status.OK),),
        statuses()[:-1] + ((ChannelId(36), cast(Status, "OK")),),
    ],
)
def test_invalid_identities_and_status_are_rejected(inputs: Statuses) -> None:
    with pytest.raises(ValueError):
        first_station_reject_words(inputs)


class ObservedSimulator(FinsUdpSimulator):
    """Test-only stage fault injection; the maintained simulator has no faults."""

    def __init__(self, stage: SubmissionStage | None, mode: FaultMode) -> None:
        super().__init__(port=0)
        self.stage = stage
        self.mode = mode
        self.requests: list[bytes] = []

    def handle_datagram(self, request: bytes) -> bytes | None:
        self.requests.append(request)
        fail_at = 50 if self.stage is SubmissionStage.BITMAP else 49
        should_fail = (
            self.stage is not None
            and request[10:13] == b"\x01\x02\xb1"
            and int.from_bytes(request[13:15], "big") == fail_at
        )
        if should_fail and self.mode == "drop_before":
            return None
        if should_fail and self.mode == "plc":
            # Independent error ACK, no mutation before a PLC end-code failure.
            return (
                b"\xc0\x00\x02"
                + request[6:9]
                + request[3:6]
                + request[9:12]
                + b"\x21\x01"
            )
        response = super().handle_datagram(request)
        if should_fail and self.mode == "drop_after":
            return None  # Remote write happened but its ACK is lost.
        if should_fail and self.mode == "malformed_after":
            assert response is not None
            return response[:-1]
        return response


@contextmanager
def station_server(
    stage: SubmissionStage | None = None, mode: FaultMode = "plc"
) -> Generator[ObservedSimulator]:
    stop = Event()
    failures: list[BaseException] = []
    server = ObservedSimulator(stage, mode)
    with server:

        def serve() -> None:
            try:
                server.serve(stop)
            except BaseException as exc:
                failures.append(exc)

        thread = Thread(target=serve, name="first-station-test-server")
        thread.start()
        try:
            yield server
        finally:
            stop.set()
            thread.join(2)
            assert not thread.is_alive()
            assert not failures, failures
    assert server.closed
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as replacement:
        replacement.bind(("127.0.0.1", server.port))


def connection(port: int) -> FinsUdpClient:
    return FinsUdpClient(
        "127.0.0.1",
        port=port,
        timeout=0.1,
        destination=FinsAddress(0, 2, 0),
        source=FinsAddress(0, 1, 0),
    )


@pytest.mark.parametrize("count", [30, 36])
def test_unchanged_results_progress_and_wrap_with_readback(count: int) -> None:
    with station_server() as server, connection(server.port) as client:
        reporter = FirstStationReporter(client, initial_sequence=65535)
        inputs = statuses(Status.UNKNOWN, count)
        client.write_words(52, (0xA5A5,), area=FinsMemoryArea.W)
        server.requests.clear()
        words = (65535, 16383) if count == 30 else (65535, 65535, 15)
        for sequence in (65535, 0, 1):
            commit = reporter.submit(inputs)
            assert commit.sequence == sequence
            assert commit.reject_words == words
            assert client.read_words(49, 4, area=FinsMemoryArea.W) == (
                sequence,
                *words,
                *((0xA5A5,) if count == 30 else ()),
            )
        assert reporter.next_sequence == 2
        # Exactly two writes per result, not readback/retry inside submit().
        writes = [r for r in server.requests if r[10:12] == b"\x01\x02"]
        assert len(writes) == 6
        for i, sequence in enumerate((65535, 0, 1)):
            bitmap, commit_request = writes[2 * i : 2 * i + 2]
            assert bitmap[12:] == bytes.fromhex(
                "B1 00 32 00 00 02 FF FF 3F FF"
                if count == 30
                else "B1 00 32 00 00 03 FF FF FF FF 00 0F"
            )
            assert commit_request[12:] == bytes.fromhex(
                "B1 00 31 00 00 01"
            ) + sequence.to_bytes(2, "big")
    assert client.closed


@pytest.mark.parametrize("stage", list(SubmissionStage))
@pytest.mark.parametrize("count", [30, 36])
@pytest.mark.parametrize(
    "mode", ["plc", "drop_before", "drop_after", "malformed_after"]
)
def test_stage_failures_preserve_uncertainty_and_never_retry(
    stage: SubmissionStage, mode: FaultMode, count: int
) -> None:
    with station_server(stage, mode) as server:
        with connection(server.port) as client:
            reporter = FirstStationReporter(client, initial_sequence=123)
            with pytest.raises(FirstStationSubmissionError) as caught:
                reporter.submit(statuses(Status.NG, count))
            assert caught.value.stage is stage
            assert caught.value.sequence == 123
            words = (65535, 16383) if count == 30 else (65535, 65535, 15)
            assert caught.value.reject_words == words
            assert reporter.next_sequence == 124  # Attempt reserved, not auto-reused.
            expected_type = (
                FinsPlcError
                if mode == "plc"
                else FinsProtocolError
                if mode == "malformed_after"
                else FinsTimeoutError
            )
            assert isinstance(caught.value.__cause__, expected_type)
            if mode == "plc":
                cause = caught.value.__cause__
                assert isinstance(cause, FinsPlcError)
                assert cause.end_code == 0x2101
                assert not client.closed
            else:
                assert client.closed
            if mode.startswith("drop"):
                assert "write execution is unknown" in str(caught.value)
        # Explicit test readback on a fresh client; reporter never reconnects.
        with connection(server.port) as reader:
            actual = reader.read_words(49, 4, area=FinsMemoryArea.W)
        applied = mode in ("drop_after", "malformed_after")
        expected_words = (
            (*words, *((0,) if count == 30 else ()))
            if applied or stage is SubmissionStage.SEQUENCE
            else (0, 0, 0)
        )
        expected_sequence = 123 if applied and stage is SubmissionStage.SEQUENCE else 0
        assert actual == (expected_sequence, *expected_words)
        writes = [r for r in server.requests if r[10:12] == b"\x01\x02"]
        assert [int.from_bytes(r[13:15], "big") for r in writes] == (
            [50] if stage is SubmissionStage.BITMAP else [50, 49]
        )


@pytest.mark.parametrize("initial", [-1, 65536, True])
def test_invalid_sequence_never_sends(initial: int) -> None:
    with station_server() as server, connection(server.port) as client:
        with pytest.raises(ValueError):
            FirstStationReporter(client, initial_sequence=initial)
        assert server.requests == []


def test_invalid_projection_does_not_consume_sequence_or_send() -> None:
    with station_server() as server, connection(server.port) as client:
        reporter = FirstStationReporter(client, initial_sequence=7)
        with pytest.raises(ValueError, match="Missing"):
            reporter.submit(statuses()[:-1])
        assert reporter.next_sequence == 7
        assert server.requests == []
        assert reporter.submit(statuses()).sequence == 7


def test_overlapping_submission_is_rejected_without_interleaved_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, release = Event(), Event()
    with station_server() as server, connection(server.port) as client:
        reporter = FirstStationReporter(client, initial_sequence=8)
        write = client.write_words

        def paused_write(
            start: int, values: tuple[int, ...], *, area: FinsMemoryArea
        ) -> None:
            if start == 50:
                entered.set()
                assert release.wait(2)
            write(start, values, area=area)

        monkeypatch.setattr(client, "write_words", paused_write)
        errors: list[BaseException] = []

        def submit() -> None:
            try:
                reporter.submit(statuses())
            except BaseException as exc:
                errors.append(exc)

        owner = Thread(target=submit, name="first-station-test-owner")
        owner.start()
        try:
            assert entered.wait(1)
            with pytest.raises(FinsTransportError, match="already active"):
                reporter.submit(statuses())
        finally:
            release.set()
            owner.join(3)
        assert not owner.is_alive() and not errors
        assert reporter.next_sequence == 9
        assert client.read_words(49, 4, area=FinsMemoryArea.W) == (8, 0, 0, 0)
