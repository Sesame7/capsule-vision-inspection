"""Logical-pixel visual tokens, themed drawing and action geometry."""

from dataclasses import dataclass
from functools import cache
from math import ceil
from typing import Literal

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFontMetricsF,
    QIcon,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import QPushButton, QStyle, QStyleOptionButton, QWidget

from capsule_vision.localization import catalog
from capsule_vision.localization import languages as language_identity

from .production_presentation import Indicator

type Theme = Literal["dark", "light"]


@dataclass(frozen=True, slots=True)
class ThemeColors:
    background: str
    surface: str
    raised: str
    border: str
    primary: str
    secondary: str
    accent: str
    selected: str
    ok: str
    ng: str
    warning: str
    fault: str
    reject: str


DARK = ThemeColors(
    "#10151d",
    "#19212c",
    "#242e3c",
    "#303c4d",
    "#edf2fa",
    "#9aa9bf",
    "#7895ff",
    "#2c3c61",
    "#49d6a0",
    "#ff6e83",
    "#e6b35c",
    "#ec839b",
    "#3b242d",
)
LIGHT = ThemeColors(
    "#edf2fa",
    "#f9fbff",
    "#e7edf8",
    "#d3deee",
    "#1c2c43",
    "#536780",
    "#2d58db",
    "#dce5ff",
    "#128360",
    "#c12e4c",
    "#936014",
    "#b53456",
    "#ffe5eb",
)


def theme_colors(widget: QWidget) -> ThemeColors:
    owner: QWidget | None = widget
    while owner is not None:
        theme = owner.property("theme")
        if theme in ("dark", "light"):
            return LIGHT if theme == "light" else DARK
        owner = owner.parentWidget()
    return DARK


class Style:
    # Dark canvas drawing tokens; themed controls use their window palette.
    background = DARK.background
    surface = DARK.surface
    raised = DARK.raised
    border = DARK.border
    primary = DARK.primary
    secondary = DARK.secondary
    accent = DARK.accent
    ok = DARK.ok
    ng = DARK.ng
    warning = DARK.warning
    fault = DARK.fault
    space = 12
    small = 6
    radius = 10
    touch = 44
    icon = 22
    icon_text_gap = 4
    body = 15
    title = 24
    number = 32
    minimum = (880, 720)


def button_width(
    button: QPushButton,
    keys: tuple[str, ...],
    *,
    padding: int = Style.small,
    languages: tuple[language_identity.Language, ...] = language_identity.LANGUAGES,
) -> int:
    """A stable budget for this action group, including shaped localized captions."""
    button.ensurePolished()
    option = QStyleOptionButton()
    button.initStyleOption(option)
    # Focus decoration must not move a localized action's slot.
    option.state &= ~QStyle.StateFlag.State_HasFocus
    contents = button.style().subElementRect(
        QStyle.SubElement.SE_PushButtonContents, option, button
    )
    return (
        max(
            ceil(
                QFontMetricsF(button.font()).horizontalAdvance(
                    catalog.text(key, language)
                )
            )
            for key in keys
            for language in languages
        )
        + button.width()
        - contents.width()
        + padding
        + (
            button.iconSize().width() + Style.icon_text_gap
            if not button.icon().isNull()
            else 0
        )
    )


def set_button_caption(button: QPushButton, caption: str, width: int) -> None:
    """Keep an action's slot stable and its full meaning available to accessibility.

    Callers retain their localized tooltips, including operation/error guidance.
    """
    button.setFixedWidth(width)
    option = QStyleOptionButton()
    button.initStyleOption(option)
    available = (
        button.style()
        .subElementRect(QStyle.SubElement.SE_PushButtonContents, option, button)
        .width()
    )
    if not button.icon().isNull():
        available -= button.iconSize().width() + Style.icon_text_gap
    button.setText(
        button.fontMetrics().elidedText(
            caption, Qt.TextElideMode.ElideRight, max(0, available)
        )
    )
    button.setAccessibleName(caption)


