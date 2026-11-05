"""Batch/Run persistence at the existing SQLite and reporting owner boundaries."""

import json
import sqlite3
import subprocess
import sys
from concurrent.futures import Future
from contextlib import closing
from pathlib import Path
from threading import Event

import pytest
from tests.support.application import WAIT, FakeSession, close, prepared
from tests.support.business import mixed_result
from tests.support.fins_tcp import TcpPeer
from tests.unit.test_batch_reporting import admit, owner, submit
from tests.unit.test_four_cycle_presentation import completed
from tests.unit.test_history import finish

from capsule_vision.application import (
    ApplicationState,
    ConfigurationIdentity,
    ProductionApplication,
    batch_records,
)
from capsule_vision.application.history import HistoryContext, ProductionHistory
from capsule_vision.application.models import PilotCounters, ProductionCounters
from capsule_vision.communication.endpoints import FinsTcpEndpoint
from capsule_vision.communication.reporting import (
    CompletedReportingBatch,
    ReportingMode,
    ReportingSettings,
    ReportOutcome,
)

RECIPE = ConfigurationIdentity("product", "general", "a" * 64)


def test_switch_end_reactivate_and_zero_cycle_runs(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite3"
    config = prepared(tmp_path)
    for restart in range(3):
        history = ProductionHistory(path)
        app = ProductionApplication(
            lambda _p, _g: FakeSession(), history=history, initial_candidate=config
        )
        try:
            history.query().result(WAIT)
            assert app.snapshot().state is ApplicationState.READY
            if restart == 0:
                app.start_batch("  Lot-A  ").result(WAIT)
                first = app.snapshot().batch_uuid
                assert first and app.snapshot().batch_id == "Lot-A"
                app.start().result(WAIT)
                run = app.snapshot().run_id
                app.stop().result(WAIT)
                assert run and app.snapshot().run_id is None
                app.start_batch("lot-a").result(WAIT)
                assert app.snapshot().batch_uuid != first
                with pytest.raises(RuntimeError, match="changed before confirmation"):
                    app.end_batch(expected_batch=first)
                app.start_batch("Lot-A").result(WAIT)
                assert app.snapshot().batch_uuid == first
            elif restart == 1:
                assert app.snapshot().batch_id == "Lot-A"
                app.end_batch().result(WAIT)
                assert app.snapshot().batch_id is None
            else:
                assert app.snapshot().batch_id is None
                app.start_batch("Lot-A").result(WAIT)
                app.start().result(WAIT)
        finally:
            close(app)
    with closing(sqlite3.connect(path)) as db:
        batches = db.execute("SELECT uuid,label FROM batches ORDER BY rowid").fetchall()
        assert [label for _, label in batches] == ["Lot-A", "lot-a"]
        rows = db.execute("SELECT batch,uuid,outcome,recipe FROM runs").fetchall()
        assert len(rows) == 2 and rows[0][1] != rows[1][1]
        assert all(row[0] == batches[0][0] and row[2] == "stopped" for row in rows)
        assert all(
            json.loads(row[3])["recipe_id"] == config.configuration.recipe_id
            for row in rows
        )
        assert db.execute("SELECT batch FROM batch_ends").fetchall() == [
            (batches[0][0],)
        ]
        assert not db.execute("SELECT * FROM cycles").fetchall()


@pytest.mark.parametrize("group_first", [True, False])
def test_group_membership_dedup_and_expiry_are_independent_of_summaries(
    tmp_path: Path, group_first: bool
) -> None:
    path = tmp_path / "history.sqlite3"
    now = 1000.0
    history = ProductionHistory(path, clock=lambda: now)
    fact = completed(11)
    expected = ProductionCounters()
    try:
        batch = history.select_batch("Batch").result(WAIT)
        assert batch
        history.start_run(
            batch.uuid,
            "r1",
            RECIPE,
            ReportingSettings(FinsTcpEndpoint(), 149, mode=ReportingMode.FOUR_CYCLE),
        ).result(WAIT)
        context = HistoryContext(now, batch.uuid, batch.label, "r1", RECIPE)
        if group_first:
            history.offer_group("r1", fact)
        for i in (14, 11, 12, 13):
            result = mixed_result(i)
            expected = expected.admitted().completed(result)
            history.admitted("r1", i)
            assert history.offer(context, result)
            assert history.offer(context, result)
        history.offer_group("r1", fact)
        history.offer_group("r1", fact)
        records = history.query().result(WAIT).records
        assert len(records) == 4
        assert history.snapshot().persisted == 4 and history.snapshot().accepted == 8
        assert all(
            r.group_first_cycle == 11 and r.group_slot == r.cycle_id - 10
            for r in records
        )
        history.finish_run("r1", "stopped", False).result(WAIT)
        restored = history.select_batch("Batch").result(WAIT)
        assert restored and restored.counters == expected
        assert restored.groups == PilotCounters().completed(fact)
        assert restored.duration_ms == 125 and restored.four_cycle
        history.select_batch(None).result(WAIT)
        assert history.select_batch("Batch").result(WAIT) == restored
    finally:
        finish(history)
    with closing(sqlite3.connect(path)) as db:
        mode, base, endpoint = db.execute(
            "SELECT reporting_mode,reporting_base,endpoint FROM runs"
        ).fetchone()
        assert (mode, base) == ("four_cycle", 149)
        assert json.loads(endpoint)["protocol"] == "fins_tcp"
        row = db.execute(
            "SELECT plc_counter,duration_ms,final_channels FROM plc_groups"
        ).fetchone()
        assert row[:2] == (fact.sequence, 125)
        assert json.loads(row[2]) == [
            [channel, status.name]
            for channel, status in PilotCounters.final_channels(fact).items()
        ]
        assert db.execute("SELECT count(*) FROM group_members").fetchone() == (4,)
    now += 30 * 86400 + 1
    history = ProductionHistory(path, clock=lambda: now)
    try:
        restored = history.initial_selection.result(WAIT)
        assert restored and restored.detail_expired and not restored.uncertain
        assert (
            restored.counters == expected
            and restored.groups == PilotCounters().completed(fact)
        )
        assert not history.query().result(WAIT).records
        # A late duplicate from a closed Run cannot rebuild expired details or count again.
        history.offer_group("r1", fact)
        history.offer(context, mixed_result(11))
        assert history.select_batch("Batch").result(WAIT) == restored
    finally:
        finish(history)
    with closing(sqlite3.connect(path)) as db:
        for table in ("cycles", "plc_groups", "group_members", "open_run_facts"):
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)


