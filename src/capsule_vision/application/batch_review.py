"""Review of named production Batches on the existing History SQLite owner."""

import json
import sqlite3
from dataclasses import dataclass

from .batch_records import run_totals
from .models import PilotCounters
from .records_query import RUN_OVERLAP


@dataclass(frozen=True, slots=True)
class StartAccount:
    account_id: str
    username: str


@dataclass(frozen=True, slots=True)
class BatchInformation:
    product: str = ""
    specification: str = ""
    notes: str = ""

    def __post_init__(self) -> None:
        for value, maximum in (
            (self.product, 96),
            (self.specification, 96),
            (self.notes, 384),
        ):
            if (
                type(value) is not str
                or len(value) > maximum
                or (value and (value != value.strip() or not value.isprintable()))
            ):
                raise ValueError(
                    f"Batch information requires up to {maximum} printable characters"
                )

    def changes(self, requested: BatchInformation) -> tuple[str, str]:
        before: list[str] = []
        after: list[str] = []
        for name, old, new in (
            ("product", self.product, requested.product),
            ("specification", self.specification, requested.specification),
            ("notes", self.notes, requested.notes),
        ):
            if old != new:
                before.append(f"{name} = {old or '—'}")
                after.append(f"{name} = {new or '—'}")
        return "\n".join(before), "\n".join(after)


class BatchInformationConflict(ValueError):
    """The Batch or the information shown when the editor opened has changed."""


@dataclass(frozen=True, slots=True)
class BatchSetup:
    uuid: str | None
    information: BatchInformation


def setup(db: sqlite3.Connection, label: str) -> BatchSetup:
    row = db.execute("SELECT uuid FROM batches WHERE label=?", (label,)).fetchone()
    return (
        BatchSetup(row[0], information(db, row[0])[1])
        if row
        else BatchSetup(None, BatchInformation())
    )


def save_information(
    db: sqlite3.Connection, batch: str, info: BatchInformation
) -> None:
    db.execute(
        """INSERT INTO batch_information VALUES (?,?,?,?)
        ON CONFLICT(batch) DO UPDATE SET product=excluded.product,
        specification=excluded.specification,notes=excluded.notes""",
        (batch, info.product, info.specification, info.notes),
    )


@dataclass(frozen=True, slots=True)
class BatchRow:
    uuid: str
    label: str
    activity: float
    first: float | None
    last: float | None
    totals: PilotCounters
    first_run: float | None
    recent_run: float | None
    recent_outcome: str | None

    @property
    def cursor(self) -> tuple[float, str]:
        return self.activity, self.label


@dataclass(frozen=True, slots=True)
class BatchList:
    rows: tuple[BatchRow, ...]
    more: bool


@dataclass(frozen=True, slots=True)
class RunRow:
    uuid: str
    started: float
    terminated: float | None
    account: StartAccount | None
    outcome: str
    totals: PilotCounters | None
    uncertain: bool


@dataclass(frozen=True, slots=True)
class BatchDetail:
    batch: BatchRow
    info: BatchInformation
    runs: tuple[RunRow, ...]
    run_count: int
    uncertain: bool
    expired: bool
    offset: int
    run_anchor: int


def initialize(db: sqlite3.Connection) -> None:
    """Add companion tables to supported History versions without rewriting existing facts."""
    db.execute("CREATE INDEX IF NOT EXISTS batch_run_start ON runs(batch,started)")
    db.execute("""CREATE TABLE IF NOT EXISTS run_start_accounts (
        run TEXT PRIMARY KEY REFERENCES runs(uuid) ON DELETE CASCADE,
        account_id TEXT NOT NULL, username TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS batch_information (
        batch TEXT PRIMARY KEY REFERENCES batches(uuid), product TEXT NOT NULL,
        specification TEXT NOT NULL, notes TEXT NOT NULL)""")
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='batch_inspection_times'"
    ).fetchone():
        db.execute("""CREATE TABLE batch_inspection_times (
            batch TEXT PRIMARY KEY REFERENCES batches(uuid), first REAL NOT NULL, last REAL NOT NULL)""")
        # Both endpoints are recorded Cycle completions. Detail that already expired
        # cannot be recovered from the Run's start/end or from an audit timestamp.
        db.execute("""INSERT INTO batch_inspection_times
            SELECT b.uuid,(SELECT min(completed_at) FROM cycles WHERE batch_segment=b.uuid),
                (SELECT max(completed_at) FROM cycles WHERE batch_segment=b.uuid)
            FROM batches b WHERE EXISTS(SELECT 1 FROM cycles WHERE batch_segment=b.uuid)""")


def observed(db: sqlite3.Connection, segment: str, stamp: float) -> None:
    db.execute(
        """INSERT INTO batch_inspection_times SELECT uuid,?,? FROM batches WHERE uuid=?
        ON CONFLICT(batch) DO UPDATE SET first=min(first,excluded.first),last=max(last,excluded.last)""",
        (stamp, stamp, segment),
    )


