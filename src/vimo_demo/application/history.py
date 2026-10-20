"""Best-effort, bounded Production history; one SQLite owner, no Qt or pixels."""

import json
import sqlite3
import zlib
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Lock, Thread
from time import time
from typing import Literal, cast

from vimo_demo.inspection.production import ProductionInspectionResult

from .models import ConfigurationIdentity

type ResultFilter = Literal["all", "OK", "NG", "ERROR", "EMPTY", "UNKNOWN"]


@dataclass(frozen=True, slots=True)
class HistoryContext:
    completed_at: float  # Host wall-clock observation, never camera/monotonic time.
    batch_segment: str
    batch_label: str | None
    run_id: str
    active: ConfigurationIdentity
    recipe_name: str | None = None  # Absent in earlier H1 records; never backfilled.


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


@dataclass(frozen=True, slots=True)
class HistoryQuery:
    since: float | None = None
    until: float | None = None
    batch_segment: str | None = None
    result: ResultFilter = "all"
    cursor: tuple[float, int] | None = None
    newer: bool = False
    limit: int = 50

    def __post_init__(self) -> None:
        if type(self.limit) is not int or not 1 <= self.limit <= 50:
            raise ValueError("History page limit must be 1..50")
        if self.result not in ("all", "OK", "NG", "ERROR", "EMPTY", "UNKNOWN"):
            raise ValueError("Unknown History result filter")


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


@dataclass(frozen=True, slots=True)
class HistorySubmission:
    context: HistoryContext
    result: ProductionInspectionResult


@dataclass(frozen=True, slots=True)
class _Read:
    query: HistoryQuery
    future: Future[HistoryPageData]


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
    ) -> None:
        if max_pending < 1 or retention_batch < 1:
            raise ValueError("History bounds must be positive")
        self.path = path
        self.clock = clock
        self.retention_batch = retention_batch
        self._queue: Queue[HistorySubmission | _Read | None] = Queue(max_pending)
        self._lock = Lock()
        self._accepting, self._ready = True, False
        self._accepted = self._persisted = self._dropped = self._failed = 0
        self._error = ""
        self._thread = Thread(target=self._run, name="ProductionHistory", daemon=False)
        self._thread.start()

    def offer(
        self, context: HistoryContext, result: ProductionInspectionResult
    ) -> bool:
        """Non-waiting admission only; never means a committed record."""
        with self._lock:
            if self._accepting:
                try:
                    self._queue.put_nowait(HistorySubmission(context, result))
                except Full:
                    self._error = "History capacity reached; record not admitted"
                else:
                    self._accepted += 1
                    return True
            self._dropped += 1
            return False

    def query(self, query: HistoryQuery | None = None) -> Future[HistoryPageData]:
        future: Future[HistoryPageData] = Future()
        future.set_running_or_notify_cancel()
        with self._lock:
            try:
                if not self._accepting:
                    raise RuntimeError(self._error or "History is closed")
                self._queue.put_nowait(_Read(query or HistoryQuery(), future))
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
            try:
                self._queue.put_nowait(
                    None
                )  # Wake an idle owner; full means work exists.
            except Full:
                pass

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
            )

    def _failure(self, error: BaseException) -> None:
        detail = f"{type(error).__name__}: {error}"[:2000]
        with self._lock:
            self._error = detail

    def _open(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=0.2)
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError("Unsupported History database version")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript("""
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
                PRAGMA user_version=1;
                COMMIT;
            """)
            return connection
        except BaseException:
            connection.close()
            raise

    def _write(self, connection: sqlite3.Connection, item: HistorySubmission) -> None:
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

    def _retain(self, connection: sqlite3.Connection) -> None:
        with connection:
            connection.execute(
                "DELETE FROM cycles WHERE id IN (SELECT id FROM cycles WHERE completed_at < ? "
                "ORDER BY completed_at,id LIMIT ?)",
                (self.clock() - 30 * 86400, self.retention_batch),
            )

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
        if query.batch_segment is not None:
            clauses.append("batch_segment = ?")
            args.append(query.batch_segment)
        if query.result in ("OK", "NG"):
            clauses.append("judgment = ?")
            args.append(query.result)
        elif query.result == "ERROR":
            clauses.append("technical = 'ERROR'")
        elif query.result in ("EMPTY", "UNKNOWN"):
            clauses.append(
                "empty_count > 0" if query.result == "EMPTY" else "unknown_count > 0"
            )
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
                "status,technical,judgment,detail FROM cycles"
                + where
                + f" ORDER BY completed_at {order},id {order} LIMIT ?",
                [*args, query.limit + 1],
            ).fetchall(),
        )
        records = tuple(self._decode(row) for row in rows[: query.limit])
        return HistoryPageData(
            tuple(reversed(records)) if query.newer else records,
            tuple(batches),
            len(rows) > query.limit,
        )

    @staticmethod
    def _decode(row: tuple[object, ...]) -> HistoryRecord:
        # Typed boundary for our versioned SQLite payload; corrupt data fails the
        # query instead of becoming fabricated judgments or affecting Production.
        detail: object = json.loads(zlib.decompress(cast(bytes, row[12])))
        payload = _items(detail)
        recipe_name = payload[2] if len(payload) > 2 else None
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
        cameras = tuple(
            HistoryCamera(
                str(r[0]), str(r[1]), str(r[2]), _strings(r[3]), _strings(r[4])
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
            int(cast(int, row[0])),
            context,
            int(cast(str, row[5])),
            str(row[9]),
            str(row[10]),
            str(row[11]),
            channels,
            cameras,
        )

    def _run(self) -> None:
        connection: sqlite3.Connection | None = None
        item: HistorySubmission | _Read | None = None
        writing = False
        try:
            connection = self._open()
            with self._lock:
                self._ready = True
            while True:
                try:
                    item = self._queue.get(
                        timeout=60
                    )  # Idle retention, interruptible by Stop.
                except Empty:
                    item = None
                writing = isinstance(item, HistorySubmission)
                try:
                    self._retain(connection)
                except Exception as exc:
                    self._failure(exc)
                if isinstance(item, HistorySubmission):
                    try:
                        self._write(connection, item)
                    except Exception as exc:
                        self._failure(exc)
                        with self._lock:
                            self._failed += 1
                    else:
                        with self._lock:
                            self._persisted += 1
                    writing = False
                elif isinstance(item, _Read):
                    try:
                        item.future.set_result(self._read(connection, item.query))
                    except Exception as exc:
                        self._failure(exc)
                        item.future.set_exception(exc)
                with self._lock:
                    if not self._accepting and self._queue.empty():
                        break
        except BaseException as exc:
            self._failure(exc)
            if writing:
                with self._lock:
                    self._failed += 1
            elif isinstance(item, _Read) and not item.future.done():
                item.future.set_exception(RuntimeError(self._error))
        finally:
            with self._lock:
                self._accepting = False
            while True:
                try:
                    pending = self._queue.get_nowait()
                except Empty:
                    break
                if isinstance(pending, HistorySubmission):
                    with self._lock:
                        self._failed += 1
                elif isinstance(pending, _Read):
                    pending.future.set_exception(
                        RuntimeError(self._error or "History stopped")
                    )
            if connection is not None:
                try:
                    connection.close()
                except Exception as exc:
                    self._failure(exc)
