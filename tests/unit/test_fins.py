"""Specification vectors plus real loopback UDP; no Qt/GPU/hardware fixtures.

Run with --confcutdir=tests/unit to omit the repository's Qt conftest entirely.
Literal vectors are transcribed from W342 sections 3-3, 5-2 and 5-3 formats,
as referenced by NX W596. They are not produced by the simulator/client codec.
"""

import socket
import subprocess
import sys
from collections.abc import Callable, Generator, Iterator
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
    FinsTimeoutError,
    FinsTransportError,
    FinsUdpClient,
)
from vimo_demo.communication.simulator import FinsUdpSimulator

SOURCE = FinsAddress(0, 1, 0)
DESTINATION = FinsAddress(0, 2, 0)
READ_D100 = bytes.fromhex("80 00 02 00 02 00 00 01 00 01 01 01 82 00 64 00 00 01")
READ_1234 = bytes.fromhex("C0 00 02 00 01 00 00 02 00 01 01 01 00 00 04 D2")
WRITE_D100 = bytes.fromhex(
    "80 00 02 00 02 00 00 01 00 01 01 02 82 00 64 00 00 01 10 E1"
)
WRITE_ACK = bytes.fromhex("C0 00 02 00 01 00 00 02 00 01 01 02 00 00")
READ_THREE = bytes.fromhex("80 00 02 00 02 00 00 01 00 01 01 01 82 01 23 00 00 03")
THREE_WORDS = bytes.fromhex(
    "C0 00 02 00 01 00 00 02 00 01 01 01 00 00 00 00 FF FF 12 34"
)
WRITE_THREE = bytes.fromhex(
    "80 00 02 00 02 00 00 01 00 01 01 02 82 01 23 00 00 03 00 00 FF FF 12 34"
)
READ_W49 = bytes.fromhex("80 00 02 00 02 00 00 01 00 01 01 01 B1 00 31 00 00 04")
W49_WORDS = bytes.fromhex(
    "C0 00 02 00 01 00 00 02 00 01 01 01 00 00 FF FF 00 01 80 00 00 0F"
)
WRITE_W50 = bytes.fromhex(
    "80 00 02 00 02 00 00 01 00 01 01 02 B1 00 32 00 00 03 00 01 80 00 00 0F"
)


def client(port: int, *, timeout: float = 0.5) -> FinsUdpClient:
    return FinsUdpClient(
        "127.0.0.1",
        port=port,
        source=SOURCE,
        destination=DESTINATION,
        timeout=timeout,
    )


@contextmanager
def peer(
    reply: bytes | Callable[[bytes], bytes], *, wrong_sender: bool = False
) -> Generator[tuple[int, list[bytes]]]:
    """One independent UDP peer: captures the real request and returns wire bytes."""
    requests: list[bytes] = []
    failures: list[BaseException] = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server:
        server.bind(("127.0.0.1", 0))
        server.settimeout(2)

        def respond() -> None:
            try:
                request, sender = server.recvfrom(65535)
                requests.append(request)
                data = reply(request) if callable(reply) else reply
                if wrong_sender:
                    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as other:
                        other.bind(("127.0.0.1", 0))
                        other.sendto(data, sender)
                else:
                    server.sendto(data, sender)
            except BaseException as exc:
                failures.append(exc)

        thread = Thread(target=respond, name="fins-test-peer")
        thread.start()
        try:
            yield server.getsockname()[1], requests
        finally:
            thread.join(3)
            assert not thread.is_alive(), "UDP peer did not terminate"
            assert not failures, failures


@pytest.fixture
def simulator() -> Iterator[FinsUdpSimulator]:
    stop = Event()
    failures: list[BaseException] = []
    with FinsUdpSimulator(port=0) as server:

        def serve() -> None:
            try:
                server.serve(stop)
            except BaseException as exc:
                failures.append(exc)

        thread = Thread(target=serve, name="fins-test-simulator")
        thread.start()
        try:
            yield server
        finally:
            stop.set()
            thread.join(2)
            assert not thread.is_alive(), "Simulator did not observe stop"
            assert not failures, failures
    assert server.closed
    # No SO_REUSEADDR: a fresh bind proves that the previous socket released it.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as replacement:
        replacement.bind(("127.0.0.1", server.port))


