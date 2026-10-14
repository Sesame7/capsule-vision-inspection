"""Opt-in, localhost-only first-station reporting for one Production generation."""

from collections import deque
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field
from enum import Enum
from threading import Condition, Thread, current_thread
from time import perf_counter
from typing import Protocol, runtime_checkable

from vimo_demo.inspection import CycleId, CycleStatus
from vimo_demo.inspection.models import nonnegative_integer
from vimo_demo.inspection.production import (
    ChannelBusinessStatus,
    ChannelId,
    ProductionInspectionResult,
)

from .fins import (
    FinsAddress,
    FinsError,
    FinsMemoryArea,
    FinsPlcError,
    FinsTransport,
    FinsUdpClient,
    validate_transport,
)
from .fins_tcp import FinsTcpClient
from .first_station import (
    FirstStationReporter,
    FirstStationSubmissionError,
    first_station_reject_words,
)
from .operations import CommunicationRequest, RequestKind


@dataclass(frozen=True, slots=True)
class LocalReportingSettings:
    host: str = "127.0.0.1"
    port: int = 9600
    timeout: float = 1.0
    destination: FinsAddress = FinsAddress(0, 2, 0)
    source: FinsAddress = FinsAddress(0, 1, 0)
    max_pending: int = 8
    transport: FinsTransport = FinsTransport.UDP

    def __post_init__(self) -> None:
        if self.host != "127.0.0.1":
            raise PermissionError("Automatic reporting requires exactly 127.0.0.1")
        CommunicationRequest(
            RequestKind.READ,
            self.host,
            self.port,
            self.timeout,
            self.destination,
            self.source,
            49,
            area=FinsMemoryArea.W,
            transport=self.transport,
        ).validate()
        if type(self.max_pending) is not int or self.max_pending < 1:
            raise ValueError("max_pending must be a positive integer")


@dataclass(frozen=True, slots=True)
class ReportingSelection:
    """In-memory next-Start preference, never a persisted machine definition."""

    enabled: bool = False
    settings: LocalReportingSettings = LocalReportingSettings()

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ValueError("Reporting enabled must be bool")


class ReportingWriteProtectedError(PermissionError):
    """Manual W49..W52 writes conflict with the automatic reporting owner."""


@dataclass(frozen=True, slots=True)
class ReportIdentity:
    generation: int
    cycle_id: CycleId

    def __post_init__(self) -> None:
        nonnegative_integer(self.generation, "generation")
        nonnegative_integer(self.cycle_id, "cycle_id")
        if self.generation == 0:
            raise ValueError("Reporting requires a positive application generation")


@dataclass(frozen=True, slots=True)
class FirstStationReport:
    """Scalar projection only: no Production result, frames, masks or SDK objects."""

    identity: ReportIdentity
    statuses: tuple[tuple[ChannelId, ChannelBusinessStatus], ...]
    reject_words: tuple[int, int, int] = field(init=False)

    def __post_init__(self) -> None:
        pairs = tuple((channel, status) for channel, status in self.statuses)
        object.__setattr__(self, "statuses", pairs)
        object.__setattr__(self, "reject_words", first_station_reject_words(pairs))

    @classmethod
    def from_result(
        cls, generation: int, result: ProductionInspectionResult
    ) -> FirstStationReport:
        if result.cycle_status not in (CycleStatus.COMPLETE, CycleStatus.FAILED):
            raise ValueError(f"Ineligible collection status {result.cycle_status.name}")
        if len(result.cameras) != 6 or any(
            camera.frame_id is None
            or camera.image_shape is None
            or camera.collection_issue is not None
            for camera in result.cameras
        ):
            raise ValueError("Reporting requires complete six-camera collection")
        return cls(
            ReportIdentity(generation, result.cycle_id),
            tuple((row.channel_id, row.business_status) for row in result.channels),
        )


class ReportOutcome(Enum):
    QUEUED = "queued"
    ATTEMPTED = "attempted"
    ACKNOWLEDGED = "acknowledged"
    FAILED = "failed"
    SKIPPED = "skipped"
    REJECTED = "rejected"


class ReportStage(Enum):
    INITIALIZATION = "initialization"
    BITMAP = "bitmap"
    SEQUENCE = "sequence"
    HANDOFF = "handoff"


