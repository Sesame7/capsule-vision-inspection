"""Exact configuration publication evidence on the existing isolated Audit owner."""

import sqlite3
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from threading import Event

import pytest
from tests.support.application import WAIT
from tests.support.startup import (
    TEST_PASSWORD,
    authenticated_startup,
    close,
    provision_account,
    saved_data,
)

from capsule_vision.application.audit import (
    AuditOperation,
    AuditQuery,
    AuditTrail,
    AuditUnavailable,
    RevisionConfirmationError,
    RevisionEvidence,
)
from capsule_vision.application.identity_models import (
    AuthorizationError,
    Role,
    StaleSessionError,
)
from capsule_vision.application.production_application import (
    SourceProductionApplication,
)
from capsule_vision.application.startup import Startup
from capsule_vision.configuration import (
    ConfigurationError,
    ConfigurationIOError,
    ConfigurationPublication,
    load_recipe,
    save_recipe,
)
from capsule_vision.configuration.communication_preferences import (
    CommunicationPreferences,
    communication_publication,
)
from capsule_vision.inference import BoundingRect


@pytest.fixture
def startup(tmp_path: Path) -> Generator[Startup]:
    value = authenticated_startup(saved_data(tmp_path))
    try:
        yield value
    finally:
        close(value)


def trigger(path: Path, sql: str) -> None:
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(sql)


def test_recipe_initial_changed_unchanged_delete_and_restart(startup: Startup) -> None:
    editor, owner = startup.editor, startup.application.identity
    assert editor and owner and editor.entry
    assert editor.save() is None
    assert not owner.audit.revisions("recipe", editor.entry.recipe.recipe_id).result(
        WAIT
    )
    editor.new()
    editor.draft.recipe_id = editor.catalog.new_identity()
    editor.draft.display_name = "胶囊 A"
    first = editor.save()
    assert first is not None
    original = first.path.read_bytes()
    editor.draft.display_name = "Renamed capsule"
    second = editor.save()
    assert second and first.recipe.recipe_id == second.recipe.recipe_id
    updated = second.path.read_bytes()
    assert editor.save() is None
    rows = owner.audit.revisions("recipe", first.recipe.recipe_id).result(WAIT)
    assert len(rows) == 2 and all(row.confirmed for row in rows)
    actor = owner.snapshot().account
    assert actor is not None
    for row, content in zip(rows, (updated, original), strict=True):
        assert row.record.operation.actor_id == actor.account_id
        assert row.record.operation.username == "TestAdmin"
        assert row.record.operation.action == "recipe.save"
        assert row.resource == first.recipe.recipe_id
        assert row.sha256 == sha256(content).hexdigest()
        assert (
            owner.audit.revision_content(row.record.operation.id).result(WAIT)
            == content
        )
        assert row.record.operation.utc <= row.record.completed_utc
    editor.delete(protected_ids=frozenset())
    assert not first.path.exists()
    assert owner.audit.revisions("recipe", first.recipe.recipe_id).result(WAIT) == rows
    close(startup)
    audit = AuditTrail(startup.data.audit, lambda _: 1)
    try:
        assert audit.revisions("recipe", first.recipe.recipe_id).result(WAIT) == rows
        assert (
            audit.revision_content(rows[1].record.operation.id).result(WAIT) == original
        )
    finally:
        audit.close()