def apply_theme(widget: QWidget, theme: Theme) -> None:
    colors = LIGHT if theme == "light" else DARK
    widget.setProperty("theme", theme)
    palette = QPalette()
    for role, color in (
        (QPalette.ColorRole.Window, colors.surface),
        (QPalette.ColorRole.WindowText, colors.primary),
        (QPalette.ColorRole.Base, colors.surface),
        (QPalette.ColorRole.AlternateBase, colors.raised),
        (QPalette.ColorRole.Text, colors.primary),
        (QPalette.ColorRole.Button, colors.raised),
        (QPalette.ColorRole.ButtonText, colors.primary),
        (QPalette.ColorRole.Highlight, colors.selected),
        (QPalette.ColorRole.HighlightedText, colors.primary),
        (QPalette.ColorRole.PlaceholderText, colors.secondary),
        (QPalette.ColorRole.ToolTipBase, colors.raised),
        (QPalette.ColorRole.ToolTipText, colors.primary),
    ):
        palette.setColor(role, QColor(color))
    palette.setColor(
        QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(colors.secondary)
    )
    palette.setColor(
        QPalette.ColorGroup.Disabled,
        QPalette.ColorRole.ButtonText,
        QColor(colors.secondary),
    )
    widget.setPalette(palette)
    widget.setStyleSheet(stylesheet(theme))


