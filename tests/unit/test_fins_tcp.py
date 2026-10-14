"""W421 7-4 / W342 literal vectors and bounded loopback peers, without hardware.

Use --confcutdir=tests/unit to omit the repository's Qt conftest.
"""

import socket
import struct
import subprocess
import sys
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from threading import Event, Thread
from threading import enumerate as threads
from time import perf_counter
from typing import cast

import pytest

from vimo_demo.communication import (
    FinsAddress,
    FinsMemoryArea,
    FinsPlcError,
    FinsProtocolError,
    FinsTcpClient,
    FinsTcpEnvelopeError,
    FinsTimeoutError,
    FinsTransportError,
    fins_tcp,
)

# W421: big-endian Length includes Command/Error/Data, excluding FINS/Length.
AUTO_NODE = bytes.fromhex("46494E53 0000000C 00000000 00000000 00000000")
NODES = bytes.fromhex("46494E53 00000010 00000001 00000000 00000065 00000014")
READ_D100 = bytes.fromhex(
    "46494E53 0000001A 00000002 00000000 "
    "80 00 02 00 14 00 00 65 00 01 01 01 82 00 64 00 00 01"
)
READ_1234 = bytes.fromhex(
    "46494E53 00000018 00000002 00000000 "
    "C0 00 02 00 65 00 00 14 00 01 01 01 00 00 04 D2"
)
WRITE_D100 = bytes.fromhex(
    "46494E53 0000001C 00000002 00000000 "
    "80 00 02 00 14 00 00 65 00 01 01 02 82 00 64 00 00 01 10 E1"
)
ACK = bytes.fromhex(
    "46494E53 00000016 00000002 00000000 C0 00 02 00 65 00 00 14 00 01 01 02 00 00"
)


def recv_exact(connection: socket.socket, count: int) -> bytes:
    data = bytearray()
    while len(data) < count:
        part = connection.recv(count - len(data))
        assert part, "client closed before expected request"
        data.extend(part)
    return bytes(data)


def expect_closed(connection: socket.socket) -> None:
    try:
        assert connection.recv(1) == b""
    except ConnectionResetError:
        pass  # Windows may reset a connection retired with unread invalid bytes.


def request(connection: socket.socket, requests: list[bytes]) -> bytes:
    prefix = recv_exact(connection, 8)
    assert prefix[:4] == b"FINS"
    length = int.from_bytes(prefix[4:], "big")
    assert 8 <= length <= 2020
    packet = prefix + recv_exact(connection, length)
    requests.append(packet)
    return packet


def negotiate(
    connection: socket.socket, requests: list[bytes], reply: bytes = NODES
) -> None:
    assert request(connection, requests) == AUTO_NODE
    connection.sendall(reply)


@contextmanager
def peer(
    handler: Callable[[socket.socket, list[bytes]], None],
) -> Generator[tuple[int, list[bytes]]]:
    requests: list[bytes] = []
    failures: list[BaseException] = []
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(2)
        port: int = listener.getsockname()[1]

        def serve() -> None:
            try:
                connection, _ = listener.accept()
                with connection:
                    connection.settimeout(2)
                    handler(connection, requests)
            except BaseException as exc:
                failures.append(exc)

        thread = Thread(target=serve, name="fins-tcp-test-peer")
        thread.start()
        try:
            yield port, requests
        finally:
            thread.join(3)
            assert not thread.is_alive(), "TCP test peer did not terminate"
            assert not failures, failures
    # A bound listening socket and its named owner have actually been released.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as replacement:
        replacement.bind(("127.0.0.1", port))
    assert not any(t.name == "fins-tcp-test-peer" for t in threads())


def client(port: int, timeout: float = 0.5) -> FinsTcpClient:
    return FinsTcpClient("127.0.0.1", port=port, timeout=timeout)


