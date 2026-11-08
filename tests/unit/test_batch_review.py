"""Named Batch review uses durable group counts, observation times and admission actors."""

import sqlite3
from concurrent.futures import Future
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from threading import Event

import pytest
from tests.support.application import WAIT, FakeSession, close, prepared
from tests.support.business import mixed_result
from tests.support.startup import TEST_PASSWORD, provision_account
from tests.unit.test_audit import hold_insert
from tests.unit.test_durable_batches import RECIPE
from tests.unit.test_four_cycle_presentation import completed
from tests.unit.test_history import finish

from capsule_vision.application import ApplicationStateError, ProductionApplication
from capsule_vision.application.account_maintenance import initialize_administrator
from capsule_vision.application.audit import (
    AuditOperation,
    AuditQuery,
    AuditUnavailable,
)
from capsule_vision.application.batch_review import (
    BatchInformation,
    BatchInformationConflict,
    BatchSetup,
    StartAccount,
)
from capsule_vision.application.history import HistoryContext, ProductionHistory
from capsule_vision.application.identity import LocalIdentity
from capsule_vision.application.identity_models import (
    AuthorizationError,
    Role,
    StaleSessionError,
)


def test_start_account_is_frozen_at_admission_and_preserved_across_launches(
    tmp_path: Path,
) -> None:
    security, path = tmp_path / "security.sqlite3", tmp_path / "history.sqlite3"
    initialize_administrator(security, "TestAdmin", TEST_PASSWORD)
    candidate = prepared(tmp_path)
    expected: list[StartAccount] = []
    batch = ""
    for launch in range(2):
        identity = LocalIdentity(security)
        history = ProductionHistory(path)
        runtime = FakeSession(automatic_start=False)
        app = ProductionApplication(
            lambda _p, _g, session=runtime: session,
            identity=identity,
            history=history,
            initial_candidate=candidate,
        )
        try:
            history.query().result(WAIT)
            assert identity.snapshot().account is None
            administrator = (
                identity.login("TestAdmin", TEST_PASSWORD).result(WAIT).account
            )
            assert administrator
            if launch == 0:
                account = provision_account(
                    identity, "Night Shift", Role.OPERATOR, TEST_PASSWORD
                )
                identity.login(account.username, TEST_PASSWORD).result(WAIT)
                app.start_batch("Lot A").result(WAIT)
                batch = app.snapshot().batch_uuid or ""
                assert batch
            else:
                account = administrator
                assert app.snapshot().batch_uuid == batch
            expected.append(StartAccount(account.account_id, account.username))
            start = app.start()
            assert runtime.start_entered.wait(WAIT)
            identity.logout()
            runtime.ready.set_result(None)
            start.result(WAIT)
            app.stop().result(WAIT)
            detail = history.batch_detail(batch).result(WAIT)
            assert [r.account for r in detail.runs] == expected[::-1]
            assert all(r.terminated and r.outcome == "stopped" for r in detail.runs)
            assert (
                detail.batch.first is detail.batch.last is None
            )  # No inspection occurred.
            assert not detail.batch.totals.batches_completed
        finally:
            if not runtime.ready.done():
                runtime.ready.set_result(None)
            close(app)


