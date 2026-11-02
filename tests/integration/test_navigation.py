"""Content-aware navigation, native button interaction and isolated capacity checks."""

from collections.abc import Iterator
from threading import Event

import pytest
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QFont, QIcon
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton, QWidget
from tests.support.qt import ensure_test_fonts, until

from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import LANGUAGES, Language
from capsule_vision.ui import shell
from capsule_vision.ui.shell import PAGES, NavigationBar, NavigationMode
from capsule_vision.ui.style import Style, apply_theme, navigation_icon


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
    destroyed = Event()
    widget.destroyed.connect(destroyed.set)
    widget.close()
    widget.deleteLater()
    until(destroyed.is_set)


def bounds(widget: QWidget, parent: QWidget) -> QRect:
    return QRect(widget.mapTo(parent, QPoint()), widget.size())


def assert_fit(bar: NavigationBar) -> None:
    assert bar.minimumSizeHint().width() <= Style.minimum[0] - 24
    controls = (*bar.buttons, bar.user, bar.theme, bar.language)
    rects = [bounds(control, bar) for control in controls]
    for index, rect in enumerate(rects):
        assert bar.rect().contains(rect), (bar.mode, rect, bar.rect())
        assert all(not rect.intersects(other) for other in rects[index + 1 :])
    assert bar.theme.height() == Style.touch
    assert bar.user.height() == bar.language.height() >= Style.touch
    assert bar.compact is (bar.mode != "comfortable")
    for button in bar.buttons:
        assert button.width() >= Style.touch and button.height() >= Style.touch
        assert button.stacked is bar.compact and not button.icon().isNull()
        budget = button.width() - 48
        if bar.compact:
            icon, caption = button.content_rects()
            assert button.rect().contains(icon) and button.rect().contains(caption)
            assert icon.bottom() < caption.top()
            assert (
                caption.height()
                >= button.fontMetrics().boundingRect(button.text()).height()
            )
            budget = caption.width()
        assert button.fontMetrics().horizontalAdvance(button.text()) <= budget
    if bar.mode == "two_row":
        assert bar.page_row.geometry().bottom() < bar.utilities.y()
    else:
        assert bar.page_row.geometry().right() < bar.utilities.x()
        assert (
            abs(
                bar.page_row.geometry().center().y()
                - bar.utilities.geometry().center().y()
            )
            <= 1
        )


@pytest.mark.parametrize(
    "font_pixels,mode", [(15, "comfortable"), (26, "compact"), (34, "two_row")]
)
def test_all_three_presentations_use_the_same_controls(
    bar: NavigationBar, qapp: QApplication, font_pixels: int, mode: NavigationMode
) -> None:
    instances = (*bar.buttons, bar.user, bar.theme, bar.language)
    bar.resize(1056, bar.height())  # 1080-wide application's shell margins.
    bar.setStyleSheet(bar.styleSheet() + f"\nQWidget {{font-size: {font_pixels}px;}}")
    qapp.processEvents()
    assert bar.mode == mode
    assert_fit(bar)
    assert len({button.width() for button in bar.buttons}) > 1
    assert instances == (*bar.buttons, bar.user, bar.theme, bar.language)
    assert len(bar.buttons) == len(PAGES) == 5
    assert len(bar.findChildren(QPushButton)) == 7


def test_short_chinese_english_names_fit_a_single_row_without_elision(
    bar: NavigationBar, qapp: QApplication
) -> None:
    for width in (1056, 1896, 1056):
        bar.resize(width, bar.height())
        for language in ("zh", "en"):
            bar.present(language, "dark", 0)
            bar.present_user("Alex", "Operator")
            qapp.processEvents()
            assert bar.mode == "comfortable" and bar.user.text() == "Alex"
            assert_fit(bar)
            for key, button in zip(PAGES, bar.buttons, strict=True):
                assert button.text() == (
                    "PLC / Web" if key == "page_communication" else text(key, language)
                )


