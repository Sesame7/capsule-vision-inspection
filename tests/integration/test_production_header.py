"""Inspect header geometry, keyboard actions and Batch identity."""

from dataclasses import replace

import pytest
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QFontMetricsF
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from tests.support.application import FakeSession
from tests.support.qt import screen as screen

from capsule_vision.application import ApplicationState
from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import Language
from capsule_vision.ui.batch_dialog import BatchDialog
from capsule_vision.ui.main_window import MainWindow, PlayerButton
from capsule_vision.ui.style import Theme


def assert_player_fit(window: MainWindow) -> None:
    buttons = tuple(window.buttons[key] for key in ("operation", "pause", "stop"))
    assert len({button.size().toTuple() for button in buttons}) == 1
    assert len({button.y() for button in buttons}) == 1
    for button in buttons:
        assert isinstance(button, PlayerButton)
        icon, caption = button.content_rects()
        assert button.rect().contains(icon) and button.rect().contains(caption)
        assert icon.bottom() < caption.top()
        assert (
            QFontMetricsF(button.font()).horizontalAdvance(button.text())
            <= caption.width()
        )
        assert (
            button.fontMetrics().boundingRect(button.text()).height()
            <= caption.height()
        )
        assert button.text() and button.accessibleName() and button.toolTip()
        assert window.summary.rect().contains(
            QRect(button.mapTo(window.summary, QPoint()), button.size())
        )
        assert window.operation_group.rect().contains(button.geometry())
    assert all(
        a.geometry().right() < b.x()
        for a, b in zip(buttons[:-1], buttons[1:], strict=True)
    )
    groups = (window.batch_group, window.identity_group, window.operation_group)
    assert all(
        a.geometry().right() < b.x()
        for a, b in zip(groups[:-1], groups[1:], strict=True)
    )


def test_player_focus_and_geometry_survive_navigation_changes(
    screen: tuple[MainWindow, FakeSession],
    qapp: QApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, session = screen
    window.set_language("en")
    window.resize(1080, 1920)
    window.activateWindow()
    start = window.buttons["operation"]
    start.setFocus(Qt.FocusReason.TabFocusReason)
    qapp.processEvents()
    before = window.bridge.application.snapshot()
    ordinary = window.summary.height()
    slots = tuple(
        button.geometry()
        for button in (window.buttons[key] for key in ("operation", "pause", "stop"))
    )
    clicks: list[bool] = []
    starts: list[bool] = []
    start.clicked.connect(lambda checked=False: clicks.append(checked))
    monkeypatch.setattr(window.bridge, "start", lambda: starts.append(True))
    profiles: tuple[tuple[Language, Theme, int, int], ...] = (
        ("zh", "light", 1080, 1920),
        ("en", "dark", 1920, 1080),
        ("de", "light", 1920, 1080),
        ("hi", "dark", 1080, 1920),
    )
    for language, theme, width, height in profiles:
        window.set_language(language)
        window.set_theme(theme)
        window.resize(width, height)
        qapp.processEvents()
        assert window.summary.height() == ordinary
        assert_player_fit(window)
        assert start.hasFocus() and window.navigation[0].isChecked()
        assert (
            tuple(
                window.buttons[key].geometry() for key in ("operation", "pause", "stop")
            )
            == slots
        )
        assert not clicks and not starts
    QTest.keyClick(start, Qt.Key.Key_Space)
    assert clicks == [False] and starts == [True]
    assert window.bridge.application.snapshot() == before and not session.cycles
    # Reset stays a separate square in its reserved context slot.
    window.bridge.snapshot_changed.emit(
        replace(before, state=ApplicationState.FAULT, runtime_owned=False)
    )
    qapp.processEvents()
    reset = window.buttons["secondary"]
    assert reset.isVisible() and reset.isEnabled() and not reset.text()
    assert reset.accessibleName() == reset.toolTip() == text("reset", window.language)
    assert (
        tuple(window.buttons[key].geometry() for key in ("operation", "pause", "stop"))
        == slots
    )


def test_elided_batch_editor_receives_the_full_identity(
    screen: tuple[MainWindow, FakeSession],
    qapp: QApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, session = screen
    full = ("Long Batch 批次 日本語 Lotto " * 30).rstrip()
    window.bridge.start_batch(full)
    qapp.processEvents()
    before = window.bridge.application.snapshot()
    assert before.batch_id == full
    assert window.batch_label.text() != full and "…" in window.batch_label.text()
    assert window.batch_label.accessibleName().endswith(full)
    initial: list[str] = []

    def dialog(editor: BatchDialog) -> int:
        initial.append(editor.label.text())
        return 0

    monkeypatch.setattr(BatchDialog, "exec", dialog)
    window.buttons["batch"].click()
    assert initial == [full]
    assert window.snapshot.batch_id == full
    assert window.bridge.application.snapshot() == before and not session.cycles
