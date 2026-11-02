"""Coordinated header geometry, genuine font growth and native button semantics."""

from dataclasses import replace

import pytest
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QFontMetricsF
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QInputDialog, QWidget
from tests.support.application import FakeSession
from tests.support.qt import screen as screen
from tests.support.qt import until

from capsule_vision.application import ApplicationState
from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import Language
from capsule_vision.ui import main_window
from capsule_vision.ui.main_window import MainWindow, PlayerButton


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


@pytest.mark.parametrize("size", [(1080, 1920), (1920, 1080)])
def test_real_font_changes_grow_then_restore_header_without_clipping(
    screen: tuple[MainWindow, FakeSession], qapp: QApplication, size: tuple[int, int]
) -> None:
    window, session = screen
    window.set_language("en")
    window.resize(*size)
    qapp.processEvents()
    before = window.bridge.application.snapshot()
    ordinary = window.summary.height()
    cards = tuple(card.geometry() for card in window.cards)
    fonts = {
        key: window.buttons[key].styleSheet() for key in ("operation", "pause", "stop")
    }
    for pixels in (44, 15, 44, 15):
        for key in fonts:
            window.buttons[key].setStyleSheet(f"font-size: {pixels}px;")
        until(
            lambda pixels=pixels: (
                window.buttons["operation"].height() > 60
                if pixels == 44
                else window.buttons["operation"].height() <= 60
            )
        )
        qapp.processEvents()
        until(
            lambda pixels=pixels: (
                window.summary.height() > ordinary
                if pixels == 44
                else window.summary.height() == ordinary
            )
        )
        assert_player_fit(window)
        assert window.size().toTuple() == size
        if pixels == 44:
            assert window.summary.height() > ordinary
        else:
            assert window.summary.height() == ordinary
            assert tuple(card.geometry() for card in window.cards) == cards
    for key, stylesheet in fonts.items():
        window.buttons[key].setStyleSheet(stylesheet)
    caption_style, value_style = (
        window.batch_caption.styleSheet(),
        window.batch_label.styleSheet(),
    )
    window.batch_caption.setStyleSheet("font-size: 30px;")
    window.batch_label.setStyleSheet("font-size: 32px; font-weight: 600;")
    qapp.processEvents()
    until(lambda: window.summary.height() > ordinary)
    assert window.summary.height() > ordinary
    for label in (window.batch_caption, window.batch_label):
        assert label.height() >= label.fontMetrics().boundingRect(label.text()).height()
        assert window.batch_group.rect().contains(label.geometry())
    assert window.batch_caption.geometry().bottom() < window.batch_label.y()
    window.batch_caption.setStyleSheet(caption_style)
    window.batch_label.setStyleSheet(value_style)
    qapp.processEvents()
    until(lambda: window.summary.height() == ordinary)
    assert window.summary.height() == ordinary
    assert window.bridge.application.snapshot() == before and not session.cycles


def test_navigation_modes_and_player_focus_survive_coordinated_reflow(
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
    sizes_and_languages: tuple[tuple[int, Language], ...] = (
        (1920, "ja"),
        (1080, "zh"),
        (1080, "it"),
        (1080, "en"),
    )
    for pixels, mode in (
        (15, "comfortable"),
        (26, "compact"),
        (34, "two_row"),
        (26, "compact"),
        (15, "comfortable"),
    ):
        window.navbar.setStyleSheet(f"QWidget {{font-size: {pixels}px;}}")
        qapp.processEvents()
        assert window.navbar.mode == mode
        for theme in ("light", "dark"):
            window.set_theme(theme)
            for width, language in sizes_and_languages:
                window.set_language(language)
                window.resize(width, 1920)
                qapp.processEvents()
                assert window.summary.height() == ordinary
                assert_player_fit(window)
                assert start.hasFocus() and window.navigation[0].isChecked()
                assert (
                    tuple(
                        window.buttons[key].geometry()
                        for key in ("operation", "pause", "stop")
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
    before = window.bridge.application.snapshot()
    full = "Long Batch 批次 日本語 Lotto " * 30
    window.bridge.snapshot_changed.emit(replace(before, batch_id=full))
    qapp.processEvents()
    assert window.batch_label.text() != full and "…" in window.batch_label.text()
    assert window.batch_label.accessibleName().endswith(full)
    initial: list[str] = []

    def dialog(
        _parent: QWidget, _title: str, _prompt: str, *, text: str
    ) -> tuple[str, bool]:
        initial.append(text)
        return text, False

    monkeypatch.setattr(QInputDialog, "getText", dialog)
    window.buttons["batch"].click()
    assert initial == [full]
    assert window.snapshot.batch_id == full
    assert window.bridge.application.snapshot() == before and not session.cycles


def test_player_width_is_not_driven_by_an_extreme_translation(
    screen: tuple[MainWindow, FakeSession],
    qapp: QApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, session = screen
    window.set_language("en")
    qapp.processEvents()
    before = window.bridge.application.snapshot()
    players = tuple(window.buttons[key] for key in ("operation", "pause", "stop"))
    slots = tuple(button.geometry() for button in players)
    assert all(button.size().toTuple() == (120, 60) for button in players)
    long_caption = "An exceptionally long localized Start caption " * 10

    def translated(key: str, language: Language) -> str:
        return long_caption if key == "replay_start" else text(key, language)

    monkeypatch.setattr(main_window, "text", translated)
    window.set_theme("light")
    qapp.processEvents()
    assert tuple(button.geometry() for button in players) == slots
    assert window.buttons["operation"].text().endswith("…")
    assert window.buttons["operation"].accessibleName() == long_caption
    assert window.buttons["operation"].toolTip() == long_caption
    assert_player_fit(window)
    assert window.bridge.application.snapshot() == before and not session.cycles
