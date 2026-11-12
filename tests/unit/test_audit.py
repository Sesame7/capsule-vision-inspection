"""Real disposable SQLite evidence, fake external writes, and explicit failure ordering."""

import sqlite3
import subprocess
import sys
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Event

import pytest
from tests.support.application import WAIT, FakeSession, close, prepared
from tests.support.startup import TEST_PASSWORD, authenticated_startup, saved_data

from capsule_vision.application import ProductionApplication
from capsule_vision.application.account_maintenance import initialize_administrator
from capsule_vision.application.audit import (
    AuditAccount,
    AuditOperation,
    AuditQuery,
    AuditTrail,
    AuditUnavailable,
)
from capsule_vision.application.identity import LocalIdentity
from capsule_vision.application.identity_models import (
    AuthenticationError,
    AuthorizationError,
    Role,
    StaleSessionError,
)
from capsule_vision.application.manual_plc import execute_manual
from capsule_vision.application.passwords import PasswordVerifier, verify_password
from capsule_vision.communication.endpoints import FinsTcpEndpoint
from capsule_vision.communication.fins import FinsTimeoutError
from capsule_vision.communication.operations import (
    CommunicationRequest,
    CommunicationResponse,
    RequestKind,
)


def hold_insert(
    monkeypatch: pytest.MonkeyPatch, action: str, entered: Event, release: Event
) -> None:
    def connect(path: Path, *, timeout: float) -> sqlite3.Connection:
        db = sqlite3.connect(path, timeout=timeout)

        def trace(statement: str) -> None:
            if (
                statement.startswith("INSERT INTO operations ")
                and f"'{action}'" in statement
            ):
                entered.set()
                release.wait(WAIT)

        db.set_trace_callback(trace)
        return db

    monkeypatch.setattr("capsule_vision.application.audit.connect", connect)


def test_append_only_idempotence_and_crash_recovery(tmp_path: Path) -> None:
    path = tmp_path / "audit.sqlite3"
    script = """
import os,sys
from pathlib import Path
from capsule_vision.application.audit import AuditTrail,AuditOperation
audit=AuditTrail(Path(sys.argv[1]),lambda _:1)
audit.intent(AuditOperation('id','Lin','plc.write','W5',id='crash')).result(5)
os._exit(23)
"""
    assert (
        subprocess.run([sys.executable, "-c", script, str(path)], timeout=15).returncode
        == 23
    )
    audit = AuditTrail(path, lambda _: 1)
    try:
        (unresolved,) = audit.page(AuditQuery()).result(WAIT)
        op = unresolved.operation
        assert (
            op.id == "crash"
            and unresolved.outcome == "unresolved"
            and not unresolved.completed_utc
        )
        assert datetime.fromisoformat(op.utc).utcoffset() == UTC.utcoffset(None)
        audit.intent(op).result(WAIT)
        audit.complete(op, "success", "ACK").result(WAIT)
        audit.complete(op, "success", "ACK").result(WAIT)
        with pytest.raises(AuditUnavailable, match="Conflicting audit completion"):
            audit.complete(op, "failed").result(WAIT)
        with pytest.raises(AuditUnavailable, match="identity"):
            audit.intent(replace(op, target="different")).result(WAIT)
        with pytest.raises(AuditUnavailable, match="identity"):
            audit.complete(replace(op, target="different"), "success").result(WAIT)
        (row,) = audit.page(AuditQuery(actor="Lin", action="plc.write")).result(WAIT)
        assert row.outcome == "success" and row.detail == "ACK"
        assert not audit.page(AuditQuery(since="9999", actor="Lin")).result(WAIT)
    finally:
        audit.close()


def test_historical_account_discovery_and_exact_selection(tmp_path: Path) -> None:
    audit = AuditTrail(tmp_path / "audit.sqlite3", lambda _: 1)
    try:
        accounts = [AuditAccount(f"id-{i}", f"Archived {i:03}") for i in range(65)]
        accounts.extend(
            (
                AuditAccount("same-id", "Historical name"),
                AuditAccount("other", "same-id"),
                AuditAccount("percent", "Past % operator"),
            )
        )
        for account in accounts:
            for action in ("recipe.save", "session.login"):
                audit.record(
                    AuditOperation(account.actor_id, account.username, action, "saved"),
                    "success",
                )
            audit.page(AuditQuery()).result(
                WAIT
            )  # Keep fixture writes within the real queue bound.
        first = audit.accounts().result(WAIT)
        assert len(first) == 51
        rest = audit.accounts(cursor=first[49].cursor).result(WAIT)
        assert set(first[:50] + rest) == set(accounts)
        assert audit.accounts("Archived 064").result(WAIT) == (accounts[64],)
        assert audit.accounts("%").result(WAIT) == (accounts[-1],)
        selected = AuditAccount("same-id", "Historical name")
        records = audit.page(AuditQuery(account=selected)).result(WAIT)
        assert len(records) == 2 and {r.operation.action for r in records} == {
            "recipe.save",
            "session.login",
        }
        assert all(
            r.operation.actor_id == "same-id"
            and r.operation.username == "Historical name"
            for r in records
        )
    finally:
        audit.close()


