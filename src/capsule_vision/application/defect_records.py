"""Effective defect evidence and group aggregation on the existing History writer."""

import json
import sqlite3
from collections import Counter
from collections.abc import Iterable

from capsule_vision.inspection.production import (
    FindingSource,
    OccupancyState,
    ProductionInspectionResult,
    ProductJudgment,
)

from .batch_records import retained_positions
from .models import PilotCounters

type Category = tuple[str, str, str, str]
type Hit = tuple[int, str, str, str, str]


def customer_hits(entries: Iterable[Hit]) -> set[Hit]:
    """Collapse reason variants before counting a channel, keeping Model identity."""
    hits: set[Hit] = set()
    for channel, source, model, sha, name in entries:
        if source == "traditional":
            if name.startswith("capsule.size."):
                name = "length"
            elif name.startswith("capsule.color."):
                name = "color"
            else:
                continue
        hits.add((channel, source, model, sha, name))
    return hits


def create(db: sqlite3.Connection) -> None:
    # Use individual statements so the caller retains transaction ownership.
    for ddl in (
        """CREATE TABLE IF NOT EXISTS cycle_classification (
            run TEXT, cycle TEXT, channel_mask INTEGER NOT NULL, complete INTEGER NOT NULL,
            findings TEXT NOT NULL, PRIMARY KEY(run,cycle),
            FOREIGN KEY(run,cycle) REFERENCES cycles(run_id,cycle_id) ON DELETE CASCADE)""",
        """CREATE INDEX IF NOT EXISTS classification_members ON group_members(run,first_cycle)""",
        """CREATE TABLE IF NOT EXISTS group_classification (
            run TEXT, first_cycle TEXT, findings TEXT NOT NULL, PRIMARY KEY(run,first_cycle),
            FOREIGN KEY(run,first_cycle) REFERENCES plc_groups ON DELETE CASCADE)""",
        """CREATE TABLE IF NOT EXISTS batch_coverage (
            batch TEXT PRIMARY KEY REFERENCES batches, groups INTEGER NOT NULL,
            ng INTEGER NOT NULL, explained INTEGER NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS batch_categories (
            batch TEXT REFERENCES batches, source TEXT, model TEXT, sha TEXT, category TEXT,
            affected INTEGER NOT NULL, PRIMARY KEY(batch,source,model,sha,category))""",
        """CREATE TABLE IF NOT EXISTS run_coverage (
            run TEXT PRIMARY KEY REFERENCES runs(uuid) ON DELETE CASCADE,
            groups INTEGER NOT NULL, ng INTEGER NOT NULL, explained INTEGER NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS run_categories (
            run TEXT REFERENCES runs(uuid) ON DELETE CASCADE,
            source TEXT, model TEXT, sha TEXT, category TEXT, affected INTEGER NOT NULL,
            PRIMARY KEY(run,source,model,sha,category))""",
    ):
        db.execute(ddl)


def initialize(db: sqlite3.Connection) -> None:
    """Add durable Run categories, proving legacy coverage before detail retention.

    Existing raw Batch aggregates cannot resolve overlapping upper/lower reasons.
    Never distribute those totals among Runs or sum them into customer categories.
    """
    create(db)
    missing = db.execute("""SELECT r.uuid,p.positions
        FROM runs r LEFT JOIN run_positions p ON p.run=r.uuid
        LEFT JOIN run_coverage c ON c.run=r.uuid WHERE c.run IS NULL""").fetchall()
    for run, saved in missing:
        db.execute("INSERT INTO run_coverage VALUES (?,0,0,0)", (run,))
        expected = PilotCounters(**json.loads(saved)) if saved is not None else None
        if expected is None or retained_positions(db, run) != expected:
            continue
        query = """SELECT g.final_channels,c.findings FROM plc_groups g
            LEFT JOIN group_classification c USING(run,first_cycle)
            WHERE g.run=?"""
        valid = True
        for encoded, hits in db.execute(query, (run,)):
            if hits is None:
                valid = False
                break
            finals: dict[int, str] = dict(json.loads(encoded))
            entries: list[Hit] = json.loads(hits)
            if any(
                finals.get(ch) != "NG"
                or source not in ("vimo", "traditional")
                or not name
                for ch, source, _, _, name in entries
            ):
                valid = False
                break
        if not valid:
            continue
        # All group membership/outcomes match an independent durable Run checkpoint.
        # Group classification itself was committed only after all four members.
        for encoded, hits in db.execute(query, (run,)):
            finals = dict(json.loads(encoded))
            entries = json.loads(hits)
            _add_run(db, run, finals, entries)


