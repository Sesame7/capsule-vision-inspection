"""Read-only Records queries for one stationary, self-contained History copy."""

import sqlite3
from collections.abc import Callable, Generator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import BoundedSemaphore
from time import time

from . import alarm_records, batch_review, ng_capsules, records_query
from .alarm_records import AlarmQuery, AlarmRecord
from .batch_review import BatchDetail, BatchList
from .history import (
    HISTORY_SCHEMA_VERSION,
    HistoryPageData,
    HistoryQuery,
    read_history_page,
    read_ng_capsule,
    request_ng_capsule_page,
)
from .records_query import BatchChoices, RecordsScope
from .report_scope import register_result_filter

_DATABASE_NAME = "history.sqlite3"
_REVIEW_COLUMNS = {
    "cycles": (
        "id completed_at batch_segment batch_label run_id cycle_id recipe_id model_id "
        "model_sha status technical judgment empty_count unknown_count detail"
    ),
    "batches": "uuid label created positions",
    "runs": "uuid batch started terminated outcome uncertain detail_expired reporting_mode",
    "plc_groups": "run first_cycle observed final_channels",
    "group_members": "run cycle first_cycle slot",
    "batch_inspection_times": "batch first last",
    "batch_information": "batch product specification notes",
    "run_start_accounts": "run account_id username",
    "run_positions": "run positions",
}


class HistoryReviewError(ValueError):
    """The supplied directory is not a usable offline History package."""


@dataclass(frozen=True, slots=True)
class _ReviewSchema:
    has_images: bool
    alarm_translations: bool | None  # None: absent; False: original bilingual table.


def _validate(db: sqlite3.Connection) -> _ReviewSchema:
    (version,) = db.execute("PRAGMA user_version").fetchone()
    if version != HISTORY_SCHEMA_VERSION:
        raise HistoryReviewError(
            f"Unsupported History schema version {version}; expected {HISTORY_SCHEMA_VERSION}"
        )
    tables: dict[str, str] = {
        name: kind for _, name, kind, *_ in db.execute("PRAGMA main.table_list")
    }
    required = _REVIEW_COLUMNS.copy()
    has_images = "archive_images" in tables
    if has_images:  # A valid History store need not have archived any images.
        required["archive_images"] = "run_id cycle_id camera_id path observed_at"
    if "cycle_area_filtered" in tables:
        required["cycle_area_filtered"] = "run cycle camera channel evidence"
    if "alarm_events" in tables:
        required["alarm_events"] = "id " + alarm_records.EVENT_COLUMNS.replace(",", " ")
    alarm_translations: bool | None = None
    for name, columns in required.items():
        if tables.get(name) != "table":
            raise HistoryReviewError(f"Incomplete History schema: missing table {name}")
        existing: set[str] = {
            column for _, column, *_ in db.execute(f"PRAGMA main.table_info({name})")
        }
        if missing := set(columns.split()) - existing:
            raise HistoryReviewError(
                f"Incomplete History schema: {name} lacks {', '.join(sorted(missing))}"
            )
        if name == "alarm_events":
            alarm_translations = "translations" in existing
    integrity = db.execute("PRAGMA integrity_check(1)").fetchone()
    if integrity != ("ok",):
        raise HistoryReviewError(
            f"Inconsistent History database: {integrity[0] if integrity else 'integrity check failed'}"
        )
    if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise HistoryReviewError(
            "Inconsistent History database: broken record references"
        )
    return _ReviewSchema(has_images, alarm_translations)


def _open(root: Path) -> tuple[sqlite3.Connection, _ReviewSchema]:
    database = root / _DATABASE_NAME
    try:
        located = database.resolve(strict=True)
    except FileNotFoundError as exc:
        raise HistoryReviewError(f"Missing {_DATABASE_NAME}") from exc
    if located != database or not database.is_file():
        raise HistoryReviewError(
            f"{_DATABASE_NAME} must be a file in the supplied directory"
        )
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = database.with_name(database.name + suffix)
        try:
            sidecar.lstat()
        except FileNotFoundError:
            continue
        raise HistoryReviewError(
            f"Found {sidecar.name}; supply a complete, closed History copy without SQLite sidecars"
        )
    # Immutable mode never creates WAL/SHM files or attempts rollback recovery.
    # The package must remain unchanged by other processes while it is open.
    db = sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA trusted_schema=OFF")
        db.execute("PRAGMA temp_store=MEMORY")
        schema = _validate(db)
        register_result_filter(db)
        return db, schema
    except BaseException:
        db.close()
        raise


