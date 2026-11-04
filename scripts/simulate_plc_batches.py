"""Offline W48/W49 proposal experiments; never opens a PLC or production owner.

Integer milliseconds are illustrative simulated time, not measured throughput.
The conditional association requires external guarantees not supplied by Production.
"""

import argparse
import heapq
import json
from collections import Counter, deque
from dataclasses import asdict, dataclass
from typing import Literal

from capsule_vision.communication.reporting import FirstStationReport, ReportIdentity
from capsule_vision.inspection import CycleId
from capsule_vision.inspection.production import ChannelBusinessStatus, ChannelId


@dataclass(frozen=True)
class CounterUpdate:
    at_ms: int
    value: int


@dataclass(frozen=True)
class CounterRead:
    requested_ms: int
    replied_ms: int
    value: int


def counter_at(updates: tuple[CounterUpdate, ...], at_ms: int) -> int:
    """Simulation oracle only: the application cannot observe this timeline."""
    if not updates or any(not 0 <= u.value <= 65535 for u in updates):
        raise ValueError("A uint16 counter timeline is required")
    if any(a.at_ms >= b.at_ms for a, b in zip(updates, updates[1:], strict=False)):
        raise ValueError("Counter transitions must be strictly ordered")
    earlier = [u for u in updates if u.at_ms <= at_ms]
    if not earlier:
        raise ValueError("Counter has no initial observation")
    return earlier[-1].value


def read_counter(
    updates: tuple[CounterUpdate, ...], request: int, latency: int, sample_after: int
) -> CounterRead:
    if not 0 <= sample_after <= latency:
        raise ValueError("The hidden sample must lie inside the request interval")
    return CounterRead(
        request, request + latency, counter_at(updates, request + sample_after)
    )


@dataclass(frozen=True)
class Capture:
    ordinal: int
    arrived_ms: int
    terminal_ms: int
    source_attributable: bool = True


def naive_association(
    capture: Capture,
    reads: tuple[CounterRead, ...],
    strategy: Literal["latest_on_arrival", "read_on_arrival", "after_inspection"],
) -> int | None:
    """Deliberately unproven candidates, used only to demonstrate counterexamples."""
    if strategy == "latest_on_arrival":
        candidates = [r for r in reads if r.replied_ms <= capture.arrived_ms]
        return max(candidates, key=lambda r: r.replied_ms).value if candidates else None
    after = capture.arrived_ms if strategy == "read_on_arrival" else capture.terminal_ms
    candidates = [r for r in reads if r.requested_ms >= after]
    return min(candidates, key=lambda r: r.requested_ms).value if candidates else None


@dataclass(frozen=True)
class Association:
    epoch: int | None
    value: int | None
    ready_ms: int
    reason: str


def bracket_association(
    capture: Capture,
    reads: tuple[CounterRead, ...],
    *,
    frame_delay_bound_ms: int | None = None,
    no_counter_reuse: bool = False,
    common_trigger_verified: bool = False,
) -> Association:
    """Conditional proof using serial read intervals, never hidden sample times.

    A left reply precedes the earliest possible trigger; a right request follows
    arrival. Equal, non-reused W48 values bracket the entire uncertainty interval.
    Common-trigger verification is still required for the camera group itself.
    """
    if capture.ordinal < 1 or capture.terminal_ms < capture.arrived_ms:
        raise ValueError("Invalid observed capture")
    if frame_delay_bound_ms is not None and frame_delay_bound_ms < 0:
        raise ValueError("Negative frame delay bound")
    if any(r.requested_ms > r.replied_ms or not 0 <= r.value <= 65535 for r in reads):
        raise ValueError("Invalid W48 read")
    if any(
        a.replied_ms > b.requested_ms for a, b in zip(reads, reads[1:], strict=False)
    ):
        raise ValueError("This experiment requires serial, ordered reads")
    if not capture.source_attributable:
        return Association(None, None, capture.terminal_ms, "source_ambiguity")
    if (
        frame_delay_bound_ms is None
        or not no_counter_reuse
        or not common_trigger_verified
    ):
        return Association(
            None, None, capture.terminal_ms, "missing_external_guarantee"
        )
    epochs: list[int] = []
    epoch = 0
    for index, read in enumerate(reads):
        if index and read.value != reads[index - 1].value:
            epoch += 1
        epochs.append(epoch)
    left = [
        i
        for i, r in enumerate(reads)
        if r.replied_ms <= capture.arrived_ms - frame_delay_bound_ms
    ]
    right = [i for i, r in enumerate(reads) if r.requested_ms >= capture.arrived_ms]
    if not left or not right:
        return Association(None, None, capture.terminal_ms, "unbracketed_capture")
    lo, hi = left[-1], right[0]
    ready = max(capture.terminal_ms, reads[hi].replied_ms)
    if epochs[lo] != epochs[hi]:
        return Association(None, None, ready, "counter_transition_in_capture_window")
    return Association(epochs[lo], reads[lo].value, ready, "conditional_bracket")


