"""Serial FINS/TCP words with automatic node allocation (W421 section 7-4).

Construction connects and negotiates once. No Qt, retry, reconnect or polling.
Only direct local-network CPU-unit addressing is supported in this subset.
"""

import math
import socket
import struct
from collections.abc import Sequence
from ipaddress import IPv4Address
from threading import Lock
from time import monotonic
from types import TracebackType

from .fins import (
    DEFAULT_PORT,
    MAX_DATAGRAM,
    MAX_READ_WORDS,
    MAX_WRITE_WORDS,
    READ_COMMAND,
    WRITE_COMMAND,
    FinsAddress,
    FinsMemoryArea,
    FinsPlcError,
    FinsProtocolError,
    FinsTimeoutError,
    FinsTransportError,
    decode_response,
    validate_integer,
    validate_word_range,
)

MAX_TCP_LENGTH = 8 + MAX_DATAGRAM  # Length counts command/error + FINS, not prefix.


class FinsTcpEnvelopeError(FinsProtocolError):
    """Negotiation/envelope failure; preserve both raw 32-bit outer fields."""

    def __init__(self, command: int, error_code: int, stage: str) -> None:
        self.command = command
        self.error_code = error_code
        super().__init__(
            f"FINS/TCP {stage}: outer command 0x{command:08X}, "
            f"error code 0x{error_code:08X}"
        )