WIRE_CASES: list[tuple[Callable[[FinsTcpClient], object], bytes, bytes, object]] = [
    (lambda c: c.read_words(100), READ_D100, READ_1234, (1234,)),
    (lambda c: c.write_words(100, (4321,)), WRITE_D100, ACK, None),
    (
        lambda c: c.read_words(0x123, 3),
        bytes.fromhex(
            "46494E53 0000001A 00000002 00000000 80 00 02 00 14 00 00 65 00 01 01 01 82 01 23 00 00 03"
        ),
        bytes.fromhex(
            "46494E53 0000001C 00000002 00000000 C0 00 02 00 65 00 00 14 00 01 01 01 00 00 00 00 FF FF 12 34"
        ),
        (0, 65535, 0x1234),
    ),
    (
        lambda c: c.write_words(0x123, (0, 65535, 0x1234)),
        bytes.fromhex(
            "46494E53 00000020 00000002 00000000 80 00 02 00 14 00 00 65 00 01 01 02 82 01 23 00 00 03 00 00 FF FF 12 34"
        ),
        ACK,
        None,
    ),
    (
        lambda c: c.read_words(511, area=FinsMemoryArea.W),
        bytes.fromhex(
            "46494E53 0000001A 00000002 00000000 80 00 02 00 14 00 00 65 00 01 01 01 B1 01 FF 00 00 01"
        ),
        READ_1234,
        (1234,),
    ),
    (
        lambda c: c.write_words(511, (4321,), area=FinsMemoryArea.W),
        bytes.fromhex(
            "46494E53 0000001C 00000002 00000000 80 00 02 00 14 00 00 65 00 01 01 02 B1 01 FF 00 00 01 10 E1"
        ),
        ACK,
        None,
    ),
    (
        lambda c: c.read_words(49, 4, area=FinsMemoryArea.W),
        bytes.fromhex(
            "46494E53 0000001A 00000002 00000000 80 00 02 00 14 00 00 65 00 01 01 01 B1 00 31 00 00 04"
        ),
        bytes.fromhex(
            "46494E53 0000001E 00000002 00000000 C0 00 02 00 65 00 00 14 00 01 01 01 00 00 FF FF 00 01 80 00 00 0F"
        ),
        (65535, 1, 32768, 15),
    ),
    (
        lambda c: c.write_words(50, (1, 32768, 15), area=FinsMemoryArea.W),
        bytes.fromhex(
            "46494E53 00000020 00000002 00000000 80 00 02 00 14 00 00 65 00 01 01 02 B1 00 32 00 00 03 00 01 80 00 00 0F"
        ),
        ACK,
        None,
    ),
]


@pytest.mark.parametrize(("operation", "expected", "reply", "result"), WIRE_CASES)
def test_official_vectors_and_negotiated_addresses(
    operation: Callable[[FinsTcpClient], object],
    expected: bytes,
    reply: bytes,
    result: object,
) -> None:
    def respond(connection: socket.socket, requests: list[bytes]) -> None:
        negotiate(connection, requests)
        assert request(connection, requests) == expected
        connection.sendall(reply)
        expect_closed(connection)  # Context manager releases the connection.

    with peer(respond) as (port, requests), client(port) as connection:
        assert connection.source == FinsAddress(0, 101, 0)
        assert connection.destination == FinsAddress(0, 20, 0)
        assert operation(connection) == result
        assert requests == [AUTO_NODE, expected]
    assert connection.closed
    connection.close()
    with pytest.raises(FinsTransportError, match="closed"):
        connection.__enter__()


def test_fragmented_handshake_and_embedded_response() -> None:
    def respond(connection: socket.socket, requests: list[bytes]) -> None:
        assert request(connection, requests) == AUTO_NODE
        for byte in NODES:
            connection.sendall(bytes((byte,)))
        assert request(connection, requests) == READ_D100
        for byte in READ_1234:
            connection.sendall(bytes((byte,)))

    with peer(respond) as (port, _), client(port) as connection:
        assert connection.read_words(100) == (1234,)


