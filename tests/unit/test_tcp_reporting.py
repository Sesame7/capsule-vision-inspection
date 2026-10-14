"""Checkpoint B: one existing reporting owner, TCP recovery, no uncertain replay."""

from collections.abc import Sequence
from contextlib import ExitStack
from threading import enumerate as threads
from threading import get_ident

import pytest
from tests.business_support import mixed_result
from tests.fins_tcp_support import Fault, Stage, TcpPeer
from tests.reporting_support import WAIT, readback, seed, server

from vimo_demo.communication import FinsMemoryArea, FinsTcpClient, FinsTransport
from vimo_demo.communication.reporting import (
    FirstStationReport,
    FirstStationReportingWorker,
    LocalReportingSettings,
    ReportingCapacityError,
    ReportingStateError,
    ReportOutcome,
    ReportStage,
)


def report(cycle: int) -> FirstStationReport:
    return FirstStationReport.from_result(7, mixed_result(cycle))


def stop(worker: FirstStationReportingWorker) -> None:
    worker.request_stop()
    assert worker.join(WAIT) and worker.cleanup_safe
    assert not any(t.name == worker.thread_name for t in threads())


@pytest.mark.parametrize("transport", list(FinsTransport))
def test_identical_ordered_bitmap_and_sequence_rollover_across_transports(
    transport: FinsTransport,
) -> None:
    with ExitStack() as stack:
        peer = (
            stack.enter_context(TcpPeer(65534))
            if transport is FinsTransport.TCP
            else stack.enter_context(server())
        )
        if not isinstance(peer, TcpPeer):
            seed(peer, 65534)
        before = len(peer.packets)
        worker = FirstStationReportingWorker(
            7, LocalReportingSettings(port=peer.port, timeout=0.2, transport=transport)
        )
        worker.start()
        try:
            for cycle, sequence in ((100, 65535), (101, 0), (102, 1)):
                outcome = worker.submit(report(cycle)).result(WAIT)
                assert outcome.outcome is ReportOutcome.ACKNOWLEDGED
                assert outcome.sequence == sequence
                assert outcome.reject_words == (0xEFBE, 0xBEFB, 15)
            packets = peer.packets[before:]
            assert [(p[10:12], int.from_bytes(p[13:15], "big")) for p in packets] == [
                (b"\x01\x01", 49),
                (b"\x01\x02", 50),
                (b"\x01\x02", 49),
                (b"\x01\x02", 50),
                (b"\x01\x02", 49),
                (b"\x01\x02", 50),
                (b"\x01\x02", 49),
            ]
            assert [p[18:] for p in packets[1::2]] == [
                bytes.fromhex("EFBE BEFB 000F")
            ] * 3
            assert [p[18:] for p in packets[2::2]] == [
                bytes.fromhex(v) for v in ("FFFF", "0000", "0001")
            ]
            assert worker.snapshot().next_sequence == 2
            if isinstance(peer, TcpPeer):
                assert peer.words(49, 4) == (1, 0xEFBE, 0xBEFB, 15)
                assert peer.connections == 1
                assert all(
                    p[3:9] == bytes.fromhex("00 14 00 00 65 00") for p in packets
                )
            else:
                assert readback(peer) == (1, 0xEFBE, 0xBEFB, 15)
        finally:
            stop(worker)


