"""The five native page actions and their utilities share one stable row."""

from collections.abc import Iterator
from threading import Event

import pytest
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QCursor, QFont
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QPushButton,
    QStyle,
    QStyleOption,
    QStyleOptionButton,
    QStylePainter,
    QVBoxLayout,
    QWidget,
)
from tests.support.qt import ensure_test_fonts, until

from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import LANGUAGES, Language
from capsule_vision.ui import shell
from capsule_vision.ui.shell import PAGE_CAPTIONS, PAGES, NavigationBar
from capsule_vision.ui.style import Style, apply_theme


@pytest.fixture
def bar(qapp: QApplication) -> Iterator[NavigationBar]:
    ensure_test_fonts()
    host = QWidget()
    font = QFont()
    font.setFamilies(["Microsoft YaHei UI", "Segoe UI", "sans-serif"])
    host.setFont(font)
    apply_theme(host, "dark")
    row = QVBoxLayout(host)
    row.setContentsMargins(0, 0, 0, 0)
    widget = NavigationBar()
    row.addWidget(widget)
    widget.present("en", "dark", 0)
    host.show()
    yield widget
    destroyed = Event()
    host.destroyed.connect(destroyed.set)
    host.close()
    host.deleteLater()
    until(destroyed.is_set)


def bounds(widget: QWidget, parent: QWidget) -> QRect:
    return QRect(widget.mapTo(parent, QPoint()), widget.size())


def assert_fit(bar: NavigationBar) -> None:
    controls = (*bar.buttons, bar.user, bar.theme, bar.language)
    rects = [bounds(control, bar) for control in controls]
    for rect in rects:
        assert bar.rect().contains(rect)
        assert min(rect.width(), rect.height()) >= Style.touch
        assert abs(rect.center().y() - rects[0].center().y()) <= 1
    assert all(a.right() < b.left() for a, b in zip(rects, rects[1:], strict=False))
    assert rects[-1].right() == bar.contentsRect().right() - 12
    assert bar.user.width() == 192


@pytest.mark.parametrize("width", [1056, 1896])  # MainWindow has 12px shell margins.
def test_localized_slots_stay_still_across_selection_theme_and_identity(
    bar: NavigationBar, qapp: QApplication, width: int
) -> None:
    bar.window().resize(width, bar.sizeHint().height())
    controls = (*bar.buttons, bar.user, bar.theme, bar.language)
    assert (
        len(bar.buttons) == len(PAGES) == 5 and len(bar.findChildren(QPushButton)) == 7
    )
    assert bar.minimumSizeHint().width() <= Style.minimum[0] - 24
    qapp.processEvents()
    initial = tuple(bounds(c, bar) for c in controls)
    assert tuple(b.width() for b in bar.buttons) == (116, 128, 116, 132, 132)
    assert bar.user.x() - initial[4].right() - 1 >= 2 * Style.small
    for theme in ("light", "dark"):
        apply_theme(bar.window(), theme)
        for language in LANGUAGES:
            for page in (0, 4):
                bar.present(language, theme, page)
                qapp.processEvents()
                assert_fit(bar)
                assert tuple(bounds(c, bar) for c in controls) == initial
                assert bar.buttons[page].isChecked()
                for key, button in zip(PAGES, bar.buttons, strict=True):
                    assert not button.icon().isNull()
                    caption = text(PAGE_CAPTIONS[key], language)
                    assert button.text() == caption
                    assert (
                        button.accessibleName()
                        == button.toolTip()
                        == text(key, language)
                    )
                    assert caption in (
                        button.accessibleName(),
                        button.accessibleDescription(),
                    )
    for username in (None, "Morgan Alvarez", "An exceptionally long account name " * 3):
        bar.present_user(username, "Operator")
        qapp.processEvents()
        assert tuple(bounds(c, bar) for c in controls) == initial
        if username and len(username) > 30:
            assert bar.user.text().endswith("…")
            assert (
                bar.user.accessibleName()
                == bar.user.toolTip()
                == f"{username} · Operator"
            )
        else:
            assert bar.user.text() == (username or "Guest")


