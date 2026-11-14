"""Normal save surfaces retain exact revisions and expose incomplete confirmation."""

import sqlite3
from contextlib import closing
from hashlib import sha256
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication
from tests.integration.test_communication_settings_ui import (
    settings_window as settings_window,
)
from tests.support.application import WAIT
from tests.support.qt import close_window, until
from tests.support.startup import authenticated_startup

from capsule_vision.application.audit import AuditQuery
from capsule_vision.application.production_application import (
    SourceProductionApplication,
)
from capsule_vision.application.startup import ApplicationData, Startup
from capsule_vision.configuration import load_camera_sources, load_recipe
from capsule_vision.configuration.communication_preferences import (
    load_communication_preferences,
)
from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import Language
from capsule_vision.ui.bootstrap import create_window
from capsule_vision.ui.future_delivery import FutureDelivery
from capsule_vision.ui.main_window import MainWindow


def refuse(path: Path, table: str, action: str) -> None:
    with closing(sqlite3.connect(path)) as db, db:
        where = (
            f"NEW.action='{action}'"
            if table == "operations"
            else f"NEW.id IN (SELECT id FROM operations WHERE action='{action}')"
        )
        db.execute(
            f"CREATE TRIGGER refuse BEFORE INSERT ON {table} WHEN {where} BEGIN SELECT RAISE(ABORT,'isolated audit failure'); END"
        )


def test_communication_revision_tracks_complete_file_and_unchanged_save(
    settings_window: tuple[Startup, MainWindow], monkeypatch: pytest.MonkeyPatch
) -> None:
    startup, window = settings_window
    app, page = startup.application, window.communication
    owner = app.identity
    assert owner and isinstance(app, SourceProductionApplication)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Configuration Save must not contact a PLC")

    monkeypatch.setattr("socket.socket.connect", forbidden)
    monkeypatch.setattr("socket.socket.sendto", forbidden)
    panel = page.reporting_panel
    assert panel
    before = app.snapshot()
    panel.port.setValue(19600)
    page.port.setValue(19601)
    page.web_panel.port.setValue(8766)
    page.running_address.setValue(216)
    page.persist_settings()
    until(lambda: not page.busy)
    saved = load_communication_preferences(startup.data.communication)
    assert saved.reporting.settings.endpoint.port == 19600
    assert saved.diagnostic.port == 19601 and saved.web.port == 8766
    assert not saved.status.enabled and saved.status.running_address == 216
    (row,) = owner.audit.revisions("communication", "communication.json").result(WAIT)
    content = startup.data.communication.read_bytes()
    assert row.confirmed and row.sha256 == sha256(content).hexdigest()
    assert owner.audit.revision_content(row.record.operation.id).result(WAIT) == content
    assert b'"running_address": 216' in content
    page.running_address.setValue(316)
    page.reload_settings()
    until(lambda: not page.busy)
    assert page.running_address.text() == "W216.1"
    page.running_address.setValue(46)
    page.persist_settings()
    until(lambda: not page.busy)
    assert "overlap" in page.preferences_status.toolTip()
    assert startup.data.communication.read_bytes() == content
    page.reload_settings()
    until(lambda: not page.busy)
    page.persist_settings()
    until(lambda: not page.busy)
    assert owner.audit.revisions("communication", "communication.json").result(
        WAIT
    ) == (row,)
    assert app.snapshot() == before
    assert app.reporting_selection() == saved.reporting