def test_review_legacy_evidence_retention_exact_segments_and_unbatched_runs(
    tmp_path: Path,
) -> None:
    now = 2_000_000_000.0
    path = tmp_path / "history.sqlite3"

    def clock() -> float:
        return now

    history = ProductionHistory(path, clock=clock)
    try:
        batch = history.select_batch("A").result(WAIT)
        assert batch
        history.start_run(batch.uuid, "legacy", RECIPE, None).result(WAIT)
        ctx = HistoryContext(now, batch.uuid, batch.label, "legacy", RECIPE)
        assert history.offer(ctx, mixed_result(11))
        assert history.offer(replace(ctx, completed_at=now - 10), mixed_result(12))
        history.offer_group("legacy", completed(11))
        history.finish_run("legacy", "stopped", True).result(WAIT)
        history.select_batch(None).result(WAIT)
        actor = StartAccount("line-1", "A. W.")
        history.start_run(None, "unbatched", RECIPE, None, actor).result(WAIT)
        assert history.offer(
            replace(
                ctx,
                run_id="unbatched",
                batch_segment="unbatched",
                batch_label=None,
                completed_at=now + 500,
            ),
            mixed_result(13),
        )
        history.finish_run("unbatched", "stopped", False).result(WAIT)
    finally:
        finish(history)
    # Recreate the actual pre-extension shape in this disposable fixture.
    with closing(sqlite3.connect(path)) as db, db:
        assert db.execute(
            "SELECT run,account_id,username FROM run_start_accounts"
        ).fetchall() == [("unbatched", "line-1", "A. W.")]
        db.execute("DROP TABLE run_start_accounts")
        db.execute("DROP TABLE batch_information")
        db.execute("DROP TABLE batch_inspection_times")
    for expired in (False, True):
        if expired:
            now += 31 * 86400
        history = ProductionHistory(path, clock=clock)
        try:
            (overview,) = history.batch_page().result(WAIT).rows
            record = history.batch_detail(overview.uuid).result(WAIT)
            assert record.batch == overview and record.batch.label == "A"
            assert (overview.first, overview.last) == (
                2_000_000_000 - 10,
                2_000_000_000,
            )
            assert record.run_count == 1 and record.runs[0].account is None
            assert record.uncertain and record.expired == expired
            assert (
                overview.totals.batches_completed == 1
                and overview.totals.inspected
                == overview.totals.channel_ok + overview.totals.channel_ng
                > 0
            )
            assert (len(history.query().result(WAIT).records) == 0) == expired
        finally:
            finish(history)


def test_batch_search_and_recent_activity_keyset_with_equal_timestamps(
    tmp_path: Path,
) -> None:
    now = 1000.0
    history = ProductionHistory(tmp_path / "history.sqlite3", clock=lambda: now)
    try:
        for i in range(51):
            row = history.select_batch(f"LOT-{i:02}").result(WAIT)
            assert row
            if i:
                history.start_run(row.uuid, f"shift-{i}", RECIPE, None).result(WAIT)
                history.finish_run(f"shift-{i}", "stopped", False).result(WAIT)
        history.select_batch("lot-01").result(WAIT)
        batch = history.select_batch("LOT-00").result(WAIT)
        assert batch
        now += 10
        history.start_run(batch.uuid, "new-run", RECIPE, None).result(WAIT)
        first = history.batch_page("LOT-").result(WAIT)
        assert (
            first.more
            and first.rows[0].uuid == batch.uuid
            and first.rows[0].last is None
        )
        second = history.batch_page("LOT-", first.rows[-1].cursor).result(WAIT)
        assert not second.more and len(second.rows) == 1
        assert len({r.uuid for r in (*first.rows, *second.rows)}) == 51
        assert (
            history.batch_page("LOT-", second.rows[0].cursor, True).result(WAIT).rows
            == first.rows
        )
        assert not history.batch_page("%").result(WAIT).rows
        day = history.batch_page("LOT-", since=1000, until=2000).result(WAIT)
        assert day == first
        next_day_page = history.batch_page(
            "LOT-", day.rows[-1].cursor, since=1000, until=2000
        ).result(WAIT)
        assert next_day_page == second
        previous = history.batch_page(
            "LOT-", next_day_page.rows[0].cursor, True, since=1000, until=2000
        ).result(WAIT)
        assert previous.rows == day.rows and not previous.more
        history.finish_run("new-run", "stopped", False).result(WAIT)
        for i in range(50):
            history.start_run(
                batch.uuid, f"r{i}", RECIPE, None, StartAccount(str(i), f"Line {i}")
            ).result(WAIT)
            history.finish_run(f"r{i}", "stopped", False).result(WAIT)
        runs = history.batch_detail(batch.uuid).result(WAIT)
        assert len(runs.runs) == 50 and runs.run_count == 51
        history.start_run(batch.uuid, "later", RECIPE, None).result(WAIT)
        last = history.batch_detail(batch.uuid, 50, runs.run_anchor).result(WAIT)
        assert (
            last.run_count == 51
            and len(last.runs) == 1
            and last.runs[0].account is None
        )
        assert history.batch_detail(batch.uuid).result(WAIT).run_count == 52
    finally:
        finish(history)