def _add_run(
    db: sqlite3.Connection, run: str, finals: dict[int, str], entries: Iterable[Hit]
) -> None:
    hits = customer_hits(entries)
    db.execute(
        """UPDATE run_coverage SET groups=groups+1,ng=ng+?,explained=explained+?
        WHERE run=?""",
        (sum(s == "NG" for s in finals.values()), len({hit[0] for hit in hits}), run),
    )
    counts = Counter((s, m, sha, name) for _, s, m, sha, name in hits)
    db.executemany(
        """INSERT INTO run_categories VALUES (?,?,?,?,?,?)
        ON CONFLICT DO UPDATE SET affected=affected+excluded.affected""",
        ((run, *key, count) for key, count in counts.items()),
    )


def record_cycle(
    db: sqlite3.Connection, run: str, result: ProductionInspectionResult
) -> None:
    findings: set[tuple[int, str, str]] = set()
    mask, complete = 0, True
    for camera in result.cameras:
        for channel in camera.channels:
            mask |= 1 << (channel.channel_id - 1)
            for check in channel.checks:
                if check.assessment.judgment is not ProductJudgment.NG:
                    continue
                if check.source is FindingSource.VIMO:
                    if (
                        channel.occupancy
                        and channel.occupancy.state is OccupancyState.EMPTY
                    ):
                        continue
                    # Use the channel's effective matches after the pipeline's label
                    # and occupancy gates. The camera's raw findings are not inputs.
                    defects = [r for r in check.reasons if r.code == "defect_region"]
                    labels = {r.label for r in defects if r.label}
                    labels.update(a.finding.label for a in channel.vimo_findings)
                    complete &= bool(labels) and all(r.label for r in defects)
                    findings.update(
                        (channel.channel_id, "vimo", name) for name in labels
                    )
                else:
                    # Execution failures and forced COLLECTION rejection are separate facts.
                    codes = {
                        r.code
                        for r in check.reasons
                        if r.exception_type is None
                        and r.code.rsplit(".", 1)[-1]
                        not in (
                            "execution_error",
                            "result_unavailable",
                            "invalid_result",
                        )
                    }
                    complete &= bool(codes)
                    findings.update(
                        (channel.channel_id, "traditional", code) for code in codes
                    )
    db.execute(
        "INSERT INTO cycle_classification VALUES (?,?,?,?,?)",
        (
            run,
            str(result.cycle_id),
            mask,
            complete,
            json.dumps(sorted(findings), ensure_ascii=False),
        ),
    )


def reconcile(db: sqlite3.Connection, run: str, cycle: int) -> None:
    group = db.execute(
        """
        SELECT g.first_cycle,g.final_channels,r.batch,r.reporting_mode FROM group_members m
        JOIN plc_groups g USING(run,first_cycle) JOIN runs r ON r.uuid=m.run
        LEFT JOIN group_classification done USING(run,first_cycle)
        WHERE m.run=? AND m.cycle=? AND done.run IS NULL""",
        (run, str(cycle)),
    ).fetchone()
    if group is None:
        return
    first, encoded, batch, mode = group
    # Before single_cycle existed, every admitted group fact had four members.
    size = 1 if mode == "single_cycle" else 4
    members = db.execute(
        """
        SELECT e.channel_mask,e.complete,e.findings,c.model_id,c.model_sha FROM group_members m
        JOIN cycle_classification e USING(run,cycle)
        JOIN cycles c ON (c.run_id,c.cycle_id)=(m.run,m.cycle)
        WHERE m.run=? AND m.first_cycle=?""",
        (run, first),
    ).fetchall()
    finals: dict[int, str] = dict(json.loads(encoded))
    mask = sum(1 << (ch - 1) for ch in finals)
    if (
        not size
        or len(members) != size
        or any(m[0] != mask or not m[1] for m in members)
    ):
        return
    hits: set[tuple[int, str, str, str, str]] = set()
    for _, _, data, model, sha in members:
        entries: list[tuple[int, str, str]] = json.loads(data)
        for channel, source, name in entries:
            if finals[channel] == "NG":
                hits.add(
                    (
                        channel,
                        source,
                        model if source == "vimo" else "",
                        sha if source == "vimo" else "",
                        name,
                    )
                )
    # Marker, retained breakdown and durable counts share the fact's transaction.
    db.execute(
        "INSERT INTO group_classification VALUES (?,?,?)",
        (run, first, json.dumps(sorted(hits), ensure_ascii=False)),
    )
    _add_run(db, run, finals, hits)
    if batch is None:
        return
    explained = len({hit[0] for hit in hits})
    db.execute(
        """INSERT INTO batch_coverage VALUES (?,1,?,?)
        ON CONFLICT(batch) DO UPDATE SET groups=groups+1,ng=ng+excluded.ng,
        explained=explained+excluded.explained""",
        (batch, sum(s == "NG" for s in finals.values()), explained),
    )
    counts = Counter((source, model, sha, name) for _, source, model, sha, name in hits)
    db.executemany(
        """INSERT INTO batch_categories VALUES (?,?,?,?,?,?)
        ON CONFLICT DO UPDATE SET affected=affected+excluded.affected""",
        ((batch, *key, count) for key, count in counts.items()),
    )
