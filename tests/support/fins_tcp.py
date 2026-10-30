"""Independent bounded serial loopback peer for manual/reporting integration.

No production codec, Qt or real equipment. Not a persistent simulator/CLI.
"""

import socket
import struct
from threading import Event, Lock, Thread
from typing import Literal

type Stage = Literal["handshake", "initialization", "read", "bitmap", "sequence"]
type Fault = Literal[
    "reject", "disconnect", "timeout", "plc", "drop_before", "drop_after"
]


class TcpPeer:
    def __init__(self, sequence: int = 0, *, base_address: int = 49) -> None:
        self.base_address = base_address
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self.port: int = self._listener.getsockname()[1]
        self._listener.listen(4)
        self._listener.settimeout(0.05)
        self._stop = Event()
        self._lock = Lock()
        self._packets: list[bytes] = []
        self._memory = {(0x82, 100): 1234, (0xB1, base_address): sequence}
        self._fault: tuple[Stage, Fault] | None = None
        self._hold: Stage | None = None
        self.entered, self.release = Event(), Event()
        self.errors: list[BaseException] = []
        self.connections = 0
        self._thread = Thread(target=self._serve, name="FinsTcpIntegrationPeer")

    @property
    def packets(self) -> tuple[bytes, ...]:
        with self._lock:
            return tuple(self._packets)

    def words(self, start: int, count: int = 1, area: int = 0xB1) -> tuple[int, ...]:
        with self._lock:
            return tuple(self._memory.get((area, start + i), 0) for i in range(count))

    def fail_next(self, stage: Stage, fault: Fault) -> None:
        with self._lock:
            self._fault = stage, fault

    def hold_next(self, stage: Stage) -> None:
        self.entered.clear()
        self.release.clear()
        with self._lock:
            self._hold = stage

    def _read(self, connection: socket.socket, count: int) -> bytes | None:
        data = bytearray()
        while len(data) < count and not self._stop.is_set():
            try:
                part = connection.recv(count - len(data))
            except TimeoutError:
                continue
            if not part:
                return None
            data.extend(part)
        return bytes(data) if len(data) == count else None

    def _receive(self, connection: socket.socket) -> bytes | None:
        prefix = self._read(connection, 8)
        if prefix is None:
            return None
        assert prefix[:4] == b"FINS"
        length = int.from_bytes(prefix[4:], "big")
        assert 8 <= length <= 2020
        return self._read(connection, length)

    def _control(self, stage: Stage) -> Fault | None:
        with self._lock:
            hold = self._hold == stage
            if hold:
                self._hold = None
            fault = self._fault[1] if self._fault and self._fault[0] == stage else None
            if fault:
                self._fault = None
        if hold:
            self.entered.set()
            assert self.release.wait(5), "test peer was not released"
        return fault

    def _respond(self, connection: socket.socket) -> None:
        node_request = self._receive(connection)
        if node_request is None:
            return
        assert node_request == bytes.fromhex("00000000 00000000 00000000")
        fault = self._control("handshake")
        if fault == "disconnect":
            return
        if fault == "timeout":
            self._read(connection, 1)  # Wait for client deadline/EOF, no retry.
            return
        connection.sendall(
            bytes.fromhex("46494E53 00000010 00000001")
            + struct.pack("!III", 0x20 if fault == "reject" else 0, 101, 20)
        )
        if fault == "reject":
            return
        while not self._stop.is_set():
            envelope = self._receive(connection)
            if envelope is None:
                return
            assert envelope[:8] == bytes.fromhex("00000002 00000000")
            packet = envelope[8:]
            assert packet[:9] == bytes.fromhex("80 00 02 00 14 00 00 65 00")
            command = packet[10:12]
            area, start, bit, count = struct.unpack("!BHBH", packet[12:18])
            assert command in (b"\x01\x01", b"\x01\x02")
            assert area in (0x82, 0xB1) and bit == 0 and count > 0
            assert start + count <= (32768 if area == 0x82 else 512)
            reading = command == b"\x01\x01"
            stage: Stage = (
                "initialization"
                if reading and area == 0xB1 and start == self.base_address
                else "read"
                if reading
                else "bitmap"
                if area == 0xB1 and start == self.base_address + 1
                else "sequence"
            )
            with self._lock:
                assert len(self._packets) < 512, "test capture must stay bounded"
                self._packets.append(packet)
            fault = self._control(stage)
            if fault == "disconnect":
                return
            if fault in ("timeout", "drop_before"):
                self._read(connection, 1)
                return
            data = b""
            if fault != "plc":
                if reading:
                    assert len(packet) == 18
                    data = struct.pack(f"!{count}H", *self.words(start, count, area))
                else:
                    assert len(packet) == 18 + count * 2
                    values = struct.unpack(f"!{count}H", packet[18:])
                    with self._lock:
                        for offset, value in enumerate(values):
                            self._memory[area, start + offset] = value
            if fault == "drop_after":
                self._read(connection, 1)
                return
            response = b"\xc0\x00\x02" + packet[6:9] + packet[3:6] + packet[9:12]
            response += b"\x21\x01" if fault == "plc" else b"\x00\x00" + data
            connection.sendall(
                b"FINS" + struct.pack("!III", 8 + len(response), 2, 0) + response
            )

    def _serve(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    connection, _ = self._listener.accept()
                except TimeoutError:
                    continue
                self.connections += 1
                with connection:
                    connection.settimeout(0.05)
                    self._respond(connection)
        except BaseException as exc:
            self.errors.append(exc)

    def __enter__(self) -> TcpPeer:
        self._thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.release.set()
        self._stop.set()
        self._thread.join(3)
        self._listener.close()
        assert not self._thread.is_alive(), "TCP peer thread did not exit"
        assert not self.errors, self.errors
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as replacement:
            replacement.bind(("127.0.0.1", self.port))
