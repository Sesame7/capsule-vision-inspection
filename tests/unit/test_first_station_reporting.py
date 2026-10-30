"""Scalar handoff, serial sequence ownership, UDP recovery and bounded shutdown."""

from dataclasses import replace
from threading import enumerate as threads

import pytest
from tests.support.application import production
from tests.support.business import channel, mixed_result
from tests.support.reporting import WAIT, FaultMode, readback, seed, server, settings
from tests.support.reporting import client as diagnostic_client

from capsule_vision.communication import (
    Endpoint,
    FinsAddress,
    FinsMemoryArea,
    FinsPlcError,
    FinsTcpEndpoint,
    FinsTimeoutError,
    FinsUdpClient,
    FinsUdpEndpoint,
    FirstStationSubmissionError,
)
from capsule_vision.communication.reporting import (
    FirstStationReport,
    FirstStationReportingWorker,
    ReportIdentity,
    ReportingCapacityError,
    ReportingSettings,
    ReportingStateError,
    ReportOutcome,
    ReportStage,
)
from capsule_vision.inference import BoundingRect
from capsule_vision.inspection import CycleId, CycleStatus
from capsule_vision.inspection.production import (
    OccupancyState,
    ProductJudgment,
    TechnicalStatus,
    ViMoFinding,
)


def report(cycle: int, generation: int = 7) -> FirstStationReport:
    return FirstStationReport.from_result(generation, mixed_result(cycle))


@pytest.mark.parametrize("base", [0, 120, 508])
@pytest.mark.parametrize("channel_count", [30, 36])
def test_custom_udp_reporting_window_and_sequence_read(
    base: int, channel_count: int
) -> None:
    with server(base) as peer:
        seed(peer, 65534)
        with diagnostic_client(peer) as client:
            client.write_words(base + 3, (0xABCD,), area=FinsMemoryArea.W)
        captured = len(peer.packets)
        worker = FirstStationReportingWorker(7, settings(peer))
        worker.start()
        try:
            for cycle, sequence in ((1, 65535), (2, 0)):
                item = report(cycle)
                item = replace(item, statuses=item.statuses[:channel_count])
                outcome = worker.submit(item).result(WAIT)
                assert outcome.outcome is ReportOutcome.ACKNOWLEDGED
                assert outcome.sequence == sequence
                expected = (
                    (0xEFBE, 0x3EFB) if channel_count == 30 else (0xEFBE, 0xBEFB, 15)
                )
                assert (
                    readback(peer) == (sequence, *expected, 0xABCD)
                    if channel_count == 30
                    else readback(peer) == (sequence, *expected)
                )
            packets = peer.packets[captured:]
            writes = [p for p in packets if p[10:12] == b"\x01\x02"]
            assert [
                (int.from_bytes(p[13:15], "big"), int.from_bytes(p[16:18], "big"))
                for p in writes
            ] == [(base + 1, 2 if channel_count == 30 else 3), (base, 1)] * 2
            # Readback queries have count four; the initialization read is one word.
            reads = [
                p
                for p in packets
                if p[10:12] == b"\x01\x01" and p[16:18] == b"\x00\x01"
            ]
            assert len(reads) == 1 and int.from_bytes(reads[0][13:15], "big") == base
        finally:
            worker.request_stop()
            assert worker.join(WAIT) and worker.cleanup_safe


@pytest.mark.parametrize("base", [-1, 509, True])
def test_invalid_reporting_window_rejected_before_client_creation(base: int) -> None:
    with pytest.raises(ValueError):
        ReportingSettings(base_address=base, endpoint=FinsUdpEndpoint())


@pytest.mark.parametrize("status", [CycleStatus.COMPLETE, CycleStatus.FAILED])
def test_complete_collection_projects_scalar_business_states(
    status: CycleStatus,
) -> None:
    result = replace(mixed_result(100), cycle_status=status)
    data = FirstStationReport.from_result(7, result)
    assert data.identity == ReportIdentity(7, CycleId(100))
    assert data.reject_words == (0xEFBE, 0xBEFB, 15)
    assert len(data.statuses) == 36
    assert all(type(channel) is int for channel, _ in data.statuses)