@pytest.mark.parametrize("stage", ["intent", "revision", "publication", "confirmation"])
def test_publication_failure_boundaries(
    startup: Startup, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    editor, owner = startup.editor, startup.application.identity
    assert editor and owner and editor.entry
    old = editor.entry.path.read_bytes()
    editor.draft.display_name = "Exact submitted target"
    if stage in ("intent", "revision", "confirmation"):
        table = {
            "intent": "operations",
            "revision": "configuration_revisions",
            "confirmation": "completions",
        }[stage]
        trigger(
            startup.data.audit,
            f"CREATE TRIGGER refuse BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT,'isolated rejection'); END",
        )
    else:

        def fail(source: Path, target: Path) -> None:
            raise PermissionError("isolated publication refusal")

        monkeypatch.setattr("capsule_vision.configuration._io.os.replace", fail)
    expected = (
        RevisionConfirmationError
        if stage == "confirmation"
        else ConfigurationIOError
        if stage == "publication"
        else AuditUnavailable
    )
    with pytest.raises(expected):
        editor.save()
    rows = owner.audit.revisions("recipe", editor.entry.recipe.recipe_id).result(WAIT)
    events = owner.audit.page(AuditQuery(action="recipe.save")).result(WAIT)
    if stage in ("intent", "revision"):
        assert not rows and not events  # Intent and bytes roll back together.
    else:
        (row,) = rows
        assert not row.confirmed and row.record == events[0]
        assert row.record.outcome == (
            "unresolved" if stage == "confirmation" else "failed"
        )
        content = owner.audit.revision_content(row.record.operation.id).result(WAIT)
        assert b"Exact submitted target" in content
        if stage == "confirmation":
            assert editor.entry.path.read_bytes() == content
            assert (
                load_recipe(editor.entry.path).display_name == "Exact submitted target"
            )
            with pytest.raises(ConfigurationError, match="externally"):
                editor.save()  # No silent retry/overwrite of the unconfirmed publication.
        else:
            assert editor.entry.path.read_bytes() == old
    assert editor.dirty
    assert startup.application.snapshot().active is None


def test_external_edits_are_not_attributed_to_current_actor(startup: Startup) -> None:
    editor, owner = startup.editor, startup.application.identity
    assert editor and editor.entry and owner
    save_recipe(
        editor.entry.path, replace(editor.entry.recipe, display_name="External edit")
    )
    editor.draft.display_name = "Local input"
    with pytest.raises(ConfigurationError, match="externally"):
        editor.save()
    assert not owner.audit.revisions("recipe", editor.entry.recipe.recipe_id).result(
        WAIT
    )
    (event,) = owner.audit.page(AuditQuery(action="recipe.save")).result(WAIT)
    assert event.outcome == "failed"
    editor.discard()
    assert editor.save() is None  # Reloading external data does not invent a revision.
    assert not owner.audit.revisions("recipe", editor.entry.recipe.recipe_id).result(
        WAIT
    )


def test_first_vision_publications_capture_both_complete_files(tmp_path: Path) -> None:
    data = saved_data(tmp_path)
    data.machine.unlink()  # Isolated new-installation fixture.
    data.sources.unlink()
    startup = authenticated_startup(data)
    try:
        editor, owner = startup.editor, startup.application.identity
        assert editor and editor.camera_sources and owner
        machine, sources = editor.machine_editor, editor.camera_sources
        assert machine.saved_definition is None and not sources.persisted
        sources.set_mode("folder")
        for camera in machine.camera_ids:
            machine.initialize(camera, 120, 90)
            sources.set_folder(camera, "../frames")
        result = editor.save_vision(None, None)
        assert result.machine_saved and result.sources_saved and not result.errors
        for artifact, path in (("machine", data.machine), ("sources", data.sources)):
            (row,) = owner.audit.revisions(artifact, path.name).result(WAIT)
            assert row.confirmed
            assert (
                owner.audit.revision_content(row.record.operation.id).result(WAIT)
                == path.read_bytes()
            )
    finally:
        close(startup)


@pytest.mark.parametrize(
    "failed", ["none", "sources", "machine", "source_confirmation"]
)
def test_independent_vision_revisions(
    startup: Startup, monkeypatch: pytest.MonkeyPatch, failed: str
) -> None:
    editor, owner = startup.editor, startup.application.identity
    assert editor and owner and editor.camera_sources
    sources, machine = editor.camera_sources, editor.machine_editor
    sources.set_loop(not sources.saved.loop)
    camera = machine.camera_ids[0]
    rectangle = machine.camera(camera).slots[0].rect
    machine.replace_rects(
        camera,
        (BoundingRect(rectangle.x, rectangle.y, rectangle.width - 1, rectangle.height),)
        + tuple(slot.rect for slot in machine.camera(camera).slots[1:]),
    )
    publish = ConfigurationPublication.publish

    def write(value: ConfigurationPublication) -> None:
        if value.path == (
            startup.data.sources
            if failed == "sources"
            else startup.data.machine
            if failed == "machine"
            else None
        ):
            raise ConfigurationIOError("isolated file failure")
        publish(value)

    monkeypatch.setattr(ConfigurationPublication, "publish", write)
    if failed == "source_confirmation":
        trigger(
            startup.data.audit,
            "CREATE TRIGGER refuse BEFORE INSERT ON completions WHEN NEW.id IN (SELECT id FROM operations WHERE action='vision.sources') BEGIN SELECT RAISE(ABORT,'source confirmation'); END",
        )
    result = editor.save_vision(None, None)
    assert result.sources_saved == (failed not in ("sources", "source_confirmation"))
    assert result.machine_saved == (failed != "machine")
    for kind, path, confirmed in (
        ("sources", startup.data.sources, result.sources_saved),
        ("machine", startup.data.machine, result.machine_saved),
    ):
        (row,) = owner.audit.revisions(kind, path.name).result(WAIT)
        assert row.confirmed == confirmed
        if confirmed or failed == "source_confirmation" and kind == "sources":
            assert (
                owner.audit.revision_content(row.record.operation.id).result(WAIT)
                == path.read_bytes()
            )
    if failed == "none":
        editor.save_vision(None, None)
        assert len(owner.audit.revisions("machine", "machine.tsv").result(WAIT)) == 1
        assert (
            len(owner.audit.revisions("sources", "camera_sources.json").result(WAIT))
            == 1
        )


@pytest.mark.parametrize("role", list(Role))
def test_revision_review_and_publication_authorization(
    startup: Startup, role: Role
) -> None:
    editor, owner = startup.editor, startup.application.identity
    assert editor and editor.entry and owner
    editor.draft.display_name = "Retained"
    editor.save()
    (row,) = owner.audit.revisions("recipe", editor.entry.recipe.recipe_id).result(WAIT)
    if role is Role.GUEST:
        owner.logout()
    elif role is not Role.ADMINISTRATOR:
        account = provision_account(owner, role.name, role, TEST_PASSWORD)
        owner.login(account.username, TEST_PASSWORD).result(WAIT)
    editor.draft.display_name = "Permission check"
    if role < Role.ENGINEER:
        with pytest.raises(AuthorizationError):
            owner.audit.revisions("recipe", row.resource)
        with pytest.raises(AuthorizationError):
            owner.audit.revision_content(row.record.operation.id)
        with pytest.raises(AuthorizationError):
            editor.save()
    else:
        assert owner.audit.revision_content(row.record.operation.id).result(WAIT)
        assert editor.save()


@pytest.mark.parametrize(
    "statement", ["INSERT INTO operations", "INSERT INTO completions"]
)
def test_database_waits_do_not_hold_application_or_identity_locks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, statement: str
) -> None:
    entered, release, armed = Event(), Event(), Event()

    def connect(path: Path, *, timeout: float) -> sqlite3.Connection:
        db = sqlite3.connect(path, timeout=timeout)

        def trace(sql: str) -> None:
            if armed.is_set() and sql.startswith(statement):
                armed.clear()
                entered.set()
                assert release.wait(WAIT)

        db.set_trace_callback(trace)
        return db

    monkeypatch.setattr("capsule_vision.application.audit.connect", connect)
    startup = authenticated_startup(saved_data(tmp_path))
    app, owner = startup.application, startup.application.identity
    assert owner and isinstance(app, SourceProductionApplication)
    value = CommunicationPreferences()
    publication = communication_publication(startup.data.communication, value)
    evidence = RevisionEvidence(
        "communication", "communication.json", publication.content
    )
    revision = owner.snapshot().revision
    owner.audit.page(AuditQuery()).result(WAIT)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            armed.set()
            task = pool.submit(
                app.persist_reporting_selection,
                value.reporting,
                publication.publish,
                revision=revision,
                evidence=evidence,
            )
            try:
                assert entered.wait(WAIT)

                def observe_and_logout() -> None:
                    app.snapshot()
                    owner.logout()

                pool.submit(observe_and_logout).result(1)
            finally:
                release.set()
            if statement.endswith("operations"):
                with pytest.raises(StaleSessionError):
                    task.result(WAIT)
                assert not startup.data.communication.exists()
            else:
                task.result(WAIT)
                assert startup.data.communication.read_bytes() == publication.content
    finally:
        release.set()
        close(startup)


