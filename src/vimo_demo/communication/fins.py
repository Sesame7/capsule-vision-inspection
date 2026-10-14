"""Small synchronous FINS/UDP DM/WR-word subset (W596 / W342).

One caller owns each client. Overlapping operations/close fail explicitly, rather
than creating a request queue. Socket creation does not establish PLC readiness.
"""

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

DEFAULT_PORT = 9600
MAX_DATAGRAM = 2012
DM_WORDS = 32768
W_WORDS = 512
MAX_READ_WORDS = 999
MAX_WRITE_WORDS = 997
DM_WORD_AREA = 0x82
W_WORD_AREA = 0xB1
READ_COMMAND = b"\x01\x01"
WRITE_COMMAND = b"\x01\x02"


class FinsMemoryArea(Enum):
    """Word access only; WR range follows W342 section 5-2-2."""

    DM = DM_WORD_AREA
    W = W_WORD_AREA

    @property
    def word_count(self) -> int:
        return DM_WORDS if self is FinsMemoryArea.DM else W_WORDS

    @property
    def prefix(self) -> str:
        return "D" if self is FinsMemoryArea.DM else "W"


class FinsError(Exception):
    """Communication failure, never an inspection/product judgment."""


class FinsProtocolError(FinsError):
    """Unexpected peer, request identity, framing, or response data."""


class FinsTransportError(FinsError):
    """Socket operation or client ownership/lifecycle failure."""


class FinsTimeoutError(FinsTransportError):
    """Request deadline elapsed; a timed-out write may already have executed."""


class FinsPlcError(FinsError):
    """PLC returned a nonzero end code, including any CPU/relay error flags."""

    def __init__(self, command: bytes, end_code: int, response_data: bytes) -> None:
        self.command = command
        self.end_code = end_code
        self.response_data = response_data
        super().__init__(
            f"FINS command {command.hex(' ')}: PLC end code 0x{end_code:04X}"
        )