WIRE_CASES: list[tuple[Callable[[FinsUdpClient], object], bytes, bytes, object]] = [
    (lambda c: c.read_words(100), READ_1234, READ_D100, (1234,)),
    (lambda c: c.read_words(0x123, 3), THREE_WORDS, READ_THREE, (0, 65535, 0x1234)),
    (lambda c: c.write_words(100, (4321,)), WRITE_ACK, WRITE_D100, None),
    (
        lambda c: c.write_words(0x123, (0, 65535, 0x1234)),
        WRITE_ACK,
        WRITE_THREE,
        None,
    ),
    (
        lambda c: c.read_words(49, 4, area=FinsMemoryArea.W),
        W49_WORDS,
        READ_W49,
        (65535, 1, 32768, 15),
    ),
    (
        lambda c: c.write_words(50, (1, 32768, 15), area=FinsMemoryArea.W),
        WRITE_ACK,
        WRITE_W50,
        None,
    ),
]


@pytest.mark.parametrize(("operation", "reply", "expected", "result"), WIRE_CASES)
def test_independent_wire_vectors(
    operation: Callable[[FinsUdpClient], object],
    reply: bytes,
    expected: bytes,
    result: object,
) -> None:
    with peer(reply) as (port, requests), client(port) as connection:
        assert operation(connection) == result
        assert requests == [expected]
    assert connection.closed


def test_explicit_routes_are_independent_of_ip() -> None:
    request = bytes.fromhex("80 00 02 07 FE 10 03 64 1F 01 01 01 82 00 64 00 00 01")
    reply = bytes.fromhex("C0 00 02 03 64 1F 07 FE 10 01 01 01 00 00 04 D2")
    with peer(reply) as (port, requests):
        with FinsUdpClient(
            "127.0.0.1",
            port=port,
            source=FinsAddress(3, 100, 0x1F),
            destination=FinsAddress(7, 254, 0x10),
        ) as connection:
            assert connection.read_words(100) == (1234,)
        assert requests == [request]


@pytest.mark.parametrize(
    ("offset", "value"),
    [(0, 0x80), (3, 1), (4, 3), (5, 1), (6, 1), (7, 3), (8, 1), (9, 2), (11, 2)],
)
def test_response_identity_mismatch_closes_client(offset: int, value: int) -> None:
    response = bytearray(READ_1234)
    response[offset] = value
    with peer(bytes(response)) as (port, _), client(port) as connection:
        with pytest.raises(FinsProtocolError):
            connection.read_words(100)
        assert connection.closed


def test_response_system_fields_are_not_identity_fields() -> None:
    # W342: do not validate response GCT, RSV, or reserved ICF bits.
    response = bytearray(READ_1234)
    response[:3] = b"\xfe\x7f\x00"
    with peer(bytes(response)) as (port, _), client(port) as connection:
        assert connection.read_words(100) == (1234,)


@pytest.mark.parametrize(
    "response",
    [
        b"",
        READ_1234[:9],
        READ_1234[:13],
        READ_1234[:-1],
        READ_1234 + b"\x00",
        READ_1234 + bytes(2012),
    ],
)
def test_truncated_extra_and_oversized_responses(response: bytes) -> None:
    with peer(response) as (port, _), client(port) as connection:
        with pytest.raises(FinsProtocolError, match="length|count"):
            connection.read_words(100)


def test_wrong_udp_sender_is_rejected() -> None:
    with peer(READ_1234, wrong_sender=True) as (port, _), client(port) as connection:
        with pytest.raises(FinsProtocolError, match="sender"):
            connection.read_words(100)
        assert connection.closed


@pytest.mark.parametrize("end_code", [0x1103, 0x2101, 0x0040, 0x0080, 0x8000])
def test_plc_end_code_is_preserved(end_code: int) -> None:
    reply = READ_1234[:12] + end_code.to_bytes(2, "big") + b"\x01\x02"
    with peer(reply) as (port, _), client(port) as connection:
        with pytest.raises(FinsPlcError) as caught:
            connection.read_words(100)
        assert caught.value.command == b"\x01\x01"
        assert caught.value.end_code == end_code
        assert caught.value.response_data == b"\x01\x02"
        assert not connection.closed