def stylesheet(theme: Theme = "dark") -> str:
    c = LIGHT if theme == "light" else DARK
    return f"""
    QWidget {{ color: {c.primary}; font-size: {Style.body}px; }}
    QMainWindow, QWidget#shell, QWidget#recipeEditor, QDialog {{ background: {c.background}; }}
    QFrame#summary, QFrame#camera, QFrame#navigation, QFrame#event, QFrame#commPanel {{
        background: {c.surface}; border: 1px solid {c.border}; border-radius: {Style.radius}px;
    }}
    QLabel {{ background: transparent; border: none; }}
    QLabel[role="secondary"] {{ color: {c.secondary}; }}
    QLabel[role="number"] {{ font-size: {Style.number}px; font-weight: 600; }}
    QLabel[role="state"] {{ font-size: {Style.title}px; font-weight: 600; }}
    QLabel[role="error"] {{ color: {c.warning}; }}
    QFrame#visionSources {{ background: {c.surface}; border: 1px solid {c.border}; border-radius: {Style.radius}px; }}
    QFrame#visionRoi {{ background: {c.raised}; border: 1px solid {c.border}; border-radius: {Style.radius}px; }}
    QFrame#recipeSection {{ background: {c.surface}; border: 1px solid {c.border}; border-radius: {Style.radius}px; }}
    QFrame#visionModeSwitch QPushButton {{ background: transparent; border: none; border-radius: 20px; padding: 0; }}
    QFrame#visionModeSwitch QPushButton:checked, QFrame#visionModeSwitch QPushButton:hover, QFrame#visionModeSwitch QPushButton:disabled {{ background: transparent; border: none; }}
    QFrame#visionModeSwitch QPushButton:focus:enabled {{ border: 1px dotted {c.accent}; }}
    QPushButton, QToolButton, QComboBox {{ background: {c.raised}; border: 1px solid {c.border}; border-radius: 7px; padding: 5px 10px; }}
    QPushButton[compactAction="true"] {{ padding: 5px 4px; }}
    QPushButton:hover, QToolButton:hover, QComboBox:hover {{ background: {c.selected}; border-color: {c.accent}; }}
    QPushButton:pressed, QToolButton:pressed {{ background: {c.selected}; }}
    QPushButton#recipeRule {{ text-align: left; }}
    QWidget#page_production QPushButton#start, QPushButton#productionAction {{ padding: 6px 8px; }}
    QPushButton:checked {{ color: {c.accent}; background: {c.selected}; border-color: {c.accent}; }}
    QPushButton:focus:enabled {{ border: 2px solid {c.accent}; }}
    QComboBox:focus, QLineEdit:focus {{ border-color: {c.accent}; }}
    QWidget#page_production QPushButton#start:focus:enabled {{ border-color: {c.primary}; }}
    QPushButton:disabled, QComboBox:disabled {{ color: {c.secondary}; background: {c.surface}; border-color: {c.border}; }}
    QDateEdit#historyDate {{ color: {c.primary}; background: {c.raised}; border: 1px solid {c.border}; border-radius: 7px; padding: 5px 10px; }}
    QDateEdit#historyDate:disabled {{ color: {c.secondary}; background: {c.surface}; }}
    QCalendarWidget#historyCalendar {{ background: {c.surface}; color: {c.primary}; }}
    QCalendarWidget#historyCalendar QWidget#qt_calendar_navigationbar {{ background: {c.raised}; }}
    QCalendarWidget#historyCalendar QToolButton {{ background: {c.raised}; color: {c.primary}; padding: 4px 6px; min-height: 24px; }}
    QCalendarWidget#historyCalendar QMenu {{ background: {c.surface}; color: {c.primary}; }}
    QCalendarWidget#historyCalendar QSpinBox {{ background: {c.surface}; color: {c.primary}; padding: 2px; }}
    QCalendarWidget#historyCalendar QAbstractItemView {{ background: {c.surface}; color: {c.primary}; alternate-background-color: {c.raised}; selection-background-color: {c.selected}; selection-color: {c.primary}; outline: 0; }}
    QCalendarWidget#historyCalendar QAbstractItemView::item:selected {{ border: 1px solid {c.accent}; }}
    QCalendarWidget#historyCalendar QAbstractItemView:disabled {{ color: {c.secondary}; }}
    QPushButton#visionSourceCard {{ background: {c.background}; border-radius: {Style.radius}px; }}
    QPushButton#visionSourceCard:hover {{ background: {c.raised}; border-color: {c.secondary}; }}
    QPushButton#visionSourceCard:checked {{ background: {c.selected}; border-color: {c.accent}; }}
    QPushButton#visionSourceCard:focus:enabled {{ border: 2px solid {c.accent}; }}
    QPushButton#visionSourceCard:disabled {{ background: {c.surface}; border-color: {c.border}; }}
    QFrame#visionSources QLineEdit#visionSourceValue, QFrame#visionSources QComboBox#visionSourceValue {{ font-weight: 600; }}
    QFrame#visionFps {{ background: {c.background}; border: 1px solid {c.border}; border-radius: 7px; }}
    QFrame#visionFps QPushButton, QFrame#visionFps QLineEdit {{ background: transparent; border: none; padding: 0; }}
    QFrame#visionFps QPushButton:hover:enabled {{ background: {c.selected}; }}
    QFrame#visionFps QPushButton:focus:enabled, QFrame#visionFps QLineEdit:focus {{ border: 1px solid {c.accent}; }}
    QPushButton#start {{ background: #5b7ffc; color: #ffffff; font-weight: 600; border-color: #5b7ffc; }}
    QPushButton#start:hover {{ background: #6d8eff; }}
    QPushButton#start:pressed {{ background: #4265de; }}
    QPushButton#start:disabled {{ background: {c.raised}; color: {c.secondary}; border-color: {c.border}; }}
    QFrame#navigation QPushButton {{ border: none; background: transparent; padding: 6px 10px; }}
    QFrame#navigation QPushButton:hover {{ background: {c.raised}; }}
    QFrame#navigation QPushButton:checked {{ color: {c.accent}; background: {c.selected}; }}
    QFrame#navigation QPushButton:focus {{ border: 1px solid {c.accent}; }}
    QFrame#navigation QPushButton:checked:focus {{ border: none; }}
    QPushButton[iconAlignment="leading"] {{ text-align: left; }}
    QFrame#batchBadge {{ background: {c.raised}; border: 1px solid {c.border}; border-radius: 8px; }}
    QPushButton#productionBatch {{ background: transparent; border-color: transparent; }}
    QFrame#productionMetrics {{ background: {c.raised}; border: 1px solid {c.border}; border-radius: 8px; }}
    QFrame#navigation QComboBox {{ padding: 5px 8px; }}
    QComboBox::drop-down {{ border: none; width: 22px; }}
    QComboBox#language {{ combobox-popup: 0; }}
    QComboBox#language::down-arrow {{ image: none; width: 0; height: 0; }}
    QComboBox#webAddress::drop-down {{ width: {Style.touch}px; }}
    QComboBox#webAddress::down-arrow {{ image: none; }}
    QPushButton#webPower, QPushButton#reportingPower {{ color: {c.primary}; border-color: {c.accent}; background: {c.selected}; }}
    QPushButton#webPower:disabled, QPushButton#reportingPower:disabled {{ color: {c.secondary}; border-color: {c.border}; background: {c.raised}; }}
    QComboBox QAbstractItemView, QMenu {{ background: {c.surface}; color: {c.primary}; border: 1px solid {c.border}; selection-background-color: {c.selected}; }}
    QLineEdit, QSpinBox, QDoubleSpinBox {{ background: {c.surface}; color: {c.primary}; padding: 7px; border: 1px solid {c.border}; border-radius: 6px; selection-background-color: {c.selected}; }}
    QLineEdit:disabled, QSpinBox:disabled {{ color: {c.secondary}; background: {c.raised}; }}
    QScrollArea, QWidget#commBody, QWidget#sourceMappings, QWidget#recipeSummary {{ background: {c.background}; border: none; }}
    QTableWidget, QPlainTextEdit {{ background: {c.surface}; alternate-background-color: {c.raised}; border: 1px solid {c.border}; gridline-color: {c.border}; }}
    QTableWidget#historyCycles, QTableWidget#historyChannels {{ border-radius: {Style.radius}px; }}
    QTableWidget#commWatchTable::item:selected, QTableWidget#historyCycles::item:selected, QTableWidget#historyChannels::item:selected {{ background: {c.selected}; color: {c.primary}; }}
    QHeaderView::section {{ background: {c.raised}; color: {c.secondary}; padding: 6px; border: none; }}
    QTableWidget#historyCycles QHeaderView, QTableWidget#historyChannels QHeaderView {{ background: {c.raised}; }}
    QTableWidget#historyCycles QScrollBar:horizontal, QTableWidget#historyChannels QScrollBar:horizontal {{ background: {c.background}; height: 14px; }}
    QTableWidget#historyCycles QScrollBar::handle:horizontal, QTableWidget#historyChannels QScrollBar::handle:horizontal {{ background: {c.border}; min-width: 40px; border-radius: 6px; }}
    QTableWidget#historyCycles QScrollBar::add-line:horizontal, QTableWidget#historyCycles QScrollBar::sub-line:horizontal, QTableWidget#historyChannels QScrollBar::add-line:horizontal, QTableWidget#historyChannels QScrollBar::sub-line:horizontal {{ width: 0; }}
    QTableWidget#historyCycles QScrollBar::add-page:horizontal, QTableWidget#historyCycles QScrollBar::sub-page:horizontal, QTableWidget#historyChannels QScrollBar::add-page:horizontal, QTableWidget#historyChannels QScrollBar::sub-page:horizontal {{ background: none; }}
    QTableWidget#historyCycles::corner, QTableWidget#historyChannels::corner {{ background: {c.surface}; border: none; }}
    QCheckBox {{ background: transparent; spacing: 8px; }}
    QCheckBox::indicator {{ width: 20px; height: 20px; }}
    QCheckBox#archiveCondition {{ color: {c.primary}; background: {c.surface}; border: 1px solid {c.border}; border-radius: 6px; padding: 4px 8px; spacing: 6px; }}
    QCheckBox#archiveCondition:hover:enabled {{ border-color: {c.secondary}; }}
    QCheckBox#archiveCondition:checked:enabled {{ background: {c.selected}; border-color: {c.accent}; }}
    QCheckBox#archiveCondition:focus:enabled {{ border-color: {c.accent}; }}
    QCheckBox#archiveCondition:disabled {{ color: {c.secondary}; background: {c.raised}; border-color: {c.border}; }}
    QTabBar::tab {{ padding: 12px 10px; min-height: 20px; color: {c.primary}; background: {c.surface}; border: none; }}
    QTabBar::tab:selected {{ color: {c.accent}; background: {c.selected}; }}
    QSlider::groove:horizontal {{ height: 6px; background: {c.border}; border-radius: 3px; }}
    QSlider::handle:horizontal {{ width: 22px; margin: -8px 0; border-radius: 11px; background: {c.accent}; }}
    QScrollBar:vertical {{ background: {c.background}; width: 14px; }}
    QScrollBar::handle:vertical {{ background: {c.border}; min-height: 40px; border-radius: 6px; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}
    QToolTip {{ color: {c.primary}; background: {c.raised}; border: 1px solid {c.border}; padding: 6px; }}
    """


