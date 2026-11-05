"""Reporting-owned progress/timing and bounded fourth-frame display, no SDK."""

import json
from dataclasses import replace
from pathlib import Path
from typing import Literal

import pytest
from tests.support.application import prepared
from tests.support.business import mixed_result
from tests.support.fins_tcp import TcpPeer
from tests.support.production import source_frame
from tests.unit.test_batch_reporting import admit, owner, submit
from tests.unit.test_web_snapshots import Source

from capsule_vision.communication import batch_reporting
from capsule_vision.communication.reporting import (
    CompletedReportingBatch,
    FirstStationReport,
    ReportingGroupProgress,
    ReportOutcome,
)
from capsule_vision.display import LatestDisplayFrameStore, matching_camera
from capsule_vision.inspection import CycleId
from capsule_vision.inspection.production import ChannelId
from capsule_vision.web.publisher import SnapshotPublisher


def completed(first: int, generation: int = 1) -> CompletedReportingBatch:
    reports = tuple(
        FirstStationReport.from_result(generation, mixed_result(i))
        for i in range(first, first + 4)
    )
    return CompletedReportingBatch(
        reports[0].identity, first, reports, reports[0].reject_words, 125
    )


@pytest.mark.parametrize("hold", ["read", "sequence"])
def test_progress_admission_order_reset_invalidation_and_duration(
    monkeypatch: pytest.MonkeyPatch,
    hold: Literal["read", "sequence"],
) -> None:
    now = 10.0
    monkeypatch.setattr(batch_reporting, "perf_counter", lambda: now)
    events: list[CompletedReportingBatch] = []
    with TcpPeer() as peer, owner(peer, on_completed=events.append) as worker:
        admit(worker, 11)
        # The fourth admitted Cycle finishes first. Only its own lamp can advance.
        submit(worker, 14, count=1)
        progress = worker.snapshot().group_progress
        assert progress and progress.cycles == (11, 12, 13, 14)
        assert progress.channel_states(ChannelId(1)) == (
            "pending",
            "pending",
            "pending",
            "ok",
        )
        assert worker.snapshot().batch_observations[-1].completed_at is None
        for cycle in (11, 12):
            submit(worker, cycle, count=1)
            assert worker.snapshot().batch_observations[-1].completed_at is None
        assert not events
        peer.hold_next(hold)  # Neither pre-write attribution nor ACK extends duration.
        now = 10.125
        (future,) = submit(worker, 13, count=1)
        assert peer.entered.wait(3)
        assert worker.snapshot().batch_observations[-1].completed_at == 10.125
        assert len(events) == (0 if hold == "read" else 1)
        now = 12.0
        peer.release.set()
        outcome = future.result(3)
        assert outcome.outcome is ReportOutcome.ACKNOWLEDGED
        assert outcome.acknowledged_at == 12.0
        assert outcome.completion_to_ack_ms == 1875
        assert events[0].duration_ms == 125
        assert tuple(report.identity.cycle_id for report in events[0].reports) == (
            11,
            12,
            13,
            14,
        )
        progress = worker.snapshot().group_progress
        assert progress and progress.eligible
        assert (
            progress.channel_states(ChannelId(3)) == ("ng",) * 4
        )  # UNKNOWN -> reject.
        assert progress.channel_states(ChannelId(2)) == ("empty",) * 4
        peer.set_words(48, (1,))
        admit(worker, 15, count=1)
        assert worker.snapshot().group_progress == progress
        submit(worker, 15, count=1)
        next_progress = worker.snapshot().group_progress
        assert next_progress and next_progress.identity.cycle_id == 15
        assert next_progress.channel_states(ChannelId(1)) == (
            "ok",
            "pending",
            "pending",
            "pending",
        )
        with pytest.raises(RuntimeError):
            worker.admit_cycle(CycleId(15))
        invalid = worker.snapshot().group_progress
        assert invalid and invalid.invalidated
        assert invalid.channel_states(ChannelId(1)) == ("pending",) * 4
        assert len(events) == 1


def test_eligible_progress_survives_later_phase_loss() -> None:
    with TcpPeer() as peer, owner(peer) as worker:
        admit(worker, 1)
        submit(worker, 1)[-1].result(3)
        with pytest.raises(RuntimeError):
            worker.admit_cycle(CycleId(4))
        progress = worker.snapshot().group_progress
        assert progress and progress.eligible and not progress.invalidated


