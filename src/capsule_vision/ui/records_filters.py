"""The single nullable date range and searchable Batch picker shared by Records."""

from dataclasses import replace

from PySide6.QtCore import QDate, QEvent, QLocale, QPoint, QSize, Qt, Signal, Slot
from PySide6.QtGui import QColor, QTextCharFormat
from PySide6.QtWidgets import (
    QCalendarWidget,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QMenu,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from capsule_vision.application.history import ProductionHistory
from capsule_vision.application.identity import LocalIdentity
from capsule_vision.application.identity_models import Role
from capsule_vision.application.records_query import (
    BatchChoice,
    BatchChoices,
    RecordsScope,
)
from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import Language

from .future_delivery import FutureDelivery
from .history_event_table import local_day_bounds
from .image_tools import image_icon
from .style import Style, theme_colors


class OptionalDate(QLineEdit):
    changed = Signal()

    def __init__(self, key: str) -> None:
        super().__init__()
        self.key = key
        self.value: QDate | None = None
        self.setClearButtonEnabled(True)
        self.setMinimumHeight(Style.touch)
        self._size_field()
        self.calendar = QCalendarWidget()
        self.calendar.setObjectName("historyCalendar")
        self.calendar.setVerticalHeaderFormat(
            QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader
        )
        self.calendar.setMinimumSize(8 * Style.touch, 8 * Style.touch)
        self.popup = QMenu(self)
        action = QWidgetAction(self.popup)
        action.setDefaultWidget(self.calendar)
        self.popup.addAction(action)
        self.open_calendar = self.addAction(
            image_icon("calendar", theme_colors(self).primary),
            QLineEdit.ActionPosition.TrailingPosition,
        )
        self.open_calendar.triggered.connect(self._open)
        self.calendar.clicked.connect(self.set_date)
        self.calendar.activated.connect(self.set_date)
        self.textChanged.connect(self._typed)

    def set_date(self, date: QDate | None) -> None:
        self.setText(date.toString("yyyy-MM-dd") if date else "")
        self.popup.hide()

    def _size_field(self) -> None:
        # Leave room for both the calendar and the clear action after styling
        # installs the actual font. The clear affordance must not hide the year.
        self.setFixedWidth(
            max(176, self.fontMetrics().horizontalAdvance("2000-00-00") + 72)
        )

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.FontChange:
            self._size_field()

    def _open(self) -> None:
        self.calendar.setSelectedDate(self.value or QDate.currentDate())
        self.popup.popup(self.mapToGlobal(QPoint(0, self.height())))
        self.calendar.setFocus()

    def _typed(self, value: str) -> None:
        date = QDate.fromString(value, "yyyy-MM-dd") if value else None
        if date is not None and not date.isValid():
            self.setToolTip("YYYY-MM-DD")
            return
        if date != self.value:
            self.value = date
            self.changed.emit()

    def set_language(self, language: Language) -> None:
        label = text(self.key, language)
        self.setPlaceholderText(label)
        self.setAccessibleName(label)
        self.setToolTip(label + " · YYYY-MM-DD")
        self.calendar.setLocale(QLocale(language))
        self.calendar.setAccessibleName(label)
        self.calendar.setFirstDayOfWeek(self.calendar.locale().firstDayOfWeek())
        color = theme_colors(self).primary
        day_format = QTextCharFormat()
        day_format.setForeground(QColor(color))
        for day in (Qt.DayOfWeek.Saturday, Qt.DayOfWeek.Sunday):
            self.calendar.setWeekdayTextFormat(day, day_format)
        for name, icon in (
            ("qt_calendar_prevmonth", "previous"),
            ("qt_calendar_nextmonth", "next"),
        ):
            button = self.calendar.findChild(QToolButton, name)
            if button is not None:
                button.setIcon(image_icon(icon, color))
                button.setIconSize(QSize(Style.icon, Style.icon))
        self.open_calendar.setToolTip(label)
        self.open_calendar.setText(label)
        self.open_calendar.setIcon(image_icon("calendar", theme_colors(self).primary))


class RecordsFilters(QWidget):
    changed = Signal()

    def __init__(
        self, history: ProductionHistory | None, identity: LocalIdentity | None = None
    ) -> None:
        super().__init__()
        self.history = history
        self.identity = identity
        self._revision: int | None = None
        self.language: Language = "zh"
        self.scope = RecordsScope()
        self._requested_scope = self.scope
        self.label = ""
        self._token = 0
        self._busy = False
        self._cursor: tuple[float, str] | None = None
        self._previous: list[tuple[float, str] | None] = []
        self._choices = BatchChoices((), False, True, False)
        self._delivery = FutureDelivery()
        self._delivery.emitter.completed.connect(
            self._completed, Qt.ConnectionType.QueuedConnection
        )
        self.destroyed.connect(
            self._delivery.detach, Qt.ConnectionType.DirectConnection
        )
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(Style.small)
        self.start = OptionalDate("records_from")
        self.end = OptionalDate("records_to")
        self.batch = QPushButton()
        self.batch.setObjectName("recordsBatch")
        self.batch.setFixedSize(208, Style.touch)
        for widget in (self.start, self.end, self.batch):
            row.addWidget(widget)
        self.popup = QMenu(self)
        panel = QWidget()
        panel.setFixedWidth(360)
        content = QVBoxLayout(panel)
        content.setContentsMargins(Style.small, Style.small, Style.small, Style.small)
        self.search = QLineEdit()
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumHeight(Style.touch)
        content.addWidget(self.search)
        self.choices = QListWidget()
        self.choices.setObjectName("reportBatches")
        self.choices.setMinimumHeight(6 * Style.touch)
        content.addWidget(self.choices)
        self.previous, self.next = QPushButton(), QPushButton()
        paging = QHBoxLayout()
        paging.addStretch()
        for button in (self.previous, self.next):
            button.setFixedSize(Style.touch, Style.touch)
            paging.addWidget(button)
        content.addLayout(paging)
        action = QWidgetAction(self.popup)
        action.setDefaultWidget(panel)
        self.popup.addAction(action)
        self.batch.clicked.connect(self._open)
        self.search.textChanged.connect(self._search)
        self.previous.clicked.connect(self._back)
        self.next.clicked.connect(self._next)
        self.choices.itemClicked.connect(self._choose)
        self.choices.itemActivated.connect(self._choose)
        self.start.changed.connect(self._dates)
        self.end.changed.connect(self._dates)
        self.set_language(self.language)

    def _dates(self) -> None:
        self.scope = replace(
            self.scope,
            since=local_day_bounds(self.start.value)[0] if self.start.value else None,
            until=local_day_bounds(self.end.value)[1] if self.end.value else None,
        )
        self.changed.emit()
        self._search()

    def select_batch(
        self, batch: str | None, label: str = "", *, unbatched: bool = False
    ) -> None:
        self.scope = replace(self.scope, batch=batch, unbatched=unbatched)
        self.label = label
        self._caption()
        self.changed.emit()

    def _caption(self) -> None:
        value = (
            self.label
            if self.scope.batch
            else text(
                "records_no_batch" if self.scope.unbatched else "records_all_batches",
                self.language,
            )
        )
        self.batch.setText(
            self.batch.fontMetrics().elidedText(
                value, Qt.TextElideMode.ElideRight, self.batch.width() - 40
            )
        )
        self.batch.setToolTip(value)
        self.batch.setAccessibleName(text("batch_id", self.language) + " · " + value)

    def _open(self) -> None:
        self._search()
        self.popup.popup(self.batch.mapToGlobal(QPoint(0, self.batch.height())))
        self.search.setFocus()

    def _search(self) -> None:
        self._cursor = None
        self._previous.clear()
        self._request()

    def _next(self) -> None:
        if self._choices.more and self._choices.rows:
            self._previous.append(self._cursor)
            self._cursor = self._choices.rows[-1].cursor
            self._request()

    def _back(self) -> None:
        if self._previous:
            self._cursor = self._previous.pop()
            self._request()

    def _request(self) -> None:
        self._token += 1
        self.choices.setEnabled(False)
        self.previous.setEnabled(False)
        self.next.setEnabled(False)
        if not self._busy:
            self._submit()

    def _submit(self) -> None:
        self._requested_scope = self.scope
        session = self.identity.snapshot() if self.identity else None
        self._revision = session.revision if session else None
        if self.history is None:
            self._completed(str(self._token), None, RuntimeError("History unavailable"))
            return
        self._busy = True
        try:
            future = self.history.batch_choices(
                self.scope,
                self.search.text().strip(),
                self._cursor,
                alarm_revision=session.revision
                if session and session.role >= Role.OPERATOR
                else None,
            )
        except RuntimeError as exc:
            self._completed(str(self._token), None, exc)
            return
        self._delivery.watch(str(self._token), future)

    @Slot(str, object, object)
    def _completed(self, token: str, result: object, error: object) -> None:
        self._busy = False
        if token != str(self._token):
            self._submit()
            return
        if self.identity and self.identity.snapshot().revision != self._revision:
            self._request()
            return
        self.choices.clear()
        self.choices.addItem(text("records_all_batches", self.language))
        self.choices.item(0).setData(Qt.ItemDataRole.UserRole, None)
        if isinstance(result, BatchChoices) and error is None:
            self._choices = result
            if not result.selected_matches and self.scope == self._requested_scope:
                self.select_batch(None)
            if result.unbatched:
                self.choices.addItem(text("records_no_batch", self.language))
                self.choices.item(1).setData(Qt.ItemDataRole.UserRole, "unbatched")
            for choice in result.rows:
                self.choices.addItem(choice.label)
                item = self.choices.item(self.choices.count() - 1)
                item.setData(Qt.ItemDataRole.UserRole, choice)
                item.setToolTip(choice.label)
                item.setData(Qt.ItemDataRole.AccessibleTextRole, choice.label)
            self.next.setEnabled(result.more)
            self.previous.setEnabled(bool(self._previous))
        else:
            self._choices = BatchChoices((), False, True, False)
            self.choices.addItem(text("history_unavailable", self.language))
            self.choices.item(1).setFlags(Qt.ItemFlag.NoItemFlags)
        for i in range(self.choices.count()):
            self.choices.item(i).setSizeHint(QSize(300, Style.touch))
            value: object = self.choices.item(i).data(Qt.ItemDataRole.UserRole)
            if (
                isinstance(value, BatchChoice)
                and value.uuid == self.scope.batch
                or value == "unbatched"
                and self.scope.unbatched
                or value is None
                and self.scope.batch is None
                and not self.scope.unbatched
            ):
                self.choices.setCurrentRow(i)
        self.choices.setEnabled(True)

    def _choose(self) -> None:
        if self.choices.currentRow() < 0:
            return
        item = self.choices.currentItem()
        value: object = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(value, BatchChoice):
            self.select_batch(value.uuid, value.label)
        else:
            self.select_batch(None, unbatched=value == "unbatched")
        self.popup.hide()
        self.batch.setFocus()

    def set_language(self, language: Language) -> None:
        self.language = language
        self.batch.setIcon(image_icon("discover", theme_colors(self).primary))
        self.batch.setIconSize(QSize(Style.icon, Style.icon))
        self.start.set_language(language)
        self.end.set_language(language)
        self.search.setPlaceholderText(text("records_find_batch", language))
        self.search.setAccessibleName(self.search.placeholderText())
        self.choices.setAccessibleName(text("batch_id", language))
        for button, icon, key in (
            (self.previous, "previous", "previous_page"),
            (self.next, "next", "next_page"),
        ):
            button.setIcon(image_icon(icon, theme_colors(self).primary))
            button.setToolTip(text(key, language))
            button.setAccessibleName(button.toolTip())
        self._caption()

    def identity_changed(self) -> None:
        self.popup.hide()
        self._choices = BatchChoices((), False, True, False)
        self.choices.clear()
        self._token += 1

    def dispose(self) -> None:
        self.popup.hide()
        self.start.popup.hide()
        self.end.popup.hide()
        self._delivery.detach()