def test_bounded_owner_drains_and_keeps_unresolved_on_overload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = Event(), Event()
    hold_insert(monkeypatch, "recipe.save", entered, release)
    audit = AuditTrail(tmp_path / "audit.sqlite3", lambda _: 1, capacity=1)
    op = AuditOperation("id", "Lin", "recipe.save", "recipe")
    first = audit.intent(op)
    try:
        assert entered.wait(WAIT)
        second = audit.intent(op)
        with pytest.raises(AuditUnavailable):
            audit.complete(op, "success").result(WAIT)
        assert audit.observation()[0] == 1
    finally:
        release.set()
        audit.close()
    assert first.result(WAIT) is None and second.result(WAIT) is None
    with closing(sqlite3.connect(audit.path)) as db:
        assert db.execute("SELECT count(*) FROM completions").fetchone() == (0,)


def test_unavailable_intent_blocks_mutations_but_allows_login_run_stop(
    tmp_path: Path,
) -> None:
    path = tmp_path / "security.sqlite3"
    (tmp_path / "audit.sqlite3").mkdir()
    initialize_administrator(path, "Admin", TEST_PASSWORD)
    identity = LocalIdentity(path)
    app = ProductionApplication(
        lambda _p, _g: FakeSession(),
        identity=identity,
        initial_candidate=prepared(tmp_path),
    )
    try:
        identity.login("Admin", TEST_PASSWORD).result(WAIT)
        with pytest.raises(AuditUnavailable):
            identity.create_account("Blocked", Role.OPERATOR, TEST_PASSWORD).result(
                WAIT
            )
        assert [a.username for a in identity.list_accounts().result(WAIT)] == ["Admin"]
        request = CommunicationRequest(RequestKind.WRITE, FinsTcpEndpoint(), 5, 1, (7,))
        with pytest.raises(AuditUnavailable):
            execute_manual(
                request,
                lambda _: pytest.fail("unaudited PLC request"),
                None,
                identity,
                identity.snapshot().revision,
            )
        app.start().result(WAIT)
        app.stop().result(WAIT)
        assert identity.audit.observation()[0] >= 2
    finally:
        close(app)


def test_manual_command_records_original_actor_after_logout_and_fences_queued_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = Event(), Event()
    hold_insert(monkeypatch, "hold", entered, release)
    startup = authenticated_startup(saved_data(tmp_path))
    identity = startup.application.identity
    assert identity
    actor = identity.snapshot()
    assert actor.account
    request = CommunicationRequest(RequestKind.WRITE, FinsTcpEndpoint(), 7, 1, (99,))

    def acknowledged(req: CommunicationRequest) -> CommunicationResponse:
        identity.logout()
        return CommunicationResponse(req, None, 1, datetime.now(UTC))

    try:
        execute_manual(request, acknowledged, None, identity, actor.revision)
        identity.login("TestAdmin", TEST_PASSWORD).result(WAIT)
        (write,) = identity.audit_page(
            AuditQuery(action="plc.write", actor=actor.account.account_id)
        ).result(WAIT)
        assert write.outcome == "success" and write.operation.after == "99"
        holding = identity.audit.intent(AuditOperation.by(actor, "hold", "fixture"))
        assert entered.wait(WAIT)
        queued = identity.audit_page(AuditQuery())
        queued_accounts = identity.audit.accounts()
        queued_revisions = identity.audit.revisions("recipe", "fixture")
        queued_content = identity.audit.revision_content("not-readable-after-logout")
        identity.logout()
        release.set()
        holding.result(WAIT)
        with pytest.raises(StaleSessionError):
            queued.result(WAIT)
        with pytest.raises(StaleSessionError):
            queued_accounts.result(WAIT)
        with pytest.raises(StaleSessionError):
            queued_revisions.result(WAIT)
        with pytest.raises(StaleSessionError):
            queued_content.result(WAIT)
    finally:
        release.set()
        close(startup.application)


