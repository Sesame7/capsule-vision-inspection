"""Production Batch transactions executed only by the History SQLite owner."""

import json
import sqlite3
from dataclasses import asdict, dataclass
from uuid import uuid4

from capsule_vision.communication.reporting import (
    CompletedReportingBatch,
    ReportingMode,
    ReportingSettings,
)
from capsule_vision.inspection.production import ProductionInspectionResult

from .models import ConfigurationIdentity, PilotCounters, ProductionCounters


@dataclass(frozen=True, slots=True)
class BatchSelection:
    uuid: str
    label: str
    counters: ProductionCounters
    groups: PilotCounters
    reporting_mode: ReportingMode | None
    duration_ms: float | None
    uncertain: bool
    detail_expired: bool


@dataclass(frozen=True, slots=True)
class ReportBatches:
    choices: tuple[tuple[str, str], ...]  # UUID and operator label
    more: bool
    attributable: frozenset[str] = frozenset()


def report_batches(
    db: sqlite3.Connection, prefix: str, cursor: str | None, previous: bool
) -> ReportBatches:
    where = ["label >= ? AND substr(label, 1, length(?)) = ?"]
    args = [prefix, prefix, prefix]
    if cursor is not None:
        where.append("label < ?" if previous else "label > ?")
        args.append(cursor)
    order = "DESC" if previous else "ASC"
    rows = db.execute(
        "SELECT uuid,label,json_extract(positions,'$.batches_completed') FROM batches WHERE "
        + " AND ".join(where)
        + f" ORDER BY label {order} LIMIT 51",
        args,
    ).fetchall()
    page = rows[:50]
    return ReportBatches(
        tuple((r[0], r[1]) for r in (reversed(page) if previous else page)),
        len(rows) > 50,
        frozenset(r[0] for r in page if r[2]),
    )


SCHEMA = """
CREATE TABLE batches (
    uuid TEXT PRIMARY KEY, label TEXT NOT NULL UNIQUE COLLATE BINARY,
    created REAL NOT NULL, selected INTEGER NOT NULL DEFAULT 0,
    occurrences TEXT NOT NULL, positions TEXT NOT NULL, duration_ms REAL
);
CREATE UNIQUE INDEX current_batch ON batches(selected) WHERE selected=1;
CREATE TABLE batch_ends (batch TEXT NOT NULL REFERENCES batches, ended REAL NOT NULL);
CREATE TABLE runs (
    uuid TEXT PRIMARY KEY, batch TEXT REFERENCES batches, started REAL NOT NULL,
    terminated REAL, outcome TEXT NOT NULL, uncertain INTEGER NOT NULL DEFAULT 0,
    detail_expired INTEGER NOT NULL DEFAULT 0, recipe TEXT NOT NULL, reporting_mode TEXT NOT NULL,
    reporting_base INTEGER, endpoint TEXT
);
CREATE INDEX batch_runs ON runs(batch);
CREATE UNIQUE INDEX open_run ON runs(outcome) WHERE outcome='open';
CREATE TABLE open_run_facts (
    run TEXT NOT NULL REFERENCES runs, kind TEXT NOT NULL, cycle TEXT NOT NULL,
    PRIMARY KEY(run,kind,cycle)
) WITHOUT ROWID;
CREATE TABLE plc_groups (
    run TEXT NOT NULL REFERENCES runs, first_cycle TEXT NOT NULL, observed REAL NOT NULL,
    plc_counter INTEGER NOT NULL, duration_ms REAL, final_channels TEXT NOT NULL,
    PRIMARY KEY(run,first_cycle)
);
CREATE INDEX group_time ON plc_groups(observed);
CREATE TABLE group_members (
    run TEXT NOT NULL, cycle TEXT NOT NULL, first_cycle TEXT NOT NULL, slot INTEGER NOT NULL,
    PRIMARY KEY(run,cycle), FOREIGN KEY(run,first_cycle) REFERENCES plc_groups ON DELETE CASCADE
);
"""

