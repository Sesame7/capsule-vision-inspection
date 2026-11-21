"""Stored History filters and the scopes of a manual production report."""

import json
import sqlite3
import zlib
from concurrent.futures import CancelledError
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import Literal

type ResultState = Literal["OK", "NG", "EMPTY", "UNKNOWN", "ERROR"]
type ResultFilter = frozenset[ResultState]
RESULT_STATES: tuple[ResultState, ...] = ("OK", "NG", "EMPTY", "UNKNOWN", "ERROR")
ALL_RESULTS: ResultFilter = frozenset(RESULT_STATES)


def result_code(states: ResultFilter) -> str:
    return (
        "all"
        if states == ALL_RESULTS
        else ", ".join(
            "ERR" if state == "ERROR" else state
            for state in RESULT_STATES
            if state in states
        )
        or "—"
    )


def register_result_filter(db: sqlite3.Connection) -> None:
    """Read existing immutable channel facts on the SQLite owner, before paging.

    Older stores have no scalar OK/NG counts. Do not rewrite them or substitute
    the Cycle's aggregate judgment for coexisting channel business states.
    """

    def business_mask(detail: bytes) -> int:
        channels: list[list[object]] = json.loads(zlib.decompress(detail))[0]
        return int(any(c[3] == "OK" for c in channels)) | (
            int(any(c[3] == "NG" for c in channels)) << 1
        )

    db.create_function("history_business_mask", 1, business_mask, deterministic=True)


type ReportType = Literal["batch", "defects"]


@dataclass(frozen=True, slots=True)
class HistoryQuery:
    since: float | None = None
    until: float | None = None
    batch_segment: str | None = None
    result: ResultFilter = ALL_RESULTS
    cursor: tuple[float, int] | None = None
    newer: bool = False
    limit: int = 50
    unbatched: bool = False

    def __post_init__(self) -> None:
        if type(self.limit) is not int or not 1 <= self.limit <= 50:
            raise ValueError("History page limit must be 1..50")
        if type(self.result) is not frozenset or not self.result <= ALL_RESULTS:
            raise ValueError("Unknown History result filter")

    def conditions(self) -> tuple[list[str], list[object]]:
        clauses: list[str] = []
        args: list[object] = []
        for field_name, operator, value in (
            ("completed_at", ">=", self.since),
            ("completed_at", "<", self.until),
            ("batch_segment", "=", self.batch_segment),
        ):
            if value is not None:
                clauses.append(f"{field_name} {operator} ?")
                args.append(value)
        if self.result != ALL_RESULTS:
            states: list[str] = []
            for state, condition in (
                ("EMPTY", "empty_count > 0"),
                ("UNKNOWN", "unknown_count > 0"),
                ("ERROR", "technical = 'ERROR'"),
            ):
                if state in self.result:
                    states.append(condition)
            mask = int("OK" in self.result) | (int("NG" in self.result) << 1)
            if mask:
                states.append(f"(history_business_mask(detail) & {mask}) != 0")
            clauses.append("(" + " OR ".join(states) + ")" if states else "0")
        if self.unbatched:
            clauses.append("batch_label IS NULL")
        return clauses, args


@dataclass(frozen=True, slots=True)
class ReportRequest:
    scope: ReportType
    batch: str = field(kw_only=True)

    def __post_init__(self) -> None:
        if self.scope not in ("batch", "defects"):
            raise ValueError("Unsupported Batch report type")
        if type(self.batch) is not str or not self.batch.strip():
            raise ValueError("A persisted Batch identity is required")


def batch_report_request(value: object) -> ReportRequest:
    if isinstance(value, str):
        return ReportRequest("batch", batch=value)
    if isinstance(value, ReportRequest):
        return value
    raise ValueError("Unsupported report request")


def capture_report(
    db: sqlite3.Connection,
    request: ReportRequest,
    snapshot: Path,
    now: float,
    loss_run: str | None,
    cancel: Event,
) -> None:
    if cancel.is_set():
        raise CancelledError()
    db.execute("ATTACH DATABASE ? AS report", (str(snapshot),))
    try:
        with db:
            db.execute("BEGIN")
            db.set_progress_handler(lambda: int(cancel.is_set()), 4096)
            try:
                _capture(db, request, now, loss_run)
            finally:
                db.set_progress_handler(None, 0)
    except sqlite3.OperationalError:
        if cancel.is_set():
            raise CancelledError() from None
        raise
    finally:
        db.execute("DETACH DATABASE report")


def _capture(
    db: sqlite3.Connection, request: ReportRequest, now: float, loss_run: str | None
) -> None:
    db.execute(
        "CREATE TABLE report.meta AS SELECT ? AS scope, ? AS observed",
        (request.scope, now),
    )
    db.execute(
        """CREATE TABLE report.batch AS SELECT b.uuid,b.label,b.occurrences,b.positions,
        i.product,i.specification,i.notes,t.first,t.last
        FROM main.batches b LEFT JOIN main.batch_information i ON i.batch=b.uuid
        LEFT JOIN main.batch_inspection_times t ON t.batch=b.uuid WHERE b.uuid=?""",
        (request.batch,),
    )
    if not db.execute("SELECT 1 FROM report.batch").fetchone():
        raise ValueError("Production Batch unavailable")
    db.execute(
        """CREATE TABLE report.runs AS SELECT r.uuid,
        row_number() OVER (ORDER BY started,r.rowid) AS number,
        started,terminated,outcome,reporting_mode,uncertain OR r.uuid IS ? AS uncertain,
        detail_expired,a.username,p.positions FROM main.runs r
        LEFT JOIN main.run_start_accounts a ON a.run=r.uuid
        LEFT JOIN main.run_positions p ON p.run=r.uuid WHERE r.batch=?""",
        (loss_run, request.batch),
    )
    if request.scope == "defects":
        for table in ("run_coverage", "run_categories"):
            db.execute(f"""CREATE TABLE report.{table} AS SELECT c.* FROM main.{table} c
                JOIN report.runs r ON r.uuid=c.run""")