@pytest.mark.parametrize("status", [CycleStatus.TIMED_OUT, CycleStatus.SHUTDOWN])
def test_partial_outcomes_preserve_completed_and_reject_missing(
    status: CycleStatus,
) -> None:
    result = production()
    missing = replace(result.cameras[0], frame_id=None, image_shape=None)
    partial = replace(
        result, cycle_status=status, cameras=(missing, *result.cameras[1:])
    )
    data = FirstStationReport.from_result(1, partial)
    expected = FirstStationReport.from_result(1, result).reject_words
    assert len(expected) == 3
    assert data.reject_words == (expected[0] | 0x3F, expected[1], expected[2])
    assert len(data.statuses) == 36 and data.result_ready_at == partial.result_ready_at


def test_unobserved_cycle_and_invalid_projection_are_not_reports() -> None:
    result = production()
    cameras = tuple(replace(c, frame_id=None, image_shape=None) for c in result.cameras)
    with pytest.raises(ValueError, match="observed cycle"):
        FirstStationReport.from_result(1, replace(result, cameras=cameras))
    data = report(1)
    for statuses in (data.statuses[:-1], data.statuses + data.statuses[:1]):
        with pytest.raises(ValueError):
            FirstStationReport(data.identity, statuses)


def test_unassigned_ng_does_not_spread_to_channel_bits() -> None:
    result = mixed_result()
    result = replace(
        result,
        cameras=tuple(
            replace(
                camera,
                channels=tuple(
                    channel(
                        row,
                        OccupancyState.PRESENT,
                        TechnicalStatus.SUCCESS,
                        ProductJudgment.OK,
                    )
                    for row in camera.channels
                ),
            )
            for camera in result.cameras
        ),
    )
    assert result.assessment.judgment is ProductJudgment.OK
    camera = result.cameras[0]
    assert camera.frame_id is not None
    finding = ViMoFinding(
        camera.frame_id, 0, "unassigned", 4, BoundingRect(58, 15, 2, 2), "fake"
    )
    changed = replace(
        result,
        cameras=(replace(camera, unassigned_findings=(finding,)), *result.cameras[1:]),
    )
    assert changed.assessment.judgment is ProductJudgment.NG
    assert finding not in tuple(
        assignment.finding
        for row in changed.channels
        for assignment in row.vimo_findings
    )
    assert (
        FirstStationReport.from_result(1, changed).reject_words
        == FirstStationReport.from_result(1, result).reject_words
        == (0, 0, 0)
    )


@pytest.mark.parametrize("host", ["192.0.2.1", "127.0.0.2", "localhost"])
@pytest.mark.parametrize("endpoint_type", [FinsUdpEndpoint, FinsTcpEndpoint])
def test_reporting_accepts_numeric_remote_endpoints(
    host: str, endpoint_type: type[FinsUdpEndpoint] | type[FinsTcpEndpoint]
) -> None:
    if host == "localhost":
        with pytest.raises(ValueError):
            ReportingSettings(endpoint=endpoint_type(host=host))
    else:
        assert (
            ReportingSettings(endpoint=endpoint_type(host=host)).endpoint.host == host
        )


@pytest.mark.parametrize("endpoint_type", [FinsUdpEndpoint, FinsTcpEndpoint])
def test_remote_reporting_forwards_endpoint_without_substitution(
    endpoint_type: type[FinsUdpEndpoint] | type[FinsTcpEndpoint],
) -> None:
    with server() as peer:
        selected = ReportingSettings(endpoint=endpoint_type(host="192.0.2.1"))
        received: list[Endpoint] = []

        def factory(settings: Endpoint) -> FinsUdpClient:
            received.append(settings)
            return FinsUdpClient(
                "127.0.0.1",
                port=peer.port,
                timeout=0.2,
                destination=FinsAddress(0, 2, 0),
                source=FinsAddress(0, 1, 0),
            )

        worker = FirstStationReportingWorker(7, selected, client_factory=factory)
        worker.start()
        try:
            outcome = worker.submit(report(100)).result(WAIT)
            assert outcome.outcome is ReportOutcome.ACKNOWLEDGED
            assert received == [selected.endpoint] and received[0].host == "192.0.2.1"
            assert outcome.bitmap_ms is not None and outcome.bitmap_ms >= 0
            assert outcome.sequence_ms is not None and outcome.sequence_ms >= 0
            assert outcome.acknowledged_at is not None
        finally:
            worker.request_stop()
            assert worker.join(WAIT) and worker.cleanup_safe