def test_actual_commits_differences_actor_and_independent_finalization_failure(
    tmp_path: Path,
) -> None:
    data = saved_data(tmp_path)
    startup = authenticated_startup(data)
    app, editor = startup.application, startup.editor
    identity = app.identity
    assert editor and identity and editor.entry
    request = CommunicationRequest(RequestKind.WRITE, FinsTcpEndpoint(), 5, 1, (7,))
    calls: list[CommunicationRequest] = []

    def ack(req: CommunicationRequest) -> CommunicationResponse:
        calls.append(req)
        return CommunicationResponse(req, None, 2.0, datetime.now(UTC))

    try:
        initial = identity.snapshot().account
        assert initial
        target = identity.create_account("Worker", Role.OPERATOR, TEST_PASSWORD).result(
            WAIT
        )
        identity.set_role(target.account_id, Role.ENGINEER).result(WAIT)
        identity.set_enabled(target.account_id, False).result(WAIT)
        identity.reset_password(target.account_id, "different isolated secret").result(
            WAIT
        )
        before = editor.entry.recipe.display_name
        editor.draft.display_name = "New product"
        editor.save()
        assert execute_manual(request, ack, None, identity, None).request == request

        def uncertain(req: CommunicationRequest) -> CommunicationResponse:
            calls.append(req)
            raise FinsTimeoutError("No ACK")

        with pytest.raises(FinsTimeoutError):
            execute_manual(request, uncertain, None, identity, None)
        with pytest.raises(AuthenticationError):
            identity.login("Worker", "definitely incorrect").result(WAIT)
        events = identity.audit_page(AuditQuery()).result(WAIT)
        changed = next(r for r in events if r.operation.action == "recipe.save")
        assert (
            changed.operation.actor_id == initial.account_id
            and changed.operation.target == editor.entry.recipe.recipe_id
        )
        assert (
            before in changed.operation.before
            and "New product" in changed.operation.after
            and "model" not in changed.operation.after
        )
        role = next(r for r in events if r.operation.action == "account.role")
        assert (role.operation.before, role.operation.after, role.outcome) == (
            "OPERATOR",
            "ENGINEER",
            "success",
        )
        assert len(calls) == 2 and {
            r.outcome for r in events if r.operation.action == "plc.write"
        } == {"success", "uncertain"}
        # Fail only the audit terminal transaction after the separate effect commits.
        with closing(sqlite3.connect(data.audit)) as db, db:
            db.execute(
                "CREATE TRIGGER no_finalize BEFORE INSERT ON completions BEGIN SELECT RAISE(ABORT,'controlled terminal failure'); END"
            )
        assert execute_manual(request, ack, None, identity, None).request == request
        identity.set_enabled(target.account_id, True).result(WAIT)
        assert next(
            a
            for a in identity.list_accounts().result(WAIT)
            if a.account_id == target.account_id
        ).enabled
        events = identity.audit_page(AuditQuery()).result(WAIT)
        assert [r.outcome for r in events[:2]] == ["unresolved", "unresolved"] and len(
            calls
        ) == 3
        assert identity.audit.observation()[0] >= 2
    finally:
        close(app)
    raw = data.audit.read_bytes()
    for secret in (TEST_PASSWORD, "definitely incorrect", "different isolated secret"):
        assert secret.encode() not in raw


def test_stale_after_durable_intent_cannot_execute_as_next_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = Event(), Event()
    data = saved_data(tmp_path)
    initialize_administrator(data.security, "TestAdmin", TEST_PASSWORD)
    hold_insert(monkeypatch, "account.create", entered, release)
    startup = authenticated_startup(data)
    identity = startup.application.identity
    assert identity
    admitted = identity.snapshot()
    assert admitted.account
    try:
        pending = identity.create_account("Rejected", Role.OPERATOR, TEST_PASSWORD)
        assert entered.wait(WAIT)
        identity.logout()
        with pytest.raises(AuthorizationError):
            identity.audit.page(AuditQuery())
        release.set()
        with pytest.raises(StaleSessionError):
            pending.result(WAIT)
        identity.login("TestAdmin", TEST_PASSWORD).result(WAIT)
        (row,) = identity.audit_page(
            AuditQuery(action="account.create", actor=admitted.account.account_id)
        ).result(WAIT)
        assert (
            row.outcome == "stale"
            and row.operation.actor_id == admitted.account.account_id
        )
        assert all(
            a.username != "Rejected" for a in identity.list_accounts().result(WAIT)
        )
    finally:
        release.set()
        close(startup.application)


