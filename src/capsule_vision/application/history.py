"""Best-effort, bounded Production history; one SQLite owner, no Qt or pixels."""

import json
import sqlite3
import zlib
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Condition, Event, Thread
from time import time
from typing import cast

from capsule_vision.communication.reporting import (
    CompletedReportingBatch,
    ReportingSettings,
)
from capsule_vision.diagnostics import RuntimeDiagnostics
from capsule_vision.inspection.production import ProductionInspectionResult

from . import (
    alarm_records,
    archive_links,
    batch_records,
    batch_review,
    defect_records,
    records_query,
)
from .alarm_records import AlarmEvent, AlarmQuery, AlarmRecord
from .alarm_report import AlarmReportRequest
from .alarm_report import capture as capture_alarm
from .archive_links import PublishedImage
from .audit import AuditRecord, AuditUnavailable
from .batch_records import BatchSelection, ReportBatches
from .batch_review import (
    BatchDetail,
    BatchInformation,
    BatchInformationConflict,
    BatchList,
    BatchSetup,
    StartAccount,
)
from .identity_models import IdentityError
from .models import ApplicationStateError, ConfigurationIdentity
from .records_query import BatchChoices, RecordsScope
from .report_scope import (
    DefectScope,
    ReportRequest,
    capture_report,
    defect_scope,
    register_result_filter,
)
from .report_scope import HistoryQuery as HistoryQuery
from .report_scope import ResultFilter as ResultFilter


@dataclass(frozen=True, slots=True)
class HistoryContext:
    completed_at: float  # Host wall-clock observation, never camera/monotonic time.
    batch_segment: str
    batch_label: str | None
    run_id: str
    active: ConfigurationIdentity
    recipe_name: str | None = None  # Current records may omit a display name.


@dataclass(frozen=True, slots=True)
class HistoryChannel:
    camera: str
    slot: int
    channel: int
    business: str
    technical: str
    judgment: str
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HistoryCamera:
    camera: str
    technical: str
    judgment: str
    reasons: tuple[str, ...]
    unassigned_labels: tuple[str, ...]
    image_path: Path | None = None


@dataclass(frozen=True, slots=True)
class HistoryRecord:
    record_id: int
    context: HistoryContext
    cycle_id: int
    status: str
    technical: str
    judgment: str
    channels: tuple[HistoryChannel, ...]
    cameras: tuple[HistoryCamera, ...]
    group_first_cycle: int | None = None
    group_slot: int | None = None
    group_size: int | None = None


@dataclass(frozen=True, slots=True)
class HistoryPageData:
    records: tuple[HistoryRecord, ...]
    # Recent segments in date scope, with earliest retained completion per segment.
    batches: tuple[tuple[str, str | None, float], ...]
    more: bool


@dataclass(frozen=True, slots=True)
class HistorySnapshot:
    ready: bool
    accepting: bool
    thread_alive: bool
    pending: int
    accepted: int
    persisted: int
    dropped: int
    failed: int
    error: str
    images_linked: int = 0
    image_links_lost: int = 0
    image_link_error: str = ""


@dataclass(frozen=True, slots=True)
class HistorySubmission:
    context: HistoryContext
    result: ProductionInspectionResult


@dataclass(frozen=True, slots=True)
class _Read:
    query: HistoryQuery
    future: Future[HistoryPageData]


@dataclass(frozen=True, slots=True)
class _Operation:
    execute: Callable[[sqlite3.Connection], None]
    failed: Callable[[BaseException], None]
    read_only: bool


@dataclass(frozen=True, slots=True)
class _Fact:
    run: str
    write: Callable[[sqlite3.Connection], object]