def test_revision_hash_immutability_and_old_schema_no_migration(tmp_path: Path) -> None:
    path = tmp_path / "audit.sqlite3"
    audit = AuditTrail(path, lambda _: 1)
    op = AuditOperation("actor", "Recorded name", "recipe.save", "r")
    evidence = RevisionEvidence("recipe", "r", b'{"display_name":"Exact"}\n')
    try:
        audit.intent(op, evidence).result(WAIT)
        audit.complete(op, "success").result(WAIT)
        audit.intent(op, evidence).result(WAIT)
        with pytest.raises(AuditUnavailable, match="Conflicting"):
            audit.intent(op, replace(evidence, content=b"other")).result(WAIT)
        with closing(sqlite3.connect(path)) as db, db:
            for sql in (
                "UPDATE configuration_revisions SET content=x'00'",
                "DELETE FROM configuration_revisions",
            ):
                with pytest.raises(sqlite3.IntegrityError, match="Immutable"):
                    db.execute(sql)
        assert audit.revision_content(op.id).result(WAIT) == evidence.content
    finally:
        audit.close()
    # OS-level direct database tampering is detected, not repaired.
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("DROP TRIGGER revision_immutable_update")
        db.execute("UPDATE configuration_revisions SET content=x'00'")
    audit = AuditTrail(path, lambda _: 1)
    try:
        with pytest.raises(AuditUnavailable, match="hash mismatch"):
            audit.revision_content(op.id).result(WAIT)
    finally:
        audit.close()
    legacy = tmp_path / "legacy.sqlite3"
    with closing(sqlite3.connect(legacy)) as db, db:
        db.execute("CREATE TABLE retained(value TEXT)")
        db.execute("INSERT INTO retained VALUES('customer evidence')")
        db.execute("PRAGMA user_version=1")
    before = legacy.read_bytes()
    audit = AuditTrail(legacy, lambda _: 1)
    try:
        with pytest.raises(AuditUnavailable, match="Unsupported"):
            audit.intent(op, evidence).result(WAIT)
    finally:
        audit.close()
    assert legacy.read_bytes() == before