def test_resizing_keeps_focus_selection_and_keyboard_actions(
    bar: NavigationBar, qapp: QApplication
) -> None:
    bar.window().activateWindow()
    bar.present("en", "dark", 2)
    bar.buttons[2].setFocus(Qt.FocusReason.TabFocusReason)
    selected: list[int] = []
    themes: list[bool] = []
    languages: list[str] = []
    bar.page_selected.connect(selected.append)
    bar.theme_requested.connect(lambda: themes.append(True))
    bar.language_selected.connect(languages.append)
    geometries: dict[int, tuple[QRect, ...]] = {}
    for width in (1056, 1896, 1056, 1896):
        bar.window().resize(width, bar.sizeHint().height())
        qapp.processEvents()
        geometry = tuple(bounds(c, bar) for c in (*bar.buttons, bar.user, bar.language))
        assert geometry == geometries.setdefault(width, geometry)
        assert_fit(bar)
        assert (
            bar.buttons[2].hasFocus() and bar.buttons[2].isChecked() and selected == []
        )
    narrow, wide = geometries[1056], geometries[1896]
    assert narrow[:5] == wide[:5]
    assert wide[5].x() - narrow[5].x() == 1896 - 1056
    QTest.keyClick(bar.buttons[2], Qt.Key.Key_Right)
    assert bar.buttons[3].hasFocus() and selected == [3]
    QTest.keyClick(bar.buttons[3], Qt.Key.Key_Tab)
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


