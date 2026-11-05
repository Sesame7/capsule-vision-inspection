"""Read-only, revision-fenced audit browser within History."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import cast

from PySide6.QtCore import QDate, QEvent, QObject, Qt, Slot
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
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from capsule_vision.application.audit import AuditQuery, AuditRecord
from capsule_vision.application.identity import LocalIdentity
from capsule_vision.application.identity_models import IdentityError, Role
from capsule_vision.localization.catalog import AUDIT_ACTIONS, text
from capsule_vision.localization.languages import Language

from .future_delivery import FutureDelivery
from .image_tools import image_icon
from .style import Style, theme_colors


class AuditPage(QWidget):
    def __init__(self, identity: LocalIdentity | None) -> None:
        super().__init__()
        self.setObjectName("auditPage")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.identity = identity
        self.language: Language = "zh"
        self.rows: tuple[AuditRecord, ...] = ()
        self._token = 0
        self._revision: int | None = None
        self._query = AuditQuery()
        self._read_error = False
        self._delivery = FutureDelivery()
        self._delivery.emitter.completed.connect(
            self._completed, Qt.ConnectionType.QueuedConnection
        )
        self.destroyed.connect(
            self._delivery.detach, Qt.ConnectionType.DirectConnection
        )
        root = QVBoxLayout(self)
        bar = QHBoxLayout()
        root.addLayout(bar)
        self.all_dates = QCheckBox()
        self.all_dates.setChecked(True)
        today = datetime.now(UTC).date()
        self.date = QDateEdit(QDate(today.year, today.month, today.day))
        self.date.setObjectName("auditDate")
        self.date.setDisplayFormat("yyyy-MM-dd")
        self.date.setCalendarPopup(True)
        self.date.calendarWidget().setMinimumSize(8 * Style.touch, 8 * Style.touch)
        self.date.setEnabled(False)
        self.actor = QLineEdit()
        self.actor.setClearButtonEnabled(True)
        self.action = QComboBox()
        for w in (self.date, self.all_dates, self.actor, self.action):
            w.setMinimumHeight(Style.touch)
            bar.addWidget(w)
        self.actor.setMaximumWidth(280)
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
        self.table.setObjectName("auditRecords")
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
        self.table.itemSelectionChanged.connect(self._details)
        self.refresh_button.clicked.connect(self.refresh)
        self.actor.returnPressed.connect(self.refresh)
        self.action.activated.connect(self.refresh)
        self.date.dateChanged.connect(self.refresh)
        self.all_dates.toggled.connect(self.refresh)
        self.newer.clicked.connect(lambda: self._turn(True))
        self.older.clicked.connect(lambda: self._turn(False))
        self.clear()

    def clear(self) -> None:
        self._token += 1
        self._revision = None
        self._read_error = False
        self.rows = ()
        self.table.setRowCount(0)
        self.actor.clear()
        self.details.clear()
        self.message.clear()
        self.message.hide()
        self.message.setToolTip("")
        self.older.setEnabled(False)
        self.newer.setEnabled(False)

    def set_language(self, language: Language) -> None:
        self.language = language
        self.all_dates.setText(text("history_all_dates", language))
        self.date.setAccessibleName(text("audit_time", language))
        self.actor.setPlaceholderText(text("audit_actor", language))
        self.actor.setAccessibleName(self.actor.placeholderText())
        action = self.action.currentData()
        self.action.clear()
        self.action.addItem(text("audit_all_actions", language), "")
        for key in AUDIT_ACTIONS:
            self.action.addItem(text(key, language), key)
        self.action.setCurrentIndex(max(0, self.action.findData(action)))
        self.action.setAccessibleName(text("audit_action", language))
        for b, icon, key in (
            (self.newer, "previous", "previous_page"),
            (self.older, "next", "next_page"),
            (self.refresh_button, "refresh", "history_refresh"),
        ):
            b.setIcon(image_icon(icon, theme_colors(self).primary))
            b.setAccessibleName(text(key, language))
            b.setToolTip(b.accessibleName())
        self.table.setHorizontalHeaderLabels(
            [
                text(k, language)
                for k in ("audit_time", "audit_actor", "audit_action", "audit_outcome")
            ]
        )
        self.table.setAccessibleName(text("audit_trail", language))
        self.details.setAccessibleName(text("audit_details", language))
        self._size_columns()
        self.refresh_status()
        self._render()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() in (QEvent.Type.Resize, QEvent.Type.FontChange):
            self._size_columns()
        return super().eventFilter(watched, event)

    def _size_columns(self) -> None:
        body = self.table.fontMetrics()
        header = self.table.horizontalHeader().fontMetrics()
        samples = (
            ("audit_time", "2026-12-28 18:58:59.888"),
            ("audit_actor", "Shift Engineer"),
            (
                "audit_action",
                max(
                    (text(k, self.language) for k in AUDIT_ACTIONS),
                    key=body.horizontalAdvance,
                ),
            ),
            ("audit_outcome", text("audit_uncertain", self.language)),
        )
        widths = [
            max(
                header.horizontalAdvance(text(key, self.language)),
                body.horizontalAdvance(sample),
            )
            + 2 * Style.space
            for key, sample in samples
        ]
        extra = max(0, self.table.viewport().width() - sum(widths))
        widths[1] += extra // 2
        widths[2] += extra - extra // 2
        for col, width in enumerate(widths):
            if self.table.columnWidth(col) != width:
                self.table.setColumnWidth(col, width)

    @Slot()
    def refresh(self) -> None:
        self.date.setEnabled(not self.all_dates.isChecked())
        day = self.date.date()
        first = datetime(day.year(), day.month(), day.day(), tzinfo=UTC)
        all_dates = self.all_dates.isChecked()
        self._read(
            AuditQuery(
                "" if all_dates else first.isoformat(),
                ""
                if all_dates or first.date() == date.max
                else (first + timedelta(days=1)).isoformat(),
                self.actor.text(),
                self.action.currentData() or "",
            )
        )

    def _turn(self, newer: bool) -> None:
        if self.rows:
            self._read(
                replace(
                    self._query,
                    before=self.rows[0 if newer else -1].cursor,
                    newer=newer,
                )
            )

    def _read(self, query: AuditQuery) -> None:
        self._query = query
        self._token += 1
        if self.identity is None:
            self.clear()
            return
        self._revision = self.identity.snapshot().revision
        try:
            future = self.identity.audit_page(query, self._revision)
        except IdentityError:
            self.clear()
            return
        self._delivery.watch(str(self._token), future)

    @Slot(str, object, object)
    def _completed(self, token: str, result: object, error: object) -> None:
        if token != str(self._token) or self.identity is None:
            return
        session = self.identity.snapshot()
        if session.revision != self._revision or session.role < Role.ENGINEER:
            self.clear()
            return
        page = (
            cast(tuple[AuditRecord, ...], result) if isinstance(result, tuple) else ()
        )
        ordered = page[:50]
        self.rows = ordered[::-1] if self._query.newer else ordered
        self.newer.setEnabled(
            len(page) > 50 if self._query.newer else self._query.before is not None
        )
        self.older.setEnabled(
            self._query.before is not None if self._query.newer else len(page) > 50
        )
        self._read_error = error is not None
        self.refresh_status()
        self._render()

    @Slot()
    def refresh_status(self) -> None:
        if self.identity is None or self._revision is None:
            return
        lost, issue = self.identity.audit.observation()
        self.message.setText(
            (text("audit_warning", self.language).format(count=lost) if issue else "")
            or (
                text("history_unavailable", self.language)
                if self._read_error
                else text("audit_no_matches", self.language)
                if not self.rows
                else ""
            )
        )
        self.message.setToolTip(issue)
        self.message.setVisible(bool(self.message.text()))

    def _render(self) -> None:
        self.table.setRowCount(len(self.rows))
        for row, record in enumerate(self.rows):
            op = record.operation
            for col, value in enumerate(
                (
                    op.utc[:23].replace("T", " "),
                    op.username,
                    text(op.action, self.language)
                    if op.action in AUDIT_ACTIONS
                    else op.action,
                    text("audit_" + record.outcome, self.language),
                )
            ):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.table.setItem(row, col, item)
        if self.rows and self.table.currentRow() < 0:
            self.table.selectRow(0)
        self._details()

    def _details(self) -> None:
        row = self.table.currentRow()
        if not 0 <= row < len(self.rows):
            self.details.clear()
            return
        r = self.rows[row]
        op = r.operation
        self.details.setPlainText(
            "\n".join(
                f"{text(k, self.language)}   {v}"
                for k, v in (
                    ("audit_target", op.target),
                    ("audit_actor", f"{op.username}  {op.actor_id}"),
                    ("audit_before", op.before),
                    (
                        "audit_after" if r.outcome == "success" else "audit_requested",
                        op.after,
                    ),
                    ("audit_remarks", r.detail),
                    ("audit_completed", r.completed_utc),
                    ("audit_id", op.id),
                )
                if v
            )
        )

    def dispose(self) -> None:
        self._delivery.detach()
