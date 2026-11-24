"""Normal save surfaces retain exact revisions and expose incomplete confirmation."""

import sqlite3
from concurrent.futures import Future
from contextlib import closing
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Never

import pytest
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication, QMessageBox
from tests.integration.test_communication_settings_ui import (
    settings_window as settings_window,
)
from tests.support.application import WAIT
from tests.support.qt import close_window, until
from tests.support.startup import TEST_PASSWORD, authenticated_startup

from capsule_vision.application import ApplicationStateError, PreparedConfiguration
from capsule_vision.application.audit import AuditQuery
from capsule_vision.application.editing import RecipeEditorSession
from capsule_vision.application.production_application import (
    SourceProductionApplication,
)
from capsule_vision.application.startup import ApplicationData, Startup
from capsule_vision.communication import FinsMemoryArea
from capsule_vision.communication.operations import (
    CommunicationRequest,
    CommunicationResponse,
    RegisterWatchGroup,
    RequestKind,
)
from capsule_vision.configuration import (
    ConfigurationIOError,
    ConfigurationPublication,
    load_camera_sources,
    load_recipe,
)
from capsule_vision.configuration.communication_preferences import (
    WebPreferences,
    load_communication_preferences,
)
from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import Language
from capsule_vision.ui.bootstrap import create_window
from capsule_vision.ui.future_delivery import FutureDelivery
from capsule_vision.ui.main_window import MainWindow
from capsule_vision.web.runtime import WebRuntime


@pytest.fixture
def diagnostic_requests(
    settings_window: tuple[Startup, MainWindow], monkeypatch: pytest.MonkeyPatch
) -> list[CommunicationRequest]:
    requests: list[CommunicationRequest] = []

    def execute(request: CommunicationRequest) -> CommunicationResponse:
        requests.append(request)
        values = (72,) * request.count if request.kind is RequestKind.READ else None
        return CommunicationResponse(request, values, 1, datetime.now(UTC))

    monkeypatch.setattr(settings_window[1].communication, "_execute", execute)
    return requests


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
    diagnostic_requests: list[CommunicationRequest],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    startup, window = settings_window
    page, owner = window.communication, startup.application.identity
    assert owner and isinstance(startup.application, SourceProductionApplication)
    window.set_language("en")
    web = WebRuntime(lambda _: pytest.fail("Communication Save must not start Web"))
    window.bind_web_runtime(web)
    web_before = web.snapshot()
    page.persist_settings()
    until(lambda: not page.busy)
    old = startup.data.communication.read_bytes()
    assert page.add_watch_group(RegisterWatchGroup(FinsMemoryArea.DM, 109))
    assert page.read_watch_once()
    until(lambda: not page.busy)
    stamp = page.watch_table.item(0, 4)
    assert stamp and stamp.text()
    last_read = stamp.text()
    validated = QSignalSpy(page.preferences_validated)
    (initial,) = owner.audit.revisions("communication", "communication.json").result(
        WAIT
    )
    page.web_panel.port.setValue(8767)
    page.port.setValue(9601)
    panel = page.reporting_panel
    assert panel
    panel.enabled.setChecked(True)
    panel.port.setValue(19610)
    page.running_address.setValue(216)
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
    expected_port = 9601 if failure == "confirmation" else 9600
    saved = load_communication_preferences(startup.data.communication)
    assert saved.diagnostic.port == expected_port
    assert startup.application.reporting_selection() == saved.reporting
    assert saved.reporting.enabled == (failure == "confirmation")
    assert startup.application.reporting_snapshot() is None
    assert startup.application.alarm_snapshot().selection == saved.status
    assert startup.application.alarm_snapshot().state == "off"
    assert web.snapshot() == web_before
    assert page.dispatch(RequestKind.READ)
    until(lambda: not page.busy)
    assert diagnostic_requests[-1].endpoint.port == expected_port
    assert page.port.value() == 9601 and validated.count() == 0
    assert text("saved", "en") != page.preferences_status.text()
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
        assert page.discard_settings.isEnabled() and not page.save_settings.isEnabled()
        assert (
            page.manual_endpoint.isVisible()
            and "127.0.0.1:9601" in page.manual_endpoint.text()
        )
        assert page.watch_status.accessibleName() == text("comm_watch_stale", "en")
        retained = page.watch_table.item(0, 4)
        assert retained and retained.text() == last_read
        page.port.setValue(9602)
    else:
        assert startup.data.communication.read_bytes() == old
        assert page.dirty and page.preferences_status.toolTip()
    assert f"127.0.0.1:{expected_port}" in page.manual_endpoint.text()
    assert page.read_watch_once()
    until(lambda: not page.busy)
    page.write_values.setText("17")
    assert page.dispatch(RequestKind.WRITE)
    until(lambda: not page.busy)
    assert [(r.kind, r.endpoint.port, r.start) for r in diagnostic_requests] == [
        (RequestKind.READ, 9600, 109),
        (RequestKind.READ, expected_port, 100),
        (RequestKind.READ, expected_port, 109),
        (RequestKind.WRITE, expected_port, 100),
    ]
    (write,) = owner.audit.page(AuditQuery(action="plc.write")).result(WAIT)
    assert write.outcome == "success" and write.operation.after == "[17]"
    assert f"127.0.0.1:{expected_port}" in write.operation.target
    assert not startup.application.snapshot().runtime_owned


