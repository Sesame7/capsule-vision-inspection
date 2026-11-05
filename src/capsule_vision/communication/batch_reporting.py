"""Attributed four-cycle reporting with startup counter sampling on one socket owner."""

from collections import deque
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field
from threading import Condition, Thread, current_thread
from time import perf_counter

from capsule_vision.inspection import CycleId, CycleStatus

from .endpoints import Endpoint, open_client
from .fins import FinsMemoryArea
from .fins_tcp import FinsTcpClient
from .reporting import (
    BatchReportObservation,
    CompletedReportingBatch,
    CounterRead,
    FirstStationReport,
    ReportIdentity,
    ReportingCapacityError,
    ReportingGroupProgress,
    ReportingIssue,
    ReportingMode,
    ReportingSettings,
    ReportingSnapshot,
    ReportingStateError,
    ReportObservation,
    ReportOutcome,
    ReportStage,
)


@dataclass
class _Batch:
    cycles: list[CycleId] = field(default_factory=list[CycleId])
    admitted_at: float = field(default_factory=perf_counter)
    reports: dict[CycleId, FirstStationReport] = field(
        default_factory=dict[CycleId, FirstStationReport]
    )
    terminals: dict[CycleId, str] = field(default_factory=dict[CycleId, str])
    futures: list[Future[ReportObservation]] = field(
        default_factory=list[Future[ReportObservation]]
    )
    reads: list[CounterRead] = field(default_factory=list[CounterRead])
    reason: str | None = None
    completed_at: float | None = None
    commit_started_at: float | None = None
    commit_finished_at: float | None = None
    commit_uncertain: bool = False
    finished: bool = False
    inspection_complete: bool = False

    def observation(
        self, report: ReportObservation | None = None
    ) -> BatchReportObservation:
        return BatchReportObservation(
            tuple(self.cycles),
            tuple(sorted(self.terminals.items())),
            self.admitted_at,
            tuple(self.reads),
            report,
            self.reason,
            self.completed_at,
            self.commit_started_at,
            self.commit_finished_at,
            self.commit_uncertain,
            self.inspection_complete,
        )


def _open_four_cycle_client(endpoint: Endpoint) -> FinsTcpClient:
    client = open_client(endpoint)
    assert isinstance(client, FinsTcpClient)
    return client