def test_navigation_spends_spare_width_then_stops_at_comfortable_limits(
    bar: NavigationBar, qapp: QApplication
) -> None:
    allocations: list[tuple[int, ...]] = []
    for width in (1056, 1896, 4096, 1056):
        bar.resize(width, bar.height())
        qapp.processEvents()
        assert bar.mode == "comfortable"
        assert_fit(bar)
        sizes = tuple(button.width() for button in bar.buttons)
        allocations.append(sizes)
        assert sizes[0] > sizes[1]  # Production naturally needs more than Recipe.
        assert max(sizes) <= 200
        assert bar.utilities.geometry().right() == bar.contentsRect().right() - 12
        assert bar.page_row.geometry().right() + 12 < bar.utilities.x()
    assert all(a <= b for a, b in zip(allocations[0], allocations[1], strict=True))
    assert allocations[0] != allocations[1] == allocations[2]
    assert allocations[0] == allocations[3]
    assert (
        sum(allocations[2]) < 1000
    )  # Large windows retain surplus, not full-width tabs.


@pytest.mark.parametrize("font_pixels", [15, 26, 34])
def test_all_languages_stable_across_selection_theme_and_repeated_resizing(
    bar: NavigationBar, qapp: QApplication, font_pixels: int
) -> None:
    geometries: dict[
        tuple[int, Language], tuple[NavigationMode, tuple[QRect, ...]]
    ] = {}
    heights: dict[NavigationMode, int] = {}
    for width in (1056, 1896, 856, 1056):
        bar.resize(width, bar.height())
        for theme in ("dark", "light"):
            apply_theme(bar, theme)
            bar.setStyleSheet(
                bar.styleSheet() + f"\nQWidget {{font-size: {font_pixels}px;}}"
            )
            for language in LANGUAGES:
                for page in (0, 4):
                    bar.present(language, theme, page)
                    qapp.processEvents()
                    assert bar.width() == width
                    assert_fit(bar)
                    geometry = tuple(
                        bounds(c, bar) for c in (*bar.buttons, bar.utilities)
                    )
                    assert (bar.mode, geometry) == geometries.setdefault(
                        (width, language), (bar.mode, geometry)
                    )
                    assert bar.height() == heights.setdefault(bar.mode, bar.height())
                    assert bar.buttons[page].isChecked()
                    for key, button in zip(PAGES, bar.buttons, strict=True):
                        assert (
                            button.accessibleName()
                            == button.toolTip()
                            == text(key, language)
                        )


def test_resize_boundaries_preserve_focus_selection_and_signals(
    bar: NavigationBar, qapp: QApplication
) -> None:
    bar.activateWindow()
    bar.present("en", "dark", 2)
    bar.buttons[2].setFocus()
    selected: list[int] = []
    bar.page_selected.connect(selected.append)
    boundaries: list[int] = []
    previous: NavigationMode = "two_row"
    seen: set[NavigationMode] = set()
    for width in range(420, 1201):
        bar.resize(width, bar.height())
        qapp.processEvents()
        seen.add(bar.mode)
        if bar.mode != previous:
            boundaries.append(width)
            previous = bar.mode
    assert seen == {"comfortable", "compact", "two_row"} and len(boundaries) == 2
    for boundary in boundaries:
        for width in (boundary - 1, boundary, boundary + 1, boundary, boundary - 1) * 3:
            bar.resize(width, bar.height())
            qapp.processEvents()
            mode, height, geometry = bar.mode, bar.height(), bar.page_row.geometry()
            for _ in range(3):
                qapp.processEvents()
                assert (bar.mode, bar.height(), bar.page_row.geometry()) == (
                    mode,
                    height,
                    geometry,
                )
            assert_fit(bar)
            assert bar.buttons[2].hasFocus() and bar.buttons[2].isChecked()
    assert selected == []
    QTest.keyClick(bar.buttons[2], Qt.Key.Key_Right)
    assert bar.buttons[3].hasFocus() and selected == [3]


