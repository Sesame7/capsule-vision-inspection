"""Bounded localhost peer for real Modbus TCP client/Qt diagnostic tests."""

import socket
import struct
from collections.abc import Callable, Sequence
from threading import Event, Thread
from types import TracebackType


def frame(
    pdu: bytes,
    *,
    transaction: int = 1,
    protocol: int = 0,
    unit: int = 255,
    length: int | None = None,
) -> bytes:
    return (
        struct.pack(
            "!HHHB",
            transaction,
            protocol,
            len(pdu) + 1 if length is None else length,
            unit,
        )
        + pdu
    )


class ModbusPeer:
    def __init__(
        self, respond: Callable[[bytes], Sequence[bytes] | None] | None = None
    ) -> None:
        self.respond = respond or self.normal_response
        self.requests: list[bytes] = []
        self.words: dict[int, int] = {100: 1234, 101: 65535}
        self.accepted = 0
        self.disconnected = 0
        self.received = Event()
        self.release = Event()
        self.stop = Event()
        self.errors: list[BaseException] = []
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.port = int(self.listener.getsockname()[1])
        self.listener.listen()
        self.listener.settimeout(0.1)
        self.owner = Thread(target=self.serve, name="test-modbus-peer")

    def normal_response(self, request: bytes) -> Sequence[bytes]:
        transaction, _, _, unit = struct.unpack("!HHHB", request[:7])
        function, address, value = struct.unpack("!BHH", request[7:12])
        if function == 3:
            pdu = bytes((3, value * 2)) + struct.pack(
                f"!{value}H", *(self.words.get(address + i, 0) for i in range(value))
            )
        elif function == 6:
            self.words[address] = value
            pdu = request[7:12]
        else:
            assert function == 16 and request[12] == value * 2
            words = struct.unpack(f"!{value}H", request[13:])
            self.words.update((address + i, word) for i, word in enumerate(words))
            pdu = request[7:12]
        return (frame(pdu, transaction=transaction, unit=unit),)

    @staticmethod
    def receive(connection: socket.socket, size: int) -> bytes:
        data = bytearray()
        while len(data) < size:
            part = connection.recv(size - len(data))
            if not part:
                if not data:
                    return b""
                raise AssertionError("Incomplete request from client")
            data.extend(part)
        return bytes(data)

    def serve(self) -> None:
        try:
            while not self.stop.is_set():
                try:
                    connection, _ = self.listener.accept()
                except TimeoutError:
                    continue
                self.accepted += 1
                with connection:
                    connection.settimeout(2)
                    while not self.stop.is_set():
                        try:
                            header = self.receive(connection, 7)
                        except ConnectionResetError:
                            self.disconnected += 1
                            break
                        if not header:
                            self.disconnected += 1
                            break
                        length = int.from_bytes(header[4:6], "big")
                        request = header + self.receive(connection, length - 1)
                        self.requests.append(request)
                        self.received.set()
                        response = self.respond(request)
                        if response is None:
                            assert self.release.wait(2), "Test must release held peer"
                            break
                        for part in response:
                            connection.sendall(part)
                        if not response:
                            break
        except BaseException as exc:
            self.errors.append(exc)

    def __enter__(self) -> ModbusPeer:
        self.owner.start()
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.stop.set()
        self.release.set()
        self.owner.join(3)
        self.listener.close()
        assert not self.owner.is_alive()
        assert not self.errors, self.errors