class HistoryReview:
    """Records reads serialized on one read-only SQLite connection."""

    def __init__(
        self,
        root: Path,
        executor: ThreadPoolExecutor,
        connection: sqlite3.Connection,
        schema: _ReviewSchema,
    ) -> None:
        self.root = root
        self._executor = executor
        self._connection = connection
        self._schema = schema
        self._pending = BoundedSemaphore(16)

    def _submit[T](self, read: Callable[[sqlite3.Connection], T]) -> Future[T]:
        if not self._pending.acquire(blocking=False):
            unavailable: Future[T] = Future()
            unavailable.set_exception(
                RuntimeError("History review request capacity reached")
            )
            return unavailable
        try:
            future = self._executor.submit(read, self._connection)
        except RuntimeError:
            self._pending.release()
            raise
        future.add_done_callback(lambda _: self._pending.release())
        return future

    def query(self, query: HistoryQuery | None = None) -> Future[HistoryPageData]:
        return self._submit(
            lambda db: read_history_page(
                db, query or HistoryQuery(), has_images=self._schema.has_images
            )
        )

    def ng_capsule_page(
        self,
        scope: RecordsScope,
        cursor: ng_capsules.CapsuleRow | None = None,
        newer: bool = False,
        mode: ng_capsules.CapsuleFilter = ng_capsules.CapsuleFilter.NG,
        area_label: str | None = None,
        *,
        include_area_labels: bool = False,
    ) -> Future[ng_capsules.CapsulePage]:
        return request_ng_capsule_page(
            self._submit, scope, cursor, newer, mode, area_label, include_area_labels
        )

    def ng_capsule_detail(
        self, key: ng_capsules.CapsuleKey
    ) -> Future[ng_capsules.CapsuleDetail | None]:
        return self._submit(
            lambda db: read_ng_capsule(db, key, has_images=self._schema.has_images)
        )

    def batch_page(
        self,
        *,
        cursor: tuple[float, str] | None = None,
        newer: bool = False,
        since: int | None = None,
        until: int | None = None,
        batch: str | None = None,
        unbatched: bool = False,
    ) -> Future[BatchList]:
        return self._submit(
            lambda db: batch_review.page(
                db, cursor, newer, since, until, time(), batch, unbatched
            )
        )

    def batch_detail(
        self, batch: str, offset: int = 0, run_anchor: int | None = None
    ) -> Future[BatchDetail]:
        return self._submit(
            lambda db: batch_review.detail(db, batch, offset, None, run_anchor)
        )

    def batch_choices(
        self,
        scope: RecordsScope,
        search: str = "",
        cursor: tuple[float, str] | None = None,
        *,
        alarm_revision: int | None = None,
    ) -> Future[BatchChoices]:
        if alarm_revision is not None:
            raise HistoryReviewError("Portable review does not use equipment sessions")
        return self._submit(
            lambda db: records_query.batch_choices(
                db,
                scope,
                search,
                cursor,
                time(),
                alarms="retained"
                if self._schema.alarm_translations is not None
                else None,
            )
        )

    def alarm_page(self, query: AlarmQuery) -> Future[tuple[AlarmRecord, ...]]:
        def read(db: sqlite3.Connection) -> tuple[AlarmRecord, ...]:
            translated = self._schema.alarm_translations
            return (
                alarm_records.page(db, query, None, translated=translated)
                if translated is not None
                else ()
            )

        return self._submit(read)


@contextmanager
def open_history_review(directory: Path) -> Generator[HistoryReview]:
    """Validate before creating Qt; close the connection on its own thread."""
    with ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="HistoryReview"
    ) as executor:
        try:
            root = directory.resolve(strict=True)
            if not root.is_dir():
                raise HistoryReviewError(
                    "Expected a directory containing history.sqlite3"
                )
            db, schema = executor.submit(_open, root).result()
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise HistoryReviewError(
                f"Cannot open History package {directory}: {exc}"
            ) from exc
        try:
            yield HistoryReview(root, executor, db, schema)
        finally:
            # The window detaches its consumers before this drain, outside Qt dispatch.
            executor.submit(db.close).result()