RUN_POSITIONS_SCHEMA = """CREATE TABLE IF NOT EXISTS run_positions (
    run TEXT PRIMARY KEY REFERENCES runs(uuid) ON DELETE CASCADE,
    positions TEXT
)"""


def run_totals(saved: str | None, mode: str) -> PilotCounters | None:
    """Unknown coverage and absence of physical grouping are not zero production."""
    totals = PilotCounters(**json.loads(saved)) if saved is not None else None
    return (
        totals
        if totals is not None
        and (mode in ("single_cycle", "four_cycle") or totals.batches_completed)
        else None
    )


def retained_positions(db: sqlite3.Connection, run: str) -> PilotCounters | None:
    (mode,) = db.execute(
        "SELECT reporting_mode FROM runs WHERE uuid=?", (run,)
    ).fetchone()
    # Before single_cycle existed, every admitted group fact had four members.
    size = 1 if mode == "single_cycle" else 4
    counts = {"OK": 0, "NG": 0, "EMPTY": 0, "UNKNOWN": 0}
    groups = 0
    for encoded, members, slots, low, high, first in db.execute(
        """SELECT g.final_channels,count(m.cycle),count(DISTINCT m.slot),
        min(m.slot),max(m.slot),sum(m.slot=1 AND m.cycle=g.first_cycle)
        FROM plc_groups g LEFT JOIN group_members m USING(run,first_cycle)
        WHERE g.run=? GROUP BY g.run,g.first_cycle""",
        (run,),
    ):
        if (members, slots, low, high, first) != (size, size, 1, size, 1):
            return None
        try:
            pairs: list[tuple[int, str]] = json.loads(encoded)
            finals = dict(pairs)
            if (
                len(pairs) not in (30, 36)
                or len(finals) != len(pairs)
                or set(finals) != set(range(1, len(pairs) + 1))
                or not set(finals.values()) <= counts.keys()
            ):
                return None
        except TypeError, ValueError:
            return None
        groups += 1
        for status in finals.values():
            counts[status] += 1
    return PilotCounters(
        groups, counts["OK"], counts["NG"], counts["EMPTY"], counts["UNKNOWN"]
    )


def initialize_run_positions(db: sqlite3.Connection) -> None:
    """Recover only verifiable legacy totals, before retention, on the History owner."""
    db.execute(RUN_POSITIONS_SCHEMA)
    missing: dict[str, str | None] = dict(
        db.execute("""SELECT r.uuid,r.batch FROM runs r LEFT JOIN run_positions p
            ON p.run=r.uuid WHERE p.run IS NULL""")
    )
    recovered: dict[str, PilotCounters] = {}
    for batch in {b for b in missing.values() if b is not None}:
        candidates: dict[str, PilotCounters] = {}
        for run, uncertain, expired, outcome, saved in db.execute(
            """SELECT r.uuid,r.uncertain,r.detail_expired,r.outcome,p.positions
            FROM runs r LEFT JOIN run_positions p ON p.run=r.uuid WHERE r.batch=?""",
            (batch,),
        ):
            if run not in missing:
                totals = (
                    PilotCounters(**json.loads(saved)) if saved is not None else None
                )
            elif not uncertain and not expired and outcome in ("stopped", "fault"):
                totals = retained_positions(db, run)
            else:
                totals = None
            if totals is None:
                break
            candidates[run] = totals
        else:
            # There is no legacy per-Run checkpoint. Require all contributions to
            # reconcile to the independent durable Batch summary; never assign a
            # residual to one Run or recover an unbatched Run without that evidence.
            (saved_batch,) = db.execute(
                "SELECT positions FROM batches WHERE uuid=?", (batch,)
            ).fetchone()
            expected = PilotCounters(**json.loads(saved_batch))
            if all(
                sum(asdict(t)[key] for t in candidates.values()) == value
                for key, value in asdict(expected).items()
            ):
                recovered.update(candidates)
    db.executemany(
        "INSERT INTO run_positions VALUES (?,?)",
        (
            (run, json.dumps(asdict(recovered[run])) if run in recovered else None)
            for run in missing
        ),
    )