@dataclass(frozen=True, slots=True)
class ReportingIssue:
    exception_type: str
    message: str
    cause_type: str | None = None
    cause_message: str | None = None
    end_code: int | None = None

    @classmethod
    def from_error(cls, error: BaseException) -> ReportingIssue:
        # Do not retain traceback frames that may own an inspection result.
        cause = error.__cause__
        plc = cause if isinstance(cause, FinsPlcError) else error
        return cls(
            type(error).__name__,
            str(error)[:1200],
            type(cause).__name__ if cause else None,
            str(cause)[:1200] if cause else None,
            plc.end_code if isinstance(plc, FinsPlcError) else None,
        )


@dataclass(frozen=True, slots=True)
class ReportObservation:
    identity: ReportIdentity
    outcome: ReportOutcome
    sequence: int | None = None
    reject_words: tuple[int, int, int] | None = None
    stage: ReportStage | None = None
    issue: ReportingIssue | None = None
    duration_ms: float | None = None
    error: FinsError | None = None  # Preserve the original FINS exception/cause chain.


@dataclass(frozen=True, slots=True)
class ReportingSnapshot:
    generation: int
    accepting: bool
    thread_alive: bool
    pending: int
    peak_pending: int
    active: ReportIdentity | None
    queued_count: int
    attempted_count: int
    acknowledged_count: int
    failed_count: int
    skipped_count: int
    rejected_count: int
    next_sequence: int | None
    last_queued: ReportIdentity | None
    last_attempted: ReportObservation | None
    last_acknowledged: ReportObservation | None
    last_outcome: ReportObservation | None
    last_failed: ReportObservation | None
    last_skipped: ReportObservation | None
    last_rejected: ReportObservation | None
    failure_issue: ReportingIssue | None
    cleanup_issue: ReportingIssue | None


@runtime_checkable
class ProductionReportingCommands(Protocol):
    """Optional composition boundary; observations reuse the one H2B owner."""

    @property
    def reporting_editable(self) -> bool: ...
    def reporting_selection(self) -> ReportingSelection: ...
    def set_reporting_selection(self, selection: ReportingSelection) -> None: ...
    def reporting_snapshot(self) -> ReportingSnapshot | None: ...
    def validate_manual_request(self, request: CommunicationRequest) -> None: ...


class ReportingCapacityError(RuntimeError):
    """Rejected result, never an inspection judgment or ordinary transport failure."""


class ReportingStateError(RuntimeError):
    """Reporting owner is unavailable or identity/order is invalid."""


@dataclass(frozen=True, slots=True)
class _PendingReport:
    report: FirstStationReport
    completed: Future[ReportObservation]


def _client(settings: LocalReportingSettings) -> FinsUdpClient | FinsTcpClient:
    if settings.transport is FinsTransport.TCP:
        return FinsTcpClient(
            settings.host, port=settings.port, timeout=settings.timeout
        )
    return FinsUdpClient(
        settings.host,
        port=settings.port,
        timeout=settings.timeout,
        destination=settings.destination,
        source=settings.source,
    )