def test_run_overlap_and_activity_use_individual_recorded_runs(tmp_path: Path) -> None:
    midnight = 2_000_000_000
    day = 86400
    now = midnight - day
    history = ProductionHistory(tmp_path / "history.sqlite3", clock=lambda: now)

    def matches(start: int) -> set[str]:
        return {
            row.label
            for row in history.batch_page(since=start, until=start + day)
            .result(WAIT)
            .rows
        }

    try:
        empty = history.select_batch("Empty").result(WAIT)
        lot = history.select_batch("Resumed").result(WAIT)
        assert empty and lot
        now = midnight + 10
        history.start_run(lot.uuid, "morning", RECIPE, None).result(WAIT)
        history.offer_group("morning", completed(11))
        now += 60
        history.finish_run("morning", "stopped", False).result(WAIT)
        now = midnight + 2 * day + 80
        history.start_run(lot.uuid, "night", RECIPE, None).result(WAIT)
        # An earlier Run's wall-clock observation cannot be evidence for a later Run.
        previous = HistoryContext(
            midnight + 6 * day, lot.uuid, lot.label, "night", RECIPE
        )
        assert history.offer(previous, mixed_result(15))
        now = midnight + 3 * day
        history.finish_run("night", "interrupted", True).result(WAIT)
        assert matches(midnight) == matches(midnight + 2 * day) == {"Resumed"}
        assert matches(midnight + day) == matches(now) == set()
        all_rows = history.batch_page().result(WAIT).rows
        resumed, unused = all_rows
        assert unused.first_run is unused.recent_run is unused.recent_outcome is None
        assert resumed.first_run == midnight + 10 and resumed.recent_run == now
        assert resumed.recent_outcome == "interrupted"
        assert history.batch_page(since=midnight, until=midnight + day).result(
            WAIT
        ).rows == (resumed,)
        detail = history.batch_detail(lot.uuid).result(WAIT)
        assert detail.run_count == 2 and detail.batch.totals.batches_completed == 1
        assert detail.uncertain

        now += day - 20
        history.start_run(lot.uuid, "ongoing", RECIPE, None).result(WAIT)
        started = now
        now += 30
        row = history.batch_page().result(WAIT).rows[0]
        assert row.recent_outcome == "open" and row.recent_run == started
        assert matches(midnight + 3 * day) == matches(midnight + 4 * day) == {"Resumed"}
        assert not matches(midnight + 5 * day)
        assert not history.batch_page(since=now, until=now).result(WAIT).rows
        assert history.offer(
            replace(previous, run_id="ongoing", completed_at=now), mixed_result(20)
        )
        assert history.batch_detail(lot.uuid).result(WAIT).batch.recent_run == now
        now += 10
        history.offer_group("ongoing", completed(20))
        assert history.batch_page().result(WAIT).rows[0].recent_run == now
        now += 10
        history.finish_run("ongoing", "stopped", False).result(WAIT)
        row = history.batch_page().result(WAIT).rows[0]
        assert row.recent_outcome == "stopped" and row.recent_run == now

        now = midnight + 5 * day
        history.start_run(lot.uuid, "instant", RECIPE, None).result(WAIT)
        history.finish_run("instant", "stopped", False).result(WAIT)
        assert matches(now) == {
            "Resumed"
        }  # A zero-duration Start at midnight is recorded.
        history.start_run(lot.uuid, "cancelled", RECIPE, None).result(WAIT)
        history.finish_run("cancelled", "unactivated", False).result(WAIT)
        assert (
            history.batch_detail(lot.uuid).result(WAIT).batch.recent_outcome
            == "stopped"
        )
    finally:
        finish(history)