class FourCycleReportingWorker:
    """Admission only copies scalar identities; this thread owns every socket call.

    A lost association latches for this generation. Subsequent cycles remain
    inspectable, but produce local diagnostics until a fresh Start synchronizes.
    """

    def __init__(
        self,
        generation: int,
        settings: ReportingSettings,
        *,
        client_factory: Callable[[Endpoint], FinsTcpClient] = _open_four_cycle_client,
        attributable: Callable[[ReportIdentity], bool] = lambda _: True,
        on_completed: Callable[[CompletedReportingBatch], None] = lambda _: None,
    ) -> None:
        ReportIdentity(generation, CycleId(0))
        settings.validate_four_cycle()
        if settings.mode is ReportingMode.PER_CYCLE:
            raise ValueError("Four-cycle worker requires four_cycle mode")
        self.generation, self.settings = generation, settings
        self._base = settings.require_base_address()
        self._factory, self._attributable = client_factory, attributable
        self._on_completed = on_completed
        self._condition = Condition()
        self._batches: deque[_Batch] = deque()
        self._recent: deque[BatchReportObservation] = deque(maxlen=16)
        self._last_cycle: CycleId | None = None
        self._last_counter: int | None = None
        self._source_frames: int | None = None
        self._candidate_frames: int | None = None
        self._startup_read: CounterRead | None = None
        self._initial_counter: int | None = None
        self._synchronized = False
        self._phase_issue: str | None = None
        self._progress: ReportingGroupProgress | None = None
        self._started = self._stopping = self._drain = False
        self._client: FinsTcpClient | None = None
        self._cleanup_issue: ReportingIssue | None = None
        self._failure_issue: ReportingIssue | None = None
        self._queued = self._attempted = self._acknowledged = 0
        self._failed = self._skipped = self._peak = 0
        self._last_queued: ReportIdentity | None = None
        self._last_attempt: ReportObservation | None = None
        self._last_ack: ReportObservation | None = None
        self._last_fail: ReportObservation | None = None
        self._last_skip: ReportObservation | None = None
        self._last_outcome: ReportObservation | None = None
        self._thread = Thread(
            target=self._run, name=f"FourCycleReporting-{generation}", daemon=False
        )

    @property
    def thread_name(self) -> str:
        return self._thread.name

    @property
    def cleanup_safe(self) -> bool:
        with self._condition:
            return not self._thread.is_alive() and self._cleanup_issue is None

    def start(self) -> None:
        with self._condition:
            if self._started or self._stopping:
                raise ReportingStateError("Reporting worker has one lifecycle")
            self._started = True
            self._thread.start()

    def snapshot(self) -> ReportingSnapshot:
        with self._condition:
            return ReportingSnapshot(
                generation=self.generation,
                accepting=self._started
                and not self._stopping
                and self._phase_issue is None,
                thread_alive=self._thread.is_alive(),
                pending=max(0, len(self._batches) - 1),
                peak_pending=self._peak,
                active=self._identity(self._batches[0]) if self._batches else None,
                queued_count=self._queued,
                attempted_count=self._attempted,
                acknowledged_count=self._acknowledged,
                failed_count=self._failed,
                skipped_count=self._skipped,
                rejected_count=0,
                next_sequence=None,
                last_queued=self._last_queued,
                last_attempted=self._last_attempt,
                last_acknowledged=self._last_ack,
                last_outcome=self._last_outcome,
                last_failed=self._last_fail,
                last_skipped=self._last_skip,
                last_rejected=None,
                failure_issue=self._failure_issue,
                cleanup_issue=self._cleanup_issue,
                mode=self.settings.mode,
                phase_issue=self._phase_issue,
                batch_observations=(
                    *self._recent,
                    *(batch.observation() for batch in self._batches),
                ),
                group_progress=self._progress,
                waiting_for_sync=self._source_frames is not None
                and not (self._synchronized or self._stopping or self._phase_issue),
                startup_read=self._startup_read,
                initial_counter=self._initial_counter,
            )

    def startup_boundary(self, frames: int, clean: Callable[[], bool]) -> bool:
        """SDK owner only, between retrieval sweeps in RUNNING. True ends sampling."""
        with self._condition:
            if self._stopping or self._phase_issue or self._synchronized:
                return True
            first = self._source_frames is None
            self._source_frames = frames
            candidate = self._candidate_frames
            if first:
                self._condition.notify_all()
            if candidate is None:
                return False
        # Only the SDK owner checks partial ordinals and actual SDK queues. The
        # PLC owner waits for this candidate's decision, without holding a lock.
        aligned = candidate == frames and clean()
        with self._condition:
            self._synchronized = aligned and not self._stopping
            self._candidate_frames = None
            self._condition.notify_all()
            return self._synchronized or self._stopping

    def stop_startup_wait(self) -> None:
        with self._condition:
            if not self._synchronized:
                self.request_stop()

    def is_fourth_cycle(self, cycle: CycleId) -> bool:
        """Display admission uses this owner's membership, never CycleId modulo."""
        with self._condition:
            return any(
                len(batch.cycles) == 4
                and batch.cycles[-1] == cycle
                and batch.reason is None
                for batch in self._batches
            )

    def _publish_progress(self, batch: _Batch) -> None:
        # Admission alone must not erase the preceding group's lamps.
        if not batch.terminals:
            return
        if (
            self._progress is not None
            and self._progress.identity.cycle_id > batch.cycles[0]
        ):
            return
        self._progress = ReportingGroupProgress(
            self._identity(batch),
            tuple(batch.cycles),
            tuple(batch.reports.get(cycle) for cycle in batch.cycles),
            eligible=batch.inspection_complete,
            invalidated=batch.reason is not None and not batch.inspection_complete,
        )

    def _identity(self, batch: _Batch) -> ReportIdentity:
        return ReportIdentity(self.generation, batch.cycles[0])

    def _lose_phase(self, reason: str) -> None:
        # Caller holds the condition. Never silently recover or reassign the counter.
        self._phase_issue = self._phase_issue or reason
        for batch in self._batches:
            if not batch.finished:
                batch.reason = batch.reason or reason
                self._publish_progress(batch)
        self._condition.notify_all()

    def admit_cycle(self, cycle: CycleId) -> bool:
        ReportIdentity(self.generation, cycle)
        with self._condition:
            if self._started and not self._synchronized:
                return False  # Inspection retains this Cycle; reporting owns no token.
            if not self._started or self._stopping:
                raise ReportingStateError("Four-cycle owner is not accepting cycles")
            if self._last_cycle is not None and cycle != self._last_cycle + 1:
                self._lose_phase("Nonconsecutive or duplicate Basler CycleId")
                if cycle <= self._last_cycle:
                    raise ReportingStateError(self._phase_issue)
            if (
                sum(len(batch.cycles) for batch in self._batches)
                >= self.settings.max_pending + 1
            ):
                self._lose_phase("Four-cycle reporting capacity exceeded")
                raise ReportingCapacityError(self._phase_issue)
            if not self._batches or len(self._batches[-1].cycles) == 4:
                if any(not batch.finished for batch in self._batches):
                    self._lose_phase(
                        "Next batch admitted before previous batch settled; late report suppressed"
                    )
                self._batches.append(
                    _Batch(admitted_at=perf_counter(), reason=self._phase_issue)
                )
                self._queued += 1
                self._last_queued = ReportIdentity(self.generation, cycle)
                self._peak = max(self._peak, len(self._batches) - 1)
            batch = self._batches[-1]
            batch.cycles.append(cycle)
            self._last_cycle = cycle
            self._condition.notify_all()
            return True

    def submit_cycle(
        self, report: FirstStationReport, status: CycleStatus
    ) -> Future[ReportObservation]:
        completed: Future[ReportObservation] = Future()
        completed.set_running_or_notify_cancel()
        with self._condition:
            if report.identity.generation != self.generation:
                self._lose_phase("Mixed reporting generation")
                raise ReportingStateError("Mixed reporting generation")
            batch = next(
                (b for b in self._batches if report.identity.cycle_id in b.cycles), None
            )
            if batch is None:
                self._lose_phase("Terminal result without a pending admission")
                raise ReportingStateError("Terminal result without a pending admission")
            cycle = report.identity.cycle_id
            if cycle in batch.terminals:
                self._lose_phase("Duplicate terminal cycle result")
                raise ReportingStateError("Duplicate terminal cycle result")
            batch.reports[cycle] = report
            batch.terminals[cycle] = status.name
            batch.futures.append(completed)
            if len(batch.reports) == 4:
                batch.completed_at = perf_counter()
            self._publish_progress(batch)
            self._condition.notify_all()
        return completed

    def note_skipped(self, identity: ReportIdentity, error: BaseException) -> None:
        with self._condition:
            batch = next(
                (b for b in self._batches if identity.cycle_id in b.cycles), None
            )
            if batch is not None:
                batch.terminals[identity.cycle_id] = "unavailable"
            self._lose_phase(
                f"Unattributable/unavailable terminal result: {str(error)[:300]}"
            )

    def request_stop(self, *, drain: bool = False) -> None:
        with self._condition:
            self._stopping = True
            self._drain = drain
            self._condition.notify_all()

    def join(self, timeout: float | None = None) -> bool:
        if current_thread() is self._thread:
            raise RuntimeError("Reporting owner cannot join itself")
        if self._thread.ident is not None:
            self._thread.join(timeout)
        return not self._thread.is_alive()

    def _read(self, batch: _Batch | None, stage: str) -> int:
        started = perf_counter()
        value: int | None = None
        try:
            if self._client is None:
                self._client = self._factory(self.settings.endpoint)
            value = self._client.read_words(self._base - 1, 1, area=FinsMemoryArea.W)[0]
            return value
        finally:
            with self._condition:
                read = CounterRead(
                    stage, started, perf_counter(), value, self._base - 1
                )
                if batch is None:
                    self._startup_read = read
                    if self._initial_counter is None:
                        self._initial_counter = value
                else:
                    batch.reads.append(read)

    def _synchronize(self) -> None:
        # A clean SDK sweep after a changed value alone could be *after* the
        # first exposure. Fence from before the preceding (old-value) request.
        previous_value = previous_frames = None
        with self._condition:
            self._condition.wait_for(
                lambda: self._source_frames is not None or self._stopping
            )
        while True:
            with self._condition:
                if self._stopping:
                    return
                frames = self._source_frames
            try:
                value = self._read(None, "startup")
            except Exception as exc:
                with self._condition:
                    self._failure_issue = ReportingIssue.from_error(exc)
                    self._lose_phase(
                        f"PLC startup counter read failed: {str(exc)[:300]}"
                    )
                return
            with self._condition:
                if previous_value is not None and value != previous_value:
                    self._candidate_frames = previous_frames
                    self._condition.wait_for(
                        lambda: self._candidate_frames is None or self._stopping
                    )
                    if self._synchronized or self._stopping:
                        return
                previous_value, previous_frames = value, frames
                if self._condition.wait_for(lambda: self._stopping, timeout=0.010):
                    return

    def _check(self, batch: _Batch, retained: int) -> None:
        if any(
            not self._attributable(ReportIdentity(self.generation, c))
            for c in batch.cycles
        ):
            with self._condition:
                self._lose_phase("Ambiguous Basler attribution")
        with self._condition:
            if batch.reason is not None:
                raise ReportingStateError(batch.reason)
            if self._stopping and not self._drain:
                raise ReportingStateError("Stopped before batch submission")
        if self._read(batch, "confirm") != retained:
            with self._condition:
                self._lose_phase(
                    "PLC counter changed before commit; retained group suppressed"
                )
            raise ReportingStateError("PLC counter changed before commit")
        # Admission can change while a read is in flight.
        with self._condition:
            if batch.reason is not None:
                raise ReportingStateError(batch.reason)
        if any(
            not self._attributable(ReportIdentity(self.generation, c))
            for c in batch.cycles
        ):
            with self._condition:
                self._lose_phase("Ambiguous Basler attribution")
            raise ReportingStateError("Ambiguous Basler attribution")

    def _attempt(self, batch: _Batch) -> ReportObservation:
        identity = self._identity(batch)
        retained: int | None = None
        words: tuple[int, int] | tuple[int, int, int] | None = None
        stage = ReportStage.INITIALIZATION
        bitmap_ms: float | None = None
        sequence_ms: float | None = None
        try:
            with self._condition:
                initial_allowed = batch.reason is None and not self._stopping
            if initial_allowed:
                try:
                    retained = self._read(batch, "admission")
                except Exception as exc:
                    # Keep admitted cycle handoffs alive even when the first read
                    # fails, so diagnostics include their terminal outcomes.
                    with self._condition:
                        self._failure_issue = ReportingIssue.from_error(exc)
                        self._lose_phase(
                            f"PLC counter admission read failed: {str(exc)[:300]}"
                        )
                with self._condition:
                    if len(batch.cycles) > 1:
                        self._lose_phase(
                            "Initial counter read completed after second cycle admission"
                        )
                    if retained == self._last_counter:
                        self._lose_phase("Stale PLC counter at next group admission")
                    if (
                        self._last_counter is None
                        and self._startup_read
                        and retained != self._startup_read.value
                    ):
                        self._lose_phase("PLC counter changed after startup alignment")
                    self._last_counter = retained
            with self._condition:
                self._condition.wait_for(
                    lambda: len(batch.terminals) == 4 or self._stopping
                )
                if len(batch.cycles) != 4 or len(batch.terminals) != 4:
                    batch.reason = (
                        batch.reason or "Interrupted/incomplete four-cycle batch"
                    )
                if batch.reason is not None:
                    raise ReportingStateError(batch.reason)
                if retained is None:
                    raise ReportingStateError("No admission counter observation")
                reports = tuple(batch.reports[c] for c in batch.cycles)
            if len({len(r.reject_words) for r in reports}) != 1:
                with self._condition:
                    self._lose_phase("Mixed channel layouts")
                raise ReportingStateError("Mixed channel layouts")
            merged = tuple(
                a | b | c | d
                for a, b, c, d in zip(*(r.reject_words for r in reports), strict=True)
            )
            words = (
                (merged[0], merged[1])
                if len(merged) == 2
                else (merged[0], merged[1], merged[2])
            )
            self._check(batch, retained)
            assert batch.completed_at is not None
            completion = CompletedReportingBatch(
                identity,
                retained,
                reports,
                words,
                (batch.completed_at - batch.admitted_at) * 1000,
            )
            with self._condition:
                if batch.reason is not None:
                    raise ReportingStateError(batch.reason)
                batch.inspection_complete = True
                self._publish_progress(batch)
            # Application consumes this immutable inspection fact synchronously,
            # outside the reporting lock and before any result write/ACK. Later
            # reporting failure does not retract completed inspection accounting.
            try:
                self._on_completed(completion)
            except BaseException as exc:
                with self._condition:
                    self._failure_issue = ReportingIssue.from_error(exc)
            with self._condition:
                self._attempted += 1
                self._last_attempt = ReportObservation(
                    identity, ReportOutcome.ATTEMPTED, retained, words
                )
            assert self._client is not None
            stage = ReportStage.BITMAP
            started = perf_counter()
            self._client.write_words(self._base + 1, words, area=FinsMemoryArea.W)
            bitmap_ms = (perf_counter() - started) * 1000
            self._check(batch, retained)  # After bitmap ACK, immediately before commit.
            stage = ReportStage.SEQUENCE
            with self._condition:
                if batch.reason is not None:
                    raise ReportingStateError(batch.reason)
                batch.commit_started_at = perf_counter()
                batch.commit_uncertain = True
            self._client.write_words(self._base, (retained,), area=FinsMemoryArea.W)
            finished = perf_counter()
            attributable = all(
                self._attributable(ReportIdentity(self.generation, cycle))
                for cycle in batch.cycles
            )
            with self._condition:
                batch.commit_finished_at = finished
                if not attributable:
                    self._lose_phase("Basler attribution lost during commit request")
                if batch.reason is not None:
                    raise ReportingStateError(
                        "Commit ACK arrived after association loss; uncertain"
                    )
                batch.commit_uncertain = False
                batch.finished = True
            sequence_ms = (finished - batch.commit_started_at) * 1000
            return ReportObservation(
                identity,
                ReportOutcome.ACKNOWLEDGED,
                retained,
                words,
                duration_ms=(finished - batch.admitted_at) * 1000,
                bitmap_ms=bitmap_ms,
                sequence_ms=sequence_ms,
                acknowledged_at=finished,
                completion_to_ack_ms=(finished - batch.completed_at) * 1000
                if batch.completed_at
                else None,
            )
        except Exception as exc:
            with self._condition:
                batch.reason = batch.reason or str(exc)[:300]
                if stage is ReportStage.INITIALIZATION:
                    self._lose_phase(batch.reason)
                if (
                    batch.commit_started_at is not None
                    and batch.commit_finished_at is None
                ):
                    batch.commit_finished_at = perf_counter()
                issue = ReportingIssue.from_error(exc)
            return ReportObservation(
                identity,
                ReportOutcome.SKIPPED
                if isinstance(exc, ReportingStateError)
                and batch.commit_started_at is None
                else ReportOutcome.FAILED,
                retained,
                words,
                stage,
                issue,
                (perf_counter() - batch.admitted_at) * 1000,
                bitmap_ms=bitmap_ms,
                sequence_ms=sequence_ms,
            )

    def _settle(self, batch: _Batch, observation: ReportObservation) -> None:
        with self._condition:
            batch.finished = True
            self._publish_progress(batch)
            self._last_outcome = observation
            if observation.outcome is ReportOutcome.ACKNOWLEDGED:
                self._acknowledged += 1
                self._last_ack = observation
            elif observation.outcome is ReportOutcome.FAILED:
                self._failed += 1
                self._last_fail = observation
            else:
                self._skipped += 1
                self._last_skip = observation
            self._recent.append(batch.observation(observation))
            self._batches.popleft()
            futures = tuple(batch.futures)
        for future in futures:
            try:
                future.set_result(observation)
            except BaseException as exc:
                # One observer must not strand the other accepted cycle handles.
                # Futures are already terminal before their callbacks run.
                with self._condition:
                    self._failure_issue = ReportingIssue.from_error(exc)
                    self._stopping = True
                    self._drain = False
                    self._condition.notify_all()

    def _run(self) -> None:
        try:
            self._synchronize()
            while True:
                with self._condition:
                    self._condition.wait_for(
                        lambda: bool(self._batches) or self._stopping
                    )
                    if not self._batches:
                        return
                    batch = self._batches[0]
                self._settle(batch, self._attempt(batch))
        except BaseException as exc:
            with self._condition:
                self._failure_issue = ReportingIssue.from_error(exc)
                self._stopping = True
            while self._batches:
                batch = self._batches[0]
                self._settle(
                    batch,
                    ReportObservation(
                        self._identity(batch),
                        ReportOutcome.FAILED,
                        issue=self._failure_issue,
                    ),
                )
        finally:
            if self._client is not None:
                try:
                    self._client.close()
                except BaseException as exc:
                    with self._condition:
                        self._cleanup_issue = ReportingIssue.from_error(exc)
