"""Scalar handoff, serial sequence ownership, UDP recovery and bounded shutdown."""

from dataclasses import replace
from threading import enumerate as threads

import pytest
from tests.application_support import production
from tests.business_support import channel, mixed_result
from tests.reporting_support import WAIT, FaultMode, readback, seed, server, settings

from vimo_demo.communication import (
    FinsPlcError,
    FinsTimeoutError,
    FinsTransport,
    FinsUdpClient,
    FirstStationSubmissionError,
)
from vimo_demo.communication.reporting import (
    FirstStationReport,
    FirstStationReportingWorker,
    LocalReportingSettings,
    ReportIdentity,
    ReportingCapacityError,
    ReportingStateError,
    ReportOutcome,
    ReportStage,
)
from vimo_demo.inference import BoundingRect
from vimo_demo.inspection import CycleId, CycleStatus
from vimo_demo.inspection.production import (
    OccupancyState,
    ProductJudgment,
    TechnicalStatus,
    ViMoFinding,
)


def report(cycle: int, generation: int = 7) -> FirstStationReport:
    return FirstStationReport.from_result(generation, mixed_result(cycle))


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
def test_incomplete_outcomes_are_ineligible(status: CycleStatus) -> None:
    with pytest.raises(ValueError, match="Ineligible"):
        FirstStationReport.from_result(1, replace(production(), cycle_status=status))


def test_missing_frame_and_invalid_projection_are_not_reports() -> None:
    result = production()
    missing = replace(result.cameras[0], frame_id=None, image_shape=None)
    with pytest.raises(ValueError, match="complete six-camera"):
        FirstStationReport.from_result(
            1, replace(result, cameras=(missing, *result.cameras[1:]))
        )
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
@pytest.mark.parametrize("transport", list(FinsTransport))
def test_non_localhost_reporting_is_rejected_before_socket(
    host: str, transport: FinsTransport
) -> None:
    with pytest.raises(PermissionError, match="exactly 127.0.0.1"):
        LocalReportingSettings(host=host, transport=transport)


@pytest.mark.parametrize("transport", list(FinsTransport))
def test_execution_boundary_rejects_nonlocal_target_before_client_creation(
    transport: FinsTransport,
) -> None:
    selected = LocalReportingSettings(transport=transport)
    # Deliberately bypass frozen-constructor validation to exercise defense in depth.
    object.__setattr__(selected, "host", "127.0.0.2")

    def forbidden(_settings: LocalReportingSettings) -> FinsUdpClient:
        raise AssertionError("Execution validation must precede socket creation")

    worker = FirstStationReportingWorker(7, selected, client_factory=forbidden)
    worker.start()
    try:
        observed = worker.submit(report(100)).result(WAIT)
        assert observed.outcome is ReportOutcome.FAILED
        assert observed.stage is ReportStage.INITIALIZATION
        assert (
            observed.issue is not None
            and observed.issue.exception_type == "PermissionError"
        )
        assert "exactly 127.0.0.1" in observed.issue.message
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
    worker = FirstStationReportingWorker(1, LocalReportingSettings())
    worker.request_stop()
    assert worker.join(0) and worker.cleanup_safe
    assert not worker.snapshot().thread_alive
    with pytest.raises(ReportingStateError):
        worker.start()