def test_revision_paging_pending_restart_and_report_isolation(tmp_path: Path) -> None:
    path = tmp_path / "audit.sqlite3"
    audit = AuditTrail(path, lambda _: 1)
    pending = AuditOperation("actor", "Recorded name", "recipe.save", "r")
    pending_content = b'{"private_configuration":"pending snapshot"}\n'
    try:
        operations = [
            AuditOperation("actor", "Recorded name", "recipe.save", "r")
            for _ in range(53)
        ]
        for index, operation in enumerate(operations):
            evidence = RevisionEvidence("recipe", "r", f"revision {index}".encode())
            audit.intent(operation, evidence).result(WAIT)
            audit.complete(operation, "success").result(WAIT)
        audit.intent(pending, RevisionEvidence("recipe", "r", pending_content)).result(
            WAIT
        )
    finally:
        audit.close()
    audit = AuditTrail(path, lambda _: 1)
    try:
        first = audit.revisions("recipe", "r").result(WAIT)
        second = audit.revisions("recipe", "r", before=first[-1].record.cursor).result(
            WAIT
        )
        assert len(first) == 51 and len(second) == 3
        assert [row.record.operation.id for row in (*first, *second)] == [
            operation.id for operation in reversed([*operations, pending])
        ]
        assert not first[0].confirmed and first[0].record.outcome == "unresolved"
        assert audit.revision_content(pending.id).result(WAIT) == pending_content
        report = tmp_path / "report.sqlite3"
        audit.capture_report(AuditQuery(), report, Event(), 1).result(WAIT)
        with closing(sqlite3.connect(report)) as db:
            assert db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall() == [("audit_records",)]
            assert db.execute("SELECT count(*) FROM audit_records").fetchone() == (54,)
            columns = [row[1] for row in db.execute("PRAGMA table_info(audit_records)")]
            assert "content" not in columns and "sha256" not in columns
        assert pending_content not in report.read_bytes()
    finally:
        audit.close()
