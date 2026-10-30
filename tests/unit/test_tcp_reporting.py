"""Checkpoint B: one existing reporting owner, TCP recovery, no uncertain replay."""

from collections.abc import Sequence
from contextlib import ExitStack
from dataclasses import replace
from threading import enumerate as threads
from threading import get_ident

import pytest
from tests.support.business import mixed_result
from tests.support.fins_tcp import Fault, Stage, TcpPeer
from tests.support.reporting import WAIT, readback, seed, server

from capsule_vision.communication import (
    FinsMemoryArea,
    FinsTcpClient,
    FinsTcpEndpoint,
    FinsUdpEndpoint,
)
from capsule_vision.communication.reporting import (
    FirstStationReport,
    FirstStationReportingWorker,
    ReportingCapacityError,
    ReportingSettings,
    ReportingStateError,
    ReportOutcome,
    ReportStage,
)


def report(cycle: int, camera_count: int = 6) -> FirstStationReport:
    result = mixed_result(cycle)
    cameras = result.cameras[:camera_count]
    return FirstStationReport.from_result(
        7,
        replace(
            result,
            layout=replace(result.layout, cameras=tuple(c.layout for c in cameras)),
            cameras=cameras,
        ),
    )


def stop(worker: FirstStationReportingWorker) -> None:
    worker.request_stop()
    assert worker.join(WAIT) and worker.cleanup_safe
    assert not any(t.name == worker.thread_name for t in threads())


@pytest.mark.parametrize("base", [120, 508])
@pytest.mark.parametrize("camera_count", [5, 6])
def test_tcp_custom_base_preserves_bitmap_commit_and_negotiation(
    base: int, camera_count: int
) -> None:
    with TcpPeer(65534, base_address=base) as peer:
        with FinsTcpClient("127.0.0.1", port=peer.port) as client:
            client.write_words(base + 3, (0xABCD,), area=FinsMemoryArea.W)
        before = len(peer.packets)
        worker = FirstStationReportingWorker(
            7,
            ReportingSettings(
                base_address=base, endpoint=FinsTcpEndpoint(port=peer.port)
            ),
        )
        worker.start()
        try:
            for cycle, sequence in ((1, 65535), (2, 0)):
                outcome = worker.submit(report(cycle, camera_count)).result(WAIT)
                assert (
                    outcome.outcome is ReportOutcome.ACKNOWLEDGED
                    and outcome.sequence == sequence
                )
                expected = (
                    (0xEFBE, 0x3EFB, 0xABCD)
                    if camera_count == 5
                    else (0xEFBE, 0xBEFB, 15)
                )
                assert peer.words(base, 4) == (sequence, *expected)
            packets = peer.packets[before:]
            assert (
                packets[0][10:12] == b"\x01\x01"
                and int.from_bytes(packets[0][13:15], "big") == base
            )
            writes = [p for p in packets if p[10:12] == b"\x01\x02"]
            assert [
                (int.from_bytes(p[13:15], "big"), int.from_bytes(p[16:18], "big"))
                for p in writes
            ] == [(base + 1, 2 if camera_count == 5 else 3), (base, 1)] * 2
        finally:
            stop(worker)