@pytest.mark.parametrize("failure", ["intent", "publication", "confirmation"])
def test_communication_failed_save_does_not_claim_confirmed_revision(
    settings_window: tuple[Startup, MainWindow],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    startup, window = settings_window
    page, owner = window.communication, startup.application.identity
    assert owner
    page.persist_settings()
    until(lambda: not page.busy)
    old = startup.data.communication.read_bytes()
    (initial,) = owner.audit.revisions("communication", "communication.json").result(
        WAIT
    )
    page.web_panel.port.setValue(8767)
    if failure in ("intent", "confirmation"):
        refuse(
            startup.data.audit,
            "operations" if failure == "intent" else "completions",
            "communication.save",
        )
    else:

        def deny(_source: Path, _target: Path) -> None:
            raise PermissionError("isolated publication refusal")

        monkeypatch.setattr("capsule_vision.configuration._io.os.replace", deny)
    page.persist_settings()
    until(lambda: not page.busy)
    rows = owner.audit.revisions("communication", "communication.json").result(WAIT)
    assert sum(row.confirmed for row in rows) == 1
    assert rows[-1] == initial
    assert len(rows) == (1 if failure == "intent" else 2)
    if failure == "confirmation":
        assert startup.data.communication.read_bytes() != old
        assert (
            owner.audit.revision_content(rows[0].record.operation.id).result(WAIT)
            == startup.data.communication.read_bytes()
        )
        assert rows[0].record.outcome == "unresolved"
        assert (
            text("configuration_confirmation_incomplete", window.language)
            in page.preferences_status.text()
        )
    else:
        assert startup.data.communication.read_bytes() == old
    assert not startup.application.snapshot().runtime_owned


@pytest.mark.parametrize("language", ["zh", "en"])
def test_recipe_published_unconfirmed_is_visible_and_blocks_old_candidate(
    settings_window: tuple[Startup, MainWindow], language: Language
) -> None:
    startup, window = settings_window
    owner, editor = startup.application.identity, startup.editor
    assert owner and editor and editor.entry
    window.set_language(language)
    window.navigate(1)
    until(lambda: not window.editing.busy and not window.recipe_page.labels_loading)
    try:
        before = startup.application.snapshot()
        refuse(startup.data.audit, "completions", "recipe.save")
        editor.draft.display_name = "Published without confirmation"
        window.recipe_page.save()
        until(lambda: not window.editing.busy)
        assert (
            load_recipe(editor.entry.path).display_name
            == "Published without confirmation"
        )
        assert editor.dirty
        assert (
            text("configuration_confirmation_incomplete", language)
            == window.recipe_page.status.text()
        )
        snapshot = startup.application.snapshot()
        assert snapshot.candidate_prepared is before.candidate_prepared
        assert snapshot.candidate_requires_preparation and snapshot.active is None
        assert not window.buttons["operation"].isEnabled()
        (row,) = owner.audit.revisions("recipe", editor.entry.recipe.recipe_id).result(
            WAIT
        )
        assert not row.confirmed and row.record.outcome == "unresolved"
        assert not owner.audit.page(AuditQuery(action="production.recipe")).result(WAIT)
    finally:
        editor.discard()


def test_vision_partial_confirmation_invalidates_published_dependency(
    settings_window: tuple[Startup, MainWindow],
) -> None:
    startup, window = settings_window
    owner, editor = startup.application.identity, startup.editor
    assert owner and editor and editor.camera_sources
    window.navigate(4)
    page = window.vision_workspace
    assert page
    until(lambda: not window.editing.busy)
    previous = editor.camera_sources.saved
    editor.camera_sources.set_loop(not previous.loop)
    refuse(startup.data.audit, "completions", "vision.sources")
    page.save_setup()
    until(lambda: not window.editing.busy)
    assert load_camera_sources(startup.data.sources).loop != previous.loop
    assert editor.camera_sources.saved == previous and editor.camera_sources.dirty
    assert page.feedback and page.feedback[0] == "configuration_confirmation_incomplete"
    assert startup.application.snapshot().candidate_requires_preparation
    assert not window.buttons["operation"].isEnabled()
    (row,) = owner.audit.revisions("sources", "camera_sources.json").result(WAIT)
    assert not row.confirmed and row.record.outcome == "unresolved"
    assert not owner.audit.revisions("machine", "machine.tsv").result(WAIT)
    editor.camera_sources.load_saved(load_camera_sources(startup.data.sources))
    QApplication.processEvents()


def test_unconfirmed_sources_with_uninitialized_machine_remain_observable(
    qapp: QApplication, tmp_path: Path
) -> None:
    startup = authenticated_startup(ApplicationData.at_entry(tmp_path))
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    try:
        window.navigate(4)
        until(lambda: not window.editing.busy)
        page, owner = window.vision_workspace, startup.application.identity
        assert page and owner and page.sources and not page.draft.complete
        refuse(startup.data.audit, "completions", "vision.sources")
        page.save_setup()
        until(lambda: not window.editing.busy)
        assert startup.data.sources.exists() and not startup.data.machine.exists()
        assert (
            page.feedback
            and page.feedback[0] == "configuration_confirmation_incomplete"
        )
        assert not page.sources.persisted and page.sources.dirty
        (row,) = owner.audit.revisions("sources", "camera_sources.json").result(WAIT)
        assert not row.confirmed and row.record.outcome == "unresolved"
        assert not owner.audit.revisions("machine", "machine.tsv").result(WAIT)
    finally:
        # Unconfirmed first-run entries remain genuine unsaved source edits.
        assert startup.editor and startup.editor.camera_sources
        startup.editor.camera_sources.discard()
        close_window(window)
        delivery.detach()
