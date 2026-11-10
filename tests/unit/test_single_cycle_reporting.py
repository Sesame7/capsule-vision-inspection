"""Single-member groups use the retained PLC counter, never a local sequence."""

from dataclasses import replace

import pytest
from tests.support.fins_tcp import Stage, TcpPeer
from tests.unit.test_batch_reporting import admit, owner, submit

from capsule_vision.application.models import PilotCounters
from capsule_vision.communication.reporting import (
    CompletedReportingBatch,
    ReportIdentity,
    ReportingCapacityError,
    ReportingMode,
    ReportingStateError,
    ReportOutcome,
)
from capsule_vision.inspection import CycleId
from capsule_vision.inspection.production import ChannelId


@pytest.mark.parametrize("base", [49, 149])
@pytest.mark.parametrize("cameras", [5, 6])
@pytest.mark.parametrize("partial", [False, True])
def test_single_counter_projection_and_accounting(
    base: int, cameras: int, partial: bool
) -> None:
    facts: list[CompletedReportingBatch] = []
    with (
        TcpPeer(900, base_address=base) as peer,
        owner(peer, ReportingMode.SINGLE_CYCLE, on_completed=facts.append) as worker,
    ):
        peer.set_words(base - 1, (77, 900, 0, 0, 0xABCD))
        admit(worker, 21, count=1)
        actual = submit(worker, 21, cameras, count=1, partial=partial)[0].result(3)
        assert actual.outcome is ReportOutcome.ACKNOWLEDGED
        assert actual.sequence == 77 and worker.snapshot().next_sequence is None
        assert len(facts) == 1 and len(facts[0].reports) == 1
        assert (
            facts[0].reports[0].reject_words
            == facts[0].reject_words
            == actual.reject_words
        )
        expected = (0xCF3C, 0x3CF3) if cameras == 5 else (0xCF3C, 0x3CF3, 15)
        if partial:
            expected = (0xEFBE, 0x3FFB) if cameras == 5 else (0xEFBE, 0xFEFB, 15)
        assert actual.reject_words == expected
        assert peer.words(base, 4) == (
            77,
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
        counts = PilotCounters().completed(facts[0])
        assert counts == PilotCounters(
            1,
            cameras - int(partial),
            5 * cameras + 1 if partial else 4 * cameras,
            0 if partial else cameras,
            0,
        )
        progress = worker.snapshot().group_progress
        assert progress and progress.eligible and progress.cycles == (21,)
        assert len(progress.channel_states(ChannelId(1))) == 1
        assert not worker.is_fourth_cycle(CycleId(21))


def test_single_counter_wrap_skip_and_repetition() -> None:
    with TcpPeer() as peer, owner(peer, ReportingMode.SINGLE_CYCLE) as worker:
        for cycle, counter in enumerate((65534, 65535, 0, 17), 11):
            peer.set_words(48, (counter,))
            admit(worker, cycle, count=1)
            assert submit(worker, cycle, count=1)[0].result(3).sequence == counter
        packets = len(peer.packets)
        admit(worker, 15, count=1)
        assert submit(worker, 15, count=1)[0].result(3).outcome is ReportOutcome.SKIPPED
        assert (
            worker.snapshot().phase_issue and worker.snapshot().acknowledged_count == 4
        )
        assert all(p[10:12] == b"\x01\x01" for p in peer.packets[packets:])


def test_single_overlap_retains_the_existing_eight_plus_one_cycle_bound() -> None:
    with TcpPeer() as peer, owner(peer, ReportingMode.SINGLE_CYCLE) as worker:
        admit(worker, 11, count=1)
        for cycle in range(12, 20):
            worker.admit_cycle(CycleId(cycle))
        with pytest.raises(ReportingCapacityError):
            worker.admit_cycle(CycleId(20))
        assert worker.snapshot().peak_pending == 8
        assert sum(len(row.cycles) for row in worker.snapshot().batch_observations) == 9
        worker.request_stop(drain=True)
        assert worker.join(3)
        assert not worker.snapshot().acknowledged_count
        assert all(p[10:12] == b"\x01\x01" for p in peer.packets)


@pytest.mark.parametrize(
    "case", ["counter", "source", "overlap", "gap", "duplicate", "missing"]
)
def test_single_unattributable_group_does_not_count_or_write(case: str) -> None:
    valid = True
    facts: list[CompletedReportingBatch] = []
    with (
        TcpPeer() as peer,
        owner(
            peer,
            ReportingMode.SINGLE_CYCLE,
            attributable=lambda _: valid,
            on_completed=facts.append,
        ) as worker,
    ):
        peer.set_words(48, (12,))
        admit(worker, 11, count=1)
        if case == "counter":
            peer.set_words(48, (13,))
        elif case == "source":
            valid = False
        elif case in ("overlap", "gap"):
            worker.admit_cycle(CycleId(12 if case == "overlap" else 13))
        elif case == "duplicate":
            with pytest.raises(ReportingStateError):
                worker.admit_cycle(CycleId(11))
        elif case == "missing":
            worker.note_skipped(
                ReportIdentity(7, CycleId(11)), ValueError("No observed frame")
            )
        if case != "missing":
            assert (
                submit(worker, 11, count=1)[0].result(3).outcome
                is ReportOutcome.SKIPPED
            )
        worker.request_stop(drain=True)
        assert worker.join(3)
        assert not facts and worker.snapshot().phase_issue
        assert all(p[10:12] == b"\x01\x01" for p in peer.packets)


@pytest.mark.parametrize("read_fails", [False, True])
def test_late_initial_read_never_adopts_next_groups_counter(read_fails: bool) -> None:
    facts: list[CompletedReportingBatch] = []
    with (
        TcpPeer() as peer,
        owner(peer, ReportingMode.SINGLE_CYCLE, on_completed=facts.append) as worker,
    ):
        peer.set_words(48, (20,))
        peer.hold_next("read")
        if read_fails:
            peer.fail_next("read", "plc")
        worker.admit_cycle(CycleId(11))
        assert peer.entered.wait(3)
        first = submit(worker, 11, count=1)[0]
        peer.set_words(48, (21,))
        worker.admit_cycle(CycleId(12))
        second = submit(worker, 12, count=1)[0]
        peer.release.set()
        assert (
            first.result(3).outcome is second.result(3).outcome is ReportOutcome.SKIPPED
        )
        assert not facts and peer.words(49) == (0,)
        assert all(p[10:12] == b"\x01\x01" for p in peer.packets)


@pytest.mark.parametrize("stage", ["bitmap", "sequence"])
def test_eligible_single_group_survives_uncertain_delivery_without_retry(
    stage: Stage,
) -> None:
    facts: list[CompletedReportingBatch] = []
    with (
        TcpPeer() as peer,
        owner(peer, ReportingMode.SINGLE_CYCLE, on_completed=facts.append) as worker,
    ):
        peer.set_words(48, (45,))
        admit(worker, 11, count=1)
        peer.hold_next(stage)
        peer.fail_next(stage, "drop_after")
        future = submit(worker, 11, count=1)[0]
        assert peer.entered.wait(3) and len(facts) == 1 and not future.done()
        peer.release.set()
        assert future.result(3).outcome is ReportOutcome.FAILED
        evidence = worker.snapshot().batch_observations[-1]
        assert evidence.inspection_complete and evidence.commit_uncertain is (
            stage == "sequence"
        )
        assert evidence.completed_at is not None
        assert (
            facts[0].duration_ms
            == (evidence.completed_at - evidence.admitted_at) * 1000
        )
        packets = peer.packets
        worker.request_stop(drain=True)
        assert worker.join(3) and peer.packets == packets and len(facts) == 1


@pytest.mark.parametrize("stage", ["bitmap", "sequence"])
def test_single_overlap_after_eligibility_keeps_counts_but_suppresses_late_commit(
    stage: Stage,
) -> None:
    facts: list[CompletedReportingBatch] = []
    with (
        TcpPeer() as peer,
        owner(peer, ReportingMode.SINGLE_CYCLE, on_completed=facts.append) as worker,
    ):
        peer.set_words(48, (8,))
        admit(worker, 11, count=1)
        peer.hold_next(stage)
        first = submit(worker, 11, count=1)[0]
        assert peer.entered.wait(3) and len(facts) == 1
        peer.set_words(48, (9,))
        worker.admit_cycle(CycleId(12))
        second = submit(worker, 12, count=1)[0]
        peer.release.set()
        outcome = first.result(3)
        assert outcome.outcome is (
            ReportOutcome.SKIPPED if stage == "bitmap" else ReportOutcome.FAILED
        )
        assert second.result(3).outcome is ReportOutcome.SKIPPED
        assert len(facts) == 1 and peer.words(49) == (
            (0,) if stage == "bitmap" else (8,)
        )


def test_no_terminal_and_unsupported_member_counts_cannot_complete() -> None:
    with TcpPeer() as peer, owner(peer, ReportingMode.SINGLE_CYCLE) as worker:
        admit(worker, 1, count=1)
        worker.request_stop(drain=True)
        assert worker.join(3)
        assert not worker.snapshot().batch_observations[-1].inspection_complete
        assert all(p[10:12] == b"\x01\x01" for p in peer.packets)
    from tests.unit.test_four_cycle_presentation import completed

    fact = completed(11)
    for size in (0, 2, 3):
        with pytest.raises(ValueError, match="one or four"):
            replace(fact, reports=fact.reports[:size])