def test_cancelled_login_and_supersession_record_the_original_actor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    startup = authenticated_startup(saved_data(tmp_path))
    identity = startup.application.identity
    assert identity
    entered, release = Event(), Event()

    def held(secret: str, verifier: PasswordVerifier) -> bool:
        entered.set()
        assert release.wait(WAIT)
        return verify_password(secret, verifier)

    monkeypatch.setattr("capsule_vision.application.identity.verify_password", held)
    try:
        actor = identity.snapshot()
        assert actor.account
        cancelled = identity.login("Uncommitted", TEST_PASSWORD)
        assert entered.wait(WAIT) and identity.cancel_login(cancelled)
        old = identity.login("Superseded", TEST_PASSWORD)
        current = identity.login("TestAdmin", TEST_PASSWORD)
        release.set()
        for attempt in (cancelled, old):
            with pytest.raises(StaleSessionError):
                attempt.result(WAIT)
        assert current.result(WAIT).revision == actor.revision + 1
        entries = identity.audit_page(AuditQuery(action="session.login")).result(WAIT)
        assert [(e.operation.target, e.outcome) for e in entries[:3]] == [
            (actor.account.account_id, "success"),
            ("Superseded", "stale"),
            ("Uncommitted", "cancelled"),
        ]
        assert all(
            e.operation.actor_id == actor.account.account_id for e in entries[:3]
        )
    finally:
        release.set()
        close(startup.application)


def test_critical_file_intent_precedes_publication_and_vision_files_settle_independently(
    tmp_path: Path,
) -> None:
    data = saved_data(tmp_path)
    startup = authenticated_startup(data)
    editor, identity = startup.editor, startup.application.identity
    assert editor and editor.entry and editor.camera_sources and identity
    try:
        before = editor.entry.path.read_bytes()
        editor.draft.display_name = "Refused publication"
        with closing(sqlite3.connect(data.audit)) as db, db:
            db.execute(
                "CREATE TRIGGER refuse BEFORE INSERT ON operations WHEN NEW.action IN ('recipe.save','vision.layout') BEGIN SELECT RAISE(ABORT,'controlled intent failure'); END"
            )
        with pytest.raises(AuditUnavailable):
            editor.save()
        assert editor.entry.path.read_bytes() == before and editor.dirty
        editor.camera_sources.set_loop(not editor.camera_sources.saved.loop)
        outcome = editor.save_vision(None, None)
        assert outcome.sources_saved and not outcome.machine_saved and outcome.errors
        (source,) = identity.audit_page(AuditQuery(action="vision.sources")).result(
            WAIT
        )
        assert source.outcome == "success" and "loop" in source.operation.after
        assert not identity.audit_page(AuditQuery(action="vision.layout")).result(WAIT)
    finally:
        close(startup.application)


def test_production_commit_actor_and_stop_during_close(tmp_path: Path) -> None:
    path = tmp_path / "security.sqlite3"
    initialize_administrator(path, "Admin", TEST_PASSWORD)
    identity = LocalIdentity(path)
    session = FakeSession(automatic_start=False)
    candidate = prepared(tmp_path)
    app = ProductionApplication(
        lambda _p, _g: session, identity=identity, initial_candidate=candidate
    )
    gate = session.join_gate = Event()
    try:
        actor = identity.login("Admin", TEST_PASSWORD).result(WAIT)
        assert actor.account
        request = app.begin_recipe_selection(candidate.configuration.recipe_id)
        app.install_recipe_selection(request, candidate)
        app.start_batch("Lot 42").result(WAIT)
        app.end_batch().result(WAIT)
        started = app.start()
        assert session.start_entered.wait(WAIT)
        identity.logout()
        session.ready.set_result(None)
        started.result(WAIT)
        run = app.snapshot().run_id
        stopped = app.stop()
        assert session.join_entered.wait(WAIT)
        closed = app.close()
        gate.set()
        stopped.result(WAIT)
        closed.result(WAIT)
        assert app.join(WAIT)
        with closing(sqlite3.connect(identity.audit.path)) as db:
            rows = {
                action: (target, username, outcome)
                for action, target, username, outcome in db.execute(
                    "SELECT action,target,username,outcome FROM operations JOIN completions USING(id)"
                )
            }
        assert rows["production.start"] == (run, "Admin", "success")
        assert rows["production.stop"] == (run, "Guest", "success")
        assert rows["production.recipe"] == (
            candidate.configuration.recipe_id,
            "Admin",
            "success",
        )
        assert rows["batch.select"][1:] == rows["batch.end"][1:] == ("Admin", "success")
    finally:
        gate.set()
        if not session.ready.done():
            session.ready.set_result(None)
        close(app)
