"""Commissioning examples use the same History, physical outcomes and workbook as app.py."""

import json
import sqlite3
import zlib
from collections import Counter
from contextlib import closing
from pathlib import Path
from secrets import token_urlsafe
from threading import Event
from time import time

import pytest
from development_records import SAMPLE, example_id, populate
from openpyxl import load_workbook
from populate_development_data import provision_accounts, review_records
from tests.support.startup import saved_data

from capsule_vision.application._security_lock import SecurityLease
from capsule_vision.application.audit import AuditOperation, AuditTrail
from capsule_vision.application.history import ProductionHistory
from capsule_vision.application.identity import LocalIdentity
from capsule_vision.application.identity_models import Role, SecurityBusyError
from capsule_vision.application.production_report import write_report
from capsule_vision.application.startup import ApplicationData
from capsule_vision.configuration.alarm_definitions import (
    TEMPLATE,
    load_alarm_definitions,
)


def test_interactive_example_accounts_keep_credentials_on_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    data = ApplicationData.at_entry(tmp_path)
    roles = (Role.ADMINISTRATOR, Role.ENGINEER, Role.OPERATOR)
    secrets = [token_urlsafe(24) for _ in roles]
    # Double masked entry on creation; only the existing Administrator authenticates on rerun.
    answers = iter(
        [value for secret in secrets for value in (secret, secret)] + [secrets[0]]
    )
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    def secret_input(prompt: str) -> str:
        return next(answers)

    def default_administrator(prompt: str) -> str:
        return ""

    monkeypatch.setattr("getpass.getpass", secret_input)
    monkeypatch.setattr("builtins.input", default_administrator)
    provision_accounts(data)
    provision_accounts(data)
    assert next(answers, None) is None
    output = capsys.readouterr()
    assert not any(secret in output.out + output.err for secret in secrets)
    owner = LocalIdentity(data.security)
    try:
        for name, role, secret in zip(
            ("DEV-Admin", "DEV-Engineer", "DEV-Operator"), roles, secrets, strict=True
        ):
            session = owner.login(name, secret).result()
            assert session.account and session.account.role == role
            assert session.rotation_required == (role != Role.ADMINISTRATOR)
            owner.logout()
    finally:
        owner.request_stop()
        owner.join()