@cache
def camera_icon(color: str = Style.primary) -> QIcon:
    """Shared Vision/Production camera strokes, including the existing dimmed mode."""
    icon = QIcon()
    for checked in (False, True):
        for dpr in (1.0, 1.25, 1.5, 2.0):
            pixmap = QPixmap(round(24 * dpr), round(24 * dpr))
            pixmap.setDevicePixelRatio(dpr)
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QPen(QColor(color), 1.6))
            painter.drawRect(3, 8, 18, 12)
            painter.drawRect(7, 5, 7, 3)
            painter.drawEllipse(8, 10, 8, 8)
            painter.drawPoint(18, 11)
            painter.end()
            state = QIcon.State.On if checked else QIcon.State.Off
            icon.addPixmap(pixmap, QIcon.Mode.Normal, state)
            dimmed = QPixmap(pixmap.size())
            dimmed.setDevicePixelRatio(dpr)
            dimmed.fill(Qt.GlobalColor.transparent)
            muted = QPainter(dimmed)
            muted.setOpacity(0.35)
            muted.drawPixmap(0, 0, pixmap)
            muted.end()
            icon.addPixmap(dimmed, QIcon.Mode.Disabled, state)
    return icon


@cache
def navigation_icon(index: int, color: str = Style.secondary) -> QIcon:
    """Internal vector strokes, rendered at common DPRs; no OS icon theme/font."""
    if index == 4:
        return camera_icon(color)
    icon = QIcon()
    for dpr in (1.0, 1.25, 1.5, 2.0):
        pixmap = QPixmap(round(Style.icon * dpr), round(Style.icon * dpr))
        pixmap.setDevicePixelRatio(dpr)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(color), 1.6))
        if index == 0:
            for x in (3, 12):
                for y in (3, 12):
                    painter.drawRoundedRect(QRectF(x, y, 6, 6), 1, 1)
        elif index == 1:
            for i in range(3):
                x = 4 + i * 7
                painter.drawLine(QPointF(x, 3), QPointF(x, 19))
                painter.drawEllipse(QPointF(x, 7 if (i + index) % 2 else 14), 2, 2)
        elif index == 2:
            painter.drawRoundedRect(QRectF(2, 3, 6, 7), 1, 1)
            painter.drawRoundedRect(QRectF(14, 4, 6, 7), 1, 1)
            for a, b in (
                (QPointF(4, 1), QPointF(4, 3)),
                (QPointF(6, 1), QPointF(6, 3)),
                (QPointF(16, 2), QPointF(16, 4)),
                (QPointF(18, 2), QPointF(18, 4)),
                (QPointF(5, 10), QPointF(5, 16)),
                (QPointF(5, 16), QPointF(17, 16)),
                (QPointF(17, 16), QPointF(17, 11)),
            ):
                painter.drawLine(a, b)
        else:
            painter.drawEllipse(QRectF(3, 3, 16, 16))
            painter.drawLine(QPointF(11, 6), QPointF(11, 11))
            painter.drawLine(QPointF(11, 11), QPointF(16, 13))
        painter.end()
        icon.addPixmap(pixmap)
    return icon


