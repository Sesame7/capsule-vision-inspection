"""Durable Run counts share the eligible group transaction, never Cycle arithmetic."""

import json
import sqlite3
from contextlib import closing
from dataclasses import asdict, replace
from pathlib import Path
from threading import Event

import pytest
from tests.support.application import WAIT
from tests.support.business import mixed_result
from tests.unit.test_durable_batches import RECIPE
from tests.unit.test_history import finish
from tests.unit.test_production_report import FOUR_CYCLE

from capsule_vision.application.history import HistoryContext, ProductionHistory
from capsule_vision.application.models import PilotCounters
from capsule_vision.application.report_content import ReportData
from capsule_vision.application.report_scope import ReportRequest
from capsule_vision.communication.reporting import (
    CompletedReportingBatch,
    FirstStationReport,
    ReportingMode,
)


def fact(first: int, cameras: int) -> CompletedReportingBatch:
    reports = tuple(
        FirstStationReport.from_result(
            1, mixed_result(first + slot, camera_count=cameras)
        )
        for slot in range(4)
    )
    return CompletedReportingBatch(
        reports[0].identity, 7, reports, reports[0].reject_words, 125
    )


@pytest.mark.parametrize("cameras", [5, 6])
def test_run_and_batch_counts_share_dedup_restart_and_expiry(
    tmp_path: Path, cameras: int
) -> None:
    path = tmp_path / "history.sqlite3"
    now = 2_000_000_000.0
    owner = ProductionHistory(path, clock=lambda: now)
    group = fact(11, cameras)
    expected = PilotCounters().completed(group)
    try:
        batch = owner.select_batch("Lot A").result(WAIT)
        assert batch
        for run in ("first", "second"):
            owner.start_run(batch.uuid, run, RECIPE, FOUR_CYCLE).result(WAIT)
            owner.offer_group(run, group)
            owner.offer_group(run, group)
            context = HistoryContext(now, batch.uuid, batch.label, run, RECIPE)
            for cycle in (14, 11, 12, 13):
                assert owner.offer(context, mixed_result(cycle, camera_count=cameras))
            if run == "second":
                now += 31 * 86400
                owner.query().result(WAIT)  # Expire detail while the Run is still open.
                owner.offer_group(run, group)
            owner.finish_run(run, "stopped", False).result(WAIT)
            owner.offer_group(run, group)  # A closed Run never admits another fact.
        review = owner.batch_detail(batch.uuid).result(WAIT)
        assert [r.totals for r in review.runs] == [expected, expected]
        assert review.batch.totals == expected.completed(group)
        assert not review.uncertain and review.expired
        owner.select_batch(None).result(WAIT)
        owner.start_run(None, "unbatched", RECIPE, FOUR_CYCLE).result(WAIT)
        owner.offer_group("unbatched", group)
        owner.finish_run("unbatched", "stopped", False).result(WAIT)
        owner.select_batch("Later lot").result(WAIT)
    finally:
        finish(owner)
    owner = ProductionHistory(path, clock=lambda: now + 31 * 86400)
    try:
        review = owner.batch_detail(batch.uuid).result(WAIT)
        assert [r.totals for r in review.runs] == [expected, expected]
        assert review.batch.totals == expected.completed(group)
        selection = owner.initial_selection.result(WAIT)
        assert (
            selection
            and selection.label == "Later lot"
            and not selection.groups.batches_completed
        )
        snapshot = tmp_path / "report.db"
        owner.capture_report(
            ReportRequest("batch", "capsule", batch.uuid), snapshot, Event()
        ).result(WAIT)
        with closing(sqlite3.connect(snapshot)) as db:
            rows = list(ReportData(db, "en", Event()).runs())
            assert len(rows) == 2
            assert all(
                row[5:]
                == (
                    expected.inspected,
                    expected.channel_ok,
                    expected.channel_ng,
                    expected.channel_ok / expected.inspected,
                )
                for row in rows
            )
    finally:
        finish(owner)
    with closing(sqlite3.connect(path)) as db:
        assert dict(db.execute("SELECT run,positions FROM run_positions")) == {
            run: json.dumps(asdict(expected))
            for run in ("first", "second", "unbatched")
        }
        assert db.execute(
            "SELECT batch FROM runs WHERE uuid='unbatched'"
        ).fetchone() == (None,)
        assert db.execute("SELECT count(*) FROM plc_groups").fetchone() == (0,)
        assert not db.execute("PRAGMA foreign_key_check").fetchall()


