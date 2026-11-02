"""Responsive navigation geometry and native button interaction, without services."""

from collections.abc import Iterator

import pytest
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QFont
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton, QWidget
from tests.support.qt import ensure_test_fonts

from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import LANGUAGES, Language
from capsule_vision.ui import shell
from capsule_vision.ui.shell import PAGES, NavigationBar
from capsule_vision.ui.style import Style, apply_theme


@pytest.fixture
def bar(qapp: QApplication) -> Iterator[NavigationBar]:
    ensure_test_fonts()
    widget = NavigationBar()
    font = QFont()
    font.setFamilies(["Microsoft YaHei UI", "Segoe UI", "sans-serif"])
    widget.setFont(font)
    apply_theme(widget, "dark")
    widget.present("en", "dark", 0)
    widget.show()
    yield widget
    widget.close()
    widget.deleteLater()
    qapp.processEvents()


def bounds(widget: QWidget, parent: QWidget) -> QRect:
    return QRect(widget.mapTo(parent, QPoint()), widget.size())


def assert_fit(bar: NavigationBar, compact: bool) -> None:
    assert bar.compact is compact
    assert bar.minimumSizeHint().width() <= Style.minimum[0] - 24
    controls = (*bar.buttons, bar.user, bar.theme, bar.language)
    rects = [bounds(control, bar) for control in controls]
    for index, rect in enumerate(rects):
        assert bar.rect().contains(rect)
        assert all(not rect.intersects(other) for other in rects[index + 1 :])
    assert bar.theme.height() == bar.language.height() == Style.touch
    widths = [button.width() for button in bar.buttons]
    assert max(widths) - min(widths) <= 1
    for button in bar.buttons:
        assert button.width() >= Style.touch
        assert button.height() >= Style.touch and button.stacked is compact
        if compact:
            icon, caption = button.content_rects()
            assert button.rect().contains(icon) and button.rect().contains(caption)
            assert icon.bottom() < caption.top()
            assert (
                caption.height()
                >= button.fontMetrics().boundingRect(button.text()).height()
            )
            assert (
                button.fontMetrics().horizontalAdvance(button.text()) <= caption.width()
            )
    if compact:
        assert (
            abs(bar.utilities.geometry().right() - bar.page_row.geometry().right()) <= 1
        )
        assert bar.page_row.geometry().bottom() < bar.utilities.y()
    else:
        assert bar.page_row.geometry().right() < bar.utilities.x()


def test_languages_pages_themes_and_repeated_resizing_keep_two_stable_modes(
    bar: NavigationBar, qapp: QApplication
) -> None:
    instances = (*bar.buttons, bar.user, bar.theme, bar.language)
    geometries: dict[bool, tuple[QRect, ...]] = {}
    heights: dict[bool, int] = {}
    for width in (1056, 1896, 1056, 1896):
        bar.resize(width, bar.height())
        qapp.processEvents()
        compact = width == 1056
        for theme in ("dark", "light"):
            apply_theme(bar, theme)
            for language in LANGUAGES:
                for page in (0, 4):
                    bar.present(language, theme, page)
                    qapp.processEvents()
                    assert bar.width() == width
                    assert_fit(bar, compact)
                    current = tuple(bounds(control, bar) for control in instances)
                    assert current == geometries.setdefault(compact, current)
                    assert bar.height() == heights.setdefault(compact, bar.height())
                    assert bar.buttons[page].isChecked()
                    for key, button in zip(PAGES, bar.buttons, strict=True):
                        assert (
                            button.accessibleName()
                            == button.toolTip()
                            == text(key, language)
                        )
        assert instances == (*bar.buttons, bar.user, bar.theme, bar.language)
        assert len(bar.findChildren(QPushButton)) == 7
    assert heights[True] > heights[False]