def test_batch_metadata_has_authorization_audited_commit_and_stale_intent_fencing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "history.sqlite3"
    security = tmp_path / "security.sqlite3"
    entered, release = Event(), Event()
    hold_insert(monkeypatch, "batch.information", entered, release)
    initialize_administrator(security, "TestAdmin", TEST_PASSWORD)
    identity = LocalIdentity(security)
    history = ProductionHistory(path)
    app = ProductionApplication(
        lambda _p, _g: FakeSession(),
        history=history,
        identity=identity,
        initial_candidate=prepared(tmp_path),
    )
    empty, info = (
        BatchInformation(),
        BatchInformation("Capsules", "Size 0", "Second shift"),
    )
    try:
        history.query().result(WAIT)
        identity.login("TestAdmin", TEST_PASSWORD).result(WAIT)
        operator = provision_account(identity, "Line", Role.OPERATOR, TEST_PASSWORD)
        app.start_batch("Lot B").result(WAIT)
        batch = app.snapshot().batch_uuid
        assert batch
        app.end_batch().result(WAIT)
        identity.logout()
        assert history.batch_detail(batch).result(WAIT).info == empty
        with pytest.raises(AuthorizationError):
            app.update_batch_information(batch, empty, info)
        release.set()  # Drain the denial before installing the controlled intent gate.
        identity.login("Line", TEST_PASSWORD).result(WAIT)
        result = app.update_batch_information(batch, empty, info)
        result.result(WAIT)
        assert history.batch_detail(batch).result(WAIT).info == info
        assert app.snapshot().batch_uuid is None
        with pytest.raises(BatchInformationConflict):
            app.update_batch_information(
                batch, empty, BatchInformation("Wrong")
            ).result(WAIT)
        assert history.snapshot().error == ""
        app.start().result(WAIT)
        with pytest.raises(ApplicationStateError):
            app.update_batch_information(batch, info, empty)
        app.stop().result(WAIT)
        identity.login("TestAdmin", TEST_PASSWORD).result(WAIT)
        completed_edit = next(
            r
            for r in identity.audit_page(AuditQuery(action="batch.information")).result(
                WAIT
            )
            if r.outcome == "success"
        )
        assert (
            completed_edit.operation.actor_id == operator.account_id
            and completed_edit.operation.target == "Lot B"
        )
        assert (
            "product = —" in completed_edit.operation.before
            and "notes = Second shift" in completed_edit.operation.after
        )
        entered.clear()
        release.clear()
        edit = app.update_batch_information(batch, info, empty)
        assert entered.wait(WAIT)
        with pytest.raises(ApplicationStateError):
            app.start()
        identity.logout()
        release.set()
        with pytest.raises(StaleSessionError):
            edit.result(WAIT)
        assert history.batch_detail(batch).result(WAIT).info == info
        identity.login("TestAdmin", TEST_PASSWORD).result(WAIT)

        def deny_intent(_op: AuditOperation) -> Future[None]:
            failed = Future[None]()
            failed.set_exception(AuditUnavailable("Audit unavailable"))
            return failed

        with monkeypatch.context() as patch:
            patch.setattr(identity.audit, "intent", deny_intent)
            with pytest.raises(AuditUnavailable):
                app.update_batch_information(batch, info, empty).result(WAIT)
        assert history.batch_detail(batch).result(WAIT).info == info
        assert not app.snapshot().batch_pending and not history.snapshot().error
    finally:
        release.set()
        close(app)
    another = ProductionHistory(path)
    try:
        assert another.batch_detail(batch).result(WAIT).info == info
    finally:
        finish(another)