@pytest.mark.parametrize("cameras", [5, 6])
def test_review_history_and_workbook(tmp_path: Path, cameras: int) -> None:
    data = saved_data(tmp_path, camera_count=cameras)
    # Keep a pre-existing business row and an actual audit event.
    owner = ProductionHistory(data.history)
    try:
        existing = owner.select_batch("existing").result()
        assert existing is not None
    finally:
        owner.request_stop()
        owner.join()
    audit = AuditTrail(data.audit, lambda _: 1)
    original = AuditOperation("fixture", "fixture", "session.login", "fixture")
    try:
        audit.intent(original).result()
        audit.complete(original, "success").result()
    finally:
        audit.close()
    populate(data)

    with closing(sqlite3.connect(data.history)) as db:
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute(
            "SELECT label FROM batches WHERE uuid=?", (existing.uuid,)
        ).fetchone() == ("existing",)
        assert db.execute(
            "SELECT count(*) FROM batches WHERE label GLOB 'DEV-LOT-*'"
        ).fetchone() == (6,)
        assert db.execute("SELECT label FROM batches WHERE selected=1").fetchone() == (
            "DEV-LOT-F",
        )
        assert db.execute("SELECT count(*) FROM batch_ends").fetchone() == (1,)
        assert db.execute(
            "SELECT count(*),sum(batch IS NULL),sum(uncertain),sum(outcome='open') FROM runs"
        ).fetchone() == (12, 1, 1, 0)
        assert db.execute(
            "SELECT count(*),sum(technical='ERROR'),count(DISTINCT date(completed_at,'unixepoch')) FROM cycles"
        ).fetchone() == (360, 2, 12)
        start, end = db.execute(
            "SELECT min(started),max(terminated) FROM runs"
        ).fetchone()
        assert time() - 14 * 86400 <= start < end <= time()
        assert (
            db.execute(
                "SELECT 1 FROM cycles c LEFT JOIN runs r ON r.uuid=c.run_id LEFT JOIN batches b ON r.batch=b.uuid WHERE r.uuid IS NULL OR c.batch_label IS NOT b.label OR c.completed_at NOT BETWEEN r.started AND r.terminated"
            ).fetchone()
            is None
        )
        assert db.execute("SELECT count(*) FROM group_members").fetchone() == (320,)
        groups = db.execute(
            "SELECT run,first_cycle,observed,final_channels FROM plc_groups"
        ).fetchall()
        assert len(groups) == 80
        channel_ids = set(range(1, cameras * 6 + 1))
        empty_counts: set[int] = set()
        for run, first, observed, encoded in groups:
            members = db.execute(
                "SELECT m.slot,c.cycle_id,c.completed_at,c.detail FROM group_members m JOIN cycles c ON (c.run_id,c.cycle_id)=(m.run,m.cycle) WHERE m.run=? AND m.first_cycle=? ORDER BY m.slot",
                (run, first),
            ).fetchall()
            assert [(slot, int(cycle)) for slot, cycle, _, _ in members] == list(
                enumerate(range(int(first), int(first) + 4), 1)
            )
            assert members[-1][2] == observed
            actual = dict(json.loads(encoded))
            assert set(actual) == channel_ids
            empty_counts.add(sum(state == "EMPTY" for state in actual.values()))
            cycles = [
                {c[2]: c[3] for c in json.loads(zlib.decompress(detail))[0]}
                for _, _, _, detail in members
            ]
            for channel, final in actual.items():
                statuses = {cycle[channel] for cycle in cycles}
                expected = (
                    "NG"
                    if statuses.intersection(("NG", "UNKNOWN"))
                    else "EMPTY"
                    if statuses == {"EMPTY"}
                    else "OK"
                )
                assert final == expected
        assert empty_counts >= {0, 1, 2, 3}
        anomaly = db.execute(
            "SELECT detail FROM cycles WHERE run_id=? AND cycle_id=27",
            (example_id("run/11"),),
        ).fetchone()
        anomalous_channels = [
            c for c in json.loads(zlib.decompress(anomaly[0]))[0] if c[4] == "ERROR"
        ]
        assert {c[3] for c in anomalous_channels} == {"NG", "UNKNOWN"}
        assert all("occupancy.execution_error" in c[6] for c in anomalous_channels)
        for uuid, positions, occurrences in db.execute(
            "SELECT uuid,positions,occurrences FROM batches"
        ):
            saved = json.loads(positions)
            facts = db.execute(
                "SELECT final_channels FROM plc_groups JOIN runs ON run=uuid WHERE batch=?",
                (uuid,),
            ).fetchall()
            counts = Counter(s for (row,) in facts for _, s in json.loads(row))
            assert saved["batches_completed"] == len(facts)
            for status in ("OK", "NG", "EMPTY", "UNKNOWN"):
                assert saved["channel_" + status.lower()] == counts[status]
            (cycles,) = db.execute(
                "SELECT count(*) FROM cycles JOIN runs ON run_id=uuid WHERE batch=?",
                (uuid,),
            ).fetchone()
            assert (
                cycles
                == json.loads(occurrences)["cycles_started"]
                == json.loads(occurrences)["cycles_completed"]
            )
        signals = {
            s.id: s for s in load_alarm_definitions(data.root / TEMPLATE.name).signals
        }
        alarms = db.execute(
            "SELECT signal,state,category,station,address,bit,detail,source,batch_label FROM alarm_events ORDER BY id"
        ).fetchall()
        assert (
            len(alarms) == 144
            and sum(row[1] == "observed_recovered" for row in alarms) == 2
        )
        software = [row for row in alarms if row[7] == "application"]
        assert len(software) == 4
        assert {row[1] for row in software} == {"activated", "recovered"}
        assert {row[8] for row in software} == {None, "DEV-LOT-F"}
        for (
            signal,
            state,
            category,
            station,
            address,
            bit,
            detail,
            source,
            label,
        ) in alarms:
            if source == "application":
                assert (signal, category, station, address, bit) == (
                    "production_fault",
                    "software",
                    None,
                    None,
                    None,
                )
                assert detail == "startup · TimeoutError"
                continue
            assert source.startswith("fins_tcp://")
            assert (category, bit) == (signals[signal].category, signals[signal].bit)
            assert (station, address) == (None, 18)
            if state == "observed_recovered":
                assert "communication gap" in detail
            elif label is None:
                assert detail == SAMPLE
    with closing(sqlite3.connect(data.audit)) as db:
        assert db.execute(
            "SELECT count(*) FROM operations JOIN completions USING(id) WHERE username LIKE 'DEV-Example-%' AND operations.utc<=completions.utc",
        ).fetchone() == (139,)
        assert dict(
            db.execute(
                "SELECT action,detail FROM operations JOIN completions USING(id) WHERE outcome='failed'"
            )
        ) == {
            "communication.save": "ConfigurationIOError",
            "vision.sources": "ConfigurationError",
        }
        assert db.execute(
            "SELECT id FROM operations ORDER BY rowid DESC LIMIT 1"
        ).fetchone() == (original.id,)
        trainee = example_id("DEV-Example-Trainee-1")
        assert db.execute(
            "SELECT action,new_value FROM operations WHERE target=? ORDER BY utc",
            (trainee,),
        ).fetchall() == [
            ("account.create", "DEV-Example-Trainee-1 · OPERATOR · enabled"),
            ("account.role", "ENGINEER"),
            ("account.enabled", "False"),
        ]

    owner = ProductionHistory(data.history)
    try:
        batch = owner.initial_selection.result()
        assert batch and batch.label == "DEV-LOT-F"
        path = tmp_path / "report.xlsx"
        write_report(owner, batch.uuid, path, "en", Event(), lambda: None)
        with closing(load_workbook(path)) as book:
            (summary,) = book.worksheets
            rows = list(summary.values)
            metrics = next(i for i, row in enumerate(rows) if row[0] == "Inspected")
            group = batch.groups
            assert group.batches_completed == 16
            assert tuple(rows[metrics][col] for col in (0, 2, 4, 6)) == (
                "Inspected",
                "OK",
                "NG",
                "Pass Rate",
            )
            assert tuple(rows[metrics + 1][col] for col in (0, 2, 4, 6)) == (
                group.inspected,
                group.channel_ok,
                group.channel_ng,
                group.channel_ok / group.inspected,
            )
            assert summary.title == "Batch Production Report"
            assert summary.max_row < 30 and summary.max_column == 9
            assert not any(row[0] == "EMPTY" for row in rows)
            assert "DEV-Example-Operator" in str(rows)
            assert "DEV-Example-Engineer" in str(rows)
        owner.select_batch(None).result()
    finally:
        owner.request_stop()
        owner.join()
    populate(data)
    with closing(sqlite3.connect(data.history)) as db:
        assert db.execute("SELECT 1 FROM batches WHERE selected=1").fetchone() is None
        assert db.execute("SELECT count(*) FROM cycles").fetchone() == (360,)
    with closing(sqlite3.connect(data.audit)) as db:
        assert db.execute("SELECT count(*) FROM operations").fetchone() == (140,)