INVALID_OPERATIONS: list[Callable[[FinsUdpClient], object]] = [
    lambda c: c.read_words(-1),
    lambda c: c.read_words(32768),
    lambda c: c.read_words(32767, 2),
    lambda c: c.read_words(100, 0),
    lambda c: c.read_words(100, 1000),
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


@pytest.mark.parametrize("operation", INVALID_OPERATIONS)
def test_invalid_values_do_not_send(
    operation: Callable[[FinsUdpClient], object],
) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as silent_peer:
        silent_peer.bind(("127.0.0.1", 0))
        silent_peer.settimeout(0.02)
        with client(silent_peer.getsockname()[1]) as connection:
            with pytest.raises(ValueError):
                operation(connection)
            assert not connection.closed
            with pytest.raises(TimeoutError):
                silent_peer.recvfrom(65535)


@pytest.mark.parametrize(
    "fields",
    [
        (-1, 1, 0),
        (128, 1, 0),
        (0, 0, 0),
        (0, 255, 0),
        (0, 1, -1),
        (0, 1, 256),
        (True, 1, 0),
    ],
)
def test_invalid_fins_addresses(fields: tuple[int, int, int]) -> None:
    with pytest.raises(ValueError):
        FinsAddress(*fields)


@pytest.mark.parametrize(
    "host", ["localhost", "::1", "0.0.0.0", "224.0.0.1", "255.255.255.255"]
)
def test_invalid_ipv4(host: str) -> None:
    with pytest.raises(ValueError):
        FinsUdpClient(host, source=SOURCE, destination=DESTINATION)


@pytest.mark.parametrize("port", [0, 65536, True])
def test_invalid_port(port: int) -> None:
    with pytest.raises(ValueError):
        client(port)


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), True])
def test_invalid_timeout(timeout: float) -> None:
    with pytest.raises(ValueError):
        client(9600, timeout=timeout)


def test_timeout_write_is_not_retried_and_socket_is_retired() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as silent_peer:
        silent_peer.bind(("127.0.0.1", 0))
        silent_peer.settimeout(0.02)
        connection = client(silent_peer.getsockname()[1], timeout=0.05)
        start = perf_counter()
        with pytest.raises(
            FinsTimeoutError, match="write execution is unknown"
        ) as caught:
            connection.write_words(100, (4321,))
        assert isinstance(caught.value.__cause__, TimeoutError)
        assert perf_counter() - start < 1
        assert connection.closed
        assert silent_peer.recvfrom(65535)[0] == WRITE_D100
        with pytest.raises(TimeoutError):
            silent_peer.recvfrom(65535)
        connection.close()
        with pytest.raises(FinsTransportError, match="closed"):
            connection.read_words(100)


def test_unreachable_simulator_has_bounded_explicit_failure() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as unused:
        unused.bind(("127.0.0.1", 0))
        port: int = unused.getsockname()[1]
    with client(port, timeout=0.05) as connection:
        start = perf_counter()
        # Windows may report ICMP port-unreachable instead of receive timeout.
        with pytest.raises(FinsTransportError) as caught:
            connection.read_words(100)
        assert caught.value.__cause__ is not None
        assert perf_counter() - start < 1
        assert connection.closed


def test_real_udp_repeated_requests_and_limits(simulator: FinsUdpSimulator) -> None:
    with client(simulator.port) as connection:
        assert connection.read_words(100) == (1234,)
        for i in range(130):  # Cross the 8-bit SID wrap with no result history.
            connection.write_words(200, (i, 65535 - i, 0x1234))
            assert connection.read_words(200, 3) == (i, 65535 - i, 0x1234)
        connection.write_words(32767, (65535,))
        assert connection.read_words(32767) == (65535,)
        values = tuple(range(997))
        connection.write_words(1000, values)
        assert connection.read_words(1000, 999) == values + (0, 0)
    assert connection.closed


def test_w_map_is_separate_and_full_range_is_accessible(
    simulator: FinsUdpSimulator,
) -> None:
    with client(simulator.port) as connection:
        assert connection.read_words(100, area=FinsMemoryArea.W) == (0,)
        connection.write_words(100, (65535,), area=FinsMemoryArea.W)
        assert connection.read_words(100) == (1234,)
        connection.write_words(100, (9,))
        assert connection.read_words(100, area=FinsMemoryArea.W) == (65535,)
        values = tuple(range(512))
        connection.write_words(0, values, area=FinsMemoryArea.W)
        assert connection.read_words(0, 512, area=FinsMemoryArea.W) == values
        connection.write_words(511, (65535,), area=FinsMemoryArea.W)
        assert connection.read_words(511, area=FinsMemoryArea.W) == (65535,)


