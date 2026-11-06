"""Conditional pilot against independent loopback TCP memory, never hardware."""

from collections.abc import Callable, Generator
from concurrent.futures import Future
from contextlib import closing, contextmanager
from dataclasses import replace
from pathlib import Path
from threading import Event, get_ident
from time import perf_counter

import pytest
from tests.support.business import mixed_result
from tests.support.diagnostics import observations
from tests.support.fins_tcp import Fault, Stage, TcpPeer

from capsule_vision.communication import (
    FinsTcpClient,
    FinsTcpEndpoint,
    FinsUdpEndpoint,
    ModbusSettings,
)
from capsule_vision.communication.batch_reporting import FourCycleReportingWorker
from capsule_vision.communication.endpoints import Endpoint
from capsule_vision.communication.reporting import (
    CompletedReportingBatch,
    FirstStationReport,
    ReportIdentity,
    ReportingCapacityError,
    ReportingMode,
    ReportingSelection,
    ReportingSettings,
    ReportingStateError,
    ReportObservation,
    ReportOutcome,
)
from capsule_vision.diagnostics import RunDiagnostics, RuntimeDiagnostics
from capsule_vision.inspection import CycleId, CycleStatus
from capsule_vision.inspection.production import ChannelBusinessStatus, ChannelId


def wait(predicate: Callable[[], bool]) -> None:
    deadline = perf_counter() + 3
    while not predicate():
        assert perf_counter() < deadline, "pilot condition not reached"
        Event().wait(0.002)


@contextmanager
def owner(
    peer: TcpPeer,
    mode: ReportingMode = ReportingMode.FOUR_CYCLE,
    attributable: Callable[[ReportIdentity], bool] = lambda _: True,
    on_completed: Callable[[CompletedReportingBatch], None] = lambda _: None,
    diagnostics: RunDiagnostics | None = None,
) -> Generator[FourCycleReportingWorker]:
    worker = FourCycleReportingWorker(
        7,
        ReportingSettings(
            endpoint=FinsTcpEndpoint(port=peer.port, timeout=0.1),
            base_address=peer.base_address,
            mode=mode,
        ),
        attributable=attributable,
        on_completed=on_completed,
        diagnostics=diagnostics,
    )
    worker.start()
    try:
        assert not peer.packets and peer.connections == 0  # Start is I/O-free.
        yield worker
    finally:
        worker.request_stop(drain=True)
        assert worker.join(3) and worker.cleanup_safe


def admit(worker: FourCycleReportingWorker, first: int, count: int = 4) -> None:
    worker.admit_cycle(CycleId(first))
    wait(
        lambda: (
            bool(worker.snapshot().batch_observations[-1].reads)
            or worker.snapshot().phase_issue is not None
        )
    )
    for cycle in range(first + 1, first + count):
        worker.admit_cycle(CycleId(cycle))


def submit(
    worker: FourCycleReportingWorker,
    first: int,
    cameras: int = 6,
    count: int = 4,
    partial: bool = False,
) -> list[Future[ReportObservation]]:
    results: list[Future[ReportObservation]] = []
    for cycle in range(first, first + count):
        result = mixed_result(cycle, camera_count=cameras)
        if partial and cycle == first + count - 1:
            result = replace(
                result,
                cycle_status=CycleStatus.TIMED_OUT,
                cameras=(
                    *result.cameras[:-1],
                    replace(result.cameras[-1], frame_id=None, image_shape=None),
                ),
            )
        results.append(
            worker.submit_cycle(
                FirstStationReport.from_result(7, result), result.cycle_status
            )
        )
    return results