def test_busy_legacy_and_conflicting_destination(tmp_path: Path) -> None:
    data = saved_data(tmp_path)
    with SecurityLease(data.security), pytest.raises(SecurityBusyError):
        populate(data)
    assert not data.history.exists() and not data.audit.exists()
    with closing(sqlite3.connect(data.history)) as db:
        db.execute("PRAGMA user_version=2")
    with pytest.raises(ValueError, match="schema 2"):
        populate(data)
    assert not data.audit.exists()
    # Reuse a separate isolated checkout for a conflicting Batch label.
    directory = tmp_path / "collision"
    data = saved_data(directory)
    owner = ProductionHistory(data.history)
    try:
        original = owner.select_batch("DEV-LOT-F").result()
    finally:
        owner.request_stop()
        owner.join()
    with pytest.raises(ValueError, match="Batch label already exists"):
        populate(data)
    assert original
    with closing(sqlite3.connect(data.history)) as db:
        assert db.execute("SELECT uuid FROM batches WHERE selected=1").fetchone() == (
            original.uuid,
        )
        assert db.execute("SELECT count(*) FROM cycles").fetchone() == (0,)
    with closing(sqlite3.connect(data.audit)) as db:
        assert (
            db.execute(
                "SELECT 1 FROM operations WHERE id=?", (example_id("audit/0/0"),)
            ).fetchone()
            is None
        )


def test_review_writes_a_fresh_pair_without_touching_installed_records(
    tmp_path: Path,
) -> None:
    source = saved_data(tmp_path / "installation")
    populate(source)
    # An older installed example can have a different observation for this same ID.
    with closing(sqlite3.connect(source.audit)) as db, db:
        db.execute(
            "UPDATE completions SET detail='Earlier sample' WHERE id=?",
            (example_id("audit/0/0"),),
        )
    populate(source)
    with closing(sqlite3.connect(source.audit)) as db:
        assert db.execute(
            "SELECT detail FROM completions WHERE id=?", (example_id("audit/0/0"),)
        ).fetchone() == ("Earlier sample",)
    originals = {p: p.read_bytes() for p in (source.audit, source.history)}
    first = review_records(source, tmp_path / "outputs")
    second = review_records(source, tmp_path / "outputs")
    assert first.root != second.root != source.root
    for sample in (first, second):
        assert not sample.security.exists()
        with closing(sqlite3.connect(sample.history)) as db:
            assert db.execute("SELECT count(*) FROM group_members").fetchone() == (320,)
        with closing(sqlite3.connect(sample.audit)) as db:
            assert db.execute(
                "SELECT count(*) FROM completions WHERE outcome='failed'"
            ).fetchone() == (2,)
    assert all(p.read_bytes() == value for p, value in originals.items())
