"""Read-only, session-fenced Alarm History inside the existing History destination."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import cast

from PySide6.QtCore import QEvent, QObject, Qt, Slot
from PySide6.QtWidgets import QTableWidgetItem

from capsule_vision.application import ProductionApplication
from capsule_vision.application.alarm_records import AlarmQuery, AlarmRecord
from capsule_vision.application.identity_models import Role
from capsule_vision.configuration.alarm_definitions import CATEGORIES
from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import Language

from .future_delivery import FutureDelivery
from .history_event_table import HistoryEventTable
from .style import Style


class AlarmPage(HistoryEventTable):
    def __init__(self, application: ProductionApplication) -> None:
        super().__init__("alarm")
        self.application = application
        self.language: Language = "zh"
        self.rows: tuple[AlarmRecord, ...] = ()
        self._token = 0
        self._revision: int | None = None
        self._query = AlarmQuery()
        self._error = ""
        self._delivery = FutureDelivery()
        self._delivery.emitter.completed.connect(
            self._completed, Qt.ConnectionType.QueuedConnection
        )
        self.destroyed.connect(
            self._delivery.detach, Qt.ConnectionType.DirectConnection
        )
        self.table.itemSelectionChanged.connect(self._details)
        self.refresh_button.clicked.connect(self.refresh)
        self.search.returnPressed.connect(self.refresh)
        self.kind.activated.connect(self.refresh)
        self.date.dateChanged.connect(self.refresh)
        self.all_dates.toggled.connect(self.refresh)
        self.newer.clicked.connect(lambda: self._turn(True))
        self.older.clicked.connect(lambda: self._turn(False))
        self.clear()

    def clear(self) -> None:
        self._token += 1
        self._revision = None
        self.rows = ()
        self._error = ""
        self.table.setRowCount(0)
        self.search.clear()
        self.details.clear()
        self.message.clear()
        self.message.setToolTip("")
        self.message.hide()
        self.newer.setEnabled(False)
        self.older.setEnabled(False)

    def set_language(self, language: Language) -> None:
        self.language = language
        self.navigation_language(language)
        self.search.setPlaceholderText(text("batch_id", language))
        self.search.setAccessibleName(self.search.placeholderText())
        selected = self.kind.currentData()
        self.kind.clear()
        self.kind.addItem(text("alarm_all_types", language), "")
        for category in CATEGORIES:
            self.kind.addItem(text("alarm_" + category, language), category)
        self.kind.setCurrentIndex(max(0, self.kind.findData(selected)))
        self.kind.setAccessibleName(text("alarm_type", language))
        self.table.setHorizontalHeaderLabels(
            [
                text(k, language)
                for k in ("audit_time", "alarm_name", "alarm_state", "batch_id")
            ]
        )
        self.table.setAccessibleName(text("alarm_history", language))
        self.details.setAccessibleName(text("alarm_details", language))
        self._size_columns()
        self._render()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() in (QEvent.Type.Resize, QEvent.Type.FontChange):
            self._size_columns()
        return super().eventFilter(watched, event)

    def _size_columns(self) -> None:
        body = self.table.fontMetrics()
        samples = (
            "2026-12-28 18:58:59",
            text("alarm_name", self.language),
            text("alarm_observed_recovered", self.language),
            text("batch_id", self.language),
        )
        widths = [
            max(body.horizontalAdvance(sample), 70) + 2 * Style.space
            for sample in samples
        ]
        extra = max(0, self.table.viewport().width() - sum(widths))
        widths[1] += 2 * extra // 3
        widths[3] += extra - 2 * extra // 3
        for col, width in enumerate(widths):
            if self.table.columnWidth(col) != width:
                self.table.setColumnWidth(col, width)

    @Slot()
    def refresh(self) -> None:
        any_date = self.all_dates.isChecked()
        self.date.setEnabled(not any_date)
        day = self.date.date()
        start = datetime(day.year(), day.month(), day.day(), tzinfo=UTC)
        self._read(
            AlarmQuery(
                None if any_date else start.timestamp(),
                None
                if any_date or start.date() == date.max
                else (start + timedelta(days=1)).timestamp(),
                self.kind.currentData() or "",
                self.search.text().strip(),
            )
        )

    def _turn(self, newer: bool) -> None:
        if self.rows:
            self._read(
                replace(
                    self._query,
                    cursor=self.rows[0 if newer else -1].cursor,
                    newer=newer,
                )
            )

    def _read(self, query: AlarmQuery) -> None:
        self._token += 1
        self._query = query
        self._revision = self.application.session_revision
        try:
            future = self.application.alarm_page(query, self._revision)
        except RuntimeError as exc:
            self.clear()
            self._error = str(exc)
            self._render()
            return
        self._delivery.watch(str(self._token), future)

    @Slot(str, object, object)
    def _completed(self, token: str, value: object, error: object) -> None:
        if token != str(self._token):
            return
        identity = self.application.identity
        session = identity.snapshot() if identity else None
        if (
            session is None
            or session.role < Role.OPERATOR
            or self._revision != session.revision
        ):
            self.clear()
            return
        self._error = str(error) if error else ""
        records = (
            cast(tuple[AlarmRecord, ...], value) if isinstance(value, tuple) else ()
        )
        self.rows = records[:50]
        if self._query.newer:
            self.rows = self.rows[::-1]
        self.newer.setEnabled(
            len(records) > 50 if self._query.newer else self._query.cursor is not None
        )
        self.older.setEnabled(
            self._query.cursor is not None if self._query.newer else len(records) > 50
        )
        self._render()

    def _render(self) -> None:
        lang = self.language
        self.table.setRowCount(len(self.rows))
        for row, record in enumerate(self.rows):
            e = record.event
            values = (
                datetime.fromtimestamp(e.observed, UTC).strftime("%Y-%m-%d %H:%M:%S"),
                e.zh if lang == "zh" else e.en,
                text("alarm_" + e.state, lang),
                e.batch_label or "—",
            )
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                item.setData(Qt.ItemDataRole.AccessibleTextRole, value)
                self.table.setItem(row, col, item)
        if self.rows and self.table.currentRow() < 0:
            self.table.selectRow(0)
        self._details()
        self.refresh_status()

    def refresh_status(self) -> None:
        history = self.application.history
        lost, issue = history.alarm_storage() if history else (0, "")
        msg = text("alarm_lost", self.language).format(count=lost) if lost else ""
        if self._error or (issue and not lost):
            msg = (msg + "  " + text("history_unavailable", self.language)).strip()
        self.message.setText(
            msg or ("" if self.rows else text("audit_no_matches", self.language))
        )
        self.message.setToolTip(self._error or issue)
        self.message.setVisible(bool(self.message.text()))

    def _details(self) -> None:
        row = self.table.currentRow()
        if not 0 <= row < len(self.rows):
            self.details.clear()
            return
        e = self.rows[row].event
        source = (
            e.source
            if e.station is None
            else f"{e.source}   {text('alarm_station', self.language)} {e.station}   W{e.address}.{e.bit}"
        )
        self.details.setPlainText(
            "\n".join(
                filter(
                    None,
                    (
                        f"{e.zh if self.language == 'zh' else e.en}   ·   {text('alarm_' + e.category, self.language)}",
                        f"{source}   ·   {e.signal}",
                        e.definition,
                        e.detail,
                        f"{text('batch_id', self.language)}   {e.batch_label}   {e.batch_id}"
                        if e.batch_id
                        else "",
                    ),
                )
            )
        )

    def dispose(self) -> None:
        self._delivery.detach()
