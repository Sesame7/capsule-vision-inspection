"""Shared read-only table and filter controls for the two event history tabs."""

from datetime import UTC, datetime

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import Language

from .image_tools import image_icon
from .style import Style, theme_colors


class HistoryEventTable(QWidget):
    def __init__(self, name: str) -> None:
        super().__init__()
        self.setObjectName(name + "Page")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        root = QVBoxLayout(self)
        bar = QHBoxLayout()
        root.addLayout(bar)
        self.all_dates = QCheckBox()
        self.all_dates.setChecked(True)
        today = datetime.now(UTC).date()
        self.date = QDateEdit(QDate(today.year, today.month, today.day))
        self.date.setObjectName(name + "Date")
        self.date.setDisplayFormat("yyyy-MM-dd")
        self.date.setCalendarPopup(True)
        self.date.calendarWidget().setMinimumSize(8 * Style.touch, 8 * Style.touch)
        self.date.setEnabled(False)
        self.search = QLineEdit()
        self.search.setClearButtonEnabled(True)
        self.kind = QComboBox()
        for w in (self.date, self.all_dates, self.search, self.kind):
            w.setMinimumHeight(Style.touch)
            bar.addWidget(w)
        self.search.setMaximumWidth(280)
        bar.addStretch()
        self.newer, self.older, self.refresh_button = (
            QPushButton(),
            QPushButton(),
            QPushButton(),
        )
        for b in (self.newer, self.older, self.refresh_button):
            b.setFixedSize(Style.touch, Style.touch)
            bar.addWidget(b)
        self.message = QLabel()
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        self.message.hide()
        root.addWidget(self.message)
        self.table = QTableWidget(0, 4)
        self.table.setObjectName(name + "Records")
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.table.viewport().installEventFilter(self)
        root.addWidget(self.table, 1)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setMaximumHeight(160)
        root.addWidget(self.details)

    def navigation_language(self, language: Language) -> None:
        self.all_dates.setText(text("history_all_dates", language))
        self.date.setAccessibleName(text("audit_time", language))
        for b, icon, key in (
            (self.newer, "previous", "previous_page"),
            (self.older, "next", "next_page"),
            (self.refresh_button, "refresh", "history_refresh"),
        ):
            b.setIcon(image_icon(icon, theme_colors(self).primary))
            b.setAccessibleName(text(key, language))
            b.setToolTip(b.accessibleName())
