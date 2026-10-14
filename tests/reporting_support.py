"""Bounded loopback peers shared only by first-station reporting tests."""

from collections.abc import Generator
from contextlib import contextmanager
from threading import Event, Lock, Thread
from typing import Literal

from vimo_demo.communication import FinsAddress, FinsMemoryArea, FinsUdpClient
from vimo_demo.communication.reporting import LocalReportingSettings, ReportStage
from vimo_demo.communication.simulator import FinsUdpSimulator

WAIT = 5
type FaultMode = Literal["plc", "drop_before", "drop_after"]


class ReportingSimulator(FinsUdpSimulator):
    def __init__(self) -> None:
        super().__init__(port=0)
        self._lock = Lock()
        self._packets: list[bytes] = []
        self._fault: tuple[ReportStage, FaultMode] | None = None
        self._pause: ReportStage | None = None
        self.entered, self.release = Event(), Event()

    @property
    def packets(self) -> tuple[bytes, ...]:
        with self._lock:
            return tuple(self._packets)

    def fail_next(self, stage: ReportStage, mode: FaultMode) -> None:
        with self._lock:
            self._fault = (stage, mode)

    def hold_next(self, stage: ReportStage) -> None:
        with self._lock:
            self._pause = stage

    def handle_datagram(self, request: bytes) -> bytes | None:
        stage = None
        if request[12:13] == b"\xb1":
            start = int.from_bytes(request[13:15], "big")
            if request[10:12] == b"\x01\x01" and start == 49:
                stage = ReportStage.INITIALIZATION
            elif request[10:12] == b"\x01\x02":
                stage = ReportStage.BITMAP if start == 50 else ReportStage.SEQUENCE
        with self._lock:
            self._packets.append(request)
            fault = self._fault if self._fault and self._fault[0] is stage else None
            if fault:
                self._fault = None
            pause = self._pause is not None and self._pause is stage
            if pause:
                self._pause = None
        if pause:
            self.entered.set()
            assert self.release.wait(WAIT)
        if fault and fault[1] == "drop_before":
            return None
        if fault and fault[1] == "plc":
            return (
                b"\xc0\x00\x02"
                + request[6:9]
                + request[3:6]
                + request[9:12]
                + b"\x21\x01"
            )
        response = super().handle_datagram(request)
        return None if fault and fault[1] == "drop_after" else response


@contextmanager
def server() -> Generator[ReportingSimulator]:
    peer = ReportingSimulator()
    stop = Event()
    failures: list[BaseException] = []
    with peer:

        def serve() -> None:
            try:
                peer.serve(stop)
            except BaseException as exc:
                failures.append(exc)

        thread = Thread(target=serve, name="reporting-test-simulator")
        thread.start()
        try:
            yield peer
        finally:
            peer.release.set()
            stop.set()
            thread.join(WAIT)
            assert not thread.is_alive() and not failures, failures
    assert peer.closed


def settings(
    peer: ReportingSimulator, *, max_pending: int = 8, timeout: float = 0.1
) -> LocalReportingSettings:
    return LocalReportingSettings(
        port=peer.port, timeout=timeout, max_pending=max_pending
    )


@contextmanager
def client(peer: ReportingSimulator) -> Generator[FinsUdpClient]:
    with FinsUdpClient(
        "127.0.0.1",
        port=peer.port,
        destination=FinsAddress(0, 2, 0),
        source=FinsAddress(0, 1, 0),
        timeout=1,
    ) as connection:
        yield connection


def seed(peer: ReportingSimulator, sequence: int) -> None:
    with client(peer) as connection:
        connection.write_words(49, (sequence,), area=FinsMemoryArea.W)


def readback(peer: ReportingSimulator) -> tuple[int, ...]:
    with client(peer) as connection:
        return connection.read_words(49, 4, area=FinsMemoryArea.W)
