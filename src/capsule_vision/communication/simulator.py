"""Finite localhost-only FINS DM/WR-word test double, not an NX102 emulator."""

import socket
import struct
from collections.abc import Callable
from threading import Event
from types import TracebackType

from .fins import (
    DEFAULT_PORT,
    DM_WORD_AREA,
    DM_WORDS,
    MAX_DATAGRAM,
    MAX_READ_WORDS,
    MAX_WRITE_WORDS,
    READ_COMMAND,
    W_WORD_AREA,
    W_WORDS,
    WRITE_COMMAND,
    FinsAddress,
    validate_address,
    validate_integer,
)

_LOCAL_ADDRESS = FinsAddress(0, 2, 0)


class FinsUdpSimulator:
    """One serial serve-loop owner; the supplied Event requests graceful stop.

    Bind is always loopback. Port 0 allocates a test port. An idle stop is observed
    within the 100-ms receive timeout; join the serve thread before close/release.
    No socket/thread/global histories are hidden inside this test double.
    """

    def __init__(
        self,
        *,
        port: int = DEFAULT_PORT,
        address: FinsAddress = _LOCAL_ADDRESS,
        on_first_station_write: Callable[[int, tuple[int, int, int]], None]
        | None = None,
    ) -> None:
        validate_integer("port", port, 0, 65535)
        self._address = validate_address(address)
        self._words = [0] * DM_WORDS
        self._words[100] = 1234
        self._w_words = [0] * W_WORDS
        self._on_first_station_write = on_first_station_write
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self._socket.bind(("127.0.0.1", port))
            self._socket.settimeout(0.1)
        except BaseException:
            self._socket.close()
            raise
        self.port: int = self._socket.getsockname()[1]

    @property
    def closed(self) -> bool:
        return self._socket.fileno() == -1

    def __enter__(self) -> FinsUdpSimulator:
        if self.closed:
            raise RuntimeError("Simulator is closed")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Idempotent; call on the owner after the serve loop has returned."""
        self._socket.close()

    def serve(self, stop: Event, *, max_requests: int | None = None) -> None:
        """Run until stop; optional finite datagram count supports CLI smoke checks."""
        if max_requests is not None:
            validate_integer("max_requests", max_requests, 1, 2**31 - 1)
        received = 0
        while not stop.is_set() and (max_requests is None or received < max_requests):
            try:
                request, sender = self._socket.recvfrom(65535)
            except TimeoutError:
                continue
            received += 1
            response = self.handle_datagram(request)
            if response is not None:
                self._socket.sendto(response, sender)

    def handle_datagram(self, request: bytes) -> bytes | None:
        """Validate fully before mutation. Unaddressable/invalid headers are ignored."""
        if len(request) < 12 or request[:2] != b"\x80\x00" or request[2] > 2:
            return None
        if request[3:6] != self._address.to_bytes():
            return None
        try:
            FinsAddress(*request[6:9])
        except ValueError:
            return None
        # Reply routes reverse the request, preserving SID and command.
        header = b"\xc0\x00\x02" + request[6:9] + request[3:6] + request[9:12]
        end_code, data = self._execute(request)
        return header + struct.pack("!H", end_code) + data

    def _execute(self, request: bytes) -> tuple[int, bytes]:
        if len(request) > MAX_DATAGRAM:
            return 0x1001, b""  # Command too long.
        command = request[10:12]
        if command not in (READ_COMMAND, WRITE_COMMAND):
            return 0x0401, b""  # Undefined/unsupported command.
        if len(request) < 18:
            return 0x1002, b""  # Command too short.
        area, start, bit, count = struct.unpack("!BHBH", request[12:18])
        if area == DM_WORD_AREA:
            words = self._words
        elif area == W_WORD_AREA:
            words = self._w_words
        else:
            return 0x1101, b""  # Area classification missing in this subset.
        if bit != 0:
            return 0x1103, b""  # Not a valid word starting address.
        if start >= len(words):
            return 0x1103, b""
        if start + count > len(words):
            return 0x1104, b""  # End address exceeds accessible area.
        if command == READ_COMMAND:
            if len(request) != 18:
                return 0x1001, b""
            if count > MAX_READ_WORDS:
                return 0x110B, b""  # Response too long.
            return 0, struct.pack(f"!{count}H", *words[start : start + count])
        if len(request) != 18 + count * 2:
            return 0x1003, b""  # Elements and write data do not match.
        if count > MAX_WRITE_WORDS:
            return 0x1001, b""
        # W342 allows count=0 as a no-op; the client API requires at least one.
        values = struct.unpack(f"!{count}H", request[18:])
        words[start : start + count] = values
        if (
            area == W_WORD_AREA
            and start <= 49 < start + count
            and self._on_first_station_write is not None
        ):
            try:
                self._on_first_station_write(
                    words[49], (words[50], words[51], words[52])
                )
            except Exception:
                # Diagnostics must not turn an accepted write into a missing ACK.
                pass
        return 0, b""