def test_unconfirmed_web_power_off_invalidates_retry_until_reload(
    settings_window: tuple[Startup, MainWindow],
) -> None:
    startup, window = settings_window
    page, panel = window.communication, window.communication.web_panel
    owner = startup.application.identity
    assert owner is not None

    def unavailable(_settings: WebPreferences) -> Never:
        raise OSError("isolated Web startup failure")

    runtime = WebRuntime(unavailable)
    window.bind_web_runtime(runtime)
    panel.power.click()
    until(lambda: panel.apply_changes.isVisible() and panel.apply_changes.isEnabled())
    failed = runtime.snapshot()
    assert failed.error and not page.dirty
    refuse(startup.data.audit, "completions", "communication.web_power")
    panel.power.click()
    until(lambda: not page.busy)
    assert not load_communication_preferences(startup.data.communication).web.enabled
    message = text("configuration_confirmation_incomplete", page.language)
    assert message in page.preferences_status.text()
    assert not panel.saved_available and panel.apply_changes.isHidden()
    panel.requested.emit("retry")
    assert runtime.snapshot() == failed and message in page.preferences_status.text()
    assert page.discard_settings.isEnabled()
    page.reload_settings()
    assert (
        panel.saved_available
        and not panel.saved.enabled
        and panel.apply_changes.isHidden()
    )
    assert runtime.snapshot() == failed
    latest, _ = owner.audit.revisions("communication", "communication.json").result(
        WAIT
    )
    assert not latest.confirmed


