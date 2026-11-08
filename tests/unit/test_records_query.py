"""Shared search limits records, never Batch accounting or evidence attribution."""

from concurrent.futures import CancelledError, Future
from dataclasses import replace
from pathlib import Path
from threading import Event

import pytest
from tests.support.application import WAIT
from tests.support.business import mixed_result
from tests.support.startup import TEST_PASSWORD
from tests.unit.test_durable_batches import RECIPE
from tests.unit.test_history import finish

from capsule_vision.application.account_maintenance import initialize_administrator
from capsule_vision.application.alarm_records import AlarmEvent, AlarmQuery
from capsule_vision.application.audit import AuditOperation, AuditQuery, AuditRecord
from capsule_vision.application.history import (
    HistoryContext,
    HistoryQuery,
    ProductionHistory,
)
from capsule_vision.application.identity import LocalIdentity
from capsule_vision.application.identity_models import StaleSessionError
from capsule_vision.application.records_query import RecordsScope, scoped_audit_page


def test_choices_search_matching_run_order_gaps_bounds_and_no_batch(
    tmp_path: Path,
) -> None:
    now = 1000.0

    def clock() -> float:
        return now

    history = ProductionHistory(tmp_path / "history.sqlite3", clock=clock)
    try:
        ids: dict[str, str] = {}
        for i in range(55):
            label = f"Lot-{i:02}"
            batch = history.select_batch(label).result(WAIT)
            assert batch
            ids[label] = batch.uuid
            now = 1000 + i * 10
            history.start_run(batch.uuid, label, RECIPE, None).result(WAIT)
            now += 5
            history.finish_run(label, "stopped", False).result(WAIT)
        history.select_batch("No Run").result(WAIT)
        now = 3000
        history.select_batch("Lot-00").result(WAIT)
        history.start_run(ids["Lot-00"], "later", RECIPE, None).result(WAIT)
        now += 5
        history.finish_run("later", "stopped", False).result(WAIT)
        first = history.batch_choices(RecordsScope()).result(WAIT)
        assert len(first.rows) == 50 and first.more and first.rows[0].label == "Lot-00"
        rest = history.batch_choices(
            RecordsScope(), cursor=first.rows[-1].cursor
        ).result(WAIT)
        assert len(rest.rows) == 5 and not rest.more
        assert len({r.uuid for r in first.rows + rest.rows}) == 55
        assert (
            history.batch_choices(RecordsScope(), "ot-01").result(WAIT).rows[0].label
            == "Lot-01"
        )
        early = history.batch_choices(RecordsScope(until=2000)).result(WAIT)
        assert early.rows[0].label == "Lot-54"  # Sort matching Runs, not the later Run.
        scope = RecordsScope(2000, 2500, ids["Lot-00"])
        assert not history.batch_choices(scope).result(WAIT).selected_matches
        assert not history.batch_page(since=2000, until=2500).result(WAIT).rows
        assert not history.batch_choices(RecordsScope(since=3005)).result(WAIT).rows
        assert (
            history.batch_choices(RecordsScope(until=1001)).result(WAIT).rows[0].label
            == "Lot-00"
        )
        now = 4000
        history.select_batch(None).result(WAIT)
        history.start_run(None, "unbatched", RECIPE, None).result(WAIT)
        assert history.batch_choices(RecordsScope(since=4000)).result(WAIT).unbatched
        assert (
            not history.batch_choices(RecordsScope(until=3999)).result(WAIT).unbatched
        )
        assert not history.batch_page(unbatched=True).result(WAIT).rows
    finally:
        finish(history)


