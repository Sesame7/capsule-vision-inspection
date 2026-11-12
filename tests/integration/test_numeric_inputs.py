"""Shared native steppers keep hit areas, input semantics and readable values."""

import pytest
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QDoubleSpinBox,
    QSpinBox,
    QStyle,
    QStyleOptionSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from tests.support.qt import ensure_test_fonts

from capsule_vision.ui.records_filters import OptionalDate
from capsule_vision.ui.style import Style, Theme, apply_theme


def step_rects(spin: QAbstractSpinBox) -> tuple[QRect, QRect]:
    option = QStyleOptionSpinBox()
    spin.initStyleOption(option)
    rects = [
        spin.style().subControlRect(QStyle.ComplexControl.CC_SpinBox, option, key, spin)
        for key in (QStyle.SubControl.SC_SpinBoxUp, QStyle.SubControl.SC_SpinBoxDown)
    ]
    return rects[0], rects[1]


@pytest.mark.parametrize("theme", ("light", "dark"))
def test_native_steppers_keep_text_limits_precision_and_hit_areas(
    qapp: QApplication, theme: Theme
) -> None:
    ensure_test_fonts()
    window = QWidget()
    layout = QVBoxLayout(window)
    integer, decimal = QSpinBox(), QDoubleSpinBox()
    integer.setRange(0, 65532)
    integer.setPrefix("HR ")
    integer.setValue(65531)
    decimal.setRange(0.05, 30)
    decimal.setDecimals(2)
    decimal.setSingleStep(0.1)
    decimal.setSuffix(" s")
    decimal.setValue(1.25)
    for spin in (integer, decimal):
        spin.setMinimumHeight(Style.touch)
        layout.addWidget(spin)
    apply_theme(window, theme)
    window.resize(240, 200)
    window.show()
    qapp.processEvents()
    try:
        for spin in (integer, decimal):
            up, down = step_rects(spin)
            assert up.width() == down.width() == 21
            assert up.height() == down.height() == spin.height() // 2
            assert up.right() == down.right() == spin.rect().right()
            assert not up.intersects(down)
            assert spin.rect().contains(up) and spin.rect().contains(down)
            line = spin.lineEdit()
            assert line.fontMetrics().horizontalAdvance(line.text()) + 4 <= line.width()
            geometry = spin.geometry(), up, down
            spin.setFocus(Qt.FocusReason.TabFocusReason)
            QTest.mouseMove(spin, up.center())
            QTest.mousePress(spin, Qt.MouseButton.LeftButton, pos=up.center())
            assert (spin.geometry(), *step_rects(spin)) == geometry
            QTest.mouseRelease(spin, Qt.MouseButton.LeftButton, pos=up.center())
            assert (spin.geometry(), *step_rects(spin)) == geometry

        assert integer.value() == 65532 and not integer.wrapping()
        QTest.keyClick(integer, Qt.Key.Key_Up)
        assert integer.value() == 65532
        assert decimal.value() == pytest.approx(1.35)
        for spin, typed, expected in (
            (integer, "0", 0),
            (decimal, "2.75", 2.75),
        ):
            spin.setFocus()
            spin.selectAll()
            QTest.keyClicks(spin, typed)
            QTest.keyClick(spin, Qt.Key.Key_Return)
            assert spin.value() == expected
            observed = QSignalSpy(spin.valueChanged)
            if spin is integer:
                QTest.keyClick(spin, Qt.Key.Key_Down)
                assert spin.value() == 0 and observed.count() == 0
            else:
                before = spin.value()
                QTest.keyClick(spin, Qt.Key.Key_Down)
                step = 0.1 if spin is decimal else 1
                assert spin.value() == pytest.approx(before - step)
                assert observed.count() == 1
            for read_only in (True, False):
                spin.setReadOnly(read_only)
                spin.setEnabled(read_only)
                value = spin.value()
                up, _ = step_rects(spin)
                QTest.mouseClick(spin, Qt.MouseButton.LeftButton, pos=up.center())
                QTest.keyClick(spin, Qt.Key.Key_Up)
                QTest.keyClicks(spin, "9")
                assert spin.value() == value
            spin.setEnabled(True)
            spin.setReadOnly(False)
            QTest.mouseMove(window, QPoint())
        decimal.setValue(30)
        QTest.keyClick(decimal, Qt.Key.Key_Up)
        assert decimal.value() == 30 and not decimal.wrapping()
        decimal.setValue(0.05)
        QTest.keyClick(decimal, Qt.Key.Key_Down)
        assert decimal.value() == 0.05
    finally:
        window.close()
        window.deleteLater()


@pytest.mark.parametrize("theme", ("light", "dark"))
def test_calendar_year_keeps_native_editing_without_selecting_a_date(
    qapp: QApplication, theme: Theme
) -> None:
    date = OptionalDate("records_from")
    apply_theme(date, theme)
    date.show()
    try:
        date.open_calendar.trigger()
        year_button = date.calendar.findChild(QToolButton, "qt_calendar_yearbutton")
        assert year_button is not None
        year_button.click()
        qapp.processEvents()
        year = date.calendar.findChild(QSpinBox, "qt_calendar_yearedit")
        assert year is not None and year.isVisible()
        assert year.buttonSymbols() == QAbstractSpinBox.ButtonSymbols.UpDownArrows
        line = year.lineEdit()
        assert line.fontMetrics().horizontalAdvance(line.text()) + 4 <= line.width()
        previous = year.value()
        QTest.keyClick(year, Qt.Key.Key_Up)
        QTest.keyClick(year, Qt.Key.Key_Return)
        assert date.calendar.yearShown() == previous + 1
        assert date.value is None
    finally:
        date.popup.hide()
        date.close()
        date.deleteLater()