def _items(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ValueError("Invalid History detail array")
    return cast(list[object], value)


def _strings(value: object) -> tuple[str, ...]:
    values = _items(value)
    if not all(isinstance(item, str) for item in values):
        raise ValueError("Invalid History reason codes")
    return cast(tuple[str, ...], tuple(values))


class ProductionHistory:
    """SQLite lives exclusively on this owner, including queries and retention.

    Handoff borrows an immutable Production result (which has no pixels/masks).
    Projection, JSON and compression happen here, never in accounting callbacks.
    The queue bounds retained results and query requests together; no retry/history
    of completed requests is kept. Only committed inserts count as persisted.
    """

    def __init__(
        self,
        path: Path,
        *,
        max_pending: int = 128,
        clock: Callable[[], float] = time,
        retention_batch: int = 256,
        on_failure: Callable[[str], None] | None = None,
        alarm_access: Callable[[int | None], int] | None = None,
        diagnostics: RuntimeDiagnostics | None = None,
    ) -> None:
        if max_pending < 1 or retention_batch < 1:
            raise ValueError("History bounds must be positive")
        self.path = path
        self._diagnostics = diagnostics
        self._on_failure = on_failure
        self._alarm_access = alarm_access
        self.clock = clock
        self.retention_batch = retention_batch
        self._capacity = max_pending
        # One reserved slot lets the lifecycle owner close a Run behind all admitted facts.
        self._queue: Queue[HistorySubmission | _Read | _Operation | _Fact] = Queue(
            max_pending + 1
        )
        self._lock = Condition()
        # Metadata only. These slots cannot consume any Run/Cycle/Group capacity.
        self._images: deque[PublishedImage] = deque()
        self._images_linked = self._image_links_lost = 0
        self._image_link_error = ""
        self._has_images = False  # SQLite owner only; optional on existing databases.
        self._next_image_retention = float("-inf")
        self._accepting, self._ready = True, False
        self._accepted = self._persisted = self._dropped = self._failed = 0
        self._error = ""
        self._reported_loss = False
        # None: absent; False: original zh/en table; True: optional translations.
        self._alarm_translations: bool | None = None  # SQLite owner only.
        self._next_alarm_retention = float("-inf")
        self._alarm_loss = 0
        self._alarm_issue = ""
        self.initial_selection: Future[BatchSelection | None] = Future()
        self.initial_selection.set_running_or_notify_cancel()
        self._expired_batch: str | None = None
        self._loss_run: str | None = None
        self._loss_persisted = False
        self._thread = Thread(target=self._run, name="ProductionHistory", daemon=False)
        self._thread.start()

    def offer(
        self, context: HistoryContext, result: ProductionInspectionResult
    ) -> bool:
        """Non-waiting admission only; never means a committed record."""
        with self._lock:
            if self._accepting:
                try:
                    if self._queue.qsize() >= self._capacity:
                        raise Full
                    self._queue.put_nowait(HistorySubmission(context, result))
                    self._lock.notify()
                except Full:
                    self._record_loss("History capacity reached; record not admitted")
                else:
                    self._accepted += 1
                    return True
            self._dropped += 1
            self._loss_run, self._loss_persisted = context.run_id, False
            if self._diagnostics:
                self._diagnostics.record(
                    "history.storage_issue",
                    stage="cycle_handoff",
                    run_id=context.run_id,
                    cycle_id=result.cycle_id,
                )
            return False

    def offer_image(self, image: PublishedImage) -> None:
        """Nonblocking handoff from the PNG writer after final publication."""
        with self._lock:
            if self._accepting and len(self._images) < self._capacity:
                self._images.append(image)
                self._lock.notify()
            else:
                self._image_links_lost += 1
                self._image_link_error = "History image link queue is full or closed"
                if self._diagnostics:
                    self._diagnostics.record(
                        "history.storage_issue",
                        stage="link_handoff",
                        run_id=image.run_id,
                        cycle_id=image.cycle_id,
                        camera_id=image.camera_id,
                    )

    def _command[T](
        self,
        action: Callable[[sqlite3.Connection], T],
        *,
        read_only: bool = False,
        reserved: bool = True,
    ) -> Future[T]:
        future: Future[T] = Future()
        future.set_running_or_notify_cancel()

        def execute(db: sqlite3.Connection) -> None:
            with db:
                value = action(db)
            future.set_result(value)

        with self._lock:
            try:
                if not self._accepting:
                    raise RuntimeError(self._error or "History is closed")
                if (
                    read_only or not reserved
                ) and self._queue.qsize() >= self._capacity:
                    raise Full
                self._queue.put_nowait(
                    _Operation(execute, future.set_exception, read_only)
                )
                self._lock.notify()
            except Full:
                future.set_exception(RuntimeError("Batch storage capacity reached"))
            except RuntimeError as exc:
                future.set_exception(RuntimeError(f"Batch storage unavailable: {exc}"))
        return future

    def offer_alarms(self, events: tuple[AlarmEvent, ...]) -> None:
        """One nonblocking handoff for an observed word (up to 16 transitions)."""
        if not 1 <= len(events) <= 16:
            raise ValueError("Expected one alarm observation with 1..16 transitions")

        def write(db: sqlite3.Connection) -> None:
            if self._alarm_translations is not True:
                alarm_records.create(db, legacy=self._alarm_translations is False)
                self._alarm_translations = True
            alarm_records.append(db, events)

        def finished(result: Future[None]) -> None:
            if error := result.exception():
                with self._lock:
                    self._alarm_loss += len(events)
                    self._alarm_issue = str(error)
                if self._diagnostics:
                    self._diagnostics.record(
                        "history.storage_issue",
                        stage="alarm_storage",
                        lost=len(events),
                        error_type=type(error).__name__,
                    )

        self._command(write, reserved=False).add_done_callback(finished)

    def alarm_storage(self) -> tuple[int, str]:
        with self._lock:
            return self._alarm_loss, self._alarm_issue

    def alarm_page(
        self, query: AlarmQuery, revision: int | None = None
    ) -> Future[tuple[AlarmRecord, ...]]:
        authorize = self._alarm_access
        if authorize is None:
            raise RuntimeError("Alarm review requires an authenticated History owner")
        ticket = authorize(revision)

        def read(db: sqlite3.Connection) -> tuple[AlarmRecord, ...]:
            authorize(ticket)
            records = (
                alarm_records.page(
                    db,
                    query,
                    self.clock() - 30 * 86400,
                    translated=self._alarm_translations,
                )
                if self._alarm_translations is not None
                else ()
            )
            authorize(ticket)
            return records

        return self._command(read, read_only=True)

    def report_batches(
        self, prefix: str, cursor: str | None, previous: bool
    ) -> Future[ReportBatches]:
        return self._command(
            lambda db: batch_records.report_batches(db, prefix, cursor, previous),
            read_only=True,
        )

    def batch_page(
        self,
        prefix: str = "",
        cursor: tuple[float, str] | None = None,
        newer: bool = False,
        *,
        since: int | None = None,
        until: int | None = None,
        batch: str | None = None,
        unbatched: bool = False,
    ) -> Future[BatchList]:
        return self._command(
            lambda db: batch_review.page(
                db, prefix, cursor, newer, since, until, self.clock(), batch, unbatched
            ),
            read_only=True,
        )

    def batch_choices(
        self,
        scope: RecordsScope,
        search: str = "",
        cursor: tuple[float, str] | None = None,
        *,
        alarm_revision: int | None = None,
    ) -> Future[BatchChoices]:
        authorize = self._alarm_access
        if alarm_revision is not None:
            if authorize is None:
                raise IdentityError("Alarm History authorization unavailable")
            authorize(alarm_revision)

        def read(db: sqlite3.Connection) -> BatchChoices:
            if alarm_revision is not None and authorize is not None:
                authorize(alarm_revision)
            result = records_query.batch_choices(
                db,
                scope,
                search,
                cursor,
                self.clock(),
                alarm_revision is not None and self._alarm_translations is not None,
            )
            if alarm_revision is not None and authorize is not None:
                authorize(alarm_revision)
            return result

        return self._command(read, read_only=True)

    def associated_audit(
        self, scope: RecordsScope, records: tuple[AuditRecord, ...]
    ) -> Future[tuple[AuditRecord, ...]]:
        return self._command(
            lambda db: records_query.associated_audit(db, scope, records),
            read_only=True,
        )

    def batch_detail(
        self, batch: str, offset: int = 0, run_anchor: int | None = None
    ) -> Future[BatchDetail]:
        def read(db: sqlite3.Connection) -> BatchDetail:
            with self._lock:
                loss = self._loss_run
            return batch_review.detail(db, batch, offset, loss, run_anchor)

        return self._command(read, read_only=True)

    def update_batch_information(
        self,
        batch: str,
        expected: BatchInformation,
        requested: BatchInformation,
        authorize: Callable[[str, BatchInformation], AbstractContextManager[None]],
    ) -> Future[None]:
        def write(db: sqlite3.Connection) -> None:
            label, actual = batch_review.information(db, batch)
            with authorize(label, actual):
                if db.execute("SELECT 1 FROM runs WHERE outcome='open'").fetchone():
                    raise ApplicationStateError(
                        "Batch editing requires idle Production"
                    )
                if actual != expected:
                    raise BatchInformationConflict(
                        "Batch information changed; refresh before editing"
                    )
                # Complete the business transaction before the audit owner records success.
                with db:
                    batch_review.save_information(db, batch, requested)

        return self._command(write, reserved=False)

    def defect_report_scope(self, filters: HistoryQuery) -> Future[DefectScope]:
        return self._command(lambda db: defect_scope(db, filters), read_only=True)

    def capture_report(
        self, batch: str | ReportRequest, snapshot: Path, cancel: Event
    ) -> Future[None]:
        def capture(db: sqlite3.Connection) -> None:
            with self._lock:
                loss_run = self._loss_run
            capture_report(
                db,
                ReportRequest("batch", "capsule", batch)
                if isinstance(batch, str)
                else batch,
                snapshot,
                self.clock(),
                loss_run,
                cancel,
            )

        return self._command(capture, read_only=True)

    def capture_alarm_report(
        self,
        request: AlarmReportRequest,
        snapshot: Path,
        cancel: Event,
        revision: int | None = None,
    ) -> Future[None]:
        authorize = self._alarm_access
        if authorize is None:
            raise RuntimeError("Alarm review requires an authenticated History owner")
        ticket = authorize(revision)

        def capture(db: sqlite3.Connection) -> None:
            authorize(ticket)
            capture_alarm(
                db,
                request,
                snapshot,
                self.clock(),
                cancel,
                available=self._alarm_translations is not None,
            )
            authorize(ticket)

        return self._command(capture, read_only=True)

    def capture_audit_links(self, snapshot: Path, cancel: Event) -> Future[None]:
        """Freeze only the existing association evidence; never wait on Audit here."""
        from .audit_report import capture_links

        return self._command(
            lambda db: capture_links(db, snapshot, cancel), read_only=True
        )

    def select_batch(self, label: str | None) -> Future[BatchSelection | None]:
        return self._command(lambda db: batch_records.select(db, label, self.clock()))

    def batch_setup(self, label: str) -> Future[BatchSetup]:
        return self._command(
            lambda db: batch_review.setup(db, label.strip()), read_only=True
        )

    def setup_batch(
        self,
        label: str,
        expected: BatchSetup,
        requested: BatchInformation,
        authorize: Callable[[str, BatchInformation], AbstractContextManager[None]],
    ) -> Future[BatchSelection | None]:
        def write(db: sqlite3.Connection) -> BatchSelection | None:
            actual = batch_review.setup(db, label)
            with authorize(label, actual.information):
                if actual != expected:
                    raise BatchInformationConflict(
                        "Batch information changed; refresh before editing"
                    )
                if db.execute("SELECT 1 FROM runs WHERE outcome='open'").fetchone():
                    raise ApplicationStateError(
                        "Batch editing requires idle Production"
                    )
                # Selection and information are one transaction, committed before
                # the authorization/audit context reports success.
                with db:
                    selection = batch_records.select(db, label, self.clock())
                    assert selection is not None
                    if actual.information != requested:
                        batch_review.save_information(db, selection.uuid, requested)
                return selection

        return self._command(write, reserved=False)

    def start_run(
        self,
        batch: str | None,
        run: str,
        recipe: ConfigurationIdentity,
        reporting: ReportingSettings | None,
        account: StartAccount | None = None,
    ) -> Future[None]:
        observed = self.clock()

        def write(db: sqlite3.Connection) -> None:
            batch_records.start_run(db, batch, run, observed, recipe, reporting)
            db.execute("INSERT INTO run_coverage VALUES (?,0,0,0)", (run,))
            if account is not None:
                db.execute(
                    "INSERT INTO run_start_accounts VALUES (?,?,?)",
                    (run, account.account_id, account.username),
                )

        return self._command(write)

    def finish_run(self, run: str, outcome: str, uncertain: bool) -> Future[None]:
        observed = self.clock()
        return self._command(
            lambda db: batch_records.finish_run(
                db, run, observed, outcome, uncertain or self.run_uncertain(run)
            )
        )

    def run_uncertain(self, run: str) -> bool:
        with self._lock:
            return self._loss_run == run or not self._accepting

    def expired_for(self, batch: str) -> bool:
        with self._lock:
            return self._expired_batch == batch

    def missing_terminal(self, run: str) -> None:
        with self._lock:
            self._loss_run, self._loss_persisted = run, False
            self._record_loss("Admitted Cycle has no confirmed terminal result")
            if self._diagnostics:
                self._diagnostics.record(
                    "history.storage_issue", stage="missing_terminal", run_id=run
                )

    def _record_loss(self, detail: str) -> None:
        # Called under the handoff lock. Notify once, without waiting on a write.
        self._error = detail
        if not self._reported_loss and self._on_failure is not None:
            self._reported_loss = True
            self._on_failure(detail)

    def _offer_fact(self, item: _Fact) -> None:
        with self._lock:
            if self._accepting and self._queue.qsize() < self._capacity:
                self._queue.put_nowait(item)
                self._lock.notify()
            else:
                self._loss_run, self._loss_persisted = item.run, False
                self._dropped += 1
                self._record_loss(
                    "Batch storage unavailable or full; accounting fact not confirmed"
                )
                if self._diagnostics:
                    self._diagnostics.record(
                        "history.storage_issue",
                        stage="accounting_handoff",
                        run_id=item.run,
                    )

    def admitted(self, run: str, cycle: int) -> None:
        self._offer_fact(
            _Fact(run, lambda db: batch_records.occurrence(db, run, cycle))
        )

    def offer_group(self, run: str, group: CompletedReportingBatch) -> None:
        observed = self.clock()

        def write(db: sqlite3.Connection) -> None:
            batch_records.group(db, run, group, observed)
            if self._has_classification:
                defect_records.reconcile(db, run, group.identity.cycle_id)

        self._offer_fact(_Fact(run, write))

    def query(self, query: HistoryQuery | None = None) -> Future[HistoryPageData]:
        future: Future[HistoryPageData] = Future()
        future.set_running_or_notify_cancel()
        with self._lock:
            try:
                if not self._accepting:
                    raise RuntimeError(self._error or "History is closed")
                if self._queue.qsize() >= self._capacity:
                    raise Full
                self._queue.put_nowait(_Read(query or HistoryQuery(), future))
                self._lock.notify()
            except Full:
                future.set_exception(
                    RuntimeError(
                        "History request capacity reached; refresh after pending work"
                    )
                )
            except RuntimeError as exc:
                future.set_exception(RuntimeError(f"History unavailable: {exc}"))
        return future

    def request_stop(self) -> None:
        with self._lock:
            if not self._accepting:
                return
            self._accepting = False
            self._lock.notify()

    def join(self, timeout: float | None = None) -> bool:
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def snapshot(self) -> HistorySnapshot:
        with self._lock:
            return HistorySnapshot(
                self._ready,
                self._accepting,
                self._thread.is_alive(),
                self._queue.qsize(),
                self._accepted,
                self._persisted,
                self._dropped,
                self._failed,
                self._error,
                self._images_linked,
                self._image_links_lost,
                self._image_link_error,
            )

    def _failure(
        self,
        error: BaseException,
        *,
        stage: str = "sqlite",
        run_id: str | None = None,
        cycle_id: int | None = None,
    ) -> None:
        detail = f"{type(error).__name__}: {error}"[:2000]
        with self._lock:
            changed = self._error != detail
            self._error = detail
        if changed and self._on_failure is not None:
            self._on_failure(type(error).__name__)
        if self._diagnostics and (changed or run_id is not None):
            self._diagnostics.record(
                "history.storage_issue",
                stage=stage,
                error_type=type(error).__name__,
                run_id=run_id,
                cycle_id=cycle_id,
            )

    def _open(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=0.2)
        register_result_filter(connection)
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 2, 3) or (
                version == 0
                and connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1"
                ).fetchone()
            ):
                raise ValueError("Unsupported History database version")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA locking_mode=EXCLUSIVE")
            connection.execute("PRAGMA journal_mode=WAL")
            if version == 0:
                connection.executescript(
                    """
                BEGIN;
                CREATE TABLE IF NOT EXISTS cycles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    completed_at REAL NOT NULL,
                    batch_segment TEXT NOT NULL, batch_label TEXT,
                    run_id TEXT NOT NULL, cycle_id TEXT NOT NULL,
                    recipe_id TEXT NOT NULL, model_id TEXT NOT NULL, model_sha TEXT NOT NULL,
                    status TEXT NOT NULL, technical TEXT NOT NULL, judgment TEXT NOT NULL,
                    empty_count INTEGER NOT NULL, unknown_count INTEGER NOT NULL,
                    detail BLOB NOT NULL, UNIQUE(run_id, cycle_id)
                );
                CREATE INDEX IF NOT EXISTS history_time ON cycles(completed_at DESC, id DESC);
                CREATE INDEX IF NOT EXISTS history_batch ON cycles(batch_segment, completed_at DESC, id DESC);
            """
                    + batch_records.SCHEMA
                    + "PRAGMA user_version=3; COMMIT;"
                )
            with connection:
                batch_records.recover(connection, self.clock())
                batch_records.initialize_run_positions(connection)
                batch_review.initialize(connection)
                defect_records.initialize(connection)
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(alarm_events)")
            }
            if columns:
                self._alarm_translations = "translations" in columns
            self._has_classification = (
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='cycle_classification'"
                ).fetchone()
                is not None
            )
            self._has_images = (
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='archive_images'"
                ).fetchone()
                is not None
            )
            return connection
        except BaseException:
            connection.close()
            raise

    def _write(self, connection: sqlite3.Connection, item: HistorySubmission) -> bool:
        result, context = item.result, item.context
        channels: list[tuple[str, int, int, str, str, str, tuple[str, ...]]] = []
        for (
            camera
        ) in result.cameras:  # Configured camera/slot order, never arrival order.
            for channel in camera.channels:
                assessment = channel.assessment
                codes = tuple(dict.fromkeys(r.code for r in channel.reasons))
                if channel.occupancy is not None and channel.occupancy.error_code:
                    codes += (channel.occupancy.error_code,)
                channels.append(
                    (
                        str(channel.camera_id),
                        channel.slot,
                        int(channel.channel_id),
                        channel.business_status.name,
                        assessment.technical_status.name,
                        assessment.judgment.name,
                        codes,
                    )
                )
        cameras = [
            (
                str(c.camera_id),
                c.assessment.technical_status.name,
                c.assessment.judgment.name,
                (c.collection_issue.code,) if c.collection_issue else (),
                tuple(dict.fromkeys(f.label for f in c.unassigned_findings)),
            )
            for c in result.cameras
        ]
        detail = zlib.compress(
            json.dumps(
                [channels, cameras, context.recipe_name],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        assessment = result.assessment
        with connection:
            if connection.execute(
                "SELECT 1 FROM runs WHERE uuid=?", (context.run_id,)
            ).fetchone():
                if not batch_records.occurrence(
                    connection, context.run_id, result.cycle_id, result
                ):
                    return False
            connection.execute(
                "INSERT INTO cycles (completed_at,batch_segment,batch_label,run_id,cycle_id,"
                "recipe_id,model_id,model_sha,status,technical,judgment,empty_count,unknown_count,detail) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    context.completed_at,
                    context.batch_segment,
                    context.batch_label,
                    context.run_id,
                    str(result.cycle_id),
                    context.active.recipe_id,
                    context.active.model_id,
                    context.active.model_sha256,
                    result.cycle_status.name,
                    assessment.technical_status.name,
                    assessment.judgment.name,
                    sum(c[3] == "EMPTY" for c in channels),
                    sum(c[3] == "UNKNOWN" for c in channels),
                    detail,
                ),
            )

            if not self._has_classification:
                defect_records.create(connection)
            defect_records.record_cycle(connection, context.run_id, result)
            defect_records.reconcile(connection, context.run_id, result.cycle_id)
            if context.batch_label is not None:
                batch_review.observed(
                    connection, context.batch_segment, context.completed_at
                )

        self._has_classification = True
        return True

    def _image_failure(
        self, error: BaseException, *, image: PublishedImage | None = None
    ) -> None:
        lost = int(image is not None)
        if self._diagnostics:
            self._diagnostics.record(
                "history.storage_issue",
                stage="image_links",
                error_type=type(error).__name__,
                lost=lost,
                run_id=image.run_id if image else None,
                cycle_id=image.cycle_id if image else None,
                camera_id=image.camera_id if image else None,
            )
        with self._lock:
            self._image_links_lost += lost
            self._image_link_error = f"{type(error).__name__}: {error}"[:500]

    def _write_image(
        self, connection: sqlite3.Connection, image: PublishedImage
    ) -> None:
        try:
            with connection:
                if not self._has_images:
                    archive_links.create(connection)
                archive_links.insert(connection, image)
        except Exception as exc:
            self._image_failure(exc, image=image)
        else:
            self._has_images = True
            with self._lock:
                self._images_linked += 1

    def _retain(self, connection: sqlite3.Connection) -> None:
        expired = False
        expired_cycles: list[tuple[str, str]] = []
        now = self.clock()
        cutoff = now - 30 * 86400
        if self._alarm_translations is not None and now >= self._next_alarm_retention:
            self._next_alarm_retention = now + 60
            try:
                with connection:
                    removed_alarms = connection.execute(
                        "DELETE FROM alarm_events WHERE id IN (SELECT id FROM alarm_events "
                        "WHERE observed < ? ORDER BY observed LIMIT ?)",
                        (cutoff, self.retention_batch),
                    )
                if removed_alarms.rowcount == self.retention_batch:
                    self._next_alarm_retention = now
            except sqlite3.Error as exc:
                # Alarm maintenance must not prevent Batch restoration or Run admission.
                with self._lock:
                    self._alarm_issue = str(exc)
        with connection:
            for table, column, returning in (
                ("cycles", "completed_at", "run_id,cycle_id"),
                ("plc_groups", "observed", "run"),
            ):
                removed = connection.execute(
                    f"DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM {table} WHERE {column} < ? "
                    f"ORDER BY {column},rowid LIMIT ?) RETURNING {returning}",
                    (cutoff, self.retention_batch),
                ).fetchall()
                connection.executemany(
                    "UPDATE runs SET detail_expired=1 WHERE uuid=?",
                    {(row[0],) for row in removed},
                )
                if table == "cycles":
                    expired_cycles = removed
                expired |= bool(removed)
        if self._has_images and (expired_cycles or now >= self._next_image_retention):
            self._next_image_retention = now + 60
            try:
                with connection:
                    orphans = archive_links.expire(
                        connection, expired_cycles, cutoff, self.retention_batch
                    )
                if orphans == self.retention_batch:
                    self._next_image_retention = now
            except sqlite3.Error as exc:
                self._image_failure(exc)
        if expired:
            row = connection.execute(
                "SELECT uuid FROM batches WHERE selected=1 AND EXISTS "
                "(SELECT 1 FROM runs WHERE batch=batches.uuid AND detail_expired)"
            ).fetchone()
            with self._lock:
                self._expired_batch = row[0] if row else None

    def _read(
        self, connection: sqlite3.Connection, query: HistoryQuery
    ) -> HistoryPageData:
        clauses: list[str] = []
        args: list[object] = []
        for field, op, value in (
            ("completed_at", ">=", query.since),
            ("completed_at", "<", query.until),
        ):
            if value is not None:
                clauses.append(f"{field} {op} ?")
                args.append(value)
        date_where = " WHERE " + " AND ".join(clauses) if clauses else ""
        batches = cast(
            list[tuple[str, str | None, float]],
            connection.execute(
                "SELECT batch_segment,batch_label,(SELECT MIN(first.completed_at) "
                "FROM cycles AS first WHERE first.batch_segment=cycles.batch_segment) FROM cycles"
                + date_where
                + " GROUP BY batch_segment ORDER BY MAX(completed_at) DESC LIMIT 100",
                args,
            ).fetchall(),
        )
        clauses, args = query.conditions()
        if query.cursor is not None:
            clauses.append(
                "(completed_at,id) " + (">" if query.newer else "<") + " (?,?)"
            )
            args.extend(query.cursor)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        order = "ASC" if query.newer else "DESC"
        rows = cast(
            list[tuple[object, ...]],
            connection.execute(
                "SELECT id,completed_at,batch_segment,batch_label,run_id,cycle_id,recipe_id,model_id,model_sha,"
                "status,technical,judgment,detail,m.first_cycle,m.slot,"
                "CASE WHEN m.run IS NULL THEN NULL WHEN r.reporting_mode='single_cycle' THEN 1 ELSE 4 END FROM cycles "
                "LEFT JOIN group_members m ON m.run=cycles.run_id AND m.cycle=cycles.cycle_id "
                "LEFT JOIN runs r ON r.uuid=m.run"
                + where
                + f" ORDER BY completed_at {order},id {order} LIMIT ?",
                [*args, query.limit + 1],
            ).fetchall(),
        )
        visible = rows[: query.limit]
        images: dict[tuple[int, str], Path] = {}
        if self._has_images and visible:
            try:
                images = archive_links.for_page(
                    connection, tuple(cast(int, row[0]) for row in visible)
                )
            except sqlite3.Error as exc:
                self._image_failure(exc)
        records = tuple(self._decode(row, images) for row in visible)
        return HistoryPageData(
            tuple(reversed(records)) if query.newer else records,
            tuple(batches),
            len(rows) > query.limit,
        )

    @staticmethod
    def _decode(
        row: tuple[object, ...], images: dict[tuple[int, str], Path]
    ) -> HistoryRecord:
        # Typed boundary for our versioned SQLite payload; corrupt data fails the
        # query instead of becoming fabricated judgments or affecting Production.
        detail: object = json.loads(zlib.decompress(cast(bytes, row[12])))
        payload = _items(detail)
        if len(payload) != 3:
            raise ValueError("Invalid History detail payload")
        recipe_name = payload[2]
        if recipe_name is not None and not isinstance(recipe_name, str):
            raise ValueError("Invalid History Recipe name")
        channels = tuple(
            HistoryChannel(
                str(r[0]),
                int(cast(int, r[1])),
                int(cast(int, r[2])),
                str(r[3]),
                str(r[4]),
                str(r[5]),
                _strings(r[6]),
            )
            for r in (_items(v) for v in _items(payload[0]))
        )
        record_id = int(cast(int, row[0]))
        cameras = tuple(
            HistoryCamera(
                str(r[0]),
                str(r[1]),
                str(r[2]),
                _strings(r[3]),
                _strings(r[4]),
                images.get((record_id, str(r[0]))),
            )
            for r in (_items(v) for v in _items(payload[1]))
        )
        context = HistoryContext(
            float(cast(float, row[1])),
            str(row[2]),
            cast(str | None, row[3]),
            str(row[4]),
            ConfigurationIdentity(str(row[6]), str(row[7]), str(row[8])),
            recipe_name,
        )
        return HistoryRecord(
            record_id,
            context,
            int(cast(str, row[5])),
            str(row[9]),
            str(row[10]),
            str(row[11]),
            channels,
            cameras,
            int(str(row[13])) if row[13] is not None else None,
            cast(int | None, row[14]),
            cast(int | None, row[15]),
        )

    def _run(self) -> None:
        connection: sqlite3.Connection | None = None
        item: HistorySubmission | _Read | _Operation | _Fact | PublishedImage | None = (
            None
        )
        try:
            connection = self._open()
            self._retain(connection)
            selection = batch_records.selected(connection)
            with self._lock:
                self._ready = True
            self.initial_selection.set_result(selection)
            while True:
                with self._lock:
                    self._lock.wait_for(
                        lambda: (
                            not self._queue.empty()
                            or bool(self._images)
                            or not self._accepting
                        ),
                        timeout=60,
                    )
                    if not self._queue.empty():
                        item = self._queue.get_nowait()
                    else:
                        item = self._images.popleft() if self._images else None
                if isinstance(item, PublishedImage):
                    self._write_image(connection, item)
                    continue
                try:
                    self._retain(connection)
                    with self._lock:
                        loss = None if self._loss_persisted else self._loss_run
                    if loss is not None:
                        with connection:
                            connection.execute(
                                "UPDATE runs SET uncertain=1 WHERE uuid=?", (loss,)
                            )
                        with self._lock:
                            self._loss_persisted = self._loss_run == loss
                except Exception as exc:
                    self._failure(exc)
                if isinstance(item, HistorySubmission):
                    try:
                        inserted = self._write(connection, item)
                    except Exception as exc:
                        self._failure(
                            exc,
                            stage="cycle_write",
                            run_id=item.context.run_id,
                            cycle_id=item.result.cycle_id,
                        )
                        with self._lock:
                            self._failed += 1
                            self._loss_run, self._loss_persisted = (
                                item.context.run_id,
                                False,
                            )
                    else:
                        with self._lock:
                            self._persisted += int(inserted)
                elif isinstance(item, _Fact):
                    try:
                        with connection:
                            item.write(connection)
                    except Exception as exc:
                        self._failure(exc, stage="accounting_write", run_id=item.run)
                        with self._lock:
                            self._failed += 1
                            self._loss_run, self._loss_persisted = item.run, False
                elif isinstance(item, _Operation):
                    try:
                        item.execute(connection)
                    except Exception as exc:
                        if not item.read_only and not isinstance(
                            exc,
                            (
                                BatchInformationConflict,
                                IdentityError,
                                ApplicationStateError,
                                AuditUnavailable,
                            ),
                        ):
                            self._failure(exc)
                        item.failed(exc)
                elif isinstance(item, _Read):
                    try:
                        item.future.set_result(self._read(connection, item.query))
                    except Exception as exc:
                        self._failure(exc)
                        item.future.set_exception(exc)
                with self._lock:
                    if not self._accepting and self._queue.empty() and not self._images:
                        break
        except BaseException as exc:
            self._failure(exc)
            if not self.initial_selection.done():
                self.initial_selection.set_exception(RuntimeError(self._error))
            if isinstance(item, (HistorySubmission, _Fact)):
                with self._lock:
                    self._failed += 1
                    self._loss_run = (
                        item.context.run_id
                        if isinstance(item, HistorySubmission)
                        else item.run
                    )
                    self._loss_persisted = False
            elif isinstance(item, _Read) and not item.future.done():
                item.future.set_exception(RuntimeError(self._error))
            elif isinstance(item, _Operation):
                item.failed(RuntimeError(self._error))
        finally:
            with self._lock:
                self._accepting = False
                if self._images:
                    self._image_links_lost += len(self._images)
                    self._image_link_error = self._error or "History stopped"
                    self._images.clear()
            while True:
                try:
                    pending = self._queue.get_nowait()
                except Empty:
                    break
                if isinstance(pending, (HistorySubmission, _Fact)):
                    with self._lock:
                        self._failed += 1
                elif isinstance(pending, _Read):
                    pending.future.set_exception(
                        RuntimeError(self._error or "History stopped")
                    )
                else:
                    pending.failed(RuntimeError(self._error or "History stopped"))
            if connection is not None:
                try:
                    connection.close()
                except Exception as exc:
                    self._failure(exc)
