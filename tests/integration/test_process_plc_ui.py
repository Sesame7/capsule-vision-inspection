"""On-demand PLC observations use existing Qt ownership without editing Recipes."""

from datetime import UTC, datetime
from pathlib import Path
from threading import Event, get_ident

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QFocusEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from tests.integration.test_process_parameters_ui import process_window
from tests.integration.test_process_preview import saved_target
from tests.support.application import WAIT
from tests.support.fins_tcp import TcpPeer
from tests.support.qt import until
from tests.support.recipe_ui import edit
from tests.support.startup import TEST_PASSWORD, provision_account

from capsule_vision.application.identity_models import Role
from capsule_vision.communication.fins import FinsMemoryArea
from capsule_vision.communication.operations import (
    CommunicationRequest,
    CommunicationResponse,
    RequestKind,
)
from capsule_vision.configuration.communication_preferences import (
    load_communication_preferences,
)
from capsule_vision.localization.catalog import text


def observation(request: CommunicationRequest) -> CommunicationResponse:
    assert request.kind is RequestKind.READ and request.area is FinsMemoryArea.DM
    return CommunicationResponse(
        request, (0xFFD6, 0xFFFF) * (request.count // 2), 0, datetime.now(UTC)
    )


def test_hold_is_an_observation_not_a_draft_and_save_uses_recipe_values(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with TcpPeer() as plc, process_window(tmp_path) as (startup, window):
        saved_target(startup.data.root, port=plc.port)
        session, page = startup.editor, window.recipe_page
        assert session is not None and session.entry is not None
        card = page.process_parameters
        assert not card.view_plc.isEnabled() and not card.expand.isChecked()
        card.expand.click()
        edit(card.fields["running_speed"], "125")
        values, draft = card.values(), session.draft.process_parameters.copy()
        candidate = startup.application.snapshot().candidate_prepared
        original = session.entry.path.read_bytes()
        calls: list[CommunicationRequest] = []
        gui_thread = get_ident()

        def execute(request: CommunicationRequest) -> CommunicationResponse:
            assert get_ident() != gui_thread
            calls.append(request)
            return observation(request)

        monkeypatch.setattr(window.communication, "_execute", execute)
        QTest.mousePress(card.view_plc, Qt.MouseButton.LeftButton)
        until(lambda: not window.communication.busy)
        assert card.plc_previewing and card.readings["running_speed"].text() == "-42"
        assert card.readings["running_speed"].isVisible()
        assert not card.fields["running_speed"].isVisible()
        assert card.values() == values and session.draft.process_parameters == draft
        assert session.dirty and session.entry.path.read_bytes() == original
        assert len(calls) == 1 and calls[0].start == 102
        assert calls[0].endpoint.port == plc.port
        assert str(plc.port) in card.plc_status.toolTip()
        assert startup.application.snapshot().candidate_prepared is candidate
        # Even programmatic Save while the hold remains active consumes only Recipe text.
        page.save()
        until(lambda: not page.busy)
        assert not session.dirty
        assert dict(session.entry.recipe.process_parameters)["running_speed"] == 125
        QTest.mouseRelease(card.view_plc, Qt.MouseButton.LeftButton)
        assert not card.plc_previewing and card.fields["running_speed"].isVisible()
        assert card.values() == values and card.expand.isChecked()
        assert window.communication.last_read is None


def test_release_pending_does_not_block_editing_or_allow_late_reactivation(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = Event(), Event()
    calls: list[CommunicationRequest] = []

    def execute(request: CommunicationRequest) -> CommunicationResponse:
        calls.append(request)
        entered.set()
        assert release.wait(WAIT)
        return observation(request)

    with TcpPeer() as plc, process_window(tmp_path) as (startup, window):
        saved_target(startup.data.root, port=plc.port)
        monkeypatch.setattr(window.communication, "_execute", execute)
        page, card = window.recipe_page, window.recipe_page.process_parameters
        card.expand.click()
        try:
            QTest.mousePress(card.view_plc, Qt.MouseButton.LeftButton)
            until(entered.is_set)
            assert card.plc_status.text() == text("process_plc_loading", page.language)
            assert card.readings["running_speed"].text() == "…"
            QTest.mouseRelease(card.view_plc, Qt.MouseButton.LeftButton)
            assert not card.plc_previewing and window.communication.busy
            # A second hold cannot queue another job behind the pending request.
            QTest.mousePress(card.view_plc, Qt.MouseButton.LeftButton)
            assert card.plc_status.text() == text(
                "process_plc_unavailable", page.language
            )
            QTest.mouseRelease(card.view_plc, Qt.MouseButton.LeftButton)
            edit(card.fields["running_speed"], "126")
            page.save()
            until(lambda: not page.busy)
            assert startup.editor is not None and not startup.editor.dirty
            assert window.communication.busy and len(calls) == 1
        finally:
            release.set()
        until(lambda: not window.communication.busy)
        assert not card.plc_previewing and card.fields["running_speed"].text() == "126"
        assert not startup.application.snapshot().runtime_owned


@pytest.mark.parametrize(
    "end",
    (
        "navigate",
        "focus",
        "deactivate",
        "escape",
        "cancel",
        "collapse",
        "close",
        "logout",
    ),
)
def test_pending_preview_cancellation_owns_actual_worker_cleanup(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, end: str
) -> None:
    entered, release = Event(), Event()

    def execute(request: CommunicationRequest) -> CommunicationResponse:
        entered.set()
        assert release.wait(WAIT)
        return observation(request)

    with process_window(tmp_path) as (startup, window):
        saved_target(startup.data.root)
        monkeypatch.setattr(window.communication, "_execute", execute)
        card = window.recipe_page.process_parameters
        card.expand.click()
        before = card.values()
        try:
            QTest.mousePress(card.view_plc, Qt.MouseButton.LeftButton)
            until(entered.is_set)
            if end == "navigate":
                window.navigate(0)
            elif end == "focus":
                QApplication.sendEvent(card.view_plc, QFocusEvent(QEvent.Type.FocusOut))
            elif end == "deactivate":
                QApplication.sendEvent(window, QEvent(QEvent.Type.WindowDeactivate))
            elif end == "escape":
                QTest.keyClick(card.view_plc, Qt.Key.Key_Escape)
            elif end == "cancel":
                QApplication.sendEvent(card.view_plc, QEvent(QEvent.Type.UngrabMouse))
            elif end == "collapse":
                card.expand.click()
            elif end == "logout":
                assert startup.application.identity is not None
                startup.application.identity.logout()
                window.identity_controls.refresh()
            else:
                window.close()
                assert not window.communication.closed
            assert not card.plc_previewing and card.values() == before
            assert window.communication.busy
        finally:
            release.set()
        until(lambda: not window.communication.busy)
        assert not card.plc_previewing and card.values() == before
        assert startup.editor is not None and not startup.editor.dirty
        if end == "close":
            until(lambda: window.communication.closed and window.bridge.closed)


@pytest.mark.parametrize("role", list(Role))
def test_preview_role_admission_and_keyboard_hold(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, role: Role
) -> None:
    calls: list[CommunicationRequest] = []

    def execute(request: CommunicationRequest) -> CommunicationResponse:
        calls.append(request)
        return observation(request)

    with process_window(tmp_path) as (startup, window):
        saved_target(startup.data.root)
        monkeypatch.setattr(window.communication, "_execute", execute)
        identity = startup.application.identity
        assert identity is not None
        if role is Role.GUEST:
            identity.logout()
        elif role is not Role.ADMINISTRATOR:
            provision_account(identity, role.name, role, TEST_PASSWORD)
            identity.login(role.name, TEST_PASSWORD).result(WAIT)
        window.identity_controls.refresh()
        card = window.recipe_page.process_parameters
        card.expand.click()
        assert card.view_plc.isEnabled() is (role >= Role.ENGINEER)
        QTest.keyPress(card.view_plc, Qt.Key.Key_Space)
        until(lambda: not window.communication.busy)
        assert card.plc_previewing is (role >= Role.ENGINEER)
        QTest.keyRelease(card.view_plc, Qt.Key.Key_Space)
        assert not card.plc_previewing
        assert len(calls) == (1 if role >= Role.ENGINEER else 0)
        assert startup.editor is not None and not startup.editor.dirty


def test_failures_never_reuse_observations_or_prevent_recipe_save(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with process_window(tmp_path) as (startup, window):
        card, page = window.recipe_page.process_parameters, window.recipe_page
        card.expand.click()
        path = startup.data.communication
        calls: list[CommunicationRequest] = []

        def execute(request: CommunicationRequest) -> CommunicationResponse:
            calls.append(request)
            if len(calls) > 1:
                raise TimeoutError("simulated PLC deadline")
            return observation(request)

        monkeypatch.setattr(window.communication, "_execute", execute)
        for settings in (None, '{"process_station":0}'):
            if settings is not None:
                path.write_text(settings)
            QTest.mousePress(card.view_plc, Qt.MouseButton.LeftButton)
            until(lambda: not window.communication.busy)
            assert card.plc_status.text() == text(
                "process_plc_unavailable", page.language
            )
            assert card.readings["running_speed"].text() == "—" and not calls
            QTest.mouseRelease(card.view_plc, Qt.MouseButton.LeftButton)
        saved_target(startup.data.root)
        for expected in ("-42", "—"):
            QTest.mousePress(card.view_plc, Qt.MouseButton.LeftButton)
            until(lambda: not window.communication.busy)
            assert card.readings["running_speed"].text() == expected
            QTest.mouseRelease(card.view_plc, Qt.MouseButton.LeftButton)
        assert "deadline" not in page.status.toolTip()
        path.write_text('{"process_station":0}')
        edit(card.fields["running_speed"], "122")
        page.save()
        until(lambda: not page.busy)
        assert startup.editor is not None and not startup.editor.dirty
        assert card.fields["running_speed"].text() == "122"
        assert path.read_text() == '{"process_station":0}'


def test_communication_save_and_discard_without_station_binding(
    qapp: QApplication, tmp_path: Path
) -> None:
    with process_window(tmp_path) as (startup, window):
        path = saved_target(startup.data.root)
        page = window.communication
        page.configure_preferences(path, load_communication_preferences(path))
        page.web_panel.port.setValue(8877)
        page.save_settings.click()
        until(lambda: not page.busy)
        assert load_communication_preferences(path).status.endpoint.port == 9600
        assert load_communication_preferences(path).web.port == 8877
        assert not page.dirty
        page.web_panel.port.setValue(8878)
        page.reload_settings()
        until(lambda: not page.busy)
        assert page.web_panel.port.value() == 8877 and not page.dirty


@pytest.mark.parametrize("width", (1080, 1920))
def test_preview_geometry_language_and_explicit_extension_remain_isolated(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, width: int
) -> None:
    with process_window(tmp_path, custom=True) as (startup, window):
        saved_target(startup.data.root)
        monkeypatch.setattr(window.communication, "_execute", observation)
        window.resize(width, 1920 if width == 1080 else 1080)
        card = window.recipe_page.process_parameters
        card.expand.click()
        qapp.processEvents()
        values = card.values()
        assert startup.editor is not None
        for language in ("zh", "en", "de"):
            window.set_language(language)
            for theme in ("dark", "light"):
                window.set_theme(theme)
                qapp.processEvents()
                geometry = (
                    card.geometry(),
                    card.view_plc.geometry(),
                    card.expand.geometry(),
                )
                QTest.mousePress(card.view_plc, Qt.MouseButton.LeftButton)
                until(lambda: not window.communication.busy)
                qapp.processEvents()
                assert geometry == (
                    card.geometry(),
                    card.view_plc.geometry(),
                    card.expand.geometry(),
                )
                assert card.readings["feeder_speed"].text() == "-42"
                assert (
                    text("process_plc_values", card.language)
                    in card.readings["feeder_speed"].toolTip()
                )
                assert (
                    text("process_plc_values", card.language)
                    in card.readings["running_speed"].accessibleName()
                )
                assert card.values() == values and not startup.editor.dirty
                QTest.mouseRelease(card.view_plc, Qt.MouseButton.LeftButton)
                qapp.processEvents()
                assert geometry == (
                    card.geometry(),
                    card.view_plc.geometry(),
                    card.expand.geometry(),
                )
                assert (
                    window.recipe_page.summary_scroll.horizontalScrollBar().maximum()
                    == 0
                )