@dataclass(frozen=True)
class BatchProjection:
    epoch: int
    w48: int
    ordinals: tuple[int, ...]
    ready_ms: int
    reject_words: tuple[int, ...]

    @property
    def proposed_w50_w52(self) -> tuple[int, ...]:
        # Proposal only: fixed three-word mailbox, unlike today's two-word CH30 write.
        return (
            (*self.reject_words, 0)
            if len(self.reject_words) == 2
            else self.reject_words
        )


@dataclass(frozen=True)
class Aggregation:
    batches: tuple[BatchProjection, ...]
    diagnostics: tuple[str, ...]


def merge_batches(
    rows: tuple[tuple[Capture, Association, FirstStationReport | None], ...],
) -> Aggregation:
    """Finite experiment, not a streaming production accumulator or retry owner."""
    if len(rows) > 64:
        raise ValueError("Experiment limited to 64 observed cycles")
    groups: dict[
        tuple[int, int], list[tuple[Capture, Association, FirstStationReport | None]]
    ] = {}
    diagnostics: list[str] = []
    identities = Counter(r.identity for _, _, r in rows if r is not None)
    for capture, association, report in rows:
        if association.epoch is None or association.value is None:
            diagnostics.append(f"cycle {capture.ordinal}: {association.reason}")
            continue
        groups.setdefault((association.epoch, association.value), []).append(
            (capture, association, report)
        )
    if diagnostics:
        # Without a proven boundary an unattributed capture could belong to any
        # candidate in this finite trace. Do not quietly ignore it and report four.
        return Aggregation((), tuple(diagnostics))
    batches: list[BatchProjection] = []
    for (epoch, value), members in groups.items():
        ordinals = tuple(sorted(c.ordinal for c, _, _ in members))
        reports = [r for _, _, r in members if r is not None]
        valid = (
            len(members) == len(set(ordinals)) == len(reports) == 4
            and all(c.source_attributable for c, _, _ in members)
            and all(
                r is not None and r.identity.cycle_id == c.ordinal
                for c, _, r in members
            )
            and len({r.identity.generation for r in reports}) == 1
            and len({len(r.reject_words) for r in reports}) == 1
            and all(identities[r.identity] == 1 for r in reports)
        )
        if not valid:
            diagnostics.append(
                f"epoch {epoch} W48={value}: incomplete_or_invalid {ordinals}"
            )
            continue
        words = [0] * len(reports[0].reject_words)
        for report in reports:
            for index, word in enumerate(report.reject_words):
                words[index] |= word
        batches.append(
            BatchProjection(
                epoch,
                value,
                ordinals,
                max(max(c.terminal_ms, a.ready_ms) for c, a, _ in members),
                tuple(words),
            )
        )
    return Aggregation(tuple(batches), tuple(diagnostics))


@dataclass(frozen=True)
class Submission:
    outcome: str
    attempted_writes: tuple[tuple[int, tuple[int, ...], int], ...]
    finished_ms: int
    plc_consumed: None = None


def simulate_submission(
    batch: BatchProjection,
    next_update_ms: int,
    *,
    bitmap_ms: int = 10,
    commit_ms: int = 10,
    fail: Literal["bitmap", "commit"] | None = None,
    available_ms: int | None = None,
) -> Submission:
    """Record hypothetical attempts only. ACK never establishes PLC consumption."""
    if min(bitmap_ms, commit_ms) < 0:
        raise ValueError("Negative transport duration")
    start = (
        max(batch.ready_ms, available_ms)
        if available_ms is not None
        else batch.ready_ms
    )
    if start >= next_update_ms:
        return Submission(
            "not_ready_before_update"
            if batch.ready_ms >= next_update_ms
            else "report_owner_busy",
            (),
            start,
        )
    writes = ((50, batch.proposed_w50_w52, start),)
    bitmap_end = start + bitmap_ms
    if fail == "bitmap":
        return Submission("bitmap_failed_execution_uncertain", writes, bitmap_end)
    if bitmap_end >= next_update_ms:
        return Submission("late_bitmap_no_commit", writes, bitmap_end)
    writes += ((49, (batch.w48,), bitmap_end),)
    end = bitmap_end + commit_ms
    outcome = (
        "commit_failed_execution_uncertain"
        if fail == "commit"
        else "late_commit"
        if end >= next_update_ms
        else "transport_acknowledged_consumption_unknown"
    )
    return Submission(outcome, writes, end)


@dataclass(frozen=True)
class BurstTiming:
    terminal_ms: tuple[int | None, ...]
    completed_cameras: tuple[int, ...]
    expired: tuple[int, ...]
    rejected: tuple[int, ...]
    peak_cycles: int
    peak_queued_cameras: int
    late_native_results: int
    native_finished_ms: int