class FinsTcpClient:
    """One synchronous owner, one negotiated connection, one active request.

    Construction has one total connect/handshake deadline; each exchange has one
    send/receive deadline. Broken exchanges retire the socket. A valid nonzero
    FINS PLC end code leaves it usable. Write failures never imply nonexecution.
    """

    def __init__(
        self, host: str, *, port: int = DEFAULT_PORT, timeout: float = 1.0
    ) -> None:
        address = IPv4Address(host)  # Numeric IPv4 avoids an unbounded DNS lookup.
        if address.is_unspecified or address.is_multicast or int(address) == 0xFFFFFFFF:
            raise ValueError("host must be a unicast IPv4 address")
        validate_integer("port", port, 1, 65535)
        if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        self._peer = (str(address), port)
        self._timeout = timeout
        self._operation = Lock()
        self._sid = 0
        try:
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        except OSError as exc:
            raise FinsTransportError(f"Cannot create FINS TCP socket: {exc}") from exc
        try:
            deadline = monotonic() + timeout
            self._set_remaining(deadline)
            self._socket.connect(self._peer)
            self._source, self._destination = self._negotiate(deadline)
        except TimeoutError as exc:
            self._socket.close()
            raise FinsTimeoutError(
                f"FINS/TCP connect/negotiation to {self._peer} timed out"
            ) from exc
        except OSError as exc:
            self._socket.close()
            raise FinsTransportError(f"FINS TCP to {self._peer}: {exc}") from exc
        except BaseException:
            self._socket.close()
            raise

    @property
    def source(self) -> FinsAddress:
        """Negotiated client node, network/unit zero; not derived from IPv4."""
        return self._source

    @property
    def destination(self) -> FinsAddress:
        """Negotiated server node at its local CPU unit."""
        return self._destination

    @property
    def closed(self) -> bool:
        return self._socket.fileno() == -1

    def __enter__(self) -> FinsTcpClient:
        if self.closed:
            raise FinsTransportError("FINS TCP client is closed")
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
        area = validate_word_range(start, count, MAX_READ_WORDS, area)
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
        area = validate_word_range(start, len(values), MAX_WRITE_WORDS, area)
        words = tuple(values)
        validate_word_range(start, len(words), MAX_WRITE_WORDS, area)
        for value in words:
            validate_integer("word", value, 0, 65535)
        body = struct.pack("!BHBH", area.value, start, 0, len(words))
        body += struct.pack(f"!{len(words)}H", *words)
        self._exchange(WRITE_COMMAND, body, 0)

    def _set_remaining(self, deadline: float) -> None:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError("FINS/TCP operation deadline elapsed")
        self._socket.settimeout(remaining)

    def _recv_exact(self, size: int, deadline: float) -> bytes:
        data = bytearray()
        while len(data) < size:
            self._set_remaining(deadline)
            part = self._socket.recv(size - len(data))
            if not part:
                raise FinsTransportError("FINS TCP peer closed during a response")
            data.extend(part)
        return bytes(data)

    def _receive(self, expected_command: int, deadline: float) -> bytes:
        prefix = self._recv_exact(8, deadline)
        if prefix[:4] != b"FINS":
            raise FinsProtocolError("Invalid FINS/TCP magic")
        length = int.from_bytes(prefix[4:8], "big")
        if not 8 <= length <= MAX_TCP_LENGTH:
            raise FinsProtocolError(f"Invalid FINS/TCP length: {length}")
        command, error = struct.unpack("!II", self._recv_exact(8, deadline))
        # This narrow client accepts zero-error envelopes only. Command 3 is an
        # error notification, not an embedded PLC end code or successful ACK.
        if command != expected_command or error:
            stage = "negotiation" if expected_command == 1 else "exchange"
            raise FinsTcpEnvelopeError(command, error, stage)
        if expected_command == 1 and length != 16:
            raise FinsProtocolError("Invalid FINS/TCP negotiation length")
        if expected_command == 2 and length < 20:
            raise FinsProtocolError("Invalid FINS/TCP embedded frame length")
        return self._recv_exact(length - 8, deadline)

    def _negotiate(self, deadline: float) -> tuple[FinsAddress, FinsAddress]:
        # W421 7-4-2: length=12, command=0, error=0, client-node=0 (automatic).
        self._set_remaining(deadline)
        self._socket.sendall(b"FINS" + struct.pack("!IIII", 12, 0, 0, 0))
        client_node, server_node = struct.unpack("!II", self._receive(1, deadline))
        if not (1 <= client_node <= 254 and 1 <= server_node <= 254):
            raise FinsProtocolError("Negotiated FINS/TCP nodes must be in 1..254")
        if client_node == server_node:
            raise FinsProtocolError("Negotiated client/server nodes must differ")
        return FinsAddress(0, client_node, 0), FinsAddress(0, server_node, 0)

    def _exchange(self, command: bytes, body: bytes, expected_bytes: int) -> bytes:
        if not self._operation.acquire(blocking=False):
            raise FinsTransportError("A FINS request is already active")
        try:
            if self.closed:
                raise FinsTransportError("FINS TCP client is closed")
            self._sid = (self._sid + 1) & 0xFF
            request = b"\x80\x00\x02" + self._destination.to_bytes()
            request += self._source.to_bytes() + bytes((self._sid,)) + command + body
            deadline = monotonic() + self._timeout
            try:
                self._set_remaining(deadline)
                self._socket.sendall(
                    b"FINS" + struct.pack("!III", 8 + len(request), 2, 0) + request
                )
                return decode_response(
                    self._receive(2, deadline),
                    command,
                    expected_bytes,
                    source=self._source,
                    destination=self._destination,
                    sid=self._sid,
                )
            except TimeoutError as exc:
                self._socket.close()
                detail = (
                    "; write execution is unknown" if command == WRITE_COMMAND else ""
                )
                raise FinsTimeoutError(
                    f"FINS TCP {command.hex(' ')} to {self._peer} timed out{detail}"
                ) from exc
            except OSError as exc:
                self._socket.close()
                detail = (
                    "; write execution is unknown" if command == WRITE_COMMAND else ""
                )
                raise FinsTransportError(
                    f"FINS TCP to {self._peer}: {exc}{detail}"
                ) from exc
            except (FinsProtocolError, FinsTransportError) as exc:
                self._socket.close()
                if command == WRITE_COMMAND:
                    exc.add_note(
                        "Write execution is unknown; no retry/reconnect attempted."
                    )
                raise
            except FinsPlcError:
                raise  # A valid PLC end code is the only recoverable exchange failure.
            except BaseException:
                self._socket.close()
                raise
        finally:
            self._operation.release()