def test_initial_read_sequence_progression_rollover_and_identical_bitmap() -> None:
    with server() as peer:
        seed(peer, 65534)
        before = len(peer.packets)
        worker = FirstStationReportingWorker(7, settings(peer))
        worker.start()
        try:
            for cycle, sequence in zip((100, 101, 102), (65535, 0, 1), strict=True):
                outcome = worker.submit(report(cycle)).result(WAIT)
                assert outcome.identity == ReportIdentity(7, CycleId(cycle))
                assert outcome.outcome is ReportOutcome.ACKNOWLEDGED
                assert outcome.sequence == sequence
                assert outcome.duration_ms is not None and outcome.duration_ms >= 0
                assert readback(peer) == (sequence, 0xEFBE, 0xBEFB, 15)
            snapshot = worker.snapshot()
            assert snapshot.acknowledged_count == snapshot.attempted_count == 3
            assert snapshot.failed_count == 0 and snapshot.next_sequence == 2
            assert (
                snapshot.last_attempted is not None
                and snapshot.last_attempted.identity.cycle_id == 102
            )
            assert (
                snapshot.last_acknowledged is not None
                and snapshot.last_acknowledged.identity.cycle_id == 102
            )
            packets = peer.packets[before:]
            assert (
                sum(
                    p[10:12] == b"\x01\x01" and p[16:18] == b"\x00\x01" for p in packets
                )
                == 1
            )
            writes = [p for p in packets if p[10:12] == b"\x01\x02"]
            assert [int.from_bytes(p[13:15], "big") for p in writes] == [50, 49] * 3
            assert all(p[12] == 0xB1 for p in writes)
        finally:
            worker.request_stop()
            assert worker.join(WAIT) and worker.cleanup_safe


@pytest.mark.parametrize(
    "stage", [ReportStage.INITIALIZATION, ReportStage.BITMAP, ReportStage.SEQUENCE]
)
@pytest.mark.parametrize("mode", ["plc", "drop_before", "drop_after"])
def test_failure_stage_original_cause_and_later_cycle_recovery(
    stage: ReportStage, mode: FaultMode
) -> None:
    with server() as peer:
        seed(peer, 10)
        before = len(peer.packets)
        peer.fail_next(stage, mode)
        worker = FirstStationReportingWorker(7, settings(peer))
        worker.start()
        try:
            failed = worker.submit(report(1)).result(WAIT)
            assert failed.outcome is ReportOutcome.FAILED and failed.stage is stage
            assert failed.identity.cycle_id == 1 and failed.issue is not None
            if stage is ReportStage.INITIALIZATION:
                assert failed.sequence is None
                assert isinstance(
                    failed.error, FinsPlcError if mode == "plc" else FinsTimeoutError
                )
            else:
                assert failed.sequence == 11
                assert isinstance(failed.error, FirstStationSubmissionError)
                assert isinstance(
                    failed.error.__cause__,
                    FinsPlcError if mode == "plc" else FinsTimeoutError,
                )
            if mode == "plc":
                assert failed.issue.end_code == 0x2101
            elif stage is not ReportStage.INITIALIZATION:
                assert "write execution is unknown" in failed.issue.message
            count_after_failure = len(peer.packets)
            assert worker.snapshot().pending == 0
            recovered = worker.submit(report(2)).result(WAIT)
            sequence = 11 if stage is ReportStage.INITIALIZATION else 12
            assert (
                recovered.outcome is ReportOutcome.ACKNOWLEDGED
                and recovered.sequence == sequence
            )
            assert readback(peer) == (sequence, 0xEFBE, 0xBEFB, 15)
            snapshot = worker.snapshot()
            assert snapshot.failed_count == snapshot.acknowledged_count == 1
            assert snapshot.last_failed == failed
            assert snapshot.last_acknowledged == recovered
            failed_packets = peer.packets[before:count_after_failure]
            assert [
                int.from_bytes(p[13:15], "big")
                for p in failed_packets
                if p[10:12] == b"\x01\x02"
            ] == (
                []
                if stage is ReportStage.INITIALIZATION
                else [50]
                if stage is ReportStage.BITMAP
                else [50, 49]
            )
            init_reads = [
                p
                for p in peer.packets[before:]
                if p[10:12] == b"\x01\x01" and p[16:18] == b"\x00\x01"
            ]
            assert len(init_reads) == (2 if stage is ReportStage.INITIALIZATION else 1)
        finally:
            worker.request_stop()
            assert worker.join(WAIT) and worker.cleanup_safe