def selected(db: sqlite3.Connection) -> BatchSelection | None:
    row = db.execute("""
        SELECT uuid,label,occurrences,positions,duration_ms,
            (SELECT reporting_mode FROM runs WHERE batch=b.uuid ORDER BY rowid DESC LIMIT 1),
            EXISTS(SELECT 1 FROM runs WHERE batch=b.uuid AND (uncertain OR outcome='open')),
            EXISTS(SELECT 1 FROM runs WHERE batch=b.uuid AND detail_expired)
        FROM batches b WHERE selected=1
    """).fetchone()
    if row is None:
        return None
    return BatchSelection(
        row[0],
        row[1],
        ProductionCounters(**json.loads(row[2])),
        PilotCounters(**json.loads(row[3])),
        (
            ReportingMode(row[5])
            if row[5] in ("single_cycle", "four_cycle")
            else ReportingMode.PER_CYCLE
        )
        if row[5] is not None
        else None,
        row[4],
        bool(row[6]),
        bool(row[7]),
    )


def select(
    db: sqlite3.Connection, label: str | None, now: float
) -> BatchSelection | None:
    if label is not None:
        label = label.strip()
        if not label:
            raise ValueError("Batch label must be nonempty")
    if label is None:
        if not db.execute("SELECT 1 FROM batches WHERE selected=1").fetchone():
            raise ValueError("Select a Batch before ending it")
        db.execute(
            "INSERT INTO batch_ends SELECT uuid,? FROM batches WHERE selected=1", (now,)
        )
    db.execute("UPDATE batches SET selected=0 WHERE selected=1")
    if label is not None:
        db.execute(
            "INSERT INTO batches(uuid,label,created,selected,occurrences,positions) VALUES (?,?,?,1,?,?) "
            "ON CONFLICT(label) DO UPDATE SET selected=1",
            (
                uuid4().hex,
                label,
                now,
                json.dumps(asdict(ProductionCounters())),
                json.dumps(asdict(PilotCounters())),
            ),
        )
    return selected(db)


def recover(db: sqlite3.Connection, observed: float) -> None:
    # The exclusive History owner can now establish that these Runs lost their owner.
    db.execute(
        "UPDATE runs SET outcome='interrupted',terminated=?,uncertain=1 WHERE outcome='open'",
        (observed,),
    )
    db.execute("DELETE FROM open_run_facts")


def start_run(
    db: sqlite3.Connection,
    batch: str | None,
    run: str,
    now: float,
    recipe: ConfigurationIdentity,
    reporting: ReportingSettings | None,
) -> None:
    inserted = db.execute(
        "INSERT INTO runs(uuid,batch,started,outcome,recipe,reporting_mode,reporting_base,endpoint) "
        "SELECT ?,?,?,'open',?,?,?,? WHERE ? IS "
        "(SELECT uuid FROM batches WHERE selected=1)",
        (
            run,
            batch,
            now,
            json.dumps(asdict(recipe)),
            reporting.mode.value if reporting else "disabled",
            reporting.base_address if reporting else None,
            json.dumps(
                {"protocol": reporting.endpoint.protocol, **asdict(reporting.endpoint)}
            )
            if reporting
            else None,
            batch,
        ),
    )
    if inserted.rowcount != 1:
        raise ValueError("Current Production Batch is unavailable")
    db.execute(
        "INSERT INTO run_positions VALUES (?,?)",
        (run, json.dumps(asdict(PilotCounters()))),
    )