@pytest.mark.parametrize("endpoint_type", [FinsUdpEndpoint, FinsTcpEndpoint])
@pytest.mark.parametrize("camera_count", [5, 6])
def test_identical_ordered_bitmap_and_sequence_rollover_across_transports(
    endpoint_type: type[FinsUdpEndpoint] | type[FinsTcpEndpoint],
    camera_count: int,
) -> None:
    with ExitStack() as stack:
        peer = (
            stack.enter_context(TcpPeer(65534))
            if endpoint_type is FinsTcpEndpoint
            else stack.enter_context(server())
        )
        if not isinstance(peer, TcpPeer):
            seed(peer, 65534)
        before = len(peer.packets)
        worker = FirstStationReportingWorker(
            7,
            ReportingSettings(endpoint=endpoint_type(port=peer.port, timeout=0.2)),
        )
        worker.start()
        try:
            for cycle, sequence in ((100, 65535), (101, 0), (102, 1)):
                result = mixed_result(cycle)
                cameras = result.cameras[:camera_count]
                result = replace(
                    result,
                    layout=replace(
                        result.layout, cameras=tuple(c.layout for c in cameras)
                    ),
                    cameras=cameras,
                )
                outcome = worker.submit(
                    FirstStationReport.from_result(7, result)
                ).result(WAIT)
                assert outcome.outcome is ReportOutcome.ACKNOWLEDGED
                assert outcome.sequence == sequence
                words = (0xEFBE, 0x3EFB) if camera_count == 5 else (0xEFBE, 0xBEFB, 15)
                assert outcome.reject_words == words
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
                bytes.fromhex("EFBE 3EFB" if camera_count == 5 else "EFBE BEFB 000F")
            ] * 3
            assert all(
                int.from_bytes(p[16:18], "big") == len(words) for p in packets[1::2]
            )
            assert [p[18:] for p in packets[2::2]] == [
                bytes.fromhex(v) for v in ("FFFF", "0000", "0001")
            ]
            assert worker.snapshot().next_sequence == 2
            if isinstance(peer, TcpPeer):
                assert peer.words(49, 4) == (
                    1,
                    *words,
                    *((0,) if camera_count == 5 else ()),
                )
                assert peer.connections == 1
                assert all(
                    p[3:9] == bytes.fromhex("00 14 00 00 65 00") for p in packets
                )
            else:
                assert readback(peer) == (
                    1,
                    *words,
                    *((0,) if camera_count == 5 else ()),
                )
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
@pytest.mark.parametrize("camera_count", [5, 6])
def test_tcp_failed_report_is_not_retried_and_later_cycle_keeps_sequence(
    stage: Stage,
    fault: Fault,
    camera_count: int,
) -> None:
    with TcpPeer(200) as peer:
        peer.fail_next(stage, fault)
        worker = FirstStationReportingWorker(
            7,
            ReportingSettings(endpoint=FinsTcpEndpoint(port=peer.port, timeout=0.1)),
        )
        worker.start()
        try:
            first = worker.submit(report(1, camera_count)).result(WAIT)
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
                worker.submit(
                    report(1, camera_count)
                )  # No resend of an already observed identity.
            assert len(peer.packets) == captured
            second = worker.submit(report(2, camera_count)).result(WAIT)
            assert second.outcome is ReportOutcome.ACKNOWLEDGED
            assert second.reject_words == (
                (0xEFBE, 0x3EFB) if camera_count == 5 else (0xEFBE, 0xBEFB, 15)
            )
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
        "capsule_vision.communication.endpoints.FinsTcpClient", ObservedClient
    )
    with TcpPeer() as peer:
        peer.hold_next("bitmap")
        worker = FirstStationReportingWorker(
            7,
            ReportingSettings(
                max_pending=1, endpoint=FinsTcpEndpoint(port=peer.port, timeout=1)
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


def test_stop_with_drain_reports_accepted_fifo_before_socket_close() -> None:
    with TcpPeer() as peer:
        peer.hold_next("bitmap")
        worker = FirstStationReportingWorker(
            7,
            ReportingSettings(endpoint=FinsTcpEndpoint(port=peer.port, timeout=2)),
        )
        worker.start()
        try:
            first = worker.submit(report(1))
            assert peer.entered.wait(WAIT)
            second = worker.submit(report(2))
            worker.request_stop(drain=True)
            assert not worker.join(0)
            peer.release.set()
            assert (
                first.result(WAIT).outcome
                is second.result(WAIT).outcome
                is ReportOutcome.ACKNOWLEDGED
            )
            assert worker.join(WAIT) and worker.cleanup_safe
            assert worker.snapshot().acknowledged_count == 2
            assert peer.connections == 1 and peer.words(49)[0] == 2
        finally:
            peer.release.set()
            stop(worker)


def test_attribution_loss_after_bitmap_ack_never_writes_sequence() -> None:
    from threading import Event

    allowed = Event()
    allowed.set()
    with TcpPeer(20) as peer:
        peer.hold_next("bitmap")
        worker = FirstStationReportingWorker(
            7,
            ReportingSettings(endpoint=FinsTcpEndpoint(port=peer.port, timeout=2)),
            attributable=lambda _: allowed.is_set(),
        )
        worker.start()
        try:
            future = worker.submit(report(1))
            assert peer.entered.wait(WAIT)
            allowed.clear()
            peer.release.set()
            outcome = future.result(WAIT)
            assert (
                outcome.outcome is ReportOutcome.FAILED
                and outcome.stage is ReportStage.SEQUENCE
            )
            assert outcome.acknowledged_at is None and peer.words(49)[0] == 20
            assert len(peer.packets) == 2  # Initial read + bitmap, no W49 write.
            second = worker.submit(report(2)).result(WAIT)
            assert second.outcome is ReportOutcome.FAILED and len(peer.packets) == 2
        finally:
            peer.release.set()
            stop(worker)