# At most one Run is open. Only its own retained evidence can advance its
# displayed activity beyond Start, including when the Batch has earlier Runs.
_ROWS = """WITH active AS MATERIALIZED (
    SELECT r.batch, max(r.started,
        coalesce((SELECT max(c.completed_at) FROM cycles c
                  WHERE c.batch_segment=r.batch AND c.run_id=r.uuid), r.started),
        coalesce((SELECT max(g.observed) FROM plc_groups g WHERE g.run=r.uuid), r.started)
    ) AS observed FROM runs r WHERE r.outcome='open' AND r.batch IS NOT NULL
), overview AS (
    SELECT b.uuid,b.label,b.created,t.first,t.last,b.positions,
        (SELECT min(started) FROM runs WHERE batch=b.uuid) AS first_run,
        coalesce(active.observed,ended.terminated) AS recent_run,
        CASE WHEN active.observed IS NULL THEN ended.outcome ELSE 'open' END AS recent_outcome
    FROM batches b
    LEFT JOIN batch_inspection_times t ON t.batch=b.uuid
    LEFT JOIN active ON active.batch=b.uuid
    LEFT JOIN runs ended ON ended.rowid=(
        SELECT rowid FROM runs WHERE batch=b.uuid AND terminated IS NOT NULL
        ORDER BY terminated DESC,rowid DESC LIMIT 1)
)
SELECT uuid,label,coalesce(recent_run,created) AS activity,first,last,positions,
    first_run,recent_run,recent_outcome FROM overview b"""


def _overview(
    row: tuple[
        str,
        str,
        float,
        float | None,
        float | None,
        str,
        float | None,
        float | None,
        str | None,
    ],
) -> BatchRow:
    uid, label, activity, first, last, positions, first_run, recent_run, outcome = row
    return BatchRow(
        uid,
        label,
        activity,
        first,
        last,
        PilotCounters(**json.loads(positions)),
        first_run,
        recent_run,
        outcome,
    )


def page(
    db: sqlite3.Connection,
    prefix: str,
    cursor: tuple[float, str] | None,
    newer: bool,
    since: int | None,
    until: int | None,
    now: float,
    batch: str | None = None,
    unbatched: bool = False,
) -> BatchList:
    if unbatched:
        return BatchList((), False)  # Absence is not a fictitious aggregate Batch.
    if since is not None and until is not None and since >= until:
        return BatchList((), False)
    clauses = ["substr(b.label,1,length(:prefix))=:prefix"]
    args: dict[str, object] = {
        "prefix": prefix,
        "since": since,
        "until": until,
        "now": now,
    }
    if batch is not None:
        clauses.append("b.uuid=:batch")
        args["batch"] = batch
    if since is not None or until is not None:
        # A Run starting on this day counts even when it stops at the same instant.
        # An earlier Run ending at midnight contributes only to the preceding day.
        clauses.append(
            "EXISTS (SELECT 1 FROM runs r WHERE r.batch=b.uuid AND " + RUN_OVERLAP + ")"
        )
    if cursor is not None:
        clauses.append(
            "(activity,b.label) " + (">" if newer else "<") + " (:activity,:label)"
        )
        args["activity"], args["label"] = cursor
    order = "ASC" if newer else "DESC"
    records = db.execute(
        _ROWS
        + " WHERE "
        + " AND ".join(clauses)
        + f" ORDER BY activity {order},b.label {order} LIMIT 51",
        args,
    ).fetchall()
    rows = tuple(_overview(row) for row in records[:50])
    return BatchList(rows[::-1] if newer else rows, len(records) > 50)


def information(db: sqlite3.Connection, batch: str) -> tuple[str, BatchInformation]:
    row = db.execute(
        """SELECT b.label,i.product,i.specification,i.notes FROM batches b
        LEFT JOIN batch_information i ON i.batch=b.uuid WHERE b.uuid=?""",
        (batch,),
    ).fetchone()
    if row is None:
        raise BatchInformationConflict("Production Batch unavailable")
    return row[0], BatchInformation(*row[1:]) if row[
        1
    ] is not None else BatchInformation()


def detail(
    db: sqlite3.Connection,
    batch: str,
    offset: int,
    loss_run: str | None,
    run_anchor: int | None,
) -> BatchDetail:
    if offset < 0:
        raise ValueError("Invalid Run page")
    _, info = information(db, batch)
    row = db.execute(_ROWS + " WHERE b.uuid=?", (batch,)).fetchone()
    assert row is not None
    overview = _overview(row)
    if run_anchor is None:
        run_anchor = int(
            db.execute(
                "SELECT coalesce(max(rowid),0) FROM runs WHERE batch=?", (batch,)
            ).fetchone()[0]
        )
    count, uncertain, expired = db.execute(
        """SELECT count(*) FILTER (WHERE rowid<=?),max(uncertain OR uuid IS ?),
        max(detail_expired) FROM runs WHERE batch=?""",
        (run_anchor, loss_run, batch),
    ).fetchone()
    runs = tuple(
        RunRow(
            run,
            start,
            end,
            StartAccount(account, name) if account is not None else None,
            outcome,
            run_totals(positions, mode),
            bool(loss),
        )
        for run, start, end, account, name, outcome, positions, mode, loss in db.execute(
            """SELECT r.uuid,r.started,r.terminated,a.account_id,a.username,r.outcome,
            p.positions,r.reporting_mode,r.uncertain OR r.uuid IS ? FROM runs r
            LEFT JOIN run_start_accounts a ON a.run=r.uuid
            LEFT JOIN run_positions p ON p.run=r.uuid WHERE r.batch=? AND r.rowid<=?
            ORDER BY r.started DESC,r.rowid DESC LIMIT 50 OFFSET ?""",
            (loss_run, batch, run_anchor, offset),
        )
    )
    return BatchDetail(
        overview, info, runs, count, bool(uncertain), bool(expired), offset, run_anchor
    )