def test_fourth_display_delayed_publication_missing_camera_and_newer_frames(
    tmp_path: Path,
) -> None:
    result = mixed_result(4)
    cameras = result.layout.camera_ids
    store = LatestDisplayFrameStore(cameras)
    store.begin_generation(1, four_cycle=True)
    assert not store.publish(source_frame(cameras[0], cycle=1), generation=1)
    store.select_fourth(1, CycleId(4))
    store.publish(
        source_frame(cameras[0], cycle=4), generation=1, source_key="fixed-source"
    )
    assert not store.snapshot().frames
    store.retain_fourth_result(1, result)
    store.complete_group(completed(1))
    source = Source()
    config = prepared(tmp_path)
    source.value = replace(
        source.value,
        pilot_counting=True,
        latest_production_result=mixed_result(5),
        active_prepared=config,
        group_progress=ReportingGroupProgress(
            completed(1).identity,
            tuple(CycleId(i) for i in range(1, 5)),
            completed(1).reports,
            eligible=True,
        ),
        batch_duration_ms=125,
    )
    frame = store.snapshot().frames[0]
    assert (
        frame.frame_id.cycle_id == 4
        and matching_camera(frame, source.value) == result.cameras[0]
    )
    assert (
        matching_camera(
            replace(frame, frame_id=replace(frame.frame_id, sequence=8)), source.value
        )
        is None
    )
    assert matching_camera(frame, replace(source.value, generation=2)) is None
    assert not store.publish(source_frame(cameras[0], cycle=5), generation=1)
    # A legitimate late callback can fill exactly the missing fourth identity.
    assert store.publish(source_frame(cameras[1], cycle=4), generation=1)
    assert len(store.snapshot().frames) == 2
    publisher = SnapshotPublisher(source, store)
    publisher.publish()
    publication = publisher.latest()
    assert publication is not None
    rows = json.loads(publication.metadata)["cameras"]
    assert rows[0]["image_identity"]["cycle_id"] == 4
    assert rows[0]["result_status"] == "matched"
    assert rows[2]["image_status"] == "unavailable"
    assert rows[2]["group_channels"][2]["states"] == ["ng"] * 4
    # Pending next group keeps the old eligible picture and its own overlays.
    store.select_fourth(1, CycleId(8))
    store.retain_fourth_result(1, mixed_result(8))
    store.publish(source_frame(cameras[2], cycle=8), generation=1)
    assert store.snapshot().frames[0] is frame
    store.complete_group(completed(5))
    assert [f.frame_id.camera_id for f in store.snapshot().frames] == [cameras[2]]
    assert not store.publish(source_frame(cameras[0], cycle=4), generation=1)
    publisher.publish()
    publication = publisher.latest()
    assert publication is not None
    rows = json.loads(publication.metadata)["cameras"]
    assert (
        rows[0]["image_status"] == "unavailable" and rows[0]["image_identity"] is None
    )
    assert rows[2]["image_identity"]["cycle_id"] == 8
    assert len(publication.deliveries) == 1  # Blank retires previous JPEGs.
    # Per-cycle launch preserves retained pixels; the new generation hides their evidence.
    store.begin_generation(2, four_cycle=False)
    assert all(
        matching_camera(frame, replace(source.value, generation=2)) is None
        for frame in store.snapshot().frames
    )
    assert not store.publish(source_frame(cameras[0], cycle=8), generation=1)
    assert store.publish(source_frame(cameras[0], cycle=1), generation=2)


def test_fourth_full_frame_identity_shape_and_bounded_cleanup() -> None:
    result = mixed_result(4)
    store = LatestDisplayFrameStore(result.layout.camera_ids)
    store.begin_generation(1, four_cycle=True)
    for first in range(1, 41, 4):
        cycle = CycleId(first + 3)
        store.select_fourth(1, cycle)
        store.retain_fourth_result(1, mixed_result(cycle))
        for camera in result.cameras:
            frame = source_frame(camera.camera_id, cycle=cycle)
            if camera is result.cameras[0]:
                frame = replace(frame, id=replace(frame.id, sequence=8))
            store.publish(frame, generation=1)
        store.complete_group(completed(first))
        assert len(store.snapshot().frames) == 5
        assert all(f.frame_id.cycle_id == cycle for f in store.snapshot().frames)
    store.select_fourth(1, CycleId(44))
    store.publish(source_frame(result.cameras[0].camera_id, cycle=44), generation=1)
    store.discard_candidate(1)
    assert not store.publish(
        source_frame(result.cameras[0].camera_id, cycle=44), generation=1
    )
    assert len(store.snapshot().frames) == 5