def finish_run(
    db: sqlite3.Connection, run: str, now: float, outcome: str, uncertain: bool
) -> None:
    db.execute("DELETE FROM open_run_facts WHERE run=?", (run,))
    if outcome == "unactivated":
        db.execute("DELETE FROM runs WHERE uuid=? AND outcome='open'", (run,))
    else:
        cursor = db.execute(
            "UPDATE runs SET terminated=?,outcome=?,uncertain=MAX(uncertain,?) WHERE uuid=? AND outcome='open'",
            (now, outcome, uncertain, run),
        )
        if cursor.rowcount != 1:
            raise ValueError("Missing open Production Run")


def _first(db: sqlite3.Connection, run: str, kind: str, cycle: int) -> bool:
    return (
        db.execute(
            "INSERT INTO open_run_facts SELECT uuid,?,? FROM runs WHERE uuid=? AND outcome='open' "
            "ON CONFLICT DO NOTHING",
            (kind, str(cycle), run),
        ).rowcount
        == 1
    )


def occurrence(
    db: sqlite3.Connection,
    run: str,
    cycle: int,
    result: ProductionInspectionResult | None = None,
) -> bool:
    kind = "terminal" if result is not None else "admitted"
    if not _first(db, run, kind, cycle):
        return False
    batch, saved = db.execute(
        "SELECT r.batch,b.occurrences FROM runs r LEFT JOIN batches b ON r.batch=b.uuid WHERE r.uuid=?",
        (run,),
    ).fetchone()
    if batch is None:  # Unbatched Runs retain Cycle facts without a Batch summary.
        return True
    counters = ProductionCounters(**json.loads(saved))
    if result is None:
        counters = counters.admitted()
    else:
        # A terminal is itself evidence of admission, including a lost admission handoff.
        if _first(db, run, "admitted", cycle):
            counters = counters.admitted()
        counters = counters.completed(result)
    db.execute(
        "UPDATE batches SET occurrences=? WHERE uuid=?",
        (json.dumps(asdict(counters)), batch),
    )
    return True


def group(
    db: sqlite3.Connection, run: str, fact: CompletedReportingBatch, now: float
) -> None:
    (mode,) = db.execute(
        "SELECT reporting_mode FROM runs WHERE uuid=?", (run,)
    ).fetchone()
    # Before single_cycle existed, every admitted group fact had four members.
    size = 1 if mode == "single_cycle" else 4
    if len(fact.reports) != size:
        raise ValueError("PLC group membership does not match the Run reporting mode")
    first = fact.identity.cycle_id
    if not _first(db, run, "group", first):
        return
    finals = PilotCounters.final_channels(fact)
    db.execute(
        "INSERT INTO plc_groups VALUES (?,?,?,?,?,?)",
        (
            run,
            str(first),
            now,
            fact.sequence,
            fact.duration_ms,
            json.dumps([(channel, status.name) for channel, status in finals.items()]),
        ),
    )
    db.executemany(
        "INSERT INTO group_members VALUES (?,?,?,?)",
        (
            (run, str(report.identity.cycle_id), str(first), slot)
            for slot, report in enumerate(fact.reports, 1)
        ),
    )
    (saved_run,) = db.execute(
        "SELECT positions FROM run_positions WHERE run=?", (run,)
    ).fetchone()
    # This is the same deduplicated fact and transaction as the Batch update.
    # New Runs always have a ledger, including unbatched Runs. Closed legacy
    # Runs cannot admit a new fact through _first.
    run_counters = PilotCounters(**json.loads(saved_run)).completed(fact)
    db.execute(
        "UPDATE run_positions SET positions=? WHERE run=?",
        (json.dumps(asdict(run_counters)), run),
    )
    batch, saved = db.execute(
        "SELECT r.batch,b.positions FROM runs r LEFT JOIN batches b ON r.batch=b.uuid WHERE r.uuid=?",
        (run,),
    ).fetchone()
    if batch is None:
        return
    counters = PilotCounters(**json.loads(saved)).completed(fact)
    db.execute(
        "UPDATE batches SET positions=?,duration_ms=? WHERE uuid=?",
        (json.dumps(asdict(counters)), fact.duration_ms, batch),
    )