@cache
def production_icon(kind: str, foreground: str | None = None) -> QIcon:
    """Compact state/action strokes using the same DPR-aware Qt icon infrastructure."""
    color = (
        Style.fault
        if kind == "FAULT"
        else Style.warning
        if kind in ("STOPPING", "replay_paused")
        else Style.accent
        if kind == "STARTING"
        else Style.ok
        if kind in ("READY", "RUNNING")
        else Style.background
        if kind in ("start", "stop", "reset", "replay_pause", "replay_continue")
        else Style.secondary
    )
    icon = QIcon()
    for dpr in (1.0, 1.25, 1.5, 2.0):
        pixmap = QPixmap(round(Style.icon * dpr), round(Style.icon * dpr))
        pixmap.setDevicePixelRatio(dpr)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(foreground or color), 1.7))
        if kind == "RUNNING":
            painter.drawEllipse(QRectF(3, 3, 16, 16))
            painter.setBrush(QColor(color))
            painter.drawEllipse(QRectF(8, 8, 6, 6))
        elif kind in ("start", "replay_continue", "replay_step"):
            for a, b in (
                (QPointF(6, 4), QPointF(18, 11)),
                (QPointF(18, 11), QPointF(6, 18)),
                (QPointF(6, 18), QPointF(6, 4)),
            ):
                painter.drawLine(a, b)
            if kind == "replay_step":
                painter.drawLine(19, 4, 19, 18)
        elif kind in ("replay_pause", "replay_paused"):
            painter.drawLine(7, 5, 7, 17)
            painter.drawLine(15, 5, 15, 17)
        elif kind in ("stop", "replay_end"):
            painter.drawRect(QRectF(5, 5, 12, 12))
        elif kind == "power":
            painter.drawArc(QRectF(4, 4, 15, 15), 135 * 16, 270 * 16)
            painter.drawLine(11, 2, 11, 11)
        elif kind == "reset":
            painter.drawArc(4, 4, 14, 14, 30 * 16, 290 * 16)
            painter.drawLine(19, 3, 19, 9)
            painter.drawLine(19, 9, 13, 9)
        elif kind == "tag":
            path = QPainterPath(QPointF(3, 3))
            for point in (
                QPointF(12, 3),
                QPointF(21, 12),
                QPointF(12, 21),
                QPointF(3, 12),
            ):
                path.lineTo(point)
            path.closeSubpath()
            painter.drawPath(path)
            painter.drawEllipse(QPointF(7, 7), 1.5, 1.5)
        elif kind == "cycles":
            painter.drawRoundedRect(QRectF(4, 3, 14, 17), 2, 2)
            painter.drawRoundedRect(QRectF(8, 1, 6, 4), 1, 1)
            for y in (8, 12, 16):
                painter.drawLine(7, y, 15, y)
        elif kind == "clock":
            painter.drawEllipse(QRectF(3, 3, 16, 16))
            painter.drawLine(11, 6, 11, 11)
            painter.drawLine(11, 11, 16, 13)
        elif kind == "batch":
            for a, b in (
                (QPointF(4, 15), QPointF(15, 4)),
                (QPointF(15, 4), QPointF(19, 8)),
                (QPointF(19, 8), QPointF(8, 19)),
                (QPointF(8, 19), QPointF(3, 20)),
                (QPointF(3, 20), QPointF(4, 15)),
            ):
                painter.drawLine(a, b)
        elif kind == "FAULT":
            for a, b in (
                (QPointF(11, 3), QPointF(20, 19)),
                (QPointF(20, 19), QPointF(2, 19)),
                (QPointF(2, 19), QPointF(11, 3)),
            ):
                painter.drawLine(a, b)
            painter.drawLine(11, 8, 11, 13)
            painter.drawPoint(11, 16)
        elif kind == "STOPPING":
            painter.drawRect(QRectF(5, 5, 12, 12))
        elif kind == "STARTING":
            painter.drawEllipse(QRectF(3, 3, 16, 16))
            painter.drawLine(11, 6, 11, 11)
            painter.drawLine(11, 11, 16, 13)
        else:
            painter.drawEllipse(QRectF(3, 3, 16, 16))
            if kind == "READY":
                painter.drawLine(6, 11, 10, 15)
                painter.drawLine(10, 15, 16, 7)
            else:
                painter.drawLine(7, 11, 15, 11)
        painter.end()
        icon.addPixmap(pixmap)
        if kind == "batch":
            disabled = QPixmap(pixmap.size())
            disabled.setDevicePixelRatio(dpr)
            disabled.fill(Qt.GlobalColor.transparent)
            painter = QPainter(disabled)
            painter.setOpacity(0.35)
            painter.drawPixmap(0, 0, pixmap)
            painter.end()
            icon.addPixmap(disabled, QIcon.Mode.Disabled)
    return icon