def test_bounded_fifo_rejection_never_replaces_an_accepted_report() -> None:
    with server() as peer:
        peer.hold_next(ReportStage.BITMAP)
        worker = FirstStationReportingWorker(
            7, settings(peer, max_pending=1, timeout=2)
        )
        worker.start()
        try:
            first = worker.submit(report(1))
            assert peer.entered.wait(WAIT)
            second = worker.submit(report(2))
            with pytest.raises(ReportingCapacityError):
                worker.submit(report(3))
            snapshot = worker.snapshot()
            assert snapshot.pending == snapshot.peak_pending == 1
            assert (
                snapshot.last_rejected is not None
                and snapshot.last_rejected.identity.cycle_id == 3
            )
            assert not first.cancel() and not second.cancel()
            peer.release.set()
            assert first.result(WAIT).identity.cycle_id == 1
            assert second.result(WAIT).identity.cycle_id == 2
            assert worker.snapshot().acknowledged_count == 2
            assert readback(peer)[0] == 2
        finally:
            peer.release.set()
            worker.request_stop()
            assert worker.join(WAIT)


def test_stop_finishes_active_report_skips_pending_and_observes_actual_exit() -> None:
    with server() as peer:
        peer.hold_next(ReportStage.BITMAP)
        worker = FirstStationReportingWorker(7, settings(peer, timeout=2))
        worker.start()
        try:
            first = worker.submit(report(1))
            assert peer.entered.wait(WAIT)
            pending = [worker.submit(report(i)) for i in (2, 3)]
            worker.request_stop()
            worker.request_stop()
            assert not worker.join(0) and not worker.cleanup_safe
            assert not worker.snapshot().accepting
            peer.release.set()
            assert first.result(WAIT).outcome is ReportOutcome.ACKNOWLEDGED
            assert all(f.result(WAIT).outcome is ReportOutcome.SKIPPED for f in pending)
            assert worker.join(WAIT) and worker.cleanup_safe
            snapshot = worker.snapshot()
            assert snapshot.acknowledged_count == 1 and snapshot.skipped_count == 2
            assert snapshot.pending == 0 and not snapshot.thread_alive
            assert worker.submit(report(4)).result().outcome is ReportOutcome.SKIPPED
            assert readback(peer)[0] == 1
            writes = [p for p in peer.packets if p[10:12] == b"\x01\x02"]
            assert len(writes) == 2
        finally:
            peer.release.set()
            worker.request_stop()
            assert worker.join(WAIT)
    assert not any(t.name.startswith("FirstStationReporting-") for t in threads())


def test_duplicate_out_of_order_and_generation_rejections_are_explicit() -> None:
    with server() as peer:
        worker = FirstStationReportingWorker(7, settings(peer))
        with pytest.raises(ReportingStateError, match="not started"):
            worker.submit(report(1))
        worker.start()
        try:
            worker.submit(report(5)).result(WAIT)
            for value in (report(5), report(4), report(6, generation=8)):
                with pytest.raises(ReportingStateError):
                    worker.submit(value)
            assert worker.snapshot().attempted_count == 1
        finally:
            worker.request_stop()
            assert worker.join(WAIT)
        with pytest.raises(ReportingStateError, match="one lifecycle"):
            worker.start()


def test_stop_before_start_is_safe_and_does_not_connect() -> None:
    worker = FirstStationReportingWorker(
        1, ReportingSettings(endpoint=FinsUdpEndpoint())
    )
    worker.request_stop()
    assert worker.join(0) and worker.cleanup_safe
    assert not worker.snapshot().thread_alive
    with pytest.raises(ReportingStateError):
        worker.start()