@pytest.mark.parametrize(
    "interference",
    [
        "same_settings",
        "invalid",
        "missing",
        "no_receipt",
        "session_change",
        "confirmed_replaced",
    ],
)
def test_unverified_diagnostics_require_explicit_reload(
    settings_window: tuple[Startup, MainWindow],
    diagnostic_requests: list[CommunicationRequest],
    monkeypatch: pytest.MonkeyPatch,
    interference: str,
) -> None:
    startup, window = settings_window
    page, owner = window.communication, startup.application.identity
    assert owner
    path = startup.data.communication
    page.persist_settings()
    until(lambda: not page.busy)
    page.port.setValue(9601)
    assert page.add_watch_group(RegisterWatchGroup(FinsMemoryArea.DM, 109))
    if interference != "confirmed_replaced":
        refuse(startup.data.audit, "completions", "communication.save")
    publish = ConfigurationPublication.publish

    def interfere(publication: ConfigurationPublication) -> None:
        publish(publication)
        if interference in ("same_settings", "confirmed_replaced"):
            path.write_bytes(publication.content + b"\n")
        elif interference == "invalid":
            path.write_bytes(b'{"unknown":true}')
        elif interference == "missing":
            path.unlink()
        elif interference == "no_receipt":
            raise ConfigurationIOError("No publication receipt")

    review = owner.audit.revision_content

    def logout_before_review(
        operation_id: str, revision: int | None = None
    ) -> Future[bytes]:
        owner.logout()
        return review(operation_id, revision)

    with monkeypatch.context() as fault:
        fault.setattr(ConfigurationPublication, "publish", interfere)
        if interference == "session_change":
            fault.setattr(owner.audit, "revision_content", logout_before_review)
        page.persist_settings()
        until(lambda: not page.busy)
    if interference == "session_change":
        assert owner.snapshot().account is None
        owner.login("TestAdmin", TEST_PASSWORD).result(WAIT)
        window.identity_controls.refresh()
    revision = owner.audit.revisions("communication", "communication.json").result(
        WAIT
    )[0]
    content = owner.audit.revision_content(revision.record.operation.id).result(WAIT)
    assert revision.record.outcome == (
        "success" if interference == "confirmed_replaced" else "unresolved"
    )
    assert page.port.value() == 9601
    assert not page.dispatch(RequestKind.READ)
    assert not page.dispatch(RequestKind.WRITE)
    assert not page.read_watch_once() and not page.start_monitoring()
    assert not any(
        b.isEnabled() for b in (page.watch_once, page.watch_auto, page.write_button)
    )
    assert text("unavailable", window.language) in page.manual_endpoint.text()
    assert not diagnostic_requests and not owner.audit.page(
        AuditQuery(action="plc.write")
    ).result(WAIT)

    path.write_bytes(b'{"diagnostic":null}')
    page.reload_settings()
    assert not page.dispatch(RequestKind.READ)
    path.write_bytes(content)
    # An unchanged Save cannot clear the interlock or confirm the earlier revision.
    page.persist_settings()
    until(lambda: not page.busy)
    assert not page.dirty and not page.dispatch(RequestKind.WRITE)
    assert page.discard_settings.isEnabled()
    page.discard_settings.click()
    dialog = page.findChild(QMessageBox)
    assert dialog is not None
    next(
        b
        for b in dialog.buttons()
        if dialog.buttonRole(b) == QMessageBox.ButtonRole.AcceptRole
    ).click()
    until(page.watch_once.isEnabled)
    assert page.dispatch(RequestKind.READ)
    until(lambda: not page.busy)
    assert [r.endpoint.port for r in diagnostic_requests] == [9601]
    assert (
        owner.audit.revisions("communication", "communication.json").result(WAIT)[0]
        == revision
    )


@pytest.mark.parametrize("language", ["zh", "en"])
def test_recipe_published_unconfirmed_is_visible_and_blocks_old_candidate(
    settings_window: tuple[Startup, MainWindow],
    language: Language,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    startup, window = settings_window
    owner, editor = startup.application.identity, startup.editor
    assert owner and editor and editor.entry
    window.set_language(language)
    window.navigate(1)
    until(lambda: not window.editing.busy and not window.recipe_page.labels_loading)

    def forbidden(
        _session: RecipeEditorSession, _identity: str
    ) -> PreparedConfiguration:
        pytest.fail("Unconfirmed publication must not prepare")

    try:
        before = startup.application.snapshot()
        refuse(startup.data.audit, "completions", "recipe.save")
        editor.draft.display_name = "Published without confirmation"
        with monkeypatch.context() as save_only:
            save_only.setattr(RecipeEditorSession, "prepare_saved", forbidden)
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
        assert snapshot.selected_recipe == before.selected_recipe
        assert not window.buttons["operation"].isEnabled()
        with pytest.raises(ApplicationStateError, match="re-preparation"):
            startup.application.start()
        (row,) = owner.audit.revisions("recipe", editor.entry.recipe.recipe_id).result(
            WAIT
        )
        assert not row.confirmed and row.record.outcome == "unresolved"
        assert not owner.audit.page(AuditQuery(action="production.recipe")).result(WAIT)

        window.navigate(0)
        picker = window.production_recipes
        assert picker.recipes.currentIndex() == -1
        index = picker.recipes.findData(editor.entry.recipe.recipe_id)
        assert (
            index >= 0
            and picker.recipes.itemText(index) == "Published without confirmation"
        )
        picker.recipes.activated.emit(index)
        until(lambda: not window.editing.busy)
        prepared = startup.application.snapshot()
        assert not prepared.candidate_requires_preparation
        assert prepared.candidate_prepared is not snapshot.candidate_prepared
        assert picker.recipes.currentText() == "Published without confirmation"
        assert (
            text("configuration_confirmation_incomplete", language)
            == window.recipe_page.status.text()
        )
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