def test_inspection_alarm_exact_batch_and_actual_event_time(tmp_path: Path) -> None:
    history = ProductionHistory(
        tmp_path / "history.sqlite3", clock=lambda: 1000, alarm_access=lambda _: 1
    )
    try:
        a = history.select_batch("A").result(WAIT)
        assert a
        # A human label equal to another Batch's UUID must not match that Batch.
        b = history.select_batch(a.uuid).result(WAIT)
        assert b
        for i, (batch, stamp) in enumerate(
            ((a, 999), (a, 1000), (b, 1000), (None, 1000))
        ):
            assert history.offer(
                HistoryContext(
                    stamp,
                    batch.uuid if batch else "no-label",
                    batch.label if batch else None,
                    f"run-{i}",
                    RECIPE,
                ),
                mixed_result(i + 1),
            )
            history.offer_alarms(
                (
                    AlarmEvent(
                        stamp,
                        str(i),
                        "raised",
                        "plc",
                        "气压",
                        "Air",
                        "PLC",
                        1,
                        18,
                        0,
                        "def",
                        batch.uuid if batch else None,
                        batch.label if batch else None,
                    ),
                )
            )
        records = (
            history.query(HistoryQuery(since=1000, until=1001, batch_segment=a.uuid))
            .result(WAIT)
            .records
        )
        assert [r.cycle_id for r in records] == [2]
        assert [
            r.cycle_id
            for r in history.query(HistoryQuery(unbatched=True)).result(WAIT).records
        ] == [4]
        alarms = history.alarm_page(
            AlarmQuery(since=1000, until=1001, batch_id=a.uuid)
        ).result(WAIT)
        assert [a.event.signal for a in alarms] == ["1"]
        assert [
            a.event.signal
            for a in history.alarm_page(AlarmQuery(unbatched=True)).result(WAIT)
        ] == ["3"]
    finally:
        finish(history)


def test_unassociated_alarm_choices_are_permission_fenced(tmp_path: Path) -> None:
    revision = 7

    def access(expected: int | None) -> int:
        if expected != revision:
            raise StaleSessionError()
        return revision

    history = ProductionHistory(
        tmp_path / "history.sqlite3", clock=lambda: 1000, alarm_access=access
    )
    try:
        history.offer_alarms(
            (
                AlarmEvent(
                    1000,
                    "startup",
                    "observed_active",
                    "startup",
                    "未就绪",
                    "Not ready",
                    "application",
                    None,
                    None,
                    None,
                    "",
                    None,
                    None,
                ),
            )
        )
        assert not history.batch_choices(RecordsScope()).result(WAIT).unbatched
        assert (
            history.batch_choices(RecordsScope(), alarm_revision=7)
            .result(WAIT)
            .unbatched
        )
        assert (
            not history.batch_choices(RecordsScope(until=1000), alarm_revision=7)
            .result(WAIT)
            .unbatched
        )
        revision = 8
        with pytest.raises(StaleSessionError):
            history.batch_choices(RecordsScope(), alarm_revision=7)
    finally:
        finish(history)


def test_unbatched_audit_requires_run_cycle_or_explicit_operation_evidence(
    tmp_path: Path,
) -> None:
    history = ProductionHistory(tmp_path / "history.sqlite3", clock=lambda: 1000)
    try:
        history.start_run(None, "run", RECIPE, None).result(WAIT)
        history.finish_run("run", "stopped", False).result(WAIT)
        assert history.offer(
            HistoryContext(1000, "segment", None, "legacy", RECIPE), mixed_result(1)
        )
        records = tuple(
            AuditRecord(i, AuditOperation("a", "A", action, target), outcome, "", "")
            for i, (action, target, outcome) in enumerate(
                (
                    ("production.start", "run", "success"),
                    ("production.stop", "run", "success"),
                    ("production.stop", "legacy", "success"),
                    ("production.clear_statistics", "unbatched", "success"),
                    ("production.start", "run", "failed"),
                    ("production.stop", "missing", "success"),
                    ("session.logout", "run", "success"),
                    ("production.clear_statistics", "unbatched", "denied"),
                )
            )
        )
        result = history.associated_audit(RecordsScope(unbatched=True), records).result(
            WAIT
        )
        assert [r.cursor for r in result] == [0, 1, 2, 3]
    finally:
        finish(history)