@pytest.mark.parametrize(
    ("packet", "end_code"),
    [
        (READ_W49[:15] + b"\x01" + READ_W49[16:], 0x1103),
        (READ_W49[:13] + bytes.fromhex("02 00 00 00 01"), 0x1103),
        (READ_W49[:13] + bytes.fromhex("01 FF 00 00 02"), 0x1104),
        (WRITE_W50[:-1], 0x1003),
        (WRITE_W50[:13] + bytes.fromhex("01 FF") + WRITE_W50[15:], 0x1104),
        (WRITE_W50 + bytes(2012), 0x1001),
    ],
)
def test_w_invalid_writes_are_atomic(packet: bytes, end_code: int) -> None:
    with FinsUdpSimulator(port=0) as server:
        assert server.handle_datagram(WRITE_W50) == WRITE_ACK
        expected_header = bytes.fromhex("C0 00 02 00 01 00 00 02 00 01")
        assert server.handle_datagram(packet) == (
            expected_header + packet[10:12] + end_code.to_bytes(2, "big")
        )
        assert server.handle_datagram(READ_W49) == W49_WORDS[:14] + bytes.fromhex(
            "00 00 00 01 80 00 00 0F"
        )
        assert server.handle_datagram(READ_W49[:16] + b"\x00\x00") == W49_WORDS[:14]
        assert server.handle_datagram(WRITE_W50[:16] + b"\x00\x00") == WRITE_ACK


@pytest.mark.parametrize(
    ("packet", "end_code"),
    [
        (READ_D100[:10] + b"\x06\x01", 0x0401),
        (READ_D100[:15], 0x1002),
        (READ_D100 + b"\x00", 0x1001),
        (READ_D100[:12] + b"\xb0" + READ_D100[13:], 0x1101),
        (READ_D100[:15] + b"\x01" + READ_D100[16:], 0x1103),
        (READ_D100[:13] + bytes.fromhex("80 00 00 00 01"), 0x1103),
        (READ_D100[:13] + bytes.fromhex("7F FF 00 00 02"), 0x1104),
        (READ_D100[:16] + bytes.fromhex("03 E8"), 0x110B),
        (WRITE_D100[:-1], 0x1003),
        (WRITE_D100 + bytes(2012), 0x1001),
    ],
)
def test_simulator_documented_errors_over_udp(
    simulator: FinsUdpSimulator, packet: bytes, end_code: int
) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as raw:
        raw.settimeout(0.5)
        raw.sendto(packet, ("127.0.0.1", simulator.port))
        response, sender = raw.recvfrom(65535)
        assert sender == ("127.0.0.1", simulator.port)
        # Independently specified response header; command echoes even on errors.
        expected = bytes.fromhex("C0 00 02 00 01 00 00 02 00 01")
        assert response == expected + packet[10:12] + end_code.to_bytes(2, "big")
    with client(simulator.port) as connection:
        assert connection.read_words(100) == (1234,)  # Invalid writes were atomic.


def test_simulator_vectors_and_safe_malformed_packets() -> None:
    with FinsUdpSimulator(port=0) as server:
        assert server.handle_datagram(READ_D100) == READ_1234
        assert server.handle_datagram(WRITE_D100) == WRITE_ACK
        assert server.handle_datagram(READ_D100) == READ_1234[:-2] + b"\x10\xe1"
        assert server.handle_datagram(WRITE_THREE) == WRITE_ACK
        assert server.handle_datagram(READ_THREE) == THREE_WORDS
        # Valid zero-element protocol no-ops, not exposed by the client API.
        assert server.handle_datagram(READ_D100[:16] + b"\x00\x00") == READ_1234[:14]
        assert server.handle_datagram(WRITE_D100[:16] + b"\x00\x00") == WRITE_ACK
        malformed = [b"", b"FINS", READ_D100[:11], READ_1234, b"\x81" + READ_D100[1:]]
        malformed += [READ_D100[:4] + b"\x03" + READ_D100[5:]]
        malformed += [READ_D100[:7] + b"\x00" + READ_D100[8:]]
        for packet in malformed:
            assert server.handle_datagram(packet) is None
        assert server.handle_datagram(READ_D100) == READ_1234[:-2] + b"\x10\xe1"
    server.close()
    with pytest.raises(RuntimeError, match="closed"):
        server.__enter__()