class FirstStationReportingWorker:
    """One serial socket owner, bounded FIFO plus one active report, no retries.

    Task Futures are noncancellable terminal observations. Their callbacks must
    remain short/nonblocking and never mutate widgets. join() proves actual exit.
    """

    def __init__(
        self,
        generation: int,
        settings: LocalReportingSettings,
        *,
        client_factory: Callable[
            [LocalReportingSettings], FinsUdpClient | FinsTcpClient
        ] = _client,
    ) -> None:
        ReportIdentity(generation, CycleId(0))
        self.generation, self.settings = generation, settings
        self._factory = client_factory
        self._condition = Condition()
        self._pending: deque[_PendingReport] = deque()
        self._started = False
        self._stopping = False
        self._active: ReportIdentity | None = None
        self._last_seen: CycleId | None = None
        self._next_sequence: int | None = None
        self._client: FinsUdpClient | FinsTcpClient | None = None
        self._reporter: FirstStationReporter | None = None
        self._queued = self._attempted = self._acknowledged = 0
        self._failed = self._skipped = self._rejected = self._peak = 0
        self._last_queued: ReportIdentity | None = None
        self._last_attempt: ReportObservation | None = None
        self._last_ack: ReportObservation | None = None
        self._last_outcome: ReportObservation | None = None
        self._last_fail: ReportObservation | None = None
        self._last_skip: ReportObservation | None = None
        self._last_reject: ReportObservation | None = None
        self._failure_issue: ReportingIssue | None = None
        self._cleanup_issue: ReportingIssue | None = None
        self._thread = Thread(
            target=self._run, name=f"FirstStationReporting-{generation}", daemon=False
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
            try:
                self._thread.start()
            except BaseException:
                self._stopping = True
                raise

    def snapshot(self) -> ReportingSnapshot:
        with self._condition:
            return ReportingSnapshot(
                self.generation,
                self._started and not self._stopping,
                self._thread.is_alive(),
                len(self._pending),
                self._peak,
                self._active,
                self._queued,
                self._attempted,
                self._acknowledged,
                self._failed,
                self._skipped,
                self._rejected,
                self._next_sequence,
                self._last_queued,
                self._last_attempt,
                self._last_ack,
                self._last_outcome,
                self._last_fail,
                self._last_skip,
                self._last_reject,
                self._failure_issue,
                self._cleanup_issue,
            )

    def note_skipped(self, identity: ReportIdentity, error: BaseException) -> None:
        with self._condition:
            self._skipped += 1
            self._last_outcome = ReportObservation(
                identity,
                ReportOutcome.SKIPPED,
                stage=ReportStage.HANDOFF,
                issue=ReportingIssue.from_error(error),
            )
            self._last_skip = self._last_outcome

    def submit(self, report: FirstStationReport) -> Future[ReportObservation]:
        with self._condition:
            error: RuntimeError | None = None
            if report.identity.generation != self.generation:
                error = ReportingStateError("Reporting generation mismatch")
            elif not self._started:
                error = ReportingStateError("Reporting worker is not started")
            elif self._failure_issue is not None:
                error = ReportingStateError("Reporting owner terminated unexpectedly")
            elif self._stopping:
                completed: Future[ReportObservation] = Future()
                completed.set_running_or_notify_cancel()
                observation = ReportObservation(
                    report.identity,
                    ReportOutcome.SKIPPED,
                    reject_words=report.reject_words,
                    stage=ReportStage.HANDOFF,
                    issue=ReportingIssue(
                        "ReportingStateError", "Session stopped admitting reports"
                    ),
                )
                self._skipped += 1
                self._last_outcome = observation
                self._last_skip = observation
                completed.set_result(observation)  # New handle has no callbacks yet.
                return completed
            elif (
                self._last_seen is not None
                and report.identity.cycle_id <= self._last_seen
            ):
                error = ReportingStateError(
                    "Duplicate/out-of-order local report identity"
                )
            elif len(self._pending) >= self.settings.max_pending:
                error = ReportingCapacityError(
                    "First-station reporting backlog is full"
                )
            if error is not None:
                self._rejected += 1
                self._last_outcome = ReportObservation(
                    report.identity,
                    ReportOutcome.REJECTED,
                    reject_words=report.reject_words,
                    stage=ReportStage.HANDOFF,
                    issue=ReportingIssue.from_error(error),
                )
                self._last_reject = self._last_outcome
                raise error
            completed = Future()
            completed.set_running_or_notify_cancel()
            self._last_seen = report.identity.cycle_id
            self._pending.append(_PendingReport(report, completed))
            self._queued += 1
            self._peak = max(self._peak, len(self._pending))
            self._last_queued = report.identity
            self._last_outcome = ReportObservation(
                report.identity, ReportOutcome.QUEUED, reject_words=report.reject_words
            )
            self._condition.notify_all()
            return completed

    def request_stop(self) -> None:
        with self._condition:
            self._stopping = True
            self._condition.notify_all()

    def join(self, timeout: float | None = None) -> bool:
        if current_thread() is self._thread:
            raise ReportingStateError("Reporting owner cannot join itself")
        if self._thread.ident is not None:
            self._thread.join(timeout)
        return not self._thread.is_alive()

    def _attempt(self, report: FirstStationReport) -> ReportObservation:
        started = perf_counter()
        sequence: int | None = None
        stage = ReportStage.INITIALIZATION
        try:
            if self.settings.host != "127.0.0.1":
                raise PermissionError("Automatic reporting requires exactly 127.0.0.1")
            validate_transport(self.settings.transport)
            if self._client is None or self._client.closed:
                self._client = self._factory(self.settings)
                self._reporter = None
            if self._next_sequence is None:
                initial = self._client.read_words(49, area=FinsMemoryArea.W)[0]
                with self._condition:
                    self._next_sequence = (initial + 1) & 0xFFFF
            if self._reporter is None:
                self._reporter = FirstStationReporter(
                    self._client, initial_sequence=self._next_sequence
                )
            sequence = self._reporter.next_sequence
            with self._condition:
                self._last_attempt = ReportObservation(
                    report.identity,
                    ReportOutcome.ATTEMPTED,
                    sequence,
                    report.reject_words,
                )
            stage = ReportStage.BITMAP
            try:
                commit = self._reporter.submit(report.statuses)
            finally:
                with self._condition:
                    self._next_sequence = self._reporter.next_sequence
            return ReportObservation(
                report.identity,
                ReportOutcome.ACKNOWLEDGED,
                commit.sequence,
                commit.reject_words,
                duration_ms=(perf_counter() - started) * 1000,
            )
        except Exception as exc:
            if isinstance(exc, FirstStationSubmissionError):
                stage = ReportStage(exc.stage.value)
                sequence = exc.sequence
            return ReportObservation(
                report.identity,
                ReportOutcome.FAILED,
                sequence,
                report.reject_words,
                stage,
                ReportingIssue.from_error(exc),
                (perf_counter() - started) * 1000,
                exc if isinstance(exc, FinsError) else None,
            )

    def _settle(self, pending: _PendingReport, observation: ReportObservation) -> None:
        with self._condition:
            if observation.outcome is ReportOutcome.ACKNOWLEDGED:
                self._acknowledged += 1
                self._last_ack = observation
            elif observation.outcome is ReportOutcome.FAILED:
                self._failed += 1
                self._last_fail = observation
            else:
                self._skipped += 1
                self._last_skip = observation
            self._last_outcome = observation
            self._active = None
        pending.completed.set_result(observation)  # Outside admission locks.

    def _run(self) -> None:
        current: _PendingReport | None = None
        try:
            while True:
                with self._condition:
                    self._condition.wait_for(
                        lambda: self._stopping or bool(self._pending)
                    )
                    if not self._pending:
                        return
                    current = self._pending.popleft()
                    stopping = self._stopping
                    if not stopping:
                        self._active = current.report.identity
                        self._attempted += 1
                        self._last_attempt = ReportObservation(
                            current.report.identity,
                            ReportOutcome.ATTEMPTED,
                            reject_words=current.report.reject_words,
                        )
                        self._last_outcome = self._last_attempt
                observation = (
                    ReportObservation(
                        current.report.identity,
                        ReportOutcome.SKIPPED,
                        reject_words=current.report.reject_words,
                        stage=ReportStage.HANDOFF,
                        issue=ReportingIssue(
                            "ReportingStateError",
                            "Unsent report skipped during shutdown",
                        ),
                    )
                    if stopping
                    else self._attempt(current.report)
                )
                self._settle(current, observation)
                current = None
        except BaseException as exc:
            # Unexpected owner failure: resolve all accepted handles, never pretend
            # a report was acknowledged. Admission stays closed for this lifetime.
            self.request_stop()
            with self._condition:
                self._failure_issue = ReportingIssue.from_error(exc)
            if current is not None and not current.completed.done():
                self._settle(
                    current,
                    ReportObservation(
                        current.report.identity,
                        ReportOutcome.FAILED,
                        reject_words=current.report.reject_words,
                        stage=ReportStage.HANDOFF,
                        issue=ReportingIssue.from_error(exc),
                    ),
                )
            while self._pending:
                with self._condition:
                    pending = self._pending.popleft()
                self._settle(
                    pending,
                    ReportObservation(
                        pending.report.identity,
                        ReportOutcome.SKIPPED,
                        reject_words=pending.report.reject_words,
                        stage=ReportStage.HANDOFF,
                        issue=ReportingIssue.from_error(exc),
                    ),
                )
        finally:
            with self._condition:
                self._stopping = True
            if self._client is not None:
                try:
                    self._client.close()
                except BaseException as exc:
                    with self._condition:
                        self._cleanup_issue = ReportingIssue.from_error(exc)
                else:
                    self._client = None
                    self._reporter = None