@pytest.mark.parametrize(
    "case",
    [
        "complete",
        "expired",
        "uncertain",
        "missing_group",
        "missing_member",
        "invalid_channels",
        "unbatched",
    ],
)
def test_legacy_recovery_requires_complete_reconcilable_evidence(
    tmp_path: Path, case: str
) -> None:
    path = tmp_path / "history.sqlite3"
    now = 2_000_000_000.0
    owner = ProductionHistory(path, clock=lambda: now)
    group = fact(11, 5)
    try:
        batch = owner.select_batch("Legacy").result(WAIT)
        assert batch
        for run in ("first", "second"):
            owner.start_run(batch.uuid, run, RECIPE, FOUR_CYCLE).result(WAIT)
            owner.offer_group(run, group)
            owner.finish_run(run, "stopped", False).result(WAIT)
    finally:
        finish(owner)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("DROP TABLE run_positions")
        if case in ("expired", "uncertain"):
            column = "detail_expired" if case == "expired" else "uncertain"
            db.execute(f"UPDATE runs SET {column}=1 WHERE uuid='first'")
        elif case == "missing_group":
            db.execute("DELETE FROM plc_groups WHERE run='first'")
        elif case == "missing_member":
            db.execute("DELETE FROM group_members WHERE run='first' AND slot=2")
        elif case == "invalid_channels":
            db.execute("UPDATE plc_groups SET final_channels='[]' WHERE run='first'")
        elif case == "unbatched":
            db.execute("UPDATE runs SET batch=NULL")
        original_batch = db.execute("SELECT positions FROM batches").fetchone()
    for expired in (False, True):
        owner = ProductionHistory(path, clock=lambda stamp=now: stamp)
        try:
            review = owner.batch_detail(batch.uuid).result(WAIT)
            if case != "unbatched":
                assert all(
                    (r.totals is not None) == (case == "complete") for r in review.runs
                )
                if case == "complete":
                    assert all(
                        r.totals == PilotCounters().completed(group)
                        for r in review.runs
                    )
            assert owner.snapshot().failed == 0
        finally:
            finish(owner)
        with closing(sqlite3.connect(path)) as db:
            assert (
                db.execute("SELECT positions FROM batches").fetchone() == original_batch
            )
            rows = db.execute("SELECT positions FROM run_positions").fetchall()
            assert len(rows) == 2 and all(
                (r[0] is not None) == (case == "complete") for r in rows
            )
        if not expired:
            now += 31 * 86400


def test_group_transaction_rolls_back_both_summaries_and_retry_key(
    tmp_path: Path,
) -> None:
    path = tmp_path / "history.sqlite3"
    owner = ProductionHistory(path)
    try:
        batch = owner.select_batch("Atomic").result(WAIT)
        assert batch
    finally:
        finish(owner)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("""CREATE TRIGGER fail_run_count BEFORE UPDATE ON run_positions
            BEGIN SELECT RAISE(ABORT,'Controlled counter failure'); END""")
    owner = ProductionHistory(path)
    try:
        owner.start_run(batch.uuid, "run", RECIPE, FOUR_CYCLE).result(WAIT)
        owner.offer_group("run", fact(11, 5))
        detail = owner.batch_detail(batch.uuid).result(WAIT)
        assert detail.batch.totals == PilotCounters()
        assert detail.runs[0].totals == PilotCounters() and detail.runs[0].uncertain
        assert owner.snapshot().failed == 1
    finally:
        finish(owner)
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT count(*) FROM plc_groups").fetchone() == (0,)
        assert db.execute(
            "SELECT count(*) FROM open_run_facts WHERE kind='group'"
        ).fetchone() == (0,)


def test_zero_unactivated_and_interrupted_run_summaries(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite3"
    owner = ProductionHistory(path)
    try:
        batch = owner.select_batch("Lifecycle").result(WAIT)
        assert batch
        owner.start_run(batch.uuid, "provisional", RECIPE, FOUR_CYCLE).result(WAIT)
        owner.finish_run("provisional", "unactivated", False).result(WAIT)
        owner.start_run(batch.uuid, "zero", RECIPE, FOUR_CYCLE).result(WAIT)
        owner.finish_run("zero", "stopped", False).result(WAIT)
        owner.start_run(
            batch.uuid,
            "per-cycle",
            RECIPE,
            replace(FOUR_CYCLE, mode=ReportingMode.PER_CYCLE),
        ).result(WAIT)
        owner.finish_run("per-cycle", "stopped", False).result(WAIT)
        owner.start_run(batch.uuid, "interrupted", RECIPE, FOUR_CYCLE).result(WAIT)
        owner.offer_group("interrupted", fact(11, 6))
    finally:
        finish(owner)
    owner = ProductionHistory(path)
    try:
        detail = owner.batch_detail(batch.uuid).result(WAIT)
        assert len(detail.runs) == 3
        interrupted, per_cycle, zero = detail.runs
        assert interrupted.totals == PilotCounters().completed(fact(11, 6))
        assert interrupted.uncertain and interrupted.outcome == "interrupted"
        assert per_cycle.totals is None and zero.totals == PilotCounters()
    finally:
        finish(owner)
    with closing(sqlite3.connect(path)) as db:
        assert (
            db.execute("SELECT 1 FROM run_positions WHERE run='provisional'").fetchone()
            is None
        )