def simulate_burst(
    camera_count: int,
    camera_ms: int,
    *,
    triggers: tuple[int, ...] = (0, 60, 120, 180),
    arrival_delay_ms: int = 0,
    timeout_ms: int = 100,
    capacity: int = 4,
) -> BurstTiming:
    """Bounded FIFO timing model of early inspection, corroborated by runtime tests.

    All cameras arrive together here. Expiration precedes completion at equality;
    an active native call survives expiration, queued expired work is discarded.
    This does not model OS scheduling, source continuity, image copying or GPU work.
    """
    if camera_count not in (5, 6) or not 1 <= len(triggers) <= 64:
        raise ValueError("Use 5/6 cameras and 1..64 captures")
    if min(camera_ms, arrival_delay_ms) < 0 or min(timeout_ms, capacity) < 1:
        raise ValueError("Invalid duration/capacity")
    if tuple(sorted(triggers)) != triggers:
        raise ValueError("Trigger times must be ordered")
    # Priority: deadline, native completion, arrival. No real-time sleeps or threads.
    events = [(t + arrival_delay_ms, 2, i) for i, t in enumerate(triggers)]
    heapq.heapify(events)
    live: set[int] = set()
    queue: deque[int] = deque()
    terminal: list[int | None] = [None] * len(triggers)
    completed = [0] * len(triggers)
    expired: list[int] = []
    rejected: list[int] = []
    active: int | None = None
    peak_cycles = peak_jobs = late = native_end = 0
    while events:
        now, kind, cycle = heapq.heappop(events)
        if kind == 2:
            if len(live) == capacity:
                rejected.append(cycle + 1)
            else:
                live.add(cycle)
                heapq.heappush(events, (now + timeout_ms, 0, cycle))
                queue.extend([cycle] * camera_count)
                peak_cycles = max(peak_cycles, len(live))
                peak_jobs = max(peak_jobs, len(queue))
        elif kind == 0 and cycle in live:
            live.remove(cycle)
            terminal[cycle] = now
            expired.append(cycle + 1)
        elif kind == 1:
            active = None
            native_end = now
            if cycle not in live:
                late += 1
            else:
                completed[cycle] += 1
                if completed[cycle] == camera_count:
                    live.remove(cycle)
                    terminal[cycle] = now
        queue = deque(c for c in queue if c in live)
        if active is None and queue:
            active = queue.popleft()
            heapq.heappush(events, (now + camera_ms, 1, active))
    return BurstTiming(
        tuple(terminal),
        tuple(completed),
        tuple(expired),
        tuple(rejected),
        peak_cycles,
        peak_jobs,
        late,
        native_end,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    worlds = (
        (CounterUpdate(-600, 7), CounterUpdate(190, 8)),
        (CounterUpdate(-600, 7), CounterUpdate(-10, 8)),
    )
    ambiguity = [
        {
            "reads": [asdict(read_counter(world, t, 0, 0)) for t in (-220, 220)],
            "true_batches": [counter_at(world, t) for t in (0, 60, 120, 180)],
        }
        for world in worlds
    ]
    projections: list[dict[str, object]] = []
    for channels in (30, 36):
        rows: list[tuple[Capture, Association, FirstStationReport | None]] = []
        reads = (
            CounterRead(-10, -6, 7),
            *(CounterRead(t + 5, t + 9, 7) for t in (0, 60, 120, 180)),
        )
        for i in range(4):
            capture = Capture(i + 1, i * 60 + 5, i * 60 + 25)
            association = bracket_association(
                capture,
                reads,
                frame_delay_bound_ms=5,
                no_counter_reuse=True,
                common_trigger_verified=True,
            )
            # These CLI fixtures have no EMPTY evidence. Evidence-rich projection
            # cases use FirstStationReport.from_result in the companion tests.
            report = FirstStationReport(
                ReportIdentity(1, CycleId(i + 1)),
                tuple(
                    (
                        ChannelId(c),
                        ChannelBusinessStatus.NG
                        if c == i + 1
                        else ChannelBusinessStatus.OK,
                    )
                    for c in range(1, channels + 1)
                ),
            )
            rows.append((capture, association, report))
        batch = merge_batches(tuple(rows)).batches[0]
        projections.append(
            {
                "channels": channels,
                "batch": asdict(batch),
                "submission": asdict(simulate_submission(batch, 370)),
            }
        )
    print(
        json.dumps(
            {
                "production_changed": False,
                "comm_m1": "blocked_pending_external_attribution_and_timing_guarantees",
                "indistinguishable_observations": ambiguity,
                "capture_times_ms": (0, 60, 120, 180),
                "conditional_projection_examples": projections,
                "timing_ms": [
                    {
                        "cameras": n,
                        "per_camera_ms": cost,
                        **asdict(simulate_burst(n, cost)),
                    }
                    for n in (5, 6)
                    for cost in (5, 10, 15, 20, 450)
                ],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
