"""Bounded serial Modbus/TCP Holding Register access (FC03/06/16)."""

import math
import socket
import struct
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from ipaddress import IPv4Address
from threading import Lock
from time import monotonic
from types import TracebackType
from typing import Literal, Self

from .fins import validate_integer

MAX_READ_REGISTERS = 125
MAX_WRITE_REGISTERS = 123


class HoldingRegisters(Enum):
    HOLDING = "holding"

    @property
    def prefix(self) -> str:
        return "HR "

    @property
    def word_count(self) -> int:
        return 65536


class ModbusError(Exception):
    """Communication failure, never a Production judgment."""


class ModbusProtocolError(ModbusError):
    """Invalid MBAP identity, framing or response payload."""


class ModbusTransportError(ModbusError):
    """Socket or serial ownership failure."""


class ModbusTimeoutError(ModbusTransportError):
    """Deadline elapsed; a write outcome may be uncertain."""


class ModbusExceptionResponse(ModbusError):
    def __init__(self, function: int, code: int) -> None:
        self.function, self.code = function, code
        super().__init__(f"Modbus FC{function:02d}: exception 0x{code:02X}")


@dataclass(frozen=True, slots=True)
class ModbusSettings:
    """Modbus-only endpoint shared by diagnostics, reporting and the wire client."""

    host: str = "127.0.0.1"
    port: int = 502
    timeout: float = 1.0
    unit_id: int = 255

    @property
    def protocol(self) -> Literal["modbus_tcp"]:
        return "modbus_tcp"

    def __post_init__(self) -> None:
        address = IPv4Address(self.host)
        if address.is_unspecified or address.is_multicast or int(address) == 0xFFFFFFFF:
            raise ValueError("host must be a numeric unicast IPv4 address")
        validate_integer("port", self.port, 1, 65535)
        validate_integer("Unit ID", self.unit_id, 0, 255)
        if (
            isinstance(self.timeout, bool)
            or not math.isfinite(self.timeout)
            or not 0.05 <= self.timeout <= 30
        ):
            raise ValueError("timeout must be in 0.05..30 seconds")


def validate_register_range(start: int, count: int, maximum: int) -> None:
    validate_integer("Holding Register address", start, 0, 65535)
    validate_integer("register count", count, 1, maximum)
    if start + count > 65536:
        raise ValueError("Holding Register range exceeds 65535")


class ModbusTcpClient:
    """One connection/operation owner. Every failed exchange retires the socket.

    No retry, reconnect or readback. A write failure does not prove nonexecution.
    One total deadline covers each connect or send/receive exchange.
    """

    def __init__(
        self, host: str, *, port: int = 502, timeout: float = 1.0, unit_id: int = 255
    ) -> None:
        self.settings = ModbusSettings(host, port, timeout, unit_id)
        self._operation = Lock()
        self._transaction = 0
        try:
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        except OSError as exc:
            raise ModbusTransportError(
                f"Cannot create Modbus TCP socket: {exc}"
            ) from exc
        try:
            self._remaining(monotonic() + timeout)
            self._socket.connect((host, port))
        except TimeoutError as exc:
            self._socket.close()
            raise ModbusTimeoutError(
                f"Modbus TCP connect to {host}:{port} timed out"
            ) from exc
        except BaseException as exc:
            self._socket.close()
            if isinstance(exc, OSError):
                raise ModbusTransportError(f"Modbus TCP connect: {exc}") from exc
            raise

    @property
    def closed(self) -> bool:
        return self._socket.fileno() == -1

    def __enter__(self) -> Self:
        if self.closed:
            raise ModbusTransportError("Modbus TCP client is closed")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        if not self._operation.acquire(blocking=False):
            raise ModbusTransportError("Cannot close during an active Modbus request")
        try:
            self._socket.close()
        finally:
            self._operation.release()

    def read_words(self, start: int, count: int = 1) -> tuple[int, ...]:
        validate_register_range(start, count, MAX_READ_REGISTERS)
        pdu = self._exchange(struct.pack("!BHH", 3, start, count), 2 + count * 2)
        return struct.unpack(f"!{count}H", pdu[2:])

    def write_words(self, start: int, values: Sequence[int]) -> None:
        validate_register_range(start, len(values), MAX_WRITE_REGISTERS)
        words = tuple(values)
        validate_register_range(start, len(words), MAX_WRITE_REGISTERS)
        for value in words:
            validate_integer("register value", value, 0, 65535)
        if len(words) == 1:
            pdu = struct.pack("!BHH", 6, start, words[0])
        else:
            pdu = struct.pack("!BHHB", 16, start, len(words), len(words) * 2)
            pdu += struct.pack(f"!{len(words)}H", *words)
        self._exchange(pdu, 5)

    def _remaining(self, deadline: float) -> None:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError("Modbus operation deadline elapsed")
        self._socket.settimeout(remaining)

    def _receive(self, size: int, deadline: float) -> bytes:
        data = bytearray()
        while len(data) < size:
            self._remaining(deadline)
            part = self._socket.recv(size - len(data))
            if not part:
                raise ModbusTransportError("Modbus peer disconnected during response")
            data.extend(part)
        return bytes(data)

    def _exchange(self, request: bytes, expected_size: int) -> bytes:
        if not self._operation.acquire(blocking=False):
            raise ModbusTransportError("A Modbus request is already active")
        try:
            if self.closed:
                raise ModbusTransportError("Modbus TCP client is closed")
            self._transaction = (self._transaction + 1) & 0xFFFF
            deadline = monotonic() + self.settings.timeout
            try:
                self._remaining(deadline)
                self._socket.sendall(
                    struct.pack(
                        "!HHHB",
                        self._transaction,
                        0,
                        len(request) + 1,
                        self.settings.unit_id,
                    )
                    + request
                )
                transaction, protocol, length, unit = struct.unpack(
                    "!HHHB", self._receive(7, deadline)
                )
                if (
                    transaction != self._transaction
                    or protocol != 0
                    or unit != self.settings.unit_id
                ):
                    raise ModbusProtocolError(
                        "Unexpected Modbus MBAP transaction/protocol/Unit ID"
                    )
                if not 2 <= length <= 254 or length not in (3, expected_size + 1):
                    raise ModbusProtocolError(f"Invalid Modbus MBAP length: {length}")
                response = self._receive(length - 1, deadline)
                function = request[0]
                if response[0] == function | 0x80:
                    if len(response) != 2 or response[1] == 0:
                        raise ModbusProtocolError("Invalid Modbus exception response")
                    raise ModbusExceptionResponse(function, response[1])
                if response[0] != function or len(response) != expected_size:
                    raise ModbusProtocolError(
                        "Unexpected Modbus function or response length"
                    )
                if function == 3:
                    if response[1] != expected_size - 2:
                        raise ModbusProtocolError("Invalid Modbus register byte count")
                elif response != request[:5]:
                    raise ModbusProtocolError(
                        "Invalid Modbus write address/value/count echo"
                    )
                return response
            except BaseException as exc:
                self._socket.close()
                if function_is_write := request[0] in (6, 16):
                    exc.add_note(
                        "Write outcome may be uncertain; no retry or readback attempted."
                    )
                detail = "; write outcome may be uncertain" if function_is_write else ""
                if isinstance(exc, TimeoutError):
                    raise ModbusTimeoutError(f"Modbus TCP timed out{detail}") from exc
                if isinstance(exc, OSError):
                    raise ModbusTransportError(f"Modbus TCP: {exc}{detail}") from exc
                raise
        finally:
            self._operation.release()
