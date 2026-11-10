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
type ReportBasis = Literal["capsule", "detections"]


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
    basis: ReportBasis
    batch: str | None = None
    filters: HistoryQuery = field(default_factory=HistoryQuery)

    def __post_init__(self) -> None:
        if self.scope not in ("batch", "defects") or self.basis not in (
            "capsule",
            "detections",
        ):
            raise ValueError("Unknown report type or counting basis")
        if self.scope == "batch":
            if not self.batch or self.basis != "capsule":
                raise ValueError(
                    "Batch reports use the selected Batch's cumulative totals"
                )
        elif self.batch is not None and self.basis != "capsule":
            raise ValueError(
                "Selected Batch classification uses durable position counts"
            )


@dataclass(frozen=True, slots=True)
class DefectScope:
    cycles: int
    groups: int
    batch_label: str | None
    covered_cycles: int = 0

    @property
    def attributable(self) -> bool:
        return self.cycles > 0 and self.cycles == self.covered_cycles


def _selected(filters: HistoryQuery) -> tuple[str, list[object]]:
    clauses, args = filters.conditions()
    return "SELECT * FROM main.cycles WHERE " + (" AND ".join(clauses) or "1"), args


def defect_scope(db: sqlite3.Connection, filters: HistoryQuery) -> DefectScope:
    """Whole-scope eligibility, never inferred from CycleId arithmetic or categories."""
    selected, args = _selected(filters)
    total = db.execute(f"SELECT count(*) FROM ({selected})", args).fetchone()[0]
    groups = covered = 0
    for encoded, size in db.execute(
        f"""WITH selected AS ({selected}) SELECT g.final_channels,
        CASE r.reporting_mode WHEN 'single_cycle' THEN 1 ELSE 4 END AS size
        FROM main.plc_groups g JOIN main.group_members m USING(run,first_cycle)
        JOIN main.runs r ON r.uuid=g.run
        LEFT JOIN selected c ON (c.run_id,c.cycle_id)=(m.run,m.cycle)
        GROUP BY g.run,g.first_cycle HAVING size>0 AND count(*)=size AND count(c.id)=size
        AND count(DISTINCT m.slot)=size AND min(m.slot)=1 AND max(m.slot)=size
        AND sum(m.slot=1 AND m.cycle=g.first_cycle)=1""",
        args,
    ):
        pairs: list[tuple[int, str]] = json.loads(encoded)
        finals = dict(pairs)
        if (
            len(pairs) in (30, 36)
            and len(finals) == len(pairs)
            and set(finals) == set(range(1, len(pairs) + 1))
            and set(finals.values()) <= {"OK", "NG", "EMPTY", "UNKNOWN"}
        ):
            groups += 1
            covered += size
    label = db.execute(
        "SELECT label FROM main.batches WHERE uuid=?", (filters.batch_segment,)
    ).fetchone()
    return DefectScope(total, groups, label[0] if label else None, covered)


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
    filters = request.filters
    batch = request.batch
    if request.scope == "defects" and batch is None:
        selected_batch = db.execute(
            "SELECT uuid FROM main.batches WHERE uuid=?", (filters.batch_segment,)
        ).fetchone()
        batch = selected_batch[0] if selected_batch else None
    if batch is not None and request.basis != "capsule":
        raise ValueError("Selected Batch classification uses durable position counts")
    scope = (
        defect_scope(db, filters)
        if request.scope == "defects" and batch is None
        else None
    )
    if scope is not None and request.basis == "capsule" and not scope.attributable:
        raise ValueError(
            "The scope no longer contains only complete attributable groups; select Detection Count"
        )
    db.execute(
        """CREATE TABLE report.meta AS SELECT ? AS scope,? AS basis,? AS observed,
        ? AS since,? AS until,? AS result,? AS scoped_segment,? AS filter_label,
        ? AS unbatched,? AS attributable""",
        (
            request.scope,
            request.basis,
            now,
            filters.since,
            filters.until,
            result_code(filters.result),
            filters.batch_segment is not None,
            scope.batch_label if scope else None,
            filters.unbatched,
            scope.attributable if scope else False,
        ),
    )
    if batch is not None:
        db.execute(
            """CREATE TABLE report.batch AS SELECT b.uuid,b.label,b.occurrences,b.positions,
            i.product,i.specification,i.notes,t.first,t.last
            FROM main.batches b LEFT JOIN main.batch_information i ON i.batch=b.uuid
            LEFT JOIN main.batch_inspection_times t ON t.batch=b.uuid WHERE b.uuid=?""",
            (batch,),
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
            (loss_run, batch),
        )
        if request.scope == "defects":
            for table in ("run_coverage", "run_categories"):
                db.execute(f"""CREATE TABLE report.{table} AS SELECT c.* FROM main.{table} c
                    JOIN report.runs r ON r.uuid=c.run""")
        return
    selected, args = _selected(filters)
    has_evidence = (
        db.execute(
            "SELECT 1 FROM main.sqlite_master WHERE name='cycle_classification'"
        ).fetchone()
        is not None
    )
    evidence = (
        "e.findings,e.complete" if has_evidence else "NULL AS findings,NULL AS complete"
    )
    join = (
        "LEFT JOIN main.cycle_classification e ON (e.run,e.cycle)=(c.run_id,c.cycle_id)"
        if has_evidence
        else ""
    )
    db.execute(
        f"""CREATE TABLE report.cycles AS SELECT c.id,c.completed_at,c.run_id,c.cycle_id,
        c.model_id,c.model_sha,c.technical,{evidence} FROM ({selected}) c {join}""",
        args,
    )
    db.execute("CREATE UNIQUE INDEX report.report_cycle ON cycles(run_id,cycle_id)")
    if request.basis == "capsule":
        # Whole-scope eligibility was checked in this same transaction. No partial
        # group can leak into position counts, even if the preview was eligible.
        db.execute("""CREATE TABLE report.plc_groups AS SELECT g.* FROM main.plc_groups g
            WHERE EXISTS(SELECT 1 FROM main.group_members m JOIN report.cycles c
                ON (c.run_id,c.cycle_id)=(m.run,m.cycle)
                WHERE (m.run,m.first_cycle)=(g.run,g.first_cycle))""")
        if has_evidence:
            db.execute("""CREATE TABLE report.group_classification AS
                SELECT e.* FROM main.group_classification e JOIN report.plc_groups g
                USING(run,first_cycle)""")
        else:
            db.execute(
                "CREATE TABLE report.group_classification(run,first_cycle,findings)"
            )