@pytest.mark.parametrize("font_pixels", [15, 26, 34])
def test_keyboard_theme_and_language_controls_in_each_mode(
    bar: NavigationBar, qapp: QApplication, font_pixels: int
) -> None:
    bar.resize(1056, bar.height())
    bar.setStyleSheet(bar.styleSheet() + f"\nQWidget {{font-size: {font_pixels}px;}}")
    bar.activateWindow()
    bar.buttons[-1].setFocus()
    qapp.processEvents()
    themes: list[bool] = []
    languages: list[str] = []
    bar.theme_requested.connect(lambda: themes.append(True))
    bar.language_selected.connect(languages.append)
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
    assert_fit(bar)


def test_font_changes_and_long_username_do_not_change_navigation_ownership(
    bar: NavigationBar, qapp: QApplication
) -> None:
    bar.resize(1056, bar.height())
    bar.activateWindow()
    bar.present("en", "dark", 1)
    bar.buttons[1].setFocus()
    modes: list[NavigationMode] = []
    name = "A long account name " * 3
    for size in (15, 26, 34, 26, 15):
        bar.setStyleSheet(bar.styleSheet() + f"\nQWidget {{font-size: {size}px;}}")
        qapp.processEvents()
        assert bar.buttons[0].font().pixelSize() == size
        assert bar.buttons[1].hasFocus() and bar.buttons[1].isChecked()
        modes.append(bar.mode)
        geometry = tuple(bounds(c, bar) for c in (*bar.buttons, bar.utilities))
        for username in ("Alex", name, None):
            bar.present_user(username, "Operator")
            qapp.processEvents()
            assert (
                tuple(bounds(c, bar) for c in (*bar.buttons, bar.utilities)) == geometry
            )
            if username == name:
                assert bar.user.text().endswith("…")
                assert name in bar.user.accessibleName() == bar.user.toolTip()
            assert (
                bar.user.fontMetrics().horizontalAdvance(bar.user.text())
                <= bar.user.width() - 64
            )
        assert_fit(bar)
    assert modes == ["comfortable", "compact", "two_row", "compact", "comfortable"]


def test_overflow_is_elided_without_sizing_for_every_full_caption(
    bar: NavigationBar, qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    long_caption = "Long localized navigation caption " * 15

    def translated(key: str, language: Language) -> str:
        return long_caption if key in PAGES else text(key, language)

    monkeypatch.setattr(shell, "text", translated)
    for width in (856, 1056, 1896):
        bar.resize(width, bar.height())
        bar.present("en", "dark", 1)
        qapp.processEvents()
        assert_fit(bar)
        for index, button in enumerate(bar.buttons):
            assert button.toolTip() == button.accessibleName() == long_caption
            if index != 2:
                assert button.text().endswith("…")


def test_isolated_sixth_slot_uses_the_same_adaptive_layout(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = PAGES

    def translated(key: str, language: Language) -> str:
        return "Sixth slot" if key == "capacity_test" else text(key, language)

    def capacity_icon(index: int, color: str) -> QIcon:
        return navigation_icon(index if index < 5 else 3, color)

    with monkeypatch.context() as patch:
        patch.setattr(shell, "PAGES", (*PAGES, "capacity_test"))
        patch.setattr(shell, "text", translated)
        patch.setattr(
            shell,
            "navigation_icon",
            capacity_icon,
        )
        ensure_test_fonts()
        widget = NavigationBar()
        apply_theme(widget, "dark")
        widget.present("en", "dark", 0)
        widget.show()
        try:
            for width in (1056, 1896, 856):
                widget.resize(width, widget.height())
                qapp.processEvents()
                assert (
                    len(widget.buttons) == 6
                    and len(widget.findChildren(QPushButton)) == 8
                )
                assert_fit(widget)
            selected: list[int] = []
            widget.page_selected.connect(selected.append)
            widget.buttons[5].click()
            assert selected == [5]
        finally:
            destroyed = Event()
            widget.destroyed.connect(destroyed.set)
            widget.close()
            widget.deleteLater()
            until(destroyed.is_set)
    assert shell.PAGES == original and len(shell.PAGES) == 5