def validate_integer(name: str, value: object, minimum: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be in {minimum}..{maximum}")


@dataclass(frozen=True, slots=True)
class FinsAddress:
    """Explicit wire address; not inferred from an IP address.

    Unit is a byte, not a claim that a given device implements that unit/service.
    The local CPU-unit example uses unit 0. Broadcast/internal node 0 is excluded.
    """

    network: int
    node: int
    unit: int

    def __post_init__(self) -> None:
        validate_integer("network", self.network, 0, 127)
        validate_integer("node", self.node, 1, 254)
        validate_integer("unit", self.unit, 0, 255)

    def to_bytes(self) -> bytes:
        return bytes((self.network, self.node, self.unit))


def validate_address(value: object) -> FinsAddress:
    if not isinstance(value, FinsAddress):
        raise ValueError("FINS address must be a FinsAddress instance")
    return value


def _word_range(start: int, count: int, maximum: int, area: object) -> FinsMemoryArea:
    if not isinstance(area, FinsMemoryArea):
        raise ValueError("area must be a FinsMemoryArea instance")
    validate_integer("start", start, 0, area.word_count - 1)
    validate_integer("count", count, 1, maximum)
    if start + count > area.word_count:
        raise ValueError(
            f"{area.name} word range exceeds {area.prefix}{area.word_count - 1}"
        )
    return area


class FinsUdpClient:
    """Serial, response-required UDP exchanges; no retries, polling, or threads.

    Validation happens before send. Protocol/transport/timeout failures close the
    socket: late datagrams cannot contaminate another exchange on this client.
    A PLC end-code error leaves the client usable. Call close() or use with.
    """

    def __init__(
        self,
        host: str,
        *,
        destination: FinsAddress,
        source: FinsAddress,
        port: int = DEFAULT_PORT,
        timeout: float = 1.0,
    ) -> None:
        address = IPv4Address(host)  # Numeric IPv4 only: no unbounded DNS lookup.
        if address.is_unspecified or address.is_multicast or int(address) == 0xFFFFFFFF:
            raise ValueError("host must be a unicast IPv4 address")
        validate_integer("port", port, 1, 65535)
        if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        self._peer = (str(address), port)
        self._destination = validate_address(destination)
        self._source = validate_address(source)
        self._timeout = timeout
        self._sid = 0
        self._operation = Lock()
        try:
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        except OSError as exc:
            raise FinsTransportError(f"Cannot create FINS UDP socket: {exc}") from exc
        try:
            self._socket.settimeout(timeout)
        except OSError as exc:
            self._socket.close()
            raise FinsTransportError(
                f"Cannot configure FINS UDP socket: {exc}"
            ) from exc
        except ValueError, OverflowError:
            self._socket.close()
            raise

    @property
    def closed(self) -> bool:
        return self._socket.fileno() == -1

    def __enter__(self) -> FinsUdpClient:
        if self.closed:
            raise FinsTransportError("FINS client is closed")
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
            raise FinsTransportError("Cannot close during an active FINS request")
        try:
            self._socket.close()
        finally:
            self._operation.release()

    def read_words(
        self, start: int, count: int = 1, *, area: FinsMemoryArea = FinsMemoryArea.DM
    ) -> tuple[int, ...]:
        """Read consecutive unsigned words; existing calls default to D/DM."""
        area = _word_range(start, count, MAX_READ_WORDS, area)
        body = struct.pack("!BHBH", area.value, start, 0, count)
        data = self._exchange(READ_COMMAND, body, count * 2)
        return struct.unpack(f"!{count}H", data)

    def write_words(
        self,
        start: int,
        values: Sequence[int],
        *,
        area: FinsMemoryArea = FinsMemoryArea.DM,
    ) -> None:
        """Write consecutive unsigned words; timeout never triggers a retry."""
        area = _word_range(start, len(values), MAX_WRITE_WORDS, area)
        words = tuple(values)
        _word_range(start, len(words), MAX_WRITE_WORDS, area)
        for value in words:
            validate_integer("word", value, 0, 65535)
        body = struct.pack("!BHBH", area.value, start, 0, len(words))
        body += struct.pack(f"!{len(words)}H", *words)
        self._exchange(WRITE_COMMAND, body, 0)

    def _exchange(self, command: bytes, body: bytes, expected_bytes: int) -> bytes:
        if not self._operation.acquire(blocking=False):
            raise FinsTransportError("A FINS request is already active")
        try:
            if self.closed:
                raise FinsTransportError("FINS client is closed")
            self._sid = (self._sid + 1) & 0xFF
            # ICF: command/response required; RSV=0; GCT=2 (W342 section 3-3).
            request = b"\x80\x00\x02" + self._destination.to_bytes()
            request += self._source.to_bytes() + bytes((self._sid,)) + command + body
            if len(request) > MAX_DATAGRAM or 14 + expected_bytes > MAX_DATAGRAM:
                raise ValueError("FINS datagram exceeds the supported 2012-byte limit")
            deadline = monotonic() + self._timeout
            try:
                self._socket.settimeout(self._timeout)
                sent = self._socket.sendto(request, self._peer)
                if sent != len(request):
                    raise FinsTransportError("Incomplete UDP send")
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise TimeoutError("FINS send exhausted request deadline")
                self._socket.settimeout(remaining)
                # Read the complete UDP datagram, then reject oversize packets.
                response, sender = self._socket.recvfrom(65535)
                if sender != self._peer:
                    raise FinsProtocolError(f"Unexpected UDP sender {sender!r}")
                return self._response(response, command, expected_bytes)
            except TimeoutError as exc:
                self._socket.close()
                detail = (
                    "; write execution is unknown" if command == WRITE_COMMAND else ""
                )
                raise FinsTimeoutError(
                    f"FINS {command.hex(' ')} to {self._peer} timed out{detail}"
                ) from exc
            except OSError as exc:
                self._socket.close()
                raise FinsTransportError(f"FINS UDP to {self._peer}: {exc}") from exc
            except FinsProtocolError, FinsTransportError:
                self._socket.close()
                raise
        finally:
            self._operation.release()

    def _response(self, response: bytes, command: bytes, expected_bytes: int) -> bytes:
        if not 14 <= len(response) <= MAX_DATAGRAM:
            raise FinsProtocolError(f"Invalid FINS response length: {len(response)}")
        if response[0] & 0xC0 != 0xC0:
            raise FinsProtocolError("Expected a FINS response header")
        # W342 says not to check response RSV, GCT or ICF reserved bits.
        if response[3:6] != self._source.to_bytes():
            raise FinsProtocolError("Response destination differs from request source")
        if response[6:9] != self._destination.to_bytes():
            raise FinsProtocolError("Response source differs from request destination")
        if response[9] != self._sid or response[10:12] != command:
            raise FinsProtocolError("Response SID/command does not match request")
        end_code = int.from_bytes(response[12:14], "big")
        if end_code:
            raise FinsPlcError(command, end_code, response[14:])
        if len(response) != 14 + expected_bytes:
            raise FinsProtocolError("Response word count/payload length does not match")
        return response[14:]