@pytest.mark.parametrize("version", [0, 1])
def test_existing_unsupported_database_is_preserved(
    tmp_path: Path, version: int
) -> None:
    path = tmp_path / "history.sqlite3"
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(f"PRAGMA user_version={version}")
        db.execute("CREATE TABLE cycles (id INTEGER PRIMARY KEY)")
        db.execute("INSERT INTO cycles VALUES (17)")
    original = path.read_bytes()
    history = ProductionHistory(path)
    try:
        with pytest.raises(RuntimeError, match="Unsupported History"):
            history.initial_selection.result(WAIT)
    finally:
        finish(history)
    assert path.read_bytes() == original


def test_recovery_requires_actual_owner_exit_and_marks_interrupted(
    tmp_path: Path,
) -> None:
    path = tmp_path / "history.sqlite3"
    first = ProductionHistory(path)
    try:
        batch = first.select_batch("Lot").result(WAIT)
        assert batch
        first.start_run(batch.uuid, "open", RECIPE, None).result(WAIT)
        first.admitted("open", 3)
        other = ProductionHistory(path)
        try:
            with pytest.raises(RuntimeError, match="locked"):
                other.initial_selection.result(WAIT)
        finally:
            finish(other)
    finally:
        finish(first)  # Committed Run without an observed Application termination.
    restarted = ProductionHistory(path, clock=lambda: 5000.0)
    try:
        batch = restarted.initial_selection.result(WAIT)
        assert batch and batch.uncertain and batch.counters.cycles_started == 1
        assert batch.counters.cycles_completed == 0
    finally:
        finish(restarted)
    with closing(sqlite3.connect(path)) as db:
        assert db.execute(
            "SELECT outcome,terminated,uncertain FROM runs"
        ).fetchone() == ("interrupted", 5000, 1)


def test_process_exit_during_fact_transaction_keeps_only_committed_totals(
    tmp_path: Path,
) -> None:
    path = tmp_path / "history.sqlite3"
    script = """
import os, sys
from pathlib import Path
from capsule_vision.application import ConfigurationIdentity, batch_records
from capsule_vision.application.history import ProductionHistory

history = ProductionHistory(Path(sys.argv[1]))
batch = history.select_batch("Interrupted").result(5)
history.start_run(batch.uuid, "r", ConfigurationIdentity("recipe", "model", "a"*64), None).result(5)
history.admitted("r", 1)
history.query().result(5)
record = batch_records.occurrence
def crash_before_commit(*args):
    record(*args)
    os._exit(37)
batch_records.occurrence = crash_before_commit
history.admitted("r", 2)
history.query().result(5)
"""
    child = subprocess.run([sys.executable, "-c", script, str(path)], timeout=20)
    assert child.returncode == 37
    history = ProductionHistory(path)
    try:
        batch = history.initial_selection.result(WAIT)
        assert batch and batch.uncertain and not batch.detail_expired
        assert batch.counters == ProductionCounters(cycles_started=1)
    finally:
        finish(history)


