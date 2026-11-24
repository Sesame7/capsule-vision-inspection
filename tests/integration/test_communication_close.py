"""Connections draft decisions through the normal window Close path."""

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox
from tests.integration.test_communication_settings_ui import (
    settings_window as settings_window,
)
from tests.integration.test_configuration_revisions_ui import refuse
from tests.integration.test_permission_presentation import login
from tests.support.application import WAIT, FakeSession
from tests.support.qt import screen as screen
from tests.support.qt import until
from tests.support.startup import TEST_PASSWORD, provision_account

from capsule_vision.application import ApplicationState
from capsule_vision.application.identity_models import AuthorizationError, Role
from capsule_vision.application.startup import Startup
from capsule_vision.communication.operations import (
    CommunicationRequest,
    CommunicationResponse,
    RequestKind,
)
from capsule_vision.configuration.communication_preferences import (
    CommunicationPreferences,
    load_communication_preferences,
)
from capsule_vision.localization.catalog import text
from capsule_vision.ui.main_window import MainWindow

Button = QMessageBox.StandardButton


def visible_prompt(window: MainWindow) -> QMessageBox:
    (box,) = [
        box
        for box in window.communication.findChildren(
            QMessageBox, "connectionsCloseDraft"
        )
        if box.isVisible()
    ]
    return box


def prompt(window: MainWindow) -> QMessageBox:
    window.close()
    return visible_prompt(window)


def choose(box: QMessageBox, choice: Button) -> None:
    button = box.button(choice)
    assert button is not None and button.isEnabled()
    button.click()


def save_outcomes(startup: Startup) -> list[str | None]:
    with closing(sqlite3.connect(startup.data.audit)) as db:
        return [
            row[0]
            for row in db.execute(
                "SELECT c.outcome FROM operations o LEFT JOIN completions c USING(id) "
                "WHERE o.action = 'communication.save' ORDER BY o.rowid"
            )
        ]


def test_clean_first_use_closes_without_a_decision_or_save(
    settings_window: tuple[Startup, MainWindow],
) -> None:
    startup, window = settings_window
    assert not window.communication.dirty and not startup.data.communication.exists()
    window.close()
    until(lambda: not window.isVisible())
    assert window.bridge.closed and not startup.data.communication.exists()
    assert not window.communication.findChildren(QMessageBox, "connectionsCloseDraft")


@pytest.mark.parametrize(
    "role,choice",
    [
        (Role.ENGINEER, Button.Save),
        (Role.ENGINEER, Button.Discard),
        (Role.OPERATOR, Button.Discard),
        (Role.GUEST, Button.Discard),
    ],
)
def test_cancel_then_close_with_inherited_draft(
    settings_window: tuple[Startup, MainWindow], role: Role, choice: Button
) -> None:
    startup, window = settings_window
    page, owner = window.communication, startup.application.identity
    assert owner is not None
    provision_account(owner, "ENGINEER", Role.ENGINEER, TEST_PASSWORD)
    if role is Role.OPERATOR:
        provision_account(owner, "OPERATOR", Role.OPERATOR, TEST_PASSWORD)
    login(window, Role.ENGINEER)
    page.persist_settings()
    until(lambda: not page.busy)
    original = startup.data.communication.read_bytes()
    page.web_panel.port.setValue(8766)
    if role is not Role.ENGINEER:
        login(window, role)
    window.navigate(0)
    box = prompt(window)
    assert prompt(window) is box
    assert box.windowTitle() == text("nav_connections", window.language)
    assert (box.button(Button.Save) is not None) == (role is Role.ENGINEER)
    assert box.defaultButton() is box.button(Button.Cancel)
    choose(box, Button.Cancel)
    assert page.dirty and page.web_panel.port.value() == 8766
    assert (
        window.isVisible() and not window.bridge.closing and not window.editing.closing
    )
    assert startup.data.communication.read_bytes() == original
    choose(prompt(window), choice)
    until(lambda: not window.isVisible())
    assert window.bridge.closed and page.closed and not page.dirty
    if choice == Button.Save:
        assert (
            load_communication_preferences(startup.data.communication).web.port == 8766
        )
        assert save_outcomes(startup) == ["success", "success"]
    else:
        assert startup.data.communication.read_bytes() == original
        assert page.web_panel.port.value() == 8765
        assert save_outcomes(startup) == ["success"]