def test_truncated_metadata_target_cannot_identify_a_prefix_batch(
    tmp_path: Path,
) -> None:
    history = ProductionHistory(tmp_path / "history.sqlite3")
    try:
        batch = history.select_batch("A" * 512).result(WAIT)
        assert batch
        history.select_batch(batch.label + "B").result(WAIT)
        record = AuditRecord(
            1,
            AuditOperation("a", "A", "batch.information", batch.label),
            "success",
            "",
            "",
        )
        assert not history.associated_audit(
            RecordsScope(batch=batch.uuid), (record,)
        ).result(WAIT)
    finally:
        finish(history)


def test_audit_bounded_join_pagination_and_non_associations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    security = tmp_path / "security.sqlite3"
    initialize_administrator(security, "Admin", TEST_PASSWORD)
    identity = LocalIdentity(security)
    history = ProductionHistory(tmp_path / "history.sqlite3", clock=lambda: 1000)
    try:
        session = identity.login("Admin", TEST_PASSWORD).result(WAIT)
        batch = history.select_batch("Lot A").result(WAIT)
        assert batch
        history.start_run(batch.uuid, "actual-run", RECIPE, None).result(WAIT)
        history.finish_run("actual-run", "stopped", False).result(WAIT)
        identity.audit.record(
            AuditOperation("x", "A", "session.login", "actual-run"), "success"
        )
        identity.audit.record(
            AuditOperation("x", "A", "production.start", "actual-run"), "failed"
        )
        identity.audit.record(
            AuditOperation("x", "A", "production.stop", "missing-run"), "success"
        )
        identity.audit.record(
            AuditOperation("x", "A", "batch.select", batch.uuid), "denied"
        )
        identity.audit.record(
            AuditOperation(
                "x", "A", "batch.information", batch.label, "product = A", "product = B"
            ),
            "success",
        )
        for i in range(110):
            identity.audit.record(
                AuditOperation(
                    "x",
                    "A",
                    "production.stop",
                    "actual-run" if i % 2 else "unknown",
                    id=f"op-{i}",
                ),
                "success",
            )
            if i % 20 == 0:
                identity.audit_page(AuditQuery(), session.revision).result(WAIT)
        scope = RecordsScope(batch=batch.uuid)
        query = AuditQuery(actor="A")
        first = scoped_audit_page(
            history, identity, query, scope, session.revision, Event()
        ).result(WAIT)
        assert len(first) == 51
        second = scoped_audit_page(
            history,
            identity,
            replace(query, before=first[49].cursor),
            scope,
            session.revision,
            Event(),
        ).result(WAIT)
        assert len(second) == 6
        assert second[-1].operation.action == "batch.information"
        newer = scoped_audit_page(
            history,
            identity,
            replace(query, before=second[0].cursor, newer=True),
            scope,
            session.revision,
            Event(),
        ).result(WAIT)
        assert newer[::-1] == first[:50]
        assert len({r.cursor for r in first[:50] + second}) == 56

        pending: Future[tuple[AuditRecord, ...]] = Future()

        def hold(
            _scope: RecordsScope, _records: tuple[AuditRecord, ...]
        ) -> Future[tuple[AuditRecord, ...]]:
            return pending

        monkeypatch.setattr(history, "associated_audit", hold)
        cancelled = Event()
        wait = scoped_audit_page(
            history, identity, query, scope, session.revision, cancelled
        )
        identity.audit_page(AuditQuery(), session.revision).result(WAIT)
        cancelled.set()
        pending.set_result(())
        with pytest.raises(CancelledError):
            wait.result(WAIT)
        pending = Future()
        stale = scoped_audit_page(
            history, identity, query, scope, session.revision, Event()
        )
        identity.audit_page(AuditQuery(), session.revision).result(WAIT)
        identity.logout()
        pending.set_result(())
        with pytest.raises(StaleSessionError):
            stale.result(WAIT)
    finally:
        finish(history)
        identity.request_stop()
        assert identity.join(WAIT)