@pytest.mark.parametrize("width", [1056, 1896])
def test_short_labels_overflow_and_keyboard_controls(
    bar: NavigationBar, qapp: QApplication, monkeypatch: pytest.MonkeyPatch, width: int
) -> None:
    bar.resize(width, bar.height())
    qapp.processEvents()
    for language in ("zh", "en", "af"):
        bar.present(language, "dark", 0)
        qapp.processEvents()
        for key, button in zip(PAGES, bar.buttons, strict=True):
            assert button.text() == (
                "PLC / Web" if key == "page_communication" else text(key, language)
            )
    geometry = tuple(bounds(control, bar) for control in (*bar.buttons, bar.utilities))
    height = bar.height()
    original = text
    long_name = "Long localized navigation caption " * 15

    def translated(key: str, language: Language) -> str:
        return long_name if key in PAGES else original(key, language)

    monkeypatch.setattr(shell, "text", translated)
    bar.present("en", "dark", 1)
    qapp.processEvents()
    assert bar.height() == height
    assert (
        tuple(bounds(control, bar) for control in (*bar.buttons, bar.utilities))
        == geometry
    )
    for index, button in enumerate(bar.buttons):
        assert button.accessibleName() == button.toolTip() == long_name
        if index != 2:
            assert button.text().endswith("…")
    selected: list[int] = []
    themes: list[bool] = []
    languages: list[str] = []
    bar.page_selected.connect(selected.append)
    bar.theme_requested.connect(lambda: themes.append(True))
    bar.language_selected.connect(languages.append)
    bar.present("en", "dark", 0)
    bar.activateWindow()
    bar.buttons[0].setFocus()
    qapp.processEvents()
    QTest.keyClick(bar.buttons[0], Qt.Key.Key_Right)
    assert bar.buttons[1].hasFocus() and bar.buttons[1].isChecked()
    assert selected[-1] == 1
    QTest.keyClick(bar.buttons[1], Qt.Key.Key_Space)
    assert selected[-1] == 1
    bar.buttons[-1].setFocus()
    QTest.keyClick(bar.buttons[-1], Qt.Key.Key_Tab)
    assert bar.user.hasFocus()
    QTest.keyClick(bar.user, Qt.Key.Key_Tab)
    assert bar.theme.hasFocus()
    QTest.keyClick(bar.theme, Qt.Key.Key_Space)
    assert themes == [True]
    QTest.keyClick(bar.theme, Qt.Key.Key_Tab)
    assert bar.language.hasFocus()
    bar.language.setCurrentIndex(bar.language.findData("ti"))
    bar.language.activated.emit(bar.language.currentIndex())
    assert languages == ["ti"]
    assert_fit(bar, width == 1056)


def test_long_username_keeps_touch_targets_and_full_identity(
    bar: NavigationBar, qapp: QApplication
) -> None:
    name = "Long account name " * 3
    for width in (856, 1056, 1896, 856):
        bar.resize(width, bar.height())
        for language in LANGUAGES:
            bar.present(language, "dark", 0)
            before = tuple(
                bounds(control, bar) for control in (*bar.buttons, bar.utilities)
            )
            bar.present_user(name, "Administrator")
            qapp.processEvents()
            assert name in bar.user.accessibleName() == bar.user.toolTip()
            assert bar.user.text().endswith("…")
            assert (
                tuple(bounds(control, bar) for control in (*bar.buttons, bar.utilities))
                == before
            )
            assert_fit(bar, width != 1896)
    bar.setStyleSheet(bar.styleSheet() + "\nQWidget {font-size: 23px;}")
    qapp.processEvents()
    assert (
        bar.user.fontMetrics().horizontalAdvance(bar.user.text())
        <= bar.user.width() - 64
    )
    assert name in bar.user.accessibleName()


def test_breakpoint_and_font_changes_do_not_oscillate(
    bar: NavigationBar, qapp: QApplication
) -> None:
    modes: list[bool] = []
    for width in range(1020, 1500, 7):
        bar.resize(width, bar.height())
        qapp.processEvents()
        mode, height = bar.compact, bar.height()
        for _ in range(3):
            qapp.processEvents()
            assert (
                bar.width() == width and bar.compact is mode and bar.height() == height
            )
        modes.append(mode)
    assert modes[0] and not modes[-1]
    assert sum(a != b for a, b in zip(modes, modes[1:], strict=False)) == 1
    old_width = bar.language.width()
    bar.setStyleSheet(bar.styleSheet() + "\nQWidget {font-size: 23px;}")
    qapp.processEvents()
    assert bar.buttons[0].font().pixelSize() == 23
    assert bar.language.width() > old_width
    for width in (1056, 1896, 1056):
        bar.resize(width, bar.height())
        qapp.processEvents()
        height = bar.height()
        for language in LANGUAGES:
            bar.present(language, "dark", 0)
            qapp.processEvents()
            assert bar.height() == height and bar.width() == width
            assert_fit(bar, width == 1056)
