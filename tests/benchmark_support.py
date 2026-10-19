"""Finite bit-command peer built on the existing loopback TCP test owner."""

import socket
import struct
from threading import Event

from tests.fins_tcp_support import TcpPeer


class BitPeer(TcpPeer):
    def __enter__(self) -> BitPeer:
        super().__enter__()
        return self

    def __init__(self) -> None:
        super().__init__()
        self.high = Event()
        self.release_seen = Event()
        self.allow_activation_ack = Event()
        self.allow_activation_ack.set()
        self.allow_release_ack = Event()
        self.allow_release_ack.set()
        self.bit = 0
        self.w10_other_bits = 0x5521
        self.writes: list[int] = []
        self.drop_activation_ack = False
        self.reject_release = False
        self.bad_readback = False

    def _respond(self, connection: socket.socket) -> None:
        request = self._receive(connection)
        if request is None:
            return
        assert request == bytes.fromhex("00000000 00000000 00000000")
        connection.sendall(
            bytes.fromhex("46494E53 00000010 00000001 00000000 00000065 00000014")
        )
        while not self._stop.is_set():
            envelope = self._receive(connection)
            if envelope is None:
                return
            assert envelope[:8] == bytes.fromhex("00000002 00000000")
            packet = envelope[8:]
            assert packet[:9] == bytes.fromhex("80 00 02 00 14 00 00 65 00")
            assert packet[12:18] == bytes.fromhex("31 00 0a 07 00 01")
            assert len(self.packets) < 100
            with self._lock:
                self._packets.append(packet)
            command = packet[10:12]
            end_code, data = b"\x00\x00", b""
            if command == b"\x01\x01":
                assert len(packet) == 18
                data = bytes((2 if self.bad_readback else self.bit,))
            else:
                assert command == b"\x01\x02" and len(packet) == 19
                value = packet[18]
                assert value in (0, 1)
                self.writes.append(value)
                if value:
                    self.bit = 1
                    self.high.set()
                    assert self.allow_activation_ack.wait(5)
                    if self.drop_activation_ack:
                        self.drop_activation_ack = False
                        return  # Accepted write, lost ACK; no repeat activation.
                else:
                    self.release_seen.set()
                    assert self.allow_release_ack.wait(5)
                    if self.reject_release:
                        end_code = b"\x21\x01"
                    else:
                        self.bit = 0
            response = (
                b"\xc0\x00\x02"
                + packet[6:9]
                + packet[3:6]
                + packet[9:12]
                + end_code
                + data
            )
            try:
                connection.sendall(
                    b"FINS" + struct.pack("!III", 8 + len(response), 2, 0) + response
                )
            except ConnectionResetError, BrokenPipeError, ConnectionAbortedError:
                return  # The controlled controller may be terminated during a request.
