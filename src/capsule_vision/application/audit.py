"""Append-only local audit evidence. One owner commits intents and terminal facts."""

import sqlite3
from collections.abc import Callable
from concurrent.futures import CancelledError, Future
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import UTC, datetime
from enum import Enum
from itertools import zip_longest
from pathlib import Path
from queue import Queue
from sqlite3 import connect
from threading import Event, Lock, Thread
from typing import cast
from uuid import uuid4

from capsule_vision.diagnostics import RuntimeDiagnostics

from .identity_models import (
    AuthorizationError,
    IdentityError,
    SessionSnapshot,
    StaleSessionError,
)


class AuditUnavailable(RuntimeError):
    """The critical operation has no confirmed durable intent."""


def _utc() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def differences(old: object, new: object) -> tuple[str, str]:
    """Compact leaf differences for the public Recipe, Vision and PLC definitions only."""
    before: list[str] = []
    after: list[str] = []

    def visit(path: str, a: object, b: object) -> None:
        if len(before) > 16 or a == b:
            return
        sample = b if b is not None else a
        if is_dataclass(sample) and not isinstance(sample, type):
            names = dict.fromkeys(
                f.name for value in (a, b) if is_dataclass(value) for f in fields(value)
            )
            if a is not None and b is not None and type(a) is not type(b):
                visit(path + ".type", type(a).__name__, type(b).__name__)
            for name in names:
                visit(
                    f"{path}.{name}" if path else name,
                    getattr(a, name, None),
                    getattr(b, name, None),
                )
        elif isinstance(sample, tuple):
            first = cast(tuple[object, ...], a) if isinstance(a, tuple) else ()
            second = cast(tuple[object, ...], b) if isinstance(b, tuple) else ()
            for i, (left, right) in enumerate(zip_longest(first, second)):
                visit(f"{path}[{i}]", left, right)
        else:

            def literal(v: object) -> str:
                return (
                    "—"
                    if v is None
                    else str(v.value if isinstance(v, Enum) else v)[:96]
                )

            before.append(f"{path} = {literal(a)}")
            after.append(f"{path} = {literal(b)}")

    visit("", old, new)
    return tuple_summary(before), tuple_summary(after)


def tuple_summary(lines: list[str]) -> str:
    return "\n".join(lines[:16] + (["…"] if len(lines) > 16 else []))


@dataclass(frozen=True, slots=True)
class AuditOperation:
    actor_id: str
    username: str
    action: str
    target: str
    before: str = ""
    after: str = ""
    id: str = field(default_factory=lambda: uuid4().hex)
    utc: str = field(default_factory=_utc)

    def values(self) -> tuple[str, ...]:
        return (
            self.id,
            self.utc,
            self.actor_id,
            self.username,
            self.action,
            self.target,
            self.before,
            self.after,
        )

    @classmethod
    def by(
        cls,
        actor: SessionSnapshot,
        action: str,
        target: str,
        before: str = "",
        after: str = "",
    ) -> AuditOperation:
        account = actor.account
        return cls(
            account.account_id if account else "guest",
            account.username if account else "Guest",
            action,
            target[:512],
            before[:2048],
            after[:2048],
        )


@dataclass(frozen=True, slots=True)
class AuditRecord:
    cursor: int
    operation: AuditOperation
    outcome: str
    completed_utc: str
    detail: str


@dataclass(frozen=True, slots=True)
class AuditAccount:
    actor_id: str
    username: str

    @property
    def cursor(self) -> tuple[str, str]:
        return self.username, self.actor_id