@pytest.mark.parametrize("error", [1, 2, 3, 0x20, 0x21, 0x22, 0x23, 0x24, 0x25])
def test_rejected_handshake_preserves_outer_code_and_closes(error: int) -> None:
    reply = NODES[:12] + error.to_bytes(4, "big") + NODES[16:]

    def reject(connection: socket.socket, requests: list[bytes]) -> None:
        negotiate(connection, requests, reply)
        expect_closed(connection)  # No memory request after rejection.

    with peer(reject) as (port, requests):
        with pytest.raises(FinsTcpEnvelopeError) as caught:
            client(port)
        assert caught.value.command == 1
        assert caught.value.error_code == error
        assert requests == [AUTO_NODE]


@pytest.mark.parametrize(
    ("client_node", "server_node"),
    [(0, 20), (255, 20), (0x10000065, 20), (101, 0), (101, 255), (101, 101)],
)
def test_invalid_negotiated_nodes(client_node: int, server_node: int) -> None:
    reply = NODES[:16] + client_node.to_bytes(4, "big") + server_node.to_bytes(4, "big")

    def reject(connection: socket.socket, requests: list[bytes]) -> None:
        negotiate(connection, requests, reply)
        expect_closed(connection)

    with peer(reject) as (port, _), pytest.raises(FinsProtocolError, match="node"):
        client(port)


@pytest.mark.parametrize(
    "reply",
    [
        b"NOPE" + NODES[4:],
        NODES[:4] + bytes(4),
        NODES[:4] + (7).to_bytes(4, "big"),
        NODES[:4] + (2021).to_bytes(4, "big"),
        NODES[:4] + bytes.fromhex("FFFFFFFF"),
        NODES[:4] + (15).to_bytes(4, "big") + NODES[8:],
        NODES[:4] + (17).to_bytes(4, "big") + NODES[8:],
        NODES[:8] + (2).to_bytes(4, "big") + NODES[12:],
    ],
)
def test_invalid_negotiation_envelope(reply: bytes) -> None:
    def reject(connection: socket.socket, requests: list[bytes]) -> None:
        negotiate(connection, requests, reply)
        expect_closed(connection)

    with peer(reject) as (port, _), pytest.raises(FinsProtocolError):
        client(port)


@pytest.mark.parametrize("partial", [b"", NODES[:3], NODES[:12], NODES[:-1]])
def test_closed_partial_handshake(partial: bytes) -> None:
    def close_early(connection: socket.socket, requests: list[bytes]) -> None:
        negotiate(connection, requests, partial)

    with (
        peer(close_early) as (port, _),
        pytest.raises(FinsTransportError, match="closed"),
    ):
        client(port)


def test_socket_connection_is_not_negotiation_and_has_bounded_timeout() -> None:
    def silent(connection: socket.socket, requests: list[bytes]) -> None:
        assert request(connection, requests) == AUTO_NODE
        expect_closed(connection)

    with peer(silent) as (port, requests):
        started = perf_counter()
        with pytest.raises(FinsTimeoutError, match="negotiation") as caught:
            client(port, timeout=0.05)
        assert isinstance(caught.value.__cause__, TimeoutError)
        assert perf_counter() - started < 1
        assert requests == [AUTO_NODE]


