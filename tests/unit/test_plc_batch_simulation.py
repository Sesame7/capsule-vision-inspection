"""Proposed physical-batch association, not production PLC behavior."""

from dataclasses import replace

import pytest
from simulate_plc_batches import (
    Association,
    BatchProjection,
    Capture,
    CounterRead,
    CounterUpdate,
    bracket_association,
    counter_at,
    merge_batches,
    naive_association,
    read_counter,
    simulate_burst,
    simulate_submission,
)
from tests.support.business import mixed_result

from capsule_vision.communication.reporting import FirstStationReport
from capsule_vision.inspection import CycleStatus
from capsule_vision.inspection.production import (
    Assessment,
    ChannelBusinessStatus,
    ProductJudgment,
    Reason,
    TechnicalStatus,
)


def reads_for(value: int = 7, start: int = 0) -> tuple[CounterRead, ...]:
    return (
        CounterRead(start - 10, start - 6, value),
        *(CounterRead(start + t + 5, start + t + 9, value) for t in (0, 60, 120, 180)),
    )


def associate(capture: Capture, reads: tuple[CounterRead, ...]) -> Association:
    return bracket_association(
        capture,
        reads,
        frame_delay_bound_ms=5,
        no_counter_reuse=True,
        common_trigger_verified=True,
    )


def batch_rows(
    count: int = 6,
) -> tuple[tuple[Capture, Association, FirstStationReport | None], ...]:
    return tuple(
        (
            capture := Capture(i + 1, i * 60 + 5, i * 60 + 25),
            associate(capture, reads_for()),
            FirstStationReport.from_result(1, mixed_result(i + 1, camera_count=count)),
        )
        for i in range(4)
    )


def test_identical_observations_have_two_valid_physical_batch_histories() -> None:
    # W48 changes before each batch, but no maximum update-to-first-trigger lead
    # or known phase was supplied. Camera exposure/arrival times are identical.
    worlds = (
        (CounterUpdate(-600, 7), CounterUpdate(190, 8)),
        (CounterUpdate(-600, 7), CounterUpdate(-10, 8)),
    )
    observations = tuple(
        tuple(read_counter(world, t, 0, 0) for t in (-220, 220)) for world in worlds
    )
    assert observations[0] == observations[1]
    assert tuple(counter_at(worlds[0], t) for t in (0, 60, 120, 180)) == (7,) * 4
    assert tuple(counter_at(worlds[1], t) for t in (0, 60, 120, 180)) == (8,) * 4
    for ordinal in range(1, 5):
        capture = Capture(ordinal, (ordinal - 1) * 60 + 5, 220)
        assert naive_association(capture, observations[0], "latest_on_arrival") == 7
        assert naive_association(capture, observations[0], "read_on_arrival") == 8
        assert naive_association(capture, observations[0], "after_inspection") == 8
        assert associate(capture, observations[0]).value is None


def test_read_on_arrival_can_sample_the_next_batch_before_reply() -> None:
    updates = (CounterUpdate(-10, 7), CounterUpdate(190, 8))
    old = read_counter(updates, 185, 10, 0)
    new = read_counter(updates, 185, 10, 10)
    assert (old.requested_ms, old.replied_ms) == (new.requested_ms, new.replied_ms)
    assert (old.value, new.value) == (7, 8)
    capture = Capture(4, 185, 205)
    assert counter_at(updates, 180) == 7
    assert naive_association(capture, (new,), "read_on_arrival") == 8
    assert associate(capture, (*reads_for()[:-1], new)).value is None


def test_required_external_guarantees_and_delayed_frame_window() -> None:
    capture = Capture(1, 5, 25)
    reads = reads_for()
    assert bracket_association(capture, reads).reason == "missing_external_guarantee"
    for delay, reuse, common in (
        (None, True, True),
        (5, False, True),
        (5, True, False),
    ):
        assert (
            bracket_association(
                capture,
                reads,
                frame_delay_bound_ms=delay,
                no_counter_reuse=reuse,
                common_trigger_verified=common,
            ).value
            is None
        )
    assert associate(capture, reads).value == 7
    assert associate(replace(capture, source_attributable=False), reads).value is None
    assert (
        bracket_association(
            capture,
            reads,
            frame_delay_bound_ms=20,
            no_counter_reuse=True,
            common_trigger_verified=True,
        ).reason
        == "unbracketed_capture"
    )
    # Equality at the left reply and right request is covered, not extrapolated.
    exact = (CounterRead(-1, 0, 7), CounterRead(5, 9, 7))
    assert associate(capture, exact).value == 7
    with pytest.raises(ValueError, match="serial"):
        associate(capture, (CounterRead(0, 10, 7), CounterRead(5, 15, 7)))


