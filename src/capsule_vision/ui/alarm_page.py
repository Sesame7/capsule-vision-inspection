"""Read-only, session-fenced Alarm History inside the existing History destination."""

from dataclasses import replace
from typing import cast

from PySide6.QtCore import Qt, Slot

from capsule_vision.application import ProductionApplication
from capsule_vision.application.alarm_records import AlarmEvent, AlarmQuery, AlarmRecord
from capsule_vision.application.identity_models import Role
from capsule_vision.application.records_query import RecordsScope
from capsule_vision.configuration.alarm_definitions import CATEGORIES
from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import Language

from .future_delivery import FutureDelivery
from .history_event_table import (
    HistoryEventTable,
    readable_value,
    record_reason,
    record_time,
)


def _source_caption(event: AlarmEvent, language: Language) -> str:
    if event.source == "application":
        return text("records_software", language)
    if event.source.startswith("fins_tcp://") or event.source == "PLC":
        if event.address is None:
            return "PLC"
        bit = f".{event.bit}" if event.bit is not None else ""
        return f"PLC · W{event.address}{bit}"
    return readable_value(event.source) or "—"


class AlarmPage(HistoryEventTable):
    def __init__(self, application: ProductionApplication) -> None:
        super().__init__("alarm")
        self.application = application
        self.scope = RecordsScope()
        self.search.hide()
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
        self.kind.activated.connect(self.refresh)
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
        self.clear_details()
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
        self.columns(
            language,
            (
                ("short_time", "2026-12-31 23:59:59", "2026-12-31 23:59:59", 1),
                ("comm_source", "PLC · W18.5", "PLC · W118.15", 2),
                (
                    "alarm_name",
                    "Low air pressure",
                    "Transfer valve actuation phase not set",
                    5,
                ),
                ("alarm_state", "Recovered", "Observed active", 2),
                ("batch_id", "DEV-LOT-F", "LOT-20261020-003", 3),
            ),
        )
        self.table.setAccessibleName(text("alarm_history", language))
        self.details.setAccessibleName(text("alarm_details", language))
        self._render()

    @Slot()
    def refresh(self) -> None:
        self._read(
            AlarmQuery(
                self.scope.since,
                self.scope.until,
                self.kind.currentData() or "",
                batch_id=self.scope.batch,
                unbatched=self.scope.unbatched,
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
            clock, full_time = record_time(e.observed)
            state_key = "alarm_" + e.state
            short_state = {
                "observed_active": "records_seen_active",
                "observed_recovered": "records_seen_clear",
            }.get(e.state, state_key)
            self.set_values(
                row,
                (
                    clock,
                    _source_caption(e, lang),
                    readable_value(e.label(lang)) or "—",
                    text(short_state, lang),
                    readable_value(e.batch_label) or "—",
                ),
                {0: full_time, 3: text(state_key, lang)},
            )
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
            msg or ("" if self.rows else text("history_none", self.language))
        )
        self.message.setToolTip(self._error or issue)
        self.message.setVisible(bool(self.message.text()))

    def _details(self) -> None:
        row = self.table.currentRow()
        if not self.table.selectedItems() or not 0 <= row < len(self.rows):
            self.clear_details()
            return
        event = self.rows[row].event
        note = event.detail.strip()
        if note in (
            event.label(self.language),
            event.en,
            event.zh,
            event.source,
            event.state,
            event.signal,
            event.definition,
            event.batch_id,
            event.batch_label,
            text("alarm_" + event.state, self.language),
        ):
            note = ""
        elif event.source == "application":
            note = record_reason(note, self.language)
        elif note == "First observation after a communication gap":
            note = text("records_after_gap", self.language)
        self.show_details(self.language, (("", note),))

    def dispose(self) -> None:
        self._delivery.detach()
