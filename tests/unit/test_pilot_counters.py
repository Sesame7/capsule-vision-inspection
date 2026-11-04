"""Four-cycle scalar accounting uses the pilot merger, not divided occurrences."""

from dataclasses import replace

import pytest
from tests.support.business import channel, mixed_result
from tests.support.fins_tcp import Stage, TcpPeer
from tests.unit.test_batch_reporting import admit, owner, submit

from capsule_vision.application.models import PilotCounters, ProductionCounters
from capsule_vision.communication.reporting import (
    CompletedPilotBatch,
    FirstStationReport,
    ReportingMode,
    ReportOutcome,
)
from capsule_vision.inspection import CycleId, CycleStatus
from capsule_vision.inspection.production import (
    OccupancyState,
    ProductJudgment,
    TechnicalStatus,
)


@pytest.mark.parametrize("cameras", [5, 6])
@pytest.mark.parametrize("mode", [ReportingMode.W48_OBSERVE, ReportingMode.W48_ACTIVE])
@pytest.mark.parametrize("partial", [False, True])
def test_one_classification_per_position_from_authoritative_merger(
    cameras: int, mode: ReportingMode, partial: bool
) -> None:
    events: list[CompletedPilotBatch] = []
    with TcpPeer() as peer, owner(peer, mode, on_completed=events.append) as worker:
        admit(worker, 11)
        results = submit(worker, 11, cameras, partial=partial)
        observation = results[-1].result(3)
        assert len(events) == 1
        assert events[0].reject_words == observation.reject_words
        counts = PilotCounters().completed(events[0])
        assert counts.batches_completed == 1
        # A partial terminal makes all EMPTY evidence conservative, and the last
        # camera's OK uncertain; confirmed NG in another cycle keeps precedence.
        assert counts.channel_ok == cameras - int(partial)
        assert counts.channel_ng == 2 * cameras
        assert counts.channel_empty == (0 if partial else cameras)
        assert counts.channel_unknown == (3 * cameras + 1 if partial else 2 * cameras)
        assert (
            sum(
                (
                    counts.channel_ok,
                    counts.channel_ng,
                    counts.channel_empty,
                    counts.channel_unknown,
                )
            )
            == cameras * 6
        )
        assert worker.snapshot().pilot_batches[-1].inspection_complete
        if mode is ReportingMode.W48_OBSERVE:
            assert not any(p[10:12] == b"\x01\x02" for p in peer.packets)


def test_ok_and_verified_empty_mixture_is_ok_and_order_is_channel_id() -> None:
    events: list[CompletedPilotBatch] = []
    raw = ProductionCounters()
    with TcpPeer() as peer, owner(peer, on_completed=events.append) as worker:
        admit(worker, 1)
        future = None
        for cycle in range(1, 5):
            result = mixed_result(cycle)
            if cycle > 1:
                result = replace(
                    result,
                    cameras=tuple(
                        replace(
                            camera,
                            channels=(
                                channel(
                                    camera.channels[0],
                                    OccupancyState.EMPTY,
                                    TechnicalStatus.SUCCESS,
                                    ProductJudgment.OK,
                                ),
                                *camera.channels[1:],
                            ),
                        )
                        for camera in result.cameras
                    ),
                )
            raw = raw.admitted().completed(result)
            report = FirstStationReport.from_result(7, result)
            # Arrival order within the scalar report must not affect channel identity.
            report = replace(
                report,
                statuses=tuple(reversed(report.statuses))
                if cycle % 2
                else report.statuses,
            )
            future = worker.submit_cycle(report, result.cycle_status)
        assert future is not None
        future.result(3)
    counts = PilotCounters().completed(events[0])
    assert counts == PilotCounters(1, 6, 12, 6, 12)
    assert (
        raw.cycles_completed == 4 and raw.business_ok == 6 and raw.business_empty == 42
    )
    assert counts.channel_ok != raw.business_ok // 4


@pytest.mark.parametrize("stage", ["bitmap", "sequence"])
def test_accounting_precedes_and_survives_failed_write_ack(stage: Stage) -> None:
    events: list[CompletedPilotBatch] = []
    with TcpPeer() as peer, owner(peer, on_completed=events.append) as worker:
        peer.hold_next(stage)
        peer.fail_next(stage, "drop_after")
        admit(worker, 1)
        results = submit(worker, 1)
        assert peer.entered.wait(3)
        assert len(events) == 1 and not results[-1].done()
        assert PilotCounters().completed(events[0]).batches_completed == 1
        peer.release.set()
        assert results[-1].result(3).outcome is ReportOutcome.FAILED
        assert (
            len(events) == 1 and worker.snapshot().pilot_batches[-1].inspection_complete
        )


@pytest.mark.parametrize("count", [1, 2, 3])
def test_incomplete_batch_never_publishes_accounting(count: int) -> None:
    events: list[CompletedPilotBatch] = []
    with TcpPeer() as peer, owner(peer, on_completed=events.append) as worker:
        admit(worker, 1, count)
        results = submit(worker, 1, count=count)
        assert not events
        worker.request_stop(drain=True)
        results[-1].result(3)
        assert (
            not events and not worker.snapshot().pilot_batches[-1].inspection_complete
        )


@pytest.mark.parametrize("failure", ["changed", "source", "read"])
def test_unattributable_batch_never_publishes_accounting(failure: str) -> None:
    events: list[CompletedPilotBatch] = []
    valid = True
    with (
        TcpPeer() as peer,
        owner(peer, attributable=lambda _: valid, on_completed=events.append) as worker,
    ):
        admit(worker, 1)
        if failure == "changed":
            peer.set_words(48, (1,))
        elif failure == "source":
            valid = False
        else:
            peer.fail_next("read", "plc")
        results = submit(worker, 1)
        assert results[-1].result(3).outcome is (
            ReportOutcome.FAILED if failure == "read" else ReportOutcome.SKIPPED
        )
        assert not events
        assert not worker.snapshot().pilot_batches[-1].inspection_complete


def test_completion_requires_four_consecutive_same_generation_reports() -> None:
    reports = tuple(
        FirstStationReport.from_result(7, mixed_result(i)) for i in range(1, 5)
    )
    with pytest.raises(ValueError, match="four consecutive"):
        CompletedPilotBatch(
            reports[0].identity, 0, reports[:3], reports[0].reject_words
        )
    with pytest.raises(ValueError, match="four consecutive"):
        CompletedPilotBatch(
            reports[0].identity,
            0,
            (reports[1], reports[0], *reports[2:]),
            reports[0].reject_words,
        )
    # Duplicate terminal submission does not publish a second completion.
    events: list[CompletedPilotBatch] = []
    with TcpPeer() as peer, owner(peer, on_completed=events.append) as worker:
        admit(worker, 1)
        submit(worker, 1)[-1].result(3)
        with pytest.raises(RuntimeError):
            worker.submit_cycle(reports[0], CycleStatus.COMPLETE)
        assert len(events) == 1
        assert events[0].identity.cycle_id == CycleId(1)