INVALID_OPERATIONS: list[Callable[[FinsTcpClient], object]] = [
    lambda c: c.read_words(-1),
    lambda c: c.read_words(32768),
    lambda c: c.read_words(32767, 2),
    lambda c: c.read_words(0, 0),
    lambda c: c.read_words(0, 1000),
    lambda c: c.read_words(True),
    lambda c: c.read_words(cast(int, 1.5)),
    lambda c: c.write_words(100, ()),
    lambda c: c.write_words(100, (65536,)),
    lambda c: c.write_words(100, (-1,)),
    lambda c: c.write_words(100, (True,)),
    lambda c: c.write_words(100, (cast(int, "1234"),)),
    lambda c: c.write_words(100, [0] * 998),
    lambda c: c.write_words(32767, (1, 2)),
    lambda c: c.read_words(-1, area=FinsMemoryArea.W),
    lambda c: c.read_words(512, area=FinsMemoryArea.W),
    lambda c: c.read_words(511, 2, area=FinsMemoryArea.W),
    lambda c: c.read_words(0, 0, area=FinsMemoryArea.W),
    lambda c: c.read_words(0, 1000, area=FinsMemoryArea.W),
    lambda c: c.read_words(True, area=FinsMemoryArea.W),
    lambda c: c.write_words(50, (), area=FinsMemoryArea.W),
    lambda c: c.write_words(50, (65536,), area=FinsMemoryArea.W),
    lambda c: c.write_words(50, (True,), area=FinsMemoryArea.W),
    lambda c: c.write_words(511, (1, 2), area=FinsMemoryArea.W),
    lambda c: c.write_words(0, [0] * 998, area=FinsMemoryArea.W),
    lambda c: c.read_words(0, area=cast(FinsMemoryArea, 0xB1)),
    lambda c: c.write_words(0, (1,), area=cast(FinsMemoryArea, "W")),
]


def test_invalid_operations_before_memory_io_then_connection_stays_usable() -> None:
    def respond(connection: socket.socket, requests: list[bytes]) -> None:
        negotiate(connection, requests)
        assert request(connection, requests) == READ_D100
        connection.sendall(READ_1234)

    with peer(respond) as (port, requests), client(port) as connection:
        for operation in INVALID_OPERATIONS:
            with pytest.raises(ValueError):
                operation(connection)
            assert not connection.closed
        assert connection.read_words(100) == (1234,)
        assert requests == [AUTO_NODE, READ_D100]