@pytest.mark.parametrize("camera_count", [5, 6])
def test_four_terminal_results_or_existing_projection_without_mutating_evidence(
    camera_count: int,
) -> None:
    rows = list(batch_rows(camera_count))
    result = mixed_result(4, camera_count=camera_count)
    camera = result.cameras[0]
    empty = camera.channels[1]
    # Preserve business EMPTY while making its enabled optional check uncertain.
    uncertain = replace(
        empty,
        traditional_skip=None,
        checks=tuple(
            replace(
                check,
                required=False,
                assessment=Assessment(TechnicalStatus.ERROR, ProductJudgment.UNKNOWN),
                reasons=(Reason(check.source, "execution_error"),),
            )
            if i
            else replace(
                check,
                assessment=Assessment(TechnicalStatus.SUCCESS, ProductJudgment.OK),
            )
            for i, check in enumerate(empty.checks)
        ),
    )
    assert uncertain.business_status is ChannelBusinessStatus.EMPTY
    changed = replace(
        result,
        cameras=(
            replace(
                camera, channels=(camera.channels[0], uncertain, *camera.channels[2:])
            ),
            *result.cameras[1:],
        ),
    )
    rows[-1] = (*rows[-1][:2], FirstStationReport.from_result(1, changed))
    merged = merge_batches(tuple(rows))
    assert not merged.diagnostics and len(merged.batches) == 1
    batch = merged.batches[0]
    expected = (0xCF3E, 0x3CF3) if camera_count == 5 else (0xCF3E, 0x3CF3, 15)
    assert batch.reject_words == expected
    assert merge_batches(tuple(reversed(rows))).batches == merged.batches
    assert batch.ordinals == (1, 2, 3, 4) and batch.w48 == 7 and batch.ready_ms == 205
    assert batch.proposed_w50_w52 == ((*expected, 0) if camera_count == 5 else expected)
    sent = simulate_submission(batch, 370)
    assert sent.attempted_writes == ((50, batch.proposed_w50_w52, 205), (49, (7,), 215))
    assert (
        batch.reject_words[1] & 0xC000 == 0
        if camera_count == 5
        else batch.reject_words[2] & 0xFFF0 == 0
    )
    # CH1 OK stays clear; CH2 uncertain EMPTY rejects; other verified EMPTY stays clear.
    assert not batch.reject_words[0] & 1 and batch.reject_words[0] & 2
    assert not batch.reject_words[0] & (1 << 7)
    assert (
        camera.channels[1] is empty
        and empty.business_status is ChannelBusinessStatus.EMPTY
    )


@pytest.mark.parametrize(
    "kind",
    [
        "missing_trigger",
        "duplicate",
        "no_result",
        "ambiguous",
        "fifth",
        "unattributed_extra",
        "mixed_layout",
        "mixed_generation",
        "wrong_identity",
    ],
)
def test_invalid_or_incomplete_batches_have_diagnostics_and_no_submission(
    kind: str,
) -> None:
    rows = list(batch_rows())
    if kind == "missing_trigger":
        rows.pop(1)
    elif kind == "duplicate":
        rows[-1] = rows[0]
    elif kind == "no_result":
        rows[-1] = (*rows[-1][:2], None)
    elif kind == "ambiguous":
        rows[-1] = (replace(rows[-1][0], source_attributable=False), *rows[-1][1:])
    elif kind in ("fifth", "unattributed_extra"):
        capture = Capture(5, 186, 206)
        reads = (
            (*reads_for(), CounterRead(190, 194, 7)) if kind == "fifth" else reads_for()
        )
        rows.append(
            (
                capture,
                associate(capture, reads),
                FirstStationReport.from_result(1, mixed_result(5)),
            )
        )
    elif kind == "mixed_layout":
        rows[-1] = (
            *rows[-1][:2],
            FirstStationReport.from_result(1, mixed_result(4, camera_count=5)),
        )
    elif kind == "mixed_generation":
        rows[-1] = (*rows[-1][:2], FirstStationReport.from_result(2, mixed_result(4)))
    else:
        rows[-1] = (*rows[-1][:2], rows[0][2])
    result = merge_batches(tuple(rows))
    assert not result.batches and result.diagnostics