@pytest.mark.parametrize(
    ("stage", "fault"),
    [
        ("handshake", "reject"),
        ("handshake", "disconnect"),
        ("handshake", "timeout"),
        ("initialization", "plc"),
        ("initialization", "disconnect"),
        ("initialization", "timeout"),
        ("bitmap", "plc"),
        ("bitmap", "drop_before"),
        ("bitmap", "drop_after"),
        ("bitmap", "disconnect"),
        ("sequence", "plc"),
        ("sequence", "drop_before"),
        ("sequence", "drop_after"),
        ("sequence", "disconnect"),
    ],
)
def test_tcp_failed_report_is_not_retried_and_later_cycle_keeps_sequence(
    stage: Stage,
    fault: Fault,
) -> None:
    with TcpPeer(200) as peer:
        peer.fail_next(stage, fault)
        worker = FirstStationReportingWorker(
            7,
            LocalReportingSettings(
                port=peer.port, timeout=0.1, transport=FinsTransport.TCP
            ),
        )
        worker.start()
        try:
            first = worker.submit(report(1)).result(WAIT)
            initialized = stage in ("bitmap", "sequence")
            assert first.outcome is ReportOutcome.FAILED
            assert first.sequence == (201 if initialized else None)
            assert first.stage is (
                ReportStage.BITMAP
                if stage == "bitmap"
                else ReportStage.SEQUENCE
                if stage == "sequence"
                else ReportStage.INITIALIZATION
            )
            assert first.issue is not None and first.error is not None
            if initialized:
                assert first.error.__cause__ is not None
            captured = len(peer.packets)
            assert (
                captured
                == {"handshake": 0, "initialization": 1, "bitmap": 2, "sequence": 3}[
                    stage
                ]
            )
            with pytest.raises(ReportingStateError):
                worker.submit(report(1))  # No resend of an already observed identity.
            assert len(peer.packets) == captured
            second = worker.submit(report(2)).result(WAIT)
            assert second.outcome is ReportOutcome.ACKNOWLEDGED
            assert second.sequence == (202 if initialized else 201)
            reads = [p for p in peer.packets if p[10:12] == b"\x01\x01"]
            assert len(reads) == (
                1 if initialized else 2 if stage == "initialization" else 1
            )
            assert peer.connections == (1 if fault == "plc" else 2)
            snapshot = worker.snapshot()
            assert snapshot.failed_count == snapshot.acknowledged_count == 1
            assert snapshot.last_failed is first
            assert snapshot.next_sequence == (203 if initialized else 202)
        finally:
            stop(worker)


def test_tcp_owner_thread_bounded_stop_and_queued_skips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ownership: list[tuple[str, int]] = []
    clients: list[FinsTcpClient] = []

    class ObservedClient(FinsTcpClient):
        def __init__(self, host: str, *, port: int, timeout: float) -> None:
            ownership.append(("create", get_ident()))
            super().__init__(host, port=port, timeout=timeout)
            clients.append(self)

        def read_words(
            self,
            start: int,
            count: int = 1,
            *,
            area: FinsMemoryArea = FinsMemoryArea.DM,
        ) -> tuple[int, ...]:
            ownership.append(("read", get_ident()))
            return super().read_words(start, count, area=area)

        def close(self) -> None:
            ownership.append(("close", get_ident()))
            super().close()

        def write_words(
            self,
            start: int,
            values: Sequence[int],
            *,
            area: FinsMemoryArea = FinsMemoryArea.DM,
        ) -> None:
            ownership.append(("write", get_ident()))
            super().write_words(start, values, area=area)

    monkeypatch.setattr(
        "vimo_demo.communication.reporting.FinsTcpClient", ObservedClient
    )
    with TcpPeer() as peer:
        peer.hold_next("bitmap")
        worker = FirstStationReportingWorker(
            7,
            LocalReportingSettings(
                port=peer.port, timeout=1, max_pending=1, transport=FinsTransport.TCP
            ),
        )
        worker.start()
        try:
            active = worker.submit(report(1))
            assert peer.entered.wait(WAIT)
            queued = worker.submit(report(2))
            with pytest.raises(ReportingCapacityError):
                worker.submit(report(3))
            worker.request_stop()
            worker.request_stop()
            assert not worker.join(0) and not worker.cleanup_safe
            assert not active.done() and not queued.done()
            peer.release.set()
            assert active.result(WAIT).outcome is ReportOutcome.ACKNOWLEDGED
            assert queued.result(WAIT).outcome is ReportOutcome.SKIPPED
        finally:
            peer.release.set()
            stop(worker)
        assert len(peer.packets) == 3 and len(clients) == 1 and clients[0].closed
        assert len({ident for _, ident in ownership}) == 1
        assert ownership[0][1] != get_ident()
        assert [action for action, _ in ownership] == [
            "create",
            "read",
            "write",
            "write",
            "close",
        ]
        snapshot = worker.snapshot()
        assert snapshot.peak_pending == 1
        assert (
            snapshot.acknowledged_count
            == snapshot.skipped_count
            == snapshot.rejected_count
            == 1
        )