@pytest.mark.parametrize("role", list(Role))
def test_batch_setup_creates_and_continues_with_metadata_without_reaccounting(
    tmp_path: Path, role: Role
) -> None:
    security = tmp_path / "security.sqlite3"
    initialize_administrator(security, "Admin", TEST_PASSWORD)
    identity = LocalIdentity(security)
    history = ProductionHistory(tmp_path / "history.sqlite3")
    app = ProductionApplication(
        lambda _p, _g: FakeSession(),
        history=history,
        identity=identity,
        initial_candidate=prepared(tmp_path),
    )
    info = BatchInformation("Capsules A", "Size 0", "Day shift")
    try:
        identity.login("Admin", TEST_PASSWORD).result(WAIT)
        if role is Role.GUEST:
            identity.logout()
        elif role is not Role.ADMINISTRATOR:
            provision_account(identity, "Operator A", role, TEST_PASSWORD)
            identity.login("Operator A", TEST_PASSWORD).result(WAIT)
        original = history.batch_setup("Lot A").result(WAIT)
        assert original == BatchSetup(None, BatchInformation())
        if role is Role.GUEST:
            with pytest.raises(AuthorizationError):
                app.start_batch("Lot A", expected=original, information=info)
            assert history.batch_setup("Lot A").result(WAIT) == original
            assert app.snapshot().batch_uuid is None
            return
        app.start_batch(" Lot A ", expected=original, information=info).result(WAIT)
        uid = app.snapshot().batch_uuid
        assert uid
        initial = history.batch_setup("Lot A").result(WAIT)
        assert initial == BatchSetup(uid, info)
        history.start_run(uid, "run-1", RECIPE, None).result(WAIT)
        history.offer_group("run-1", completed(11))
        history.finish_run("run-1", "stopped", False).result(WAIT)
        before = history.batch_detail(uid).result(WAIT)
        app.end_batch().result(WAIT)
        changed = BatchInformation("Capsules A", "Size 0", "Continued by evening shift")
        app.start_batch("Lot A", expected=initial, information=changed).result(WAIT)
        after = history.batch_detail(uid).result(WAIT)
        assert app.snapshot().batch_uuid == uid
        assert after.batch.totals == before.batch.totals and after.runs == before.runs
        assert after.info == changed
        with pytest.raises(BatchInformationConflict):
            app.start_batch("Lot A", expected=initial, information=info).result(WAIT)
        assert history.batch_detail(uid).result(WAIT).info == changed
        app.end_batch().result(WAIT)
        assert app.snapshot().batch_uuid is None
        assert history.batch_setup("Lot A").result(WAIT).uuid == uid
        identity.login("Admin", TEST_PASSWORD).result(WAIT)
        records = identity.audit_page(AuditQuery(action="batch.information")).result(
            WAIT
        )
        successful = [r for r in records if r.outcome == "success"]
        assert len(successful) == 2
        assert all(r.operation.target == "Lot A" for r in successful)
        assert any(
            "Day shift" in r.operation.before and "evening shift" in r.operation.after
            for r in successful
        )
    finally:
        close(app)


def test_batch_setup_rechecks_session_before_atomic_selection_and_information(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    security = tmp_path / "security.sqlite3"
    entered, release = Event(), Event()
    hold_insert(monkeypatch, "batch.information", entered, release)
    initialize_administrator(security, "Admin", TEST_PASSWORD)
    identity = LocalIdentity(security)
    history = ProductionHistory(tmp_path / "history.sqlite3")
    app = ProductionApplication(
        lambda _p, _g: FakeSession(),
        history=history,
        identity=identity,
        initial_candidate=prepared(tmp_path),
    )
    try:
        identity.login("Admin", TEST_PASSWORD).result(WAIT)
        app.start_batch("Original").result(WAIT)
        old = app.snapshot().batch_uuid
        expected = history.batch_setup("New").result(WAIT)
        pending = app.start_batch(
            "New", expected=expected, information=BatchInformation("Capsules")
        )
        assert entered.wait(WAIT)
        with pytest.raises(ApplicationStateError):
            app.start()
        with pytest.raises(ApplicationStateError):
            app.end_batch()
        identity.logout()
        release.set()
        with pytest.raises(StaleSessionError):
            pending.result(WAIT)
        assert app.snapshot().batch_uuid == old and not app.snapshot().batch_pending
        assert history.batch_setup("New").result(WAIT) == expected
        assert not history.snapshot().error
    finally:
        release.set()
        close(app)