def test_failed_activation_zero_cycle_fault_and_stop_during_recording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "history.sqlite3"
    history = ProductionHistory(path)
    failed = FakeSession(automatic_start=False)
    failed.ready.set_exception(ValueError("warm-up failed"))
    faulted = FakeSession()
    sessions = iter((failed, faulted, FakeSession(), FakeSession()))
    app = ProductionApplication(
        lambda _p, _g: next(sessions),
        initial_candidate=prepared(tmp_path),
        history=history,
    )
    try:
        app.start_batch("Lot").result(WAIT)
        with pytest.raises(RuntimeError, match="activate"):
            app.start().result(WAIT)
        assert app.snapshot().active is None
        app.start().result(WAIT)
        ending, allow_end = Event(), Event()
        finish_run = history.finish_run

        def held_end(run: str, outcome: str, uncertain: bool) -> Future[None]:
            ending.set()
            assert allow_end.wait(WAIT)
            return finish_run(run, outcome, uncertain)

        monkeypatch.setattr(history, "finish_run", held_end)
        faulted.join_gate = Event()
        faulted.failure = RuntimeError("source failed")
        faulted.finished.set_result(None)
        assert faulted.join_entered.wait(WAIT)
        cleanup = app.stop()
        faulted.join_gate.set()
        try:
            assert ending.wait(WAIT)
            assert app.snapshot().runtime_owned
            with pytest.raises(RuntimeError, match="safely released"):
                app.reset_fault()
        finally:
            allow_end.set()
        with pytest.raises(RuntimeError):
            cleanup.result(WAIT)
        app.reset_fault()

        write = batch_records.start_run
        entered, release = Event(), Event()

        def delayed(
            db: sqlite3.Connection,
            batch: str,
            run: str,
            now: float,
            recipe: ConfigurationIdentity,
            reporting: ReportingSettings | None,
        ) -> None:
            write(db, batch, run, now, recipe, reporting)
            entered.set()
            assert release.wait(WAIT)

        with monkeypatch.context() as patch:
            patch.setattr(batch_records, "start_run", delayed)
            starting = app.start()
            try:
                assert entered.wait(WAIT)
                assert app.snapshot().state is ApplicationState.STARTING
                stopped = app.stop()
            finally:
                release.set()
            with pytest.raises(RuntimeError, match="activate"):
                starting.result(WAIT)
            stopped.result(WAIT)

        def unavailable(*_args: object) -> Future[None]:
            rejected: Future[None] = Future()
            rejected.set_exception(sqlite3.OperationalError("read-only filesystem"))
            return rejected

        monkeypatch.setattr(history, "start_run", unavailable)
        with pytest.raises(RuntimeError, match="activate"):
            app.start().result(WAIT)
        assert app.snapshot().run_id is None
    finally:
        close(app)
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT outcome,uncertain FROM runs").fetchall() == [
            ("fault", 1)
        ]


def test_blocked_group_write_does_not_delay_ack_and_failure_is_durable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "history.sqlite3"
    history = ProductionHistory(path, max_pending=1)
    entered, release = Event(), Event()
    write = batch_records.group

    def broken(
        db: sqlite3.Connection, run: str, fact: CompletedReportingBatch, now: float
    ) -> None:
        write(db, run, fact, now)
        entered.set()
        assert release.wait(WAIT)
        raise sqlite3.OperationalError("controlled interruption before commit")

    monkeypatch.setattr(batch_records, "group", broken)
    try:
        batch = history.select_batch("Held").result(WAIT)
        assert batch
        history.start_run(
            batch.uuid,
            "r",
            RECIPE,
            ReportingSettings(FinsTcpEndpoint(), 49, mode=ReportingMode.FOUR_CYCLE),
        ).result(WAIT)
        with (
            TcpPeer() as peer,
            owner(
                peer, on_completed=lambda fact: history.offer_group("r", fact)
            ) as reporter,
        ):
            admit(reporter, 11)
            futures = submit(reporter, 11)
            assert entered.wait(WAIT)
            assert all(
                f.result(WAIT).outcome is ReportOutcome.ACKNOWLEDGED for f in futures
            )
            history.admitted("r", 11)
            history.admitted("r", 12)
            assert history.run_uncertain("r") and history.snapshot().dropped == 1
            closing_run = history.finish_run("r", "stopped", False)
            assert not closing_run.done()
            release.set()
            closing_run.result(WAIT)
        assert history.snapshot().failed == 1
    finally:
        release.set()
        finish(history)
    restarted = ProductionHistory(path)
    try:
        saved = restarted.initial_selection.result(WAIT)
        assert saved and saved.uncertain and saved.groups == PilotCounters()
        assert saved.counters.cycles_started == 1
    finally:
        finish(restarted)
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT count(*) FROM plc_groups").fetchone() == (0,)
        assert db.execute("SELECT outcome,uncertain FROM runs").fetchone() == (
            "stopped",
            1,
        )