@dataclass(frozen=True, slots=True)
class AuditQuery:
    since: str = ""
    until: str = ""
    actor: str = ""
    action: str = ""
    before: int | None = None
    newer: bool = False
    associated_only: bool = False
    account: AuditAccount | None = None

    def conditions(self) -> tuple[list[str], list[str | int]]:
        clauses: list[str] = []
        args: list[str | int] = []
        if self.associated_only:
            clauses.append(
                "o.action IN ('production.start','production.stop','batch.select','batch.end','batch.information','production.clear_statistics')"
            )
        for column, value in (
            ("o.utc >= ?", self.since),
            ("o.utc < ?", self.until),
            ("o.action = ?", self.action),
        ):
            if value:
                clauses.append(column)
                args.append(value)
        if self.actor:
            clauses.append("(o.username = ? OR o.actor_id = ?)")
            args.extend((self.actor, self.actor))
        if self.account is not None:
            clauses.append("o.actor_id = ? AND o.username = ?")
            args.extend((self.account.actor_id, self.account.username))
        if self.before is not None:
            clauses.append("o.rowid > ?" if self.newer else "o.rowid < ?")
            args.append(self.before)
        return clauses, args


def error_outcome(error: BaseException) -> str:
    if isinstance(error, StaleSessionError):
        return "stale"
    if isinstance(error, AuthorizationError):
        return "denied"
    if isinstance(error, CancelledError):
        return "cancelled"
    return "failed"