@pytest.mark.parametrize("cameras", [5, 6])
@pytest.mark.parametrize("base", [49, 149])
@pytest.mark.parametrize("partial", [False, True])
def test_four_authoritative_projections_and_register_ownership(
    cameras: int, base: int, partial: bool
) -> None:
    with TcpPeer(900, base_address=base) as peer:
        peer.set_words(base - 1, (123, 900, 0, 0, 0xABCD))
        other = 149 if base == 49 else 49
        peer.set_words(other - 1, (17, 18, 19, 20, 21))
        with owner(peer) as worker:
            admit(worker, 21)
            futures = submit(worker, 21, cameras, partial=partial)
            outcome = futures[-1].result(3)
            assert all(f.result(3) is outcome for f in futures)
            assert outcome.outcome is ReportOutcome.ACKNOWLEDGED
            assert outcome.sequence == 123
            expected = (0xCF3C, 0x3CF3) if cameras == 5 else (0xCF3C, 0x3CF3, 15)
            if partial:
                # Partial cycle makes EMPTY conservative everywhere, missing camera all reject.
                expected = (0xEFBE, 0x3FFB) if cameras == 5 else (0xEFBE, 0xFEFB, 15)
            assert outcome.reject_words == expected
            writes = [p for p in peer.packets if p[10:12] == b"\x01\x02"]
            assert [
                (int.from_bytes(p[13:15]), int.from_bytes(p[16:18])) for p in writes
            ] == [(base + 1, cameras // 6 + 2), (base, 1)]
            assert peer.words(base, 4) == (
                123,
                *expected,
                *((0xABCD,) if cameras == 5 else ()),
            )
            assert [int.from_bytes(p[13:15]) for p in peer.packets] == [
                base - 1,
                base - 1,
                base + 1,
                base - 1,
                base,
            ]
            assert peer.words(other - 1, 5) == (17, 18, 19, 20, 21)
            evidence = worker.snapshot().batch_observations[-1]
            assert (
                evidence.cycles == (21, 22, 23, 24)
                and len(evidence.terminal_outcomes) == 4
            )
            assert all(
                r.value == 123
                and r.address == base - 1
                and r.finished_at >= r.started_at
                for r in evidence.reads
            )
            assert evidence.completed_at is not None and not evidence.commit_uncertain


@pytest.mark.parametrize("case", ["ack", "uncertain", "stop"])
def test_detailed_diagnostics_observe_actual_handshake(
    tmp_path: Path, case: str
) -> None:
    with closing(RuntimeDiagnostics(tmp_path, "detailed")) as writer:
        trace = RunDiagnostics(writer, 7, "local", "actual-run")
        with (
            TcpPeer(base_address=149) as peer,
            owner(peer, diagnostics=trace) as worker,
        ):
            peer.set_words(148, (41,))
            count = 2 if case == "stop" else 4
            admit(worker, 11, count)
            if case == "uncertain":
                peer.fail_next("sequence", "drop_after")
            future = submit(worker, 11, count=count)[-1]
            if case == "stop":
                worker.request_stop(drain=True)
            actual = future.result(3)
            fact = worker.snapshot().batch_observations[-1]
            assert (
                actual.outcome
                is {
                    "ack": ReportOutcome.ACKNOWLEDGED,
                    "uncertain": ReportOutcome.FAILED,
                    "stop": ReportOutcome.SKIPPED,
                }[case]
            )
            writes = [p for p in peer.packets if p[10:12] == b"\x01\x02"]
            assert len(writes) == (0 if case == "stop" else 2)
    members = observations(tmp_path, "four_cycle.member")
    assert [(r["cycle_id"], r["exposure"]) for r in members] == [
        (11 + i, i + 1) for i in range(count)
    ]
    assert all(
        r["run_id"] == "actual-run" and r["first_cycle_id"] == 11 for r in members
    )
    completed = observations(tmp_path, "four_cycle.completed")
    assert len(completed) == int(case != "stop")
    if completed:
        assert completed[0]["completed_at_s"] == fact.completed_at
    (report,) = observations(tmp_path, "four_cycle.report")
    assert (
        report["outcome"] == actual.outcome.value
        and report["eligible"] == fact.inspection_complete
    )
    assert report["commit_uncertain"] == (case == "uncertain")
    assert report["cycle_ids"] == list(fact.cycles)
    assert report["completion_to_ack_ms"] == actual.completion_to_ack_ms
    stages = observations(tmp_path, "four_cycle.counter_read")
    assert [
        (r["stage"], r["value"], r["address"], r["finished_at_s"]) for r in stages
    ] == [(r.stage, r.value, r.address, r.finished_at) for r in fact.reads]
    (stage,) = observations(tmp_path, "four_cycle.writes")
    assert stage["acknowledged_at_s"] == actual.acknowledged_at
    assert (
        stage["bitmap_ack_ms"] == actual.bitmap_ms
        and stage["sequence_ack_ms"] == actual.sequence_ms
    )
    assert (
        stage["commit_started_at_s"] == fact.commit_started_at
        and stage["commit_finished_at_s"] == fact.commit_finished_at
    )


def test_counter_skips_wrap_bounded_evidence_and_no_local_sequence() -> None:
    with TcpPeer() as peer, owner(peer) as worker:
        counters = (65534, 65535, 0, 17, *range(18, 36))
        for index, counter in enumerate(counters):
            peer.set_words(48, (counter,))
            first = index * 4 + 1
            admit(worker, first)
            assert submit(worker, first)[-1].result(3).sequence == counter
        snapshot = worker.snapshot()
        assert (
            snapshot.acknowledged_count == len(counters)
            and snapshot.next_sequence is None
        )
        assert len(snapshot.batch_observations) == 16 and snapshot.phase_issue is None


@pytest.mark.parametrize(
    "scenario", ["counter", "stale", "ambiguous", "duplicate", "gap", "late"]
)
@pytest.mark.parametrize("base", [49, 149])
def test_observable_association_loss_suppresses_and_latches(
    scenario: str, base: int
) -> None:
    valid = True
    with (
        TcpPeer(base_address=base) as peer,
        owner(peer, attributable=lambda _: valid) as worker,
    ):
        peer.set_words(base - 1, (5,))
        first = 1
        if scenario == "stale":
            admit(worker, first)
            submit(worker, first)[-1].result(3)
            first = 5
        before = len(peer.packets)
        admit(worker, first)
        if scenario == "counter":
            peer.set_words(base - 1, (6,))
        elif scenario == "ambiguous":
            valid = False
        elif scenario == "duplicate":
            with pytest.raises(ReportingStateError):
                worker.admit_cycle(CycleId(first + 3))
        elif scenario in ("gap", "late"):
            worker.admit_cycle(CycleId(first + (5 if scenario == "gap" else 4)))
        result = submit(worker, first)[-1].result(3)
        assert result.outcome is ReportOutcome.SKIPPED
        assert worker.snapshot().phase_issue is not None
        assert not [p for p in peer.packets[before:] if p[10:12] == b"\x01\x02"]


@pytest.mark.parametrize("base", [49, 149])
def test_changed_counter_after_bitmap_ack_never_commits(base: int) -> None:
    with TcpPeer(base_address=base) as peer, owner(peer) as worker:
        peer.set_words(base - 1, (14,))
        admit(worker, 1)
        peer.hold_next("bitmap")
        future = submit(worker, 1)[-1]
        assert peer.entered.wait(3)
        peer.set_words(base - 1, (15,))
        peer.release.set()
        assert future.result(3).outcome is ReportOutcome.SKIPPED
        assert peer.words(base) == (0,)
        assert worker.snapshot().batch_observations[-1].commit_started_at is None


@pytest.mark.parametrize(
    ("stage", "fault"),
    [
        ("bitmap", "plc"),
        ("bitmap", "drop_after"),
        ("sequence", "plc"),
        ("sequence", "drop_before"),
        ("sequence", "drop_after"),
    ],
)
@pytest.mark.parametrize("base", [49, 149])
def test_failed_or_uncertain_writes_never_replayed(
    stage: Stage, fault: Fault, base: int
) -> None:
    with TcpPeer(base_address=base) as peer, owner(peer) as worker:
        peer.set_words(base - 1, (15,))
        admit(worker, 1)
        peer.fail_next(stage, fault)
        outcome = submit(worker, 1)[-1].result(3)
        assert outcome.outcome is ReportOutcome.FAILED and outcome.issue is not None
        evidence = worker.snapshot().batch_observations[-1]
        assert evidence.commit_uncertain == (stage == "sequence")
        assert peer.words(base) == (
            (15,) if stage == "sequence" and fault == "drop_after" else (0,)
        )
        packets = peer.packets
        worker.request_stop(drain=True)
        assert worker.join(3) and peer.packets == packets


@pytest.mark.parametrize("count", [1, 2, 3])
@pytest.mark.parametrize("base", [49, 149])
def test_incomplete_stop_and_fresh_generation(count: int, base: int) -> None:
    with TcpPeer(base_address=base) as peer, owner(peer) as worker:
        admit(worker, 1, count)
        futures = submit(worker, 1, count=count)
        worker.request_stop(drain=True)
        assert worker.join(3)
        assert all(f.result(3).outcome is ReportOutcome.SKIPPED for f in futures)
        assert len(worker.snapshot().batch_observations[-1].cycles) == count
        assert all(p[10:12] == b"\x01\x01" for p in peer.packets)
    with TcpPeer(base_address=base) as peer, owner(peer) as fresh:
        admit(fresh, 20)
        assert submit(fresh, 20)[-1].result(3).outcome is ReportOutcome.ACKNOWLEDGED


@pytest.mark.parametrize("base", [49, 149])
def test_delayed_initial_read_and_read_failure_preserve_terminal_handoffs(
    base: int,
) -> None:
    for failing in (False, True):
        with TcpPeer(base_address=base) as peer, owner(peer) as worker:
            peer.hold_next("read")
            if failing:
                peer.fail_next("read", "plc")
            worker.admit_cycle(CycleId(1))
            assert peer.entered.wait(3)
            for cycle in (2, 3, 4):
                worker.admit_cycle(CycleId(cycle))
            futures = submit(worker, 1)
            peer.release.set()
            assert futures[-1].result(3).outcome is ReportOutcome.SKIPPED
            assert len(worker.snapshot().batch_observations[-1].terminal_outcomes) == 4
            assert all(p[10:12] == b"\x01\x01" for p in peer.packets)


def test_socket_factory_and_cleanup_stay_on_one_communication_owner() -> None:
    owners: list[int] = []

    class Client(FinsTcpClient):
        def close(self) -> None:
            owners.append(get_ident())
            super().close()

    with TcpPeer() as peer:

        def factory(endpoint: Endpoint) -> FinsTcpClient:
            owners.append(get_ident())
            return Client(endpoint.host, port=endpoint.port)

        worker = FourCycleReportingWorker(
            7,
            ReportingSettings(
                endpoint=FinsTcpEndpoint(port=peer.port), mode=ReportingMode.FOUR_CYCLE
            ),
            client_factory=factory,
        )
        worker.start()
        try:
            admit(worker, 1)
            submit(worker, 1)[-1].result(3)
        finally:
            worker.request_stop(drain=True)
            assert worker.join(3)
        assert len(owners) == 2 and len(set(owners)) == 1 and owners[0] != get_ident()


@pytest.mark.parametrize("endpoint", [FinsUdpEndpoint(), ModbusSettings()])
def test_unsupported_transport_rejected_without_io(endpoint: Endpoint) -> None:
    with pytest.raises(ValueError, match="FINS/TCP"):
        ReportingSelection(
            True, ReportingSettings(endpoint, 49, mode=ReportingMode.FOUR_CYCLE)
        )
    with pytest.raises(ValueError, match="W49"):
        ReportingSelection(
            True,
            ReportingSettings(FinsTcpEndpoint(), 120, mode=ReportingMode.FOUR_CYCLE),
        )


def test_terminal_order_independent_and_capacity_remains_bounded() -> None:
    with TcpPeer() as peer, owner(peer) as worker:
        admit(worker, 1)
        futures: list[Future[ReportObservation]] = []
        for cycle in (4, 2, 3, 1):
            # Each cycle contributes a different bit, including bits in all three
            # words. Keeping only the newest/largest cycle cannot pass this case.
            channel = {1: 1, 2: 17, 3: 36, 4: 2}[cycle]
            status = (
                ChannelBusinessStatus.UNKNOWN
                if cycle == 4
                else ChannelBusinessStatus.NG
            )
            report = FirstStationReport(
                ReportIdentity(7, CycleId(cycle)),
                tuple(
                    (ChannelId(i), status if i == channel else ChannelBusinessStatus.OK)
                    for i in range(1, 37)
                ),
            )
            futures.append(worker.submit_cycle(report, CycleStatus.COMPLETE))
        assert futures[-1].result(3).outcome is ReportOutcome.ACKNOWLEDGED
        assert futures[-1].result(3).reject_words == (3, 1, 8)
        assert peer.words(50, 3) == (3, 1, 8)
        assert all(f.result(3) == futures[-1].result(3) for f in futures)
        peer.set_words(48, (1,))
        admit(worker, 5)
        for cycle in range(9, 14):
            worker.admit_cycle(CycleId(cycle))
        with pytest.raises(ReportingCapacityError):
            worker.admit_cycle(CycleId(14))
        assert (
            sum(
                len(b.cycles)
                for b in worker.snapshot().batch_observations
                if b.report is None
            )
            == 9
        )


def test_duplicate_terminal_and_missing_result_never_fabricate_reports() -> None:
    with TcpPeer() as peer, owner(peer) as worker:
        admit(worker, 1)
        first = submit(worker, 1, count=1)[0]
        with pytest.raises(ReportingStateError, match="Duplicate"):
            submit(worker, 1, count=1)
        worker.note_skipped(
            ReportIdentity(7, CycleId(2)), ValueError("no terminal inspection")
        )
        submit(worker, 3, count=2)
        assert first.result(3).outcome is ReportOutcome.SKIPPED
        assert worker.snapshot().batch_observations[-1].completed_at is None
        assert all(p[10:12] == b"\x01\x01" for p in peer.packets)


@pytest.mark.parametrize("base", [49, 149])
def test_next_batch_during_sequence_request_is_visible_uncertain_commit(
    base: int,
) -> None:
    with TcpPeer(base_address=base) as peer, owner(peer) as worker:
        peer.set_words(base - 1, (19,))
        admit(worker, 1)
        peer.hold_next("sequence")
        future = submit(worker, 1)[-1]
        assert peer.entered.wait(3)
        worker.admit_cycle(CycleId(5))
        peer.set_words(base - 1, (20,))
        peer.release.set()
        assert future.result(3).outcome is ReportOutcome.FAILED
        assert peer.words(base) == (19,)  # Already in flight: cannot recall this write.
        assert worker.snapshot().batch_observations[-2].commit_uncertain


@pytest.mark.parametrize("base", [49, 149])
def test_source_fence_during_inflight_commit_is_uncertain(base: int) -> None:
    valid = True
    with (
        TcpPeer(base_address=base) as peer,
        owner(peer, attributable=lambda _: valid) as worker,
    ):
        admit(worker, 1)
        peer.hold_next("sequence")
        future = submit(worker, 1)[-1]
        assert peer.entered.wait(3)
        valid = False
        peer.release.set()
        assert future.result(3).outcome is ReportOutcome.FAILED
        assert worker.snapshot().batch_observations[-1].commit_uncertain


@pytest.mark.parametrize("defect", ["generation", "layout"])
def test_mixed_batch_identity_is_rejected(defect: str) -> None:
    with TcpPeer() as peer, owner(peer) as worker:
        admit(worker, 1)
        futures = submit(worker, 1, count=3)
        if defect == "generation":
            with pytest.raises(ReportingStateError, match="generation"):
                worker.submit_cycle(
                    FirstStationReport.from_result(8, mixed_result(4)),
                    CycleStatus.COMPLETE,
                )
            worker.note_skipped(
                ReportIdentity(7, CycleId(4)), ValueError("mixed generation")
            )
        else:
            submit(worker, 4, cameras=5, count=1)
        assert futures[0].result(3).outcome is ReportOutcome.SKIPPED
        assert all(p[10:12] == b"\x01\x01" for p in peer.packets)


def test_failed_observer_cannot_strand_other_accepted_cycle_handles() -> None:
    class ObserverFailure(BaseException):
        pass

    def broken(_future: Future[ReportObservation]) -> None:
        raise ObserverFailure("controlled observer failure")

    with TcpPeer() as peer, owner(peer) as worker:
        admit(worker, 1)
        first = submit(worker, 1, count=1)[0]
        first.add_done_callback(broken)
        remaining = submit(worker, 2, count=3)
        assert all(
            f.result(3).outcome is ReportOutcome.ACKNOWLEDGED
            for f in (first, *remaining)
        )
        assert worker.join(3) and worker.cleanup_safe
        assert worker.snapshot().failure_issue is not None