@pytest.mark.parametrize(
    "fields",
    [
        {"host": "localhost"},
        {"host": "::1"},
        {"host": "0.0.0.0"},
        {"host": "224.0.0.1"},
        {"host": "255.255.255.255"},
        {"port": 0},
        {"port": 65536},
        {"port": True},
        {"timeout": 0},
        {"timeout": -1},
        {"timeout": float("inf")},
        {"timeout": float("nan")},
        {"timeout": True},
    ],
)
def test_invalid_configuration_before_socket_io(
    fields: dict[str, str | int | float], monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> socket.socket:
        pytest.fail("invalid configuration must not create a socket")

    monkeypatch.setattr(socket, "socket", forbidden)
    host = cast(str, fields.get("host", "127.0.0.1"))
    with pytest.raises(ValueError):
        FinsTcpClient(
            host,
            port=cast(int, fields.get("port", 9600)),
            timeout=cast(float, fields.get("timeout", 1.0)),
        )


@pytest.mark.parametrize("offset", [0, 3, 4, 5, 6, 7, 8, 9, 10, 11])
def test_embedded_response_identity_rejection(offset: int) -> None:
    response = bytearray(READ_1234)
    response[16 + offset] ^= 0x80

    def respond(connection: socket.socket, requests: list[bytes]) -> None:
        negotiate(connection, requests)
        request(connection, requests)
        connection.sendall(response)
        expect_closed(connection)

    with peer(respond) as (port, _), client(port) as connection:
        with pytest.raises(FinsProtocolError):
            connection.read_words(100)
        assert connection.closed


@pytest.mark.parametrize(
    "reply",
    [
        b"FAIL" + READ_1234[4:],
        READ_1234[:4] + bytes(4),
        READ_1234[:4] + (2021).to_bytes(4, "big"),
        READ_1234[:4] + (19).to_bytes(4, "big") + READ_1234[8:],
        READ_1234[:4] + (20).to_bytes(4, "big") + READ_1234[8:28],
        READ_1234[:4] + (23).to_bytes(4, "big") + READ_1234[8:-1],
        READ_1234[:4] + (25).to_bytes(4, "big") + READ_1234[8:] + b"\x00",
        READ_1234[:8] + (1).to_bytes(4, "big") + READ_1234[12:],
        READ_1234[:12] + (2).to_bytes(4, "big") + READ_1234[16:],
        bytes.fromhex("46494E53 00000008 00000003 00000003"),
    ],
)
def test_invalid_exchange_envelope_or_payload(reply: bytes) -> None:
    def respond(connection: socket.socket, requests: list[bytes]) -> None:
        negotiate(connection, requests)
        request(connection, requests)
        connection.sendall(reply)
        expect_closed(connection)

    with peer(respond) as (port, _), client(port) as connection:
        with pytest.raises(FinsProtocolError):
            connection.read_words(100)
        assert connection.closed


@pytest.mark.parametrize("end_code", [0x1103, 0x2101, 0x0040, 0x0080, 0x8000])
def test_plc_error_preserves_code_and_allows_a_later_command(end_code: int) -> None:
    reply = READ_1234[:28] + end_code.to_bytes(2, "big") + b"\x01\x02"

    def respond(connection: socket.socket, requests: list[bytes]) -> None:
        negotiate(connection, requests)
        request(connection, requests)
        connection.sendall(reply)
        second = request(connection, requests)
        assert second[25] == 2
        response = bytearray(READ_1234)
        response[25] = 2
        connection.sendall(response)

    with peer(respond) as (port, _), client(port) as connection:
        with pytest.raises(FinsPlcError) as caught:
            connection.read_words(100)
        assert caught.value.end_code == end_code
        assert caught.value.command == b"\x01\x01"
        assert caught.value.response_data == b"\x01\x02"
        assert not connection.closed
        assert connection.read_words(100) == (1234,)


@pytest.mark.parametrize("failure", ["timeout", "disconnect", "malformed", "envelope"])
@pytest.mark.parametrize("executed", [False, True])
def test_uncertain_write_is_never_retried_or_reconnected(
    failure: str, executed: bool
) -> None:
    memory = {100: 1234}

    def respond(connection: socket.socket, requests: list[bytes]) -> None:
        negotiate(connection, requests)
        assert request(connection, requests) == WRITE_D100
        if executed:
            memory[100] = 4321  # Remote execution can precede missing/invalid ACK.
        if failure == "disconnect":
            return
        if failure == "malformed":
            connection.sendall(ACK[:-1])
            return
        if failure == "envelope":
            connection.sendall(bytes.fromhex("46494E53 00000008 00000003 00000002"))
        expect_closed(connection)

    with peer(respond) as (port, requests), client(port, timeout=0.05) as connection:
        with pytest.raises((FinsTransportError, FinsProtocolError)) as caught:
            connection.write_words(100, (4321,))
        assert connection.closed
        assert "unknown" in str(caught.value).lower() or any(
            "unknown" in note.lower() for note in getattr(caught.value, "__notes__", ())
        )
        if failure == "envelope":
            assert isinstance(caught.value, FinsTcpEnvelopeError)
            assert caught.value.command == 3 and caught.value.error_code == 2
        with pytest.raises(FinsTransportError, match="closed"):
            connection.write_words(100, (4321,))
        assert memory[100] == (4321 if executed else 1234)
        assert requests == [AUTO_NODE, WRITE_D100]


def test_active_request_rejects_overlap_and_close_without_losing_owner() -> None:
    received, release = Event(), Event()
    failures: list[BaseException] = []
    results: list[tuple[int, ...]] = []

    def respond(connection: socket.socket, requests: list[bytes]) -> None:
        negotiate(connection, requests)
        assert request(connection, requests) == READ_D100
        received.set()
        assert release.wait(2)
        connection.sendall(READ_1234)
        expect_closed(connection)

    with peer(respond) as (port, requests):
        connection = client(port, timeout=1)

        def invoke() -> None:
            try:
                results.append(connection.read_words(100))
            except BaseException as exc:
                failures.append(exc)

        owner = Thread(target=invoke, name="fins-tcp-test-owner")
        owner.start()
        try:
            assert received.wait(2)
            with pytest.raises(FinsTransportError, match="active"):
                connection.read_words(100)
            with pytest.raises(FinsTransportError, match="active"):
                connection.close()
            assert not connection.closed
        finally:
            release.set()
            owner.join(2)
            connection.close()
        assert not owner.is_alive()
        assert not failures and results == [(1234,)]
        assert requests == [AUTO_NODE, READ_D100]
    assert not any(t.name == "fins-tcp-test-owner" for t in threads())


def test_context_exception_still_releases_socket() -> None:
    def respond(connection: socket.socket, requests: list[bytes]) -> None:
        negotiate(connection, requests)
        expect_closed(connection)

    with peer(respond) as (port, _):
        connection = client(port)
        with pytest.raises(RuntimeError, match="caller error"):
            with connection:
                raise RuntimeError("caller error")
        assert connection.closed
        connection.close()


@pytest.mark.parametrize(("client_node", "server_node"), [(1, 254), (254, 1)])
def test_negotiated_node_boundaries(client_node: int, server_node: int) -> None:
    reply = NODES[:16] + struct.pack("!II", client_node, server_node)

    def respond(connection: socket.socket, requests: list[bytes]) -> None:
        negotiate(connection, requests, reply)
        expect_closed(connection)

    with peer(respond) as (port, _), client(port) as connection:
        assert connection.source.node == client_node
        assert connection.destination.node == server_node


def reply_to(packet: bytes, data: bytes = b"") -> bytes:
    # Test peer's dynamic response is used only for repeated/boundary operations;
    # standards evidence comes from the independently literal WIRE_CASES above.
    embedded = bytes.fromhex("C0 00 02 00 65 00 00 14 00")
    embedded += packet[25:28] + b"\x00\x00" + data
    return b"FINS" + struct.pack("!III", 8 + len(embedded), 2, 0) + embedded


def test_maximum_word_limits_and_dm_w_boundaries() -> None:
    def respond(connection: socket.socket, requests: list[bytes]) -> None:
        negotiate(connection, requests)
        for _ in range(5):
            packet = request(connection, requests)
            assert packet[28] in (0x82, 0xB1)
            assert packet[31] == 0
            count = int.from_bytes(packet[32:34], "big")
            reading = packet[26:28] == b"\x01\x01"
            assert len(packet) == (34 if reading else 34 + count * 2)
            data = struct.pack(f"!{count}H", *range(count)) if reading else b""
            connection.sendall(reply_to(packet, data))

    with peer(respond) as (port, _), client(port) as connection:
        assert connection.read_words(32767) == (0,)
        assert connection.read_words(0, 999) == tuple(range(999))
        connection.write_words(0, tuple(range(997)))
        assert connection.read_words(0, 512, area=FinsMemoryArea.W) == tuple(range(512))
        connection.write_words(0, tuple(range(512)), area=FinsMemoryArea.W)


def test_sid_rollover_and_reserved_response_fields() -> None:
    def respond(connection: socket.socket, requests: list[bytes]) -> None:
        negotiate(connection, requests)
        for index in range(258):
            packet = request(connection, requests)
            assert packet[25] == (index + 1) % 256
            response = bytearray(reply_to(packet, b"\x04\xd2"))
            response[16:19] = b"\xfe\x7f\x00"  # W342 reserved RSV/GCT/ICF.
            connection.sendall(response)

    with peer(respond) as (port, requests), client(port) as connection:
        for _ in range(258):
            assert connection.read_words(100) == (1234,)
        assert len(requests) == 259  # Only one initial negotiation.


class StreamSocket:
    """Test-only one-byte stream with an explicit clock; no scheduler/sleeps."""

    def __init__(self) -> None:
        self.incoming = NODES + READ_1234
        self.elapsed = 0.0
        self.advance = 0.0
        self.timeouts: list[float] = []
        self.sent: list[bytes] = []
        self.closed = False
        self.failure: BaseException | None = None

    def settimeout(self, timeout: float) -> None:
        self.timeouts.append(timeout)

    def connect(self, _peer: tuple[str, int]) -> None:
        self.elapsed += self.advance

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)
        self.elapsed += self.advance

    def recv(self, _count: int) -> bytes:
        if self.failure is not None:
            raise self.failure
        if self.advance >= self.timeouts[-1]:
            self.elapsed += self.timeouts[-1]
            raise TimeoutError("test receive deadline")
        self.elapsed += self.advance
        part, self.incoming = self.incoming[:1], self.incoming[1:]
        return part

    def fileno(self) -> int:
        return -1 if self.closed else 42

    def close(self) -> None:
        self.closed = True