class AuditTrail:
    def __init__(
        self,
        path: Path,
        authorize_review: Callable[[int | None], int],
        *,
        capacity: int = 128,
        diagnostics: RuntimeDiagnostics | None = None,
    ) -> None:
        if capacity < 1:
            raise ValueError("Audit capacity must be positive")
        self.path = path
        self._diagnostics = diagnostics
        self._authorize_review = authorize_review
        self._queue: Queue[Callable[[sqlite3.Connection | None], None] | None] = Queue(
            capacity + 1
        )
        self._capacity = capacity
        self._lock = Lock()
        self._accepting = True
        self._lost = 0
        self._issue = ""
        self._thread = Thread(target=self._run, name="AuditTrail", daemon=False)
        self._thread.start()

    def observation(self) -> tuple[int, str]:
        with self._lock:
            return self._lost, self._issue

    def _submit[T](
        self, fn: Callable[[sqlite3.Connection], T], *, write: bool = True
    ) -> Future[T]:
        done: Future[T] = Future()
        done.set_running_or_notify_cancel()

        def execute(db: sqlite3.Connection | None) -> None:
            if db is None:
                with self._lock:
                    self._lost += int(write)
                    issue = self._issue
                done.set_exception(
                    AuditUnavailable(issue or "Audit database unavailable")
                )
                return
            try:
                with db:
                    result = fn(db)
            except (IdentityError, CancelledError) as exc:
                done.set_exception(exc)
            except Exception as exc:
                with self._lock:
                    self._lost += int(write)
                    self._issue = f"Audit {type(exc).__name__}: {exc}"
                if self._diagnostics:
                    self._diagnostics.record(
                        "audit.storage_issue",
                        stage="execute",
                        write=write,
                        error_type=type(exc).__name__,
                    )
                done.set_exception(AuditUnavailable(self._issue))
            else:
                done.set_result(result)

        with self._lock:
            if self._accepting and self._queue.qsize() < self._capacity:
                self._queue.put_nowait(execute)
            else:
                self._lost += int(write)
                self._issue = "Audit request capacity reached or owner closed"
                if self._diagnostics:
                    self._diagnostics.record(
                        "audit.storage_issue",
                        stage="handoff",
                        write=write,
                        lost=self._lost,
                    )
                done.set_exception(AuditUnavailable(self._issue))
        return done

    def _run(self) -> None:
        db: sqlite3.Connection | None = None
        try:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                db = connect(self.path, timeout=1)
                db.execute("PRAGMA foreign_keys=ON")
                db.execute("PRAGMA synchronous=FULL")
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if (
                    version == 0
                    and not db.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
                ):
                    db.executescript("""
                        BEGIN IMMEDIATE;
                        CREATE TABLE operations(id TEXT PRIMARY KEY, utc TEXT NOT NULL,
                            actor_id TEXT NOT NULL, username TEXT NOT NULL, action TEXT NOT NULL,
                            target TEXT NOT NULL, old_value TEXT NOT NULL, new_value TEXT NOT NULL);
                        CREATE INDEX audit_time ON operations(utc);
                        CREATE INDEX audit_actor ON operations(username);
                        CREATE INDEX audit_actor_id ON operations(actor_id);
                        CREATE INDEX audit_action ON operations(action);
                        CREATE TABLE completions(id TEXT PRIMARY KEY REFERENCES operations(id),
                            utc TEXT NOT NULL, outcome TEXT NOT NULL, detail TEXT NOT NULL);
                        PRAGMA user_version=1;
                        COMMIT;
                    """)
                elif version != 1:
                    raise AuditUnavailable("Unsupported audit database")
                db.execute(
                    "SELECT o.id,o.utc,actor_id,username,action,target,old_value,new_value,c.utc,outcome,detail FROM operations o LEFT JOIN completions c USING(id) LIMIT 0"
                )
            except Exception as exc:
                if db is not None:
                    db.close()
                    db = None
                with self._lock:
                    self._issue = str(exc)
                if self._diagnostics:
                    self._diagnostics.record(
                        "audit.storage_issue",
                        stage="open",
                        error_type=type(exc).__name__,
                    )
            while (work := self._queue.get()) is not None:
                work(db)
        finally:
            if db is not None:
                db.close()

    @staticmethod
    def _intent(db: sqlite3.Connection, op: AuditOperation) -> None:
        values = op.values()
        if not db.execute(
            "INSERT INTO operations VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
            values,
        ).rowcount:
            if (
                db.execute("SELECT * FROM operations WHERE id=?", (op.id,)).fetchone()
                != values
            ):
                raise ValueError("Conflicting audit operation identity")

    @staticmethod
    def _terminal(
        db: sqlite3.Connection,
        op: AuditOperation,
        outcome: str,
        detail: str,
        stamp: str,
    ) -> None:
        if outcome not in {
            "success",
            "failed",
            "stale",
            "denied",
            "uncertain",
            "cancelled",
        }:
            raise ValueError("Invalid audit outcome")
        if not db.execute(
            "INSERT INTO completions VALUES(?,?,?,?) ON CONFLICT(id) DO NOTHING",
            (op.id, stamp, outcome, detail),
        ).rowcount:
            if db.execute(
                "SELECT outcome,detail FROM completions WHERE id=?", (op.id,)
            ).fetchone() != (outcome, detail):
                raise ValueError("Conflicting audit completion")

    def intent(self, op: AuditOperation) -> Future[None]:
        return self._submit(lambda db: self._intent(db, op))

    def complete(
        self, op: AuditOperation, outcome: str, detail: str = ""
    ) -> Future[None]:
        stamp = _utc()

        def finish(db: sqlite3.Connection) -> None:
            if (
                db.execute("SELECT * FROM operations WHERE id=?", (op.id,)).fetchone()
                != op.values()
            ):
                raise ValueError("Unknown or conflicting audit operation identity")
            self._terminal(db, op, outcome, detail[:1024], stamp)

        return self._submit(finish)

    def record(self, op: AuditOperation, outcome: str, detail: str = "") -> None:
        """Best effort. Admission, recording loss and completion never wait on a caller."""

        stamp = _utc()

        def append(db: sqlite3.Connection) -> None:
            self._intent(db, op)
            self._terminal(db, op, outcome, detail[:1024], stamp)

        self._submit(append)

    def observe[T](self, op: AuditOperation, future: Future[T]) -> Future[T]:
        def finished(done: Future[T]) -> None:
            error = CancelledError() if done.cancelled() else done.exception()
            self.record(
                op,
                error_outcome(error) if error else "success",
                type(error).__name__ if error else "",
            )

        future.add_done_callback(finished)
        return future

    def accounts(
        self,
        search: str = "",
        cursor: tuple[str, str] | None = None,
        revision: int | None = None,
    ) -> Future[tuple[AuditAccount, ...]]:
        """Discover recorded actors, independently of current accounts or record filters."""
        revision = self._authorize_review(revision)

        def read(db: sqlite3.Connection) -> tuple[AuditAccount, ...]:
            self._authorize_review(revision)
            clauses = ["instr(lower(username),lower(?))>0"]
            args = [search]
            if cursor is not None:
                clauses.append("(username,actor_id)>(?,?)")
                args.extend(cursor)
            rows = db.execute(
                "SELECT DISTINCT actor_id,username FROM operations WHERE "
                + " AND ".join(clauses)
                + " ORDER BY username,actor_id LIMIT 51",
                args,
            )
            accounts = tuple(AuditAccount(*row) for row in rows)
            self._authorize_review(revision)
            return accounts

        return self._submit(read, write=False)

    def page(
        self, query: AuditQuery, revision: int | None = None
    ) -> Future[tuple[AuditRecord, ...]]:
        revision = self._authorize_review(revision)

        def read(db: sqlite3.Connection) -> tuple[AuditRecord, ...]:
            self._authorize_review(revision)
            clauses, args = query.conditions()
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            order = "ASC" if query.newer else "DESC"
            rows = db.execute(
                "SELECT o.rowid,o.actor_id,o.username,o.action,o.target,o.old_value,o.new_value,o.id,o.utc,"
                "coalesce(c.outcome,'unresolved'),coalesce(c.utc,''),coalesce(c.detail,'') "
                "FROM operations o LEFT JOIN completions c USING(id)"
                + where
                + f" ORDER BY o.rowid {order} LIMIT 51",
                args,
            )
            records = tuple(
                AuditRecord(row[0], AuditOperation(*row[1:9]), *row[9:]) for row in rows
            )
            self._authorize_review(revision)
            return records

        return self._submit(read, write=False)

    def capture_report(
        self, query: AuditQuery, snapshot: Path, cancel: Event, revision: int
    ) -> Future[None]:
        """Freeze matching operation/completion facts on the existing Audit owner."""
        self._authorize_review(revision)

        def capture(db: sqlite3.Connection) -> None:
            self._authorize_review(revision)
            if cancel.is_set():
                raise CancelledError()
            clauses, args = query.conditions()
            db.execute("ATTACH DATABASE ? AS report", (str(snapshot),))
            try:
                with db:
                    db.execute("BEGIN")
                    db.set_progress_handler(lambda: int(cancel.is_set()), 4096)
                    db.execute(
                        """CREATE TABLE report.audit_records AS SELECT
                        o.rowid AS cursor,o.actor_id,o.username,o.action,o.target,
                        o.old_value,o.new_value,o.id,o.utc,coalesce(c.outcome,'unresolved') AS outcome,
                        coalesce(c.utc,'') AS completed,coalesce(c.detail,'') AS detail
                        FROM operations o LEFT JOIN completions c USING(id) WHERE """
                        + (" AND ".join(clauses) or "1"),
                        args,
                    )
                    db.execute(
                        "CREATE UNIQUE INDEX report.audit_cursor ON audit_records(cursor)"
                    )
                    self._authorize_review(revision)
                    if cancel.is_set():
                        raise CancelledError()
            except sqlite3.OperationalError:
                if cancel.is_set():
                    raise CancelledError() from None
                raise
            finally:
                db.set_progress_handler(None, 0)
                db.execute("DETACH DATABASE report")

        return self._submit(capture, write=False)

    def close(self) -> None:
        """Drain accepted records; only called off dispatch by the owning worker."""
        with self._lock:
            if self._accepting:
                self._accepting = False
                self._queue.put_nowait(None)  # Reserved shutdown slot.
        self._thread.join()