def test_malformed_udp_does_not_terminate_simulator(
    simulator: FinsUdpSimulator,
) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as raw:
        raw.sendto(b"broken", ("127.0.0.1", simulator.port))
    with client(simulator.port) as connection:
        assert connection.read_words(100) == (1234,)


def test_overlapping_request_and_close_fail_without_cancelling_active_call() -> None:
    admitted, release = Event(), Event()

    def reply(_: bytes) -> bytes:
        admitted.set()
        assert release.wait(2)
        return READ_1234

    with peer(reply) as (port, _), client(port, timeout=2) as connection:
        outcomes: list[tuple[int, ...] | BaseException] = []

        def infer() -> None:
            try:
                outcomes.append(connection.read_words(100))
            except BaseException as exc:
                outcomes.append(exc)

        owner = Thread(target=infer, name="fins-test-client")
        owner.start()
        try:
            assert admitted.wait(1)
            with pytest.raises(FinsTransportError, match="active"):
                connection.read_words(100)
            with pytest.raises(FinsTransportError, match="active"):
                connection.close()
            assert not connection.closed
        finally:
            release.set()
            owner.join(3)
        assert not owner.is_alive()
        assert outcomes == [(1234,)]


def test_cli_demo_in_independent_processes_without_site_packages() -> None:
    root = Path(__file__).resolve().parents[2]
    # -S omits installed Qt/NumPy/ViMo packages; add only our source path.
    bootstrap = "\n".join(
        (
            "import runpy,sys",
            "sys.path.insert(0,'src')",
            "try:",
            "    runpy.run_path(sys.argv.pop(1),run_name='__main__')",
            "except SystemExit as exc:",
            "    assert exc.code == 0, exc.code",
            "assert not any(n.startswith(('PySide6','smartmore','numpy')) for n in sys.modules)",
        )
    )
    command = [sys.executable, "-B", "-S", "-u", "-c", bootstrap]
    with subprocess.Popen(
        command
        + ["scripts/run_fins_simulator.py", "--port", "0", "--max-requests", "3"],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as process:
        assert process.stdout is not None
        ready = process.stdout.readline()
        assert "FINS simulator 127.0.0.1:" in ready
        port = ready.split(":", 1)[1].split(";", 1)[0]
        try:
            demo = subprocess.run(
                command
                + ["scripts/fins_local_demo.py", "--port", port, "--timeout", "0.05"],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            output, errors = process.communicate(timeout=5)
        finally:
            # A failed client must not leave the independent test server waiting
            # for its three requests. Empty datagrams finish the finite serve loop.
            if process.poll() is None:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as cleanup:
                    for _ in range(3):
                        cleanup.sendto(b"", ("127.0.0.1", int(port)))
                try:
                    process.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    # Only a broken, test-owned stdlib server: do not let Popen's
                    # context wait forever. This is never successful-stop evidence.
                    process.kill()
                    process.communicate(timeout=3)
                    raise
        assert process.returncode == 0, errors
        assert "Simulator socket closed" in output
        assert demo.returncode == 0, demo.stderr
        assert "Read D100: 1234" in demo.stdout
        assert "Wrote D100: 4321 (localhost)" in demo.stdout
        assert "Read D100: 4321" in demo.stdout
        assert "Expected timeout:" in demo.stdout
        assert "Demo sockets closed" in demo.stdout
    assert not any(t.name.startswith("fins-test-") for t in threads())


def test_socket_setup_failure_preserves_cause_and_releases_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = OSError("test-only socket setup failure")
    created: list[socket.socket] = []

    class BrokenSocket(socket.socket):
        def settimeout(self, value: float | None) -> None:
            raise failure

    def create(*args: object, **kwargs: object) -> socket.socket:
        result = BrokenSocket(socket.AF_INET, socket.SOCK_DGRAM)
        created.append(result)
        return result

    monkeypatch.setattr(socket, "socket", create)
    with pytest.raises(FinsTransportError, match="configure") as caught:
        client(9600)
    assert caught.value.__cause__ is failure
    assert len(created) == 1 and created[0].fileno() == -1


def test_context_closes_on_caller_exception_without_an_online_handshake() -> None:
    connection = client(9600)  # Creation sends no packet or connectivity probe.
    with pytest.raises(RuntimeError, match="caller failure"):
        with connection:
            raise RuntimeError("caller failure")
    assert connection.closed
    connection.close()
    with pytest.raises(FinsTransportError, match="closed"):
        connection.__enter__()