@pytest.mark.parametrize(("language", "selected"), [("zh", 0), ("en", 2)])
def test_painted_icon_and_caption_are_centered_together(
    bar: NavigationBar, qapp: QApplication, language: Language, selected: int
) -> None:
    bar.window().resize(1056, bar.sizeHint().height())
    bar.present(language, "dark", selected)
    qapp.processEvents()
    for button in bar.buttons:
        image = button.grab().toImage()
        # Measure the painted group inside the flat fill, excluding rounded corners.
        inset = round(6 * image.devicePixelRatio())
        background = image.pixel(image.width() // 2, inset)
        ink_columns = [
            x
            for x in range(inset, image.width() - inset)
            if any(
                image.pixel(x, y) != background
                for y in range(inset, image.height() - inset)
            )
        ]
        assert ink_columns
        left = ink_columns[0]
        right = image.width() - 1 - ink_columns[-1]
        assert abs(left - right) <= 4 * image.devicePixelRatio(), button.text()


def test_captions_painted_in_full_or_elided_only_on_overflow(
    bar: NavigationBar, qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    bar.window().resize(1056, bar.sizeHint().height())
    original = QStylePainter.drawControl
    painted: list[str] = []

    def draw(
        painter: QStylePainter, element: QStyle.ControlElement, option: QStyleOption
    ) -> None:
        if isinstance(option, QStyleOptionButton):
            painted.append(option.text)
        original(painter, element, option)

    monkeypatch.setattr(QStylePainter, "drawControl", draw)
    for language in ("zh", "en", "de", "fr", "km", "ms", "fil", "sw"):
        bar.present(language, "dark", 0)
        qapp.processEvents()
        for button in bar.buttons:
            button.grab()
            assert painted[-1] == button.text(), (language, button.text(), painted[-1])

    long_name = "A longer translated caption " * 6

    def translated(key: str, language: Language) -> str:
        return long_name if key == "nav_records" else text(key, language)

    monkeypatch.setattr(shell, "text", translated)
    old = tuple(b.geometry() for b in bar.buttons)
    bar.present("en", "dark", 3)
    qapp.processEvents()
    records = bar.buttons[3]
    records.grab()
    assert painted[-1].endswith("…") and long_name.startswith(painted[-1][:-1])
    assert records.text() == records.accessibleDescription() == long_name
    assert records.toolTip() == records.accessibleName() == "History"
    assert old == tuple(b.geometry() for b in bar.buttons)


def test_font_change_remeasures_but_does_not_reorder_or_replace_actions(
    bar: NavigationBar, qapp: QApplication
) -> None:
    instances = (*bar.buttons, bar.user, bar.theme, bar.language)
    bar.window().activateWindow()
    bar.buttons[0].setFocus(Qt.FocusReason.TabFocusReason)
    hints: list[int] = []
    for size in (15, 26, 15):
        bar.setStyleSheet(bar.styleSheet() + f"\nQWidget {{font-size: {size}px;}}")
        bar.window().resize(1056, bar.sizeHint().height())
        qapp.processEvents()
        hints.append(bar.buttons[0].sizeHint().height())
        assert_fit(bar)
        assert instances == (*bar.buttons, bar.user, bar.theme, bar.language)
        assert bar.buttons[0].hasFocus() and bar.buttons[0].isChecked()
    assert hints[0] == hints[2] < hints[1]


def test_pointer_leaves_no_focus_border_but_tab_and_check_remain_visible(
    bar: NavigationBar, qapp: QApplication
) -> None:
    bar.window().resize(1056, bar.sizeHint().height())
    bar.window().activateWindow()
    bar.buttons[0].setFocus()
    qapp.processEvents()
    button = bar.theme
    QCursor.setPos(bar.mapToGlobal(QPoint(-20, -20)))
    ordinary = button.grab().toImage()
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    QTest.mouseMove(bar.buttons[0])
    qapp.processEvents()
    assert button.hasFocus() and button.grab().toImage() == ordinary
    bar.user.setFocus()
    QTest.keyClick(bar.user, Qt.Key.Key_Tab)
    assert button.hasFocus() and button.grab().toImage() != ordinary
    # A pointer click on the already keyboard-focused action removes only its focus cue.
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    QTest.mouseMove(bar.buttons[0])
    qapp.processEvents()
    assert button.hasFocus() and button.grab().toImage() == ordinary
    page = bar.buttons[1]
    unchecked = page.grab().toImage()
    QTest.mouseClick(page, Qt.MouseButton.LeftButton)
    QTest.mouseMove(bar.buttons[0])
    assert page.isChecked() and page.grab().toImage() != unchecked


def test_short_dropdown_shows_all_choices_and_long_list_still_scrolls(
    qapp: QApplication,
) -> None:
    panel = QWidget()
    combo = QComboBox()
    QVBoxLayout(panel).addWidget(combo)
    combo.addItems([f"Choice {i}" for i in range(14)])
    apply_theme(panel, "dark")
    panel.show()
    qapp.processEvents()
    try:
        for long_list in (False, True):
            if long_list:
                combo.addItems([f"Extra {i}" for i in range(80)])
            combo.showPopup()
            qapp.processEvents()
            view = combo.view()
            assert (
                view.isVisible()
                and (view.verticalScrollBar().maximum() > 0) is long_list
            )
            if not long_list:
                assert all(
                    view.viewport()
                    .rect()
                    .contains(view.visualRect(combo.model().index(i, 0)))
                    for i in range(combo.count())
                )
            QTest.keyClick(view, Qt.Key.Key_End)
            QTest.keyClick(view, Qt.Key.Key_Return)
            assert combo.currentIndex() == combo.count() - 1 and not view.isVisible()
    finally:
        combo.hidePopup()
        panel.close()


def test_shared_action_distinguishes_hover_press_touch_and_keyboard_focus(
    qapp: QApplication,
) -> None:
    panel = QWidget()
    row = QVBoxLayout(panel)
    first = QPushButton("First")
    action = QPushButton("Refresh")
    for button in (first, action):
        row.addWidget(button)
    apply_theme(panel, "light")
    panel.show()
    try:
        panel.activateWindow()
        first.setFocus()
        qapp.processEvents()
        QTest.mouseMove(action)
        hover = action.grab().toImage()
        QTest.mousePress(action, Qt.MouseButton.LeftButton)
        assert action.grab().toImage() != hover
        QTest.mouseRelease(action, Qt.MouseButton.LeftButton)
        assert action.grab().toImage() == hover
        first.setFocus()
        QTest.mouseMove(first)
        ordinary = action.grab().toImage()
        assert ordinary != hover
        QTest.keyClick(first, Qt.Key.Key_Tab)
        assert action.hasFocus() and action.grab().toImage() != ordinary
        clicks: list[bool] = []
        action.clicked.connect(lambda: clicks.append(True))
        touch = QTest.touchEvent(panel, QTest.createTouchDevice(), False)
        point = action.rect().center()
        touch.press(0, point, action).commit()
        touch.release(0, point, action).commit()
        # Send a real pointer exit so the comparison excludes the Qt hover state.
        QTest.mouseMove(action)
        QTest.mouseMove(first)
        qapp.processEvents()
        assert clicks == [True] and action.hasFocus()
        assert action.grab().toImage() == ordinary
    finally:
        panel.close()


def test_extra_test_slot_reuses_native_selection_group(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The capacity fixture is isolated; the real application still has five pages.
    monkeypatch.setattr(shell, "PAGES", (*PAGES, "test_extra"))
    monkeypatch.setitem(PAGE_CAPTIONS, "test_extra", "nav_records")

    def translated(key: str, language: Language) -> str:
        return "Extra" if key == "test_extra" else text(key, language)

    monkeypatch.setattr(shell, "text", translated)
    widget = NavigationBar()
    apply_theme(widget, "dark")
    widget.present("en", "dark", 0)
    widget.resize(1056, widget.sizeHint().height())
    widget.show()
    try:
        qapp.processEvents()
        assert_fit(widget)
        assert len(widget.findChildren(QPushButton)) == 8
        selected: list[int] = []
        widget.page_selected.connect(selected.append)
        widget.buttons[5].click()
        assert selected == [5] and widget.buttons[5].isChecked()
    finally:
        widget.close()
