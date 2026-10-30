"""Exact Modbus TCP framing and bounded socket/serial ownership, localhost only."""

import socket
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event
from typing import cast

import pytest
from tests.support.modbus import ModbusPeer, frame

from capsule_vision.communication import FinsMemoryArea, FinsTcpEndpoint
from capsule_vision.communication.modbus_tcp import (
    HoldingRegisters,
    ModbusExceptionResponse,
    ModbusProtocolError,
    ModbusSettings,
    ModbusTcpClient,
    ModbusTimeoutError,
    ModbusTransportError,
)
from capsule_vision.communication.operations import (
    CommunicationRequest,
    RequestKind,
    execute_request,
)


class RolloverClient(ModbusTcpClient):
    def at_rollover(self) -> None:
        self._transaction = 65535


def test_fragmented_reads_use_one_deadline_and_bounded_receive_sizes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FragmentSocket:
        def __init__(self) -> None:
            self.data = bytearray(frame(b"\x03\x02\x12\x34"))
            self.timeouts: list[float] = []
            self.sizes: list[int] = []
            self.closed = False

        def connect(self, address: tuple[str, int]) -> None:
            assert address == ("127.0.0.1", 502)

        def settimeout(self, timeout: float) -> None:
            self.timeouts.append(timeout)

        def fileno(self) -> int:
            return -1 if self.closed else 42

        def sendall(self, data: bytes) -> None:
            assert data == bytes.fromhex("000100000006ff0300640001")

        def recv(self, size: int) -> bytes:
            self.sizes.append(size)
            return bytes((self.data.pop(0),))

        def close(self) -> None:
            self.closed = True

    peer = FragmentSocket()
    ticks = iter(i / 100 for i in range(100))
    monkeypatch.setattr(
        "capsule_vision.communication.modbus_tcp.monotonic", lambda: next(ticks)
    )

    def socket_factory(*args: object) -> FragmentSocket:
        return peer

    monkeypatch.setattr(
        "capsule_vision.communication.modbus_tcp.socket.socket", socket_factory
    )
    with ModbusTcpClient("127.0.0.1") as client:
        assert client.read_words(100) == (0x1234,)
    assert peer.closed and not peer.data
    assert peer.sizes == [7, 6, 5, 4, 3, 2, 1, 4, 3, 2, 1]
    # Connect and exchange each start their own deadline. Every fragment reduces
    # the exchange's remaining budget; fragmented traffic cannot extend it.
    assert all(
        a > b > 0 for a, b in zip(peer.timeouts[1:], peer.timeouts[2:], strict=False)
    )


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError("controlled connect timeout"),
        OSError("controlled connection failure"),
    ],
)
def test_connect_failure_closes_allocated_socket(
    failure: OSError,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[bool] = []

    class FailedConnect:
        def settimeout(self, timeout: float) -> None:
            assert timeout > 0

        def connect(self, address: tuple[str, int]) -> None:
            raise failure

        def close(self) -> None:
            closed.append(True)

    def socket_factory(*args: object) -> FailedConnect:
        return FailedConnect()

    monkeypatch.setattr(
        "capsule_vision.communication.modbus_tcp.socket.socket", socket_factory
    )
    with pytest.raises(
        ModbusTimeoutError
        if isinstance(failure, TimeoutError)
        else ModbusTransportError
    ):
        ModbusTcpClient("127.0.0.1")
    assert closed == [True]


def test_exact_requests_fragmentation_rollover_and_context_cleanup() -> None:
    with ModbusPeer() as peer:
        original = peer.normal_response

        def fragments(request: bytes) -> tuple[bytes, ...]:
            response = original(request)[0]
            return tuple(bytes((byte,)) for byte in response)

        peer.respond = fragments
        with RolloverClient("127.0.0.1", port=peer.port, unit_id=7) as client:
            assert client.read_words(100, 2) == (1234, 65535)
            client.write_words(65535, (0,))
            client.write_words(0, (0x1234, 65535))
            client.at_rollover()
            assert client.read_words(0, 2) == (0x1234, 65535)
        assert client.closed
        assert peer.requests == [
            bytes.fromhex("000100000006070300640002"),
            bytes.fromhex("0002000000060706ffff0000"),
            bytes.fromhex("00030000000b071000000002041234ffff"),
            bytes.fromhex("000000000006070300000002"),
        ]
    assert peer.accepted == 1 and peer.disconnected == 1


def test_maximum_ranges_and_values() -> None:
    with ModbusPeer() as peer, ModbusTcpClient("127.0.0.1", port=peer.port) as client:
        client.write_words(65536 - 123, (65535,) * 123)
        assert client.read_words(65536 - 125, 125) == (0, 0) + (65535,) * 123
        assert len(peer.requests[0]) == 259 and len(peer.requests[1]) == 12


@pytest.mark.parametrize(
    "start,count,values",
    [
        (-1, 1, None),
        (65536, 1, None),
        (65535, 2, None),
        (True, 1, None),
        (0, 0, None),
        (0, 126, None),
        (0, True, None),
        (0, 0, ()),
        (0, 124, (0,) * 124),
        (65535, 2, (1, 2)),
        (0, 1, (-1,)),
        (0, 1, (65536,)),
        (0, 1, (True,)),
    ],
)
def test_invalid_operations_send_nothing(
    start: int, count: int, values: tuple[int, ...] | None
) -> None:
    with ModbusPeer() as peer, ModbusTcpClient("127.0.0.1", port=peer.port) as client:
        with pytest.raises(ValueError):
            if values is None:
                client.read_words(start, count)
            else:
                client.write_words(start, values)
        assert not peer.requests and not client.closed
        assert client.read_words(65535) == (0,)


@pytest.mark.parametrize(
    "response",
    [
        frame(b"\x03\x02\x00\x01", transaction=2),
        frame(b"\x03\x02\x00\x01", protocol=1),
        frame(b"\x03\x02\x00\x01", unit=1),
        frame(b"\x03", length=0),
        frame(b"\x03", length=1),
        frame(b"\x03", length=255),
        frame(b"\x03", length=65535),
        frame(b"\x03\x02\x00\x01\x00"),
        frame(b"\x03\x02\x00"),
        frame(b"\x04\x02\x00\x01"),
        frame(b"\x03\x01\x00\x01"),
        frame(b"\x83\x00"),
        frame(b"\x83\x02\x00\x00"),
    ],
)
def test_malformed_responses_poison_without_retry(response: bytes) -> None:
    with ModbusPeer(lambda _: (response,)) as peer:
        client = ModbusTcpClient("127.0.0.1", port=peer.port)
        with pytest.raises(ModbusProtocolError):
            client.read_words(100)
        assert client.closed
        with pytest.raises(ModbusTransportError, match="closed"):
            client.read_words(100)
        with pytest.raises(ModbusTransportError, match="closed"):
            client.__enter__()
        client.close()
    assert len(peer.requests) == peer.accepted == 1


@pytest.mark.parametrize("function,code", [(3, 1), (6, 2), (16, 4), (3, 11)])
def test_exception_responses_are_explicit_and_retire(function: int, code: int) -> None:
    with ModbusPeer(lambda _: (frame(bytes((function | 0x80, code))),)) as peer:
        client = ModbusTcpClient("127.0.0.1", port=peer.port)
        with pytest.raises(ModbusExceptionResponse) as caught:
            if function == 3:
                client.read_words(100)
            else:
                client.write_words(100, (1,) if function == 6 else (1, 2))
        assert (caught.value.function, caught.value.code) == (function, code)
        assert client.closed and len(peer.requests) == 1


@pytest.mark.parametrize("multiple", [False, True])
def test_write_echo_must_match_exactly(multiple: bool) -> None:
    function = 16 if multiple else 6
    with ModbusPeer(lambda _: (frame(bytes((function, 0, 101, 0, 1))),)) as peer:
        with ModbusTcpClient("127.0.0.1", port=peer.port) as client:
            with pytest.raises(ModbusProtocolError, match="echo"):
                client.write_words(100, (4321, 4322) if multiple else (4321,))
            assert client.closed


@pytest.mark.parametrize(
    "partial", [b"", b"\x00\x01", frame(b"\x06\x00\x64\x10\xe1")[:8]]
)
def test_partial_deadline_and_serial_ownership(partial: bytes) -> None:
    waiting = Event()

    def hold(request: bytes) -> tuple[bytes, ...] | None:
        if not waiting.is_set():
            waiting.set()
            return (partial,) if partial else None
        return None

    with ModbusPeer(hold) as peer:
        client = ModbusTcpClient("127.0.0.1", port=peer.port, timeout=0.2)
        with ThreadPoolExecutor(1) as owner:
            future = owner.submit(client.write_words, 100, (4321,))
            assert waiting.wait(1)
            with pytest.raises(ModbusTransportError, match="already active"):
                client.read_words(0)
            with pytest.raises(ModbusTransportError, match="active"):
                client.close()
            with pytest.raises(ModbusTimeoutError, match="uncertain"):
                future.result(2)
        assert client.closed and len(peer.requests) == 1
    assert peer.accepted == 1


def test_disconnect_and_connect_failure_release_socket() -> None:
    with ModbusPeer(lambda _: ()) as peer:
        client = ModbusTcpClient("127.0.0.1", port=peer.port)
        with pytest.raises(ModbusTransportError, match="disconnected"):
            client.read_words(100)
        assert client.closed
        port = peer.port
    with pytest.raises(ModbusTransportError, match="connect"):
        ModbusTcpClient("127.0.0.1", port=port, timeout=0.1)
    with socket.socket() as replacement:
        replacement.bind(("127.0.0.1", port))


@pytest.mark.parametrize(
    "changes",
    [
        {"host": "localhost"},
        {"host": "0.0.0.0"},
        {"host": "224.0.0.1"},
        {"port": 0},
        {"port": True},
        {"timeout": float("inf")},
        {"timeout": True},
        {"unit_id": -1},
        {"unit_id": 256},
        {"unit_id": True},
    ],
)
def test_invalid_settings_never_construct_socket(
    changes: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("invalid settings reached socket")

    monkeypatch.setattr(
        "capsule_vision.communication.modbus_tcp.socket.socket", forbidden
    )
    with pytest.raises(ValueError):
        replace(ModbusSettings(), **cast(dict[str, int], changes))


def test_request_execution_selects_fc06_fc16_and_saved_unit() -> None:
    with ModbusPeer() as peer:
        request = CommunicationRequest(
            kind=RequestKind.READ,
            start=100,
            count=2,
            area=HoldingRegisters.HOLDING,
            endpoint=ModbusSettings(
                host="127.0.0.1", port=peer.port, timeout=1, unit_id=17
            ),
        )
        result = execute_request(request)
        assert result.values == (1234, 65535) and result.completed_at.tzinfo is not None
        for values in ((4321,), (1, 2)):
            assert (
                execute_request(
                    replace(
                        request,
                        kind=RequestKind.WRITE,
                        count=len(values),
                        values=values,
                    )
                ).values
                is None
            )
        assert [packet[7] for packet in peer.requests] == [3, 6, 16]
        assert all(packet[6] == 17 for packet in peer.requests)
        for invalid in (
            replace(request, area=FinsMemoryArea.DM),
            replace(
                request,
                endpoint=FinsTcpEndpoint(
                    host=request.endpoint.host,
                    port=request.endpoint.port,
                    timeout=request.endpoint.timeout,
                ),
            ),
            replace(request, count=126),
        ):
            with pytest.raises(ValueError):
                execute_request(invalid)
        with pytest.raises(ValueError):
            replace(request.endpoint, unit_id=256)
        assert len(peer.requests) == 3