@cache
def indicator_icon(state: Indicator, foreground: str | None = None) -> QIcon:
    """Semantic shapes, not font-dependent checkmark/warning glyphs."""
    color = {
        Indicator.OK: Style.ok,
        Indicator.NG: Style.ng,
        Indicator.ERROR_NG: Style.ng,
        Indicator.ERROR: Style.warning,
        Indicator.UNKNOWN: Style.warning,
        Indicator.PENDING: Style.secondary,
        Indicator.DISABLED: "#727985",
        Indicator.EMPTY: Style.secondary,
    }[state]
    color = foreground or color
    icon = QIcon()
    for dpr in (1.0, 1.25, 1.5, 2.0):
        pixmap = QPixmap(round(18 * dpr), round(18 * dpr))
        pixmap.setDevicePixelRatio(dpr)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(color), 1.5))
        if state is Indicator.PENDING:
            painter.drawEllipse(QRectF(6, 6, 6, 6))
        elif state is Indicator.DISABLED:
            painter.drawLine(QPointF(4, 9), QPointF(14, 9))
        elif state is Indicator.EMPTY:
            # An empty outlined slot, distinct from pending dot and disabled dash.
            painter.drawRoundedRect(QRectF(2, 3, 14, 12), 2, 2)
        elif state is Indicator.UNKNOWN:
            painter.drawEllipse(QRectF(2, 2, 14, 14))
            path = QPainterPath(QPointF(6, 6))
            path.cubicTo(6, 2, 14, 3, 12, 7)
            path.cubicTo(11, 9, 9, 8, 9, 11)
            painter.drawPath(path)
            painter.drawPoint(9, 14)
        elif state is Indicator.ERROR:
            for a, b in (
                (QPointF(9, 2), QPointF(17, 16)),
                (QPointF(17, 16), QPointF(1, 16)),
                (QPointF(1, 16), QPointF(9, 2)),
            ):
                painter.drawLine(a, b)
            painter.drawLine(QPointF(9, 7), QPointF(9, 11))
            painter.drawPoint(QPointF(9, 14))
        else:
            painter.setBrush(QColor(color))
            if state is Indicator.OK:
                painter.drawEllipse(QRectF(2, 2, 14, 14))
                painter.setPen(QPen(QColor(Style.background), 1.5))
                painter.drawLine(QPointF(5, 9), QPointF(8, 12))
                painter.drawLine(QPointF(8, 12), QPointF(13, 6))
            else:
                painter.drawRoundedRect(QRectF(2, 2, 14, 14), 3, 3)
                painter.setPen(QPen(QColor(Style.background), 1.5))
                painter.drawLine(QPointF(6, 6), QPointF(12, 12))
                painter.drawLine(QPointF(6, 12), QPointF(12, 6))
        painter.end()
        icon.addPixmap(pixmap)
    return icon