@pytest.mark.parametrize(
    "failure", ["invalid", "authorization", "publication", "intent", "confirmation"]
)
def test_close_save_failure_keeps_window_and_feedback(
    settings_window: tuple[Startup, MainWindow],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    startup, window = settings_window
    page = window.communication
    page.persist_settings()
    until(lambda: not page.busy)
    old = startup.data.communication.read_bytes()
    page.web_panel.port.setValue(8766)
    if failure == "invalid":
        page.web_panel.host.setText("0.0.0.0")
    elif failure == "authorization":

        def deny_save(_revision: int | None) -> int:
            raise AuthorizationError("isolated admission refusal")

        monkeypatch.setattr(page, "_authorize", deny_save)
    elif failure == "publication":

        def deny(_source: Path, _target: Path) -> None:
            raise PermissionError("isolated replace failure")

        monkeypatch.setattr("capsule_vision.configuration._io.os.replace", deny)
    else:
        refuse(
            startup.data.audit,
            "operations" if failure == "intent" else "completions",
            "communication.save",
        )
    window.navigate(0)
    choose(prompt(window), Button.Save)
    until(lambda: not page.busy)
    assert window.isVisible() and window.pages.currentIndex() == 0
    assert not window.bridge.closing and not window.editing.closing
    assert page.web_panel.port.value() == 8766 and page.preferences_status.toolTip()
    assert page.preferences_status.text() == window.event_label.accessibleName()
    window.navigate(2)
    if failure == "confirmation":
        assert (
            text("configuration_confirmation_incomplete", window.language)
            in page.preferences_status.text()
        )
        assert (
            load_communication_preferences(startup.data.communication).web.port == 8766
        )
        assert save_outcomes(startup) == ["success", None]
        # Discarding a later draft must keep the verified publication and unresolved Audit.
        page.web_panel.port.setValue(8767)
        choose(prompt(window), Button.Discard)
        until(lambda: not window.isVisible())
        assert (
            page.web_panel.port.value() == 8766 and save_outcomes(startup)[-1] is None
        )
    else:
        assert page.dirty and startup.data.communication.read_bytes() == old


@pytest.fixture
def held_worker(monkeypatch: pytest.MonkeyPatch) -> tuple[Event, Event]:
    """Hold the worker after its Future notification and before actual thread exit."""
    returned, release = Event(), Event()

    class HeldThread(Thread):
        def run(self) -> None:
            super().run()
            returned.set()
            assert release.wait(WAIT)

    monkeypatch.setattr("capsule_vision.ui.communication_page.Thread", HeldThread)
    return returned, release


def test_held_diagnostic_read_delays_save_until_actual_exit(
    settings_window: tuple[Startup, MainWindow],
    monkeypatch: pytest.MonkeyPatch,
    held_worker: tuple[Event, Event],
) -> None:
    startup, window = settings_window
    page = window.communication
    returned, release = held_worker

    def read(request: CommunicationRequest) -> CommunicationResponse:
        return CommunicationResponse(request, (17,), 1, datetime.now(UTC))

    monkeypatch.setattr(page, "_execute", read)
    page.web_panel.port.setValue(8766)
    try:
        assert page.dispatch(RequestKind.READ)
        until(returned.is_set)
        box = prompt(window)
        save = box.button(Button.Save)
        assert save is not None and not save.isEnabled()
        assert (
            page.busy
            and not window.bridge.closing
            and not startup.data.communication.exists()
        )
    finally:
        release.set()
        until(lambda: not page.busy)
    assert save.isEnabled()
    choose(box, Button.Save)
    until(lambda: not window.isVisible())
    assert save_outcomes(startup) == ["success"]


@pytest.mark.parametrize("result", ["confirmed", "unconfirmed", "session_changed"])
def test_close_during_settings_save_uses_its_own_completion(
    settings_window: tuple[Startup, MainWindow],
    held_worker: tuple[Event, Event],
    result: str,
) -> None:
    startup, window = settings_window
    page = window.communication
    returned, release = held_worker
    if result == "unconfirmed":
        refuse(startup.data.audit, "completions", "communication.save")
    page.web_panel.port.setValue(8766)
    try:
        if result == "session_changed":
            choose(prompt(window), Button.Save)
        else:
            page.persist_settings()
        until(returned.is_set)
        window.close()
        window.close()
        assert window.isVisible() and page.busy and not window.bridge.closing
        assert not any(box.isVisible() for box in page.findChildren(QMessageBox))
        if result == "session_changed":
            login(window, Role.GUEST)
    finally:
        release.set()
        until(lambda: not page.busy)
    if result == "confirmed":
        until(lambda: not window.isVisible())
    else:
        assert window.isVisible() and not window.bridge.closing
    assert load_communication_preferences(startup.data.communication).web.port == 8766
    assert save_outcomes(startup) == [None if result == "unconfirmed" else "success"]


def test_session_change_dismisses_close_decision(
    settings_window: tuple[Startup, MainWindow],
) -> None:
    startup, window = settings_window
    page = window.communication
    page.web_panel.port.setValue(8766)
    page.discard_settings.click()
    footer = next(box for box in page.findChildren(QMessageBox) if box.isVisible())
    box = prompt(window)
    assert not footer.isVisible()
    login(window, Role.GUEST)
    assert not box.isVisible() and window.isVisible() and page.dirty
    assert not startup.data.communication.exists()
    guest_box = prompt(window)
    assert guest_box.button(Button.Save) is None
    choose(guest_box, Button.Discard)
    until(lambda: not window.isVisible())
    assert not startup.data.communication.exists()


@pytest.mark.parametrize("decision", [Button.Save, Button.Discard])
def test_failed_application_close_keeps_completed_draft_decision(
    screen: tuple[MainWindow, FakeSession],
    tmp_path: Path,
    decision: Button,
) -> None:
    window, session = screen
    page = window.communication
    path = tmp_path / "communication.json"
    page.configure_preferences(path, CommunicationPreferences())
    page.persist_settings()
    session.ready.set_result(None)
    window.bridge.start()
    until(lambda: window.snapshot.state is ApplicationState.RUNNING)
    session.cleanup_safe = False
    page.web_panel.port.setValue(8766)
    try:
        choose(prompt(window), decision)
        until(
            lambda: (
                window.snapshot.state is ApplicationState.FAULT
                and not window.bridge.closing
            )
        )
        assert window.isVisible() and not page.dirty and not window.editing.closing
        expected = 8766 if decision == Button.Save else 8765
        assert (
            page.web_panel.port.value()
            == load_communication_preferences(path).web.port
            == expected
        )
    finally:
        session.cleanup_safe = True
    window.close()
    until(lambda: not window.isVisible())
    assert window.bridge.closed


def test_connections_rechecked_after_another_editor_decision(
    settings_window: tuple[Startup, MainWindow],
) -> None:
    startup, window = settings_window
    editor = startup.editor
    assert editor is not None
    page = window.communication
    editor.draft.display_name = "Unsaved recipe"
    page.web_panel.port.setValue(8766)
    try:
        choose(prompt(window), Button.Discard)
        recipe_box = next(
            b for b in window.recipe_page.findChildren(QMessageBox) if b.isVisible()
        )
        recipe_box.reject()
        assert editor.dirty and not page.dirty and window.isVisible()
        window.close()
        recipe_box = next(
            b for b in window.recipe_page.findChildren(QMessageBox) if b.isVisible()
        )
        page.web_panel.port.setValue(8767)
        next(
            b
            for b in recipe_box.buttons()
            if recipe_box.buttonRole(b) == QMessageBox.ButtonRole.DestructiveRole
        ).click()
        QApplication.processEvents()
        choose(visible_prompt(window), Button.Cancel)
        assert not editor.dirty and page.dirty and not window.editing.closing
        assert not window.bridge.closing and page.web_panel.port.value() == 8767
    finally:
        editor.discard()


def test_failed_close_save_keeps_active_camera_draft(
    settings_window: tuple[Startup, MainWindow],
) -> None:
    startup, window = settings_window
    page = window.communication
    page.web_panel.host.setText("0.0.0.0")
    window.navigate(4)
    workspace = window.vision_workspace
    assert workspace is not None and workspace.sources is not None
    until(lambda: not window.editing.busy)
    sources = workspace.sources
    try:
        workspace.loop_changed(not sources.saved.loop)
        draft = sources.definition
        assert workspace.vision_dirty
        choose(prompt(window), Button.Save)
        assert (
            window.pages.currentWidget() is window.vision_page
            and not window.bridge.closing
        )
        assert workspace.vision_dirty and sources.definition == draft
        assert not any(box.isVisible() for box in window.findChildren(QMessageBox))
        assert page.preferences_status.text() == window.event_label.accessibleName()
        assert not startup.data.communication.exists()
    finally:
        sources.discard()