def install_stream(monkeypatch: pytest.MonkeyPatch, stream: StreamSocket) -> None:
    def create(*_args: object, **_kwargs: object) -> socket.socket:
        # Controlled stdlib socket substitute; no production typing suppression.
        return cast(socket.socket, stream)

    monkeypatch.setattr(socket, "socket", create)
    monkeypatch.setattr(fins_tcp, "monotonic", lambda: stream.elapsed)


def test_one_byte_partial_reads_and_total_exchange_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = StreamSocket()
    install_stream(monkeypatch, stream)
    connection = client(9600, timeout=0.1)
    assert stream.sent == [AUTO_NODE]
    stream.advance = 0.025
    with pytest.raises(FinsTimeoutError) as caught:
        connection.read_words(100)
    assert isinstance(caught.value.__cause__, TimeoutError)
    assert stream.elapsed == pytest.approx(0.1)
    assert stream.closed
    assert stream.incoming  # A partial prefix did not renew the total deadline.
    assert stream.sent == [AUTO_NODE, READ_D100]
    assert min(stream.timeouts) < 0.026


def test_connect_and_handshake_share_one_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = StreamSocket()
    stream.advance = 0.04
    install_stream(monkeypatch, stream)
    with pytest.raises(FinsTimeoutError, match="negotiation"):
        client(9600, timeout=0.1)
    assert stream.closed and stream.elapsed == pytest.approx(0.1)
    assert stream.sent == [AUTO_NODE]