@cache
def shell_icon(kind: str, color: str = Style.secondary) -> QIcon:
    """Shared monochrome shell symbols drawn with Qt, independent of icon fonts."""
    if kind == "plc":
        return navigation_icon(2, color)
    if kind == "state":
        return production_icon("READY", color)
    icon = QIcon()
    for dpr in (1.0, 1.25, 1.5, 2.0):
        pixmap = QPixmap(round(24 * dpr), round(24 * dpr))
        pixmap.setDevicePixelRatio(dpr)
        pixmap.fill(Qt.GlobalColor.transparent)
        p = QPainter(pixmap)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(color), 1.6))
        if kind == "brand":
            p.drawEllipse(QRectF(2, 6, 20, 12))
            p.setBrush(QColor(color))
            p.drawEllipse(QRectF(8, 8, 8, 8))
        elif kind == "sun":
            p.drawEllipse(QRectF(8, 8, 8, 8))
            for x, y, dx, dy in (
                (12, 2, 0, 3),
                (12, 19, 0, 3),
                (2, 12, 3, 0),
                (19, 12, 3, 0),
                (4, 4, 2, 2),
                (18, 18, 2, 2),
                (4, 20, 2, -2),
                (18, 6, 2, -2),
            ):
                p.drawLine(x, y, x + dx, y + dy)
        elif kind == "moon":
            path = QPainterPath()
            path.addEllipse(QRectF(3, 3, 18, 18))
            cutout = QPainterPath()
            cutout.addEllipse(QRectF(9, 0, 16, 16))
            p.fillPath(path.subtracted(cutout), QColor(color))
        elif kind == "chevron":
            p.drawLine(6, 9, 12, 15)
            p.drawLine(12, 15, 18, 9)
        elif kind == "globe":
            p.drawEllipse(QRectF(3, 3, 18, 18))
            p.drawEllipse(QRectF(8, 3, 8, 18))
            p.drawLine(3, 12, 21, 12)
            p.drawLine(5, 7, 19, 7)
            p.drawLine(5, 17, 19, 17)
        elif kind == "folder":
            path = QPainterPath(QPointF(3, 8))
            for x, y in ((3, 5), (10, 5), (13, 8), (21, 8), (21, 19), (3, 19), (3, 8)):
                path.lineTo(x, y)
            p.drawPath(path)
        else:
            p.drawEllipse(QRectF(3, 3, 18, 18))
            p.drawLine(12, 6, 12, 12)
            p.drawLine(12, 12, 17, 15)
        p.end()
        icon.addPixmap(pixmap)
    return icon