def test_ordinal_groups_shift_after_a_wholly_unobserved_trigger() -> None:
    # ImageNumber counts acquired frames; a trigger missed by every camera need
    # not make an SDK counter gap. Fourth observed cycle is already batch 8.
    true_values = (7, 7, 7, 8, 8, 8, 8)
    guessed_groups = tuple((ordinal - 1) // 4 for ordinal in range(1, 8))
    assert len(set(true_values[:4])) == 2 and guessed_groups[:4] == (0,) * 4
    reads = (*reads_for(7), *reads_for(8, 400))
    rows: list[tuple[Capture, Association, FirstStationReport | None]] = []
    for index, trigger in enumerate((0, 60, 120, 400, 460, 520, 580)):
        capture = Capture(index + 1, trigger + 5, trigger + 25)
        rows.append(
            (
                capture,
                associate(capture, reads),
                FirstStationReport.from_result(1, mixed_result(index + 1)),
            )
        )
    result = merge_batches(tuple(rows))
    assert [b.w48 for b in result.batches] == [8]
    assert result.batches[0].ordinals == (4, 5, 6, 7)
    assert "W48=7" in result.diagnostics[0]


def test_wrap_skip_and_repeated_value_have_distinct_observed_epochs() -> None:
    values = (65535, 0, 2, 65535)
    reads = tuple(r for i, v in enumerate(values) for r in reads_for(v, i * 400))
    rows: list[tuple[Capture, Association, FirstStationReport | None]] = []
    for i in range(16):
        at = (i // 4) * 400 + (i % 4) * 60
        capture = Capture(i + 1, at + 5, at + 25)
        rows.append(
            (
                capture,
                associate(capture, reads),
                FirstStationReport.from_result(1, mixed_result(i + 1)),
            )
        )
    result = merge_batches(tuple(rows))
    assert not result.diagnostics
    assert tuple(b.w48 for b in result.batches) == values  # Never invent skipped 1.
    assert tuple(b.epoch for b in result.batches) == (0, 1, 2, 3)
    for batch in result.batches:
        submission = simulate_submission(batch, batch.ready_ms + 100)
        assert submission.attempted_writes[-1][1] == (batch.w48,)


@pytest.mark.parametrize("status", [CycleStatus.TIMED_OUT, CycleStatus.FAILED])
def test_attributable_partial_terminal_cycle_still_contributes(
    status: CycleStatus,
) -> None:
    rows = list(batch_rows())
    result = mixed_result(4)
    result = replace(
        result,
        cycle_status=status,
        cameras=(
            replace(result.cameras[0], frame_id=None, image_shape=None),
            *result.cameras[1:],
        ),
    )
    rows[-1] = (*rows[-1][:2], FirstStationReport.from_result(1, result))
    batch = merge_batches(tuple(rows)).batches[0]
    assert batch.reject_words == (0xEFBF, 0xBEFB, 15)
    assert len(batch.ordinals) == 4


@pytest.mark.parametrize("camera_count", [5, 6])
def test_fast_burst_and_serial_backlog_deadlines(camera_count: int) -> None:
    timing = simulate_burst(camera_count, 10)
    assert timing.terminal_ms == tuple(t + camera_count * 10 for t in (0, 60, 120, 180))
    assert timing.completed_cameras == (camera_count,) * 4 and not timing.expired
    slow = simulate_burst(camera_count, 20)
    assert slow.terminal_ms == (100, 160, 220, 280)
    assert slow.completed_cameras == (4, 2, 2, 2) and slow.expired == (1, 2, 3, 4)
    assert slow.late_native_results == 4 and slow.peak_cycles <= 2
    assert slow.peak_queued_cameras <= 2 * camera_count
    overdue = simulate_burst(
        camera_count, 450, triggers=(0, 60, 120, 180, 380, 440, 500, 560)
    )
    assert overdue.completed_cameras == (0,) * 8
    assert overdue.native_finished_ms == 900 and overdue.late_native_results == 2
    overflow = simulate_burst(camera_count, 450, triggers=(0, 10, 20, 30, 40))
    assert overflow.rejected == (5,) and overflow.peak_cycles == 4


def test_exact_report_deadlines_transport_failure_and_no_consumption_claim() -> None:
    batch = BatchProjection(0, 65535, (1, 2, 3, 4), 260, (1, 2, 3))
    success = simulate_submission(batch, 380)
    assert success.attempted_writes == ((50, (1, 2, 3), 260), (49, (65535,), 270))
    assert success.outcome == "transport_acknowledged_consumption_unknown"
    assert success.plc_consumed is None
    assert (
        simulate_submission(batch, 380, available_ms=380).outcome == "report_owner_busy"
    )
    assert simulate_submission(replace(batch, ready_ms=380), 380).attempted_writes == ()
    assert (
        simulate_submission(batch, 380, bitmap_ms=120).outcome
        == "late_bitmap_no_commit"
    )
    assert (
        simulate_submission(batch, 380, bitmap_ms=100, commit_ms=20).outcome
        == "late_commit"
    )
    for stage, count in (("bitmap", 1), ("commit", 2)):
        # Separate calls are experiments, not a retry of the prior uncertain write.
        failed = simulate_submission(
            batch, 380, fail="bitmap" if stage == "bitmap" else "commit"
        )
        assert failed.outcome == f"{stage}_failed_execution_uncertain"
        assert len(failed.attempted_writes) == count and failed.plc_consumed is None
    delayed = simulate_burst(6, 10, arrival_delay_ms=250)
    assert delayed.terminal_ms[-1] == 490


def test_equal_reads_do_not_prove_absence_of_hidden_counter_reuse() -> None:
    updates = (CounterUpdate(-10, 7), CounterUpdate(0, 8), CounterUpdate(10, 7))
    reads = (read_counter(updates, -5, 0, 0), read_counter(updates, 15, 0, 0))
    capture = Capture(1, 5, 25)
    assert reads[0].value == reads[1].value == 7 and counter_at(updates, 3) == 8
    assert (
        bracket_association(
            capture,
            reads,
            frame_delay_bound_ms=5,
            common_trigger_verified=True,
        ).value
        is None
    )
    observed_transition = (reads[0], CounterRead(1, 2, 8), reads[1])
    assert (
        associate(capture, observed_transition).reason
        == "counter_transition_in_capture_window"
    )


def test_late_read_completion_delays_batch_even_when_all_inspection_is_ready() -> None:
    reads = (*reads_for()[:-1], CounterRead(185, 365, 7))
    rows = tuple((c, associate(c, reads), r) for c, _, r in batch_rows())
    batch = merge_batches(rows).batches[0]
    assert batch.ready_ms == 365
    assert simulate_submission(batch, 370).outcome == "late_bitmap_no_commit"


@pytest.mark.parametrize(
    "cameras,cost,complete",
    [(5, 13, True), (5, 14, False), (6, 11, True), (6, 12, False)],
)
def test_four_trigger_burst_has_stricter_last_cycle_deadline(
    cameras: int,
    cost: int,
    complete: bool,
) -> None:
    timing = simulate_burst(cameras, cost)
    assert (timing.completed_cameras == (cameras,) * 4) == complete
    assert bool(timing.expired) != complete


def test_counter_reuse_can_match_stale_w49_before_any_new_batch_report() -> None:
    old = BatchProjection(0, 0, (1, 2, 3, 4), 205, (0, 0, 0))
    w49 = simulate_submission(old, 370).attempted_writes[-1][1][0]
    updates = (CounterUpdate(-10, 0), CounterUpdate(370, 65535), CounterUpdate(770, 0))
    # The intermediate batch never produced a report. W49 therefore remains 0.
    assert w49 == counter_at(updates, 800) == 0
    reads = (*reads_for(0), *reads_for(65535, 400), *reads_for(0, 800))
    rows: list[tuple[Capture, Association, FirstStationReport | None]] = []
    for i in range(3):
        capture = Capture(i + 9, 805 + i * 60, 825 + i * 60)
        rows.append(
            (
                capture,
                associate(capture, reads),
                FirstStationReport.from_result(1, mixed_result(i + 9)),
            )
        )
    result = merge_batches(tuple(rows))
    assert not result.batches and result.diagnostics
    # Numeric W49 == W48 was already true, despite no fresh complete result.