@pytest.mark.parametrize(
    "failure", [OSError("stream interrupted"), KeyboardInterrupt()]
)
def test_interrupted_exchange_retires_socket_and_preserves_error(
    failure: BaseException, monkeypatch: pytest.MonkeyPatch
) -> None:
    stream = StreamSocket()
    install_stream(monkeypatch, stream)
    connection = client(9600)
    stream.failure = failure
    if isinstance(failure, OSError):
        with pytest.raises(FinsTransportError) as caught:
            connection.read_words(100)
        assert caught.value.__cause__ is failure
    else:
        with pytest.raises(KeyboardInterrupt):
            connection.read_words(100)
    assert connection.closed
    connection.close()  # The active-operation guard was released, too.


def test_socket_creation_error_keeps_os_cause(monkeypatch: pytest.MonkeyPatch) -> None:
    failure = OSError("no socket")

    def create(*_args: object, **_kwargs: object) -> socket.socket:
        raise failure

    monkeypatch.setattr(socket, "socket", create)
    with pytest.raises(FinsTransportError, match="create") as caught:
        client(9600)
    assert caught.value.__cause__ is failure


def test_local_connection_refusal_is_bounded_and_explicit() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reserved:
        reserved.bind(("127.0.0.1", 0))  # Bound but not listening.
        started = perf_counter()
        with pytest.raises(FinsTransportError) as caught:
            client(reserved.getsockname()[1], timeout=0.05)
        assert caught.value.__cause__ is not None
        assert perf_counter() - started < 1


def test_transport_imports_without_qt_numpy_or_vimo() -> None:
    root = Path(__file__).resolve().parents[2]
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "from vimo_demo.communication import FinsTcpClient, FinsTcpEnvelopeError; "
        "assert not any(name.startswith(('PySide6', 'numpy', 'cv2', 'smartmore')) "
        "for name in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-S", "-c", code, str(root / "src")],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stderr
