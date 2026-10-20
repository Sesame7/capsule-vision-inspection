"""Bounded read-only history browsing; all database work stays on its owner."""

from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta

from PySide6.QtCore import (
    QDate,
    QEvent,
    QLocale,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QRect,
    QSize,
    Qt,
    QTimer,
    Slot,
)
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QTextCharFormat
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from vimo_demo.application.history import (
    HistoryChannel,
    HistoryPageData,
    HistoryQuery,
    HistoryRecord,
    ProductionHistory,
    ResultFilter,
)

from .image_tools import image_icon
from .inference_bridge import FutureDelivery
from .production_style import Language, Style, text, theme_colors


class _ResultDelegate(QStyledItemDelegate):
    """Right-aligned two-digit fields, independent of proportional space widths."""

    warnings_role = Qt.ItemDataRole.UserRole + 1

    @staticmethod
    def counts(record: HistoryRecord) -> tuple[tuple[str, int], ...]:
        counts = Counter(c.business for c in record.channels)
        parts = [(state, counts[state]) for state in ("OK", "NG", "EMPTY")]
        if counts["UNKNOWN"]:
            parts.append(("UNKNOWN", counts["UNKNOWN"]))
        errors = sum(c.technical == "ERROR" for c in record.channels)
        if errors:
            parts.append(("ERR", errors))
        return tuple(parts)

    @staticmethod
    def spans(
        metrics: QFontMetrics,
        counts: tuple[tuple[str, int], ...],
        warnings: str = "",
    ) -> tuple[tuple[str, int, Qt.AlignmentFlag], ...]:
        number_width = 2 * max(metrics.horizontalAdvance(str(i)) for i in range(10))
        left, right = Qt.AlignmentFlag.AlignLeft, Qt.AlignmentFlag.AlignRight
        spans: list[tuple[str, int, Qt.AlignmentFlag]] = []
        for label, count in counts:
            if spans:
                spans.append((" · ", metrics.horizontalAdvance(" · "), left))
            spans.extend(
                (
                    (label + " ", metrics.horizontalAdvance(label + " "), left),
                    (
                        str(count),
                        max(number_width, metrics.horizontalAdvance(str(count))),
                        right,
                    ),
                )
            )
        if warnings:
            value = " · " + warnings
            spans.append((value, metrics.horizontalAdvance(value), left))
        return tuple(spans)

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        record: object = index.data(Qt.ItemDataRole.UserRole)
        if not isinstance(record, HistoryRecord):
            super().paint(painter, option, index)
            return
        warnings: object = index.data(self.warnings_role)
        view = QStyleOptionViewItem(option)
        self.initStyleOption(view, index)
        view.text = ""
        option.widget.style().drawControl(
            QStyle.ControlElement.CE_ItemViewItem, view, painter, option.widget
        )
        rect = option.rect.adjusted(Style.small, 0, -Style.small, 0)
        painter.save()
        painter.setClipRect(rect)
        painter.setFont(option.font)
        painter.setPen(QColor(theme_colors(option.widget).primary))
        x = rect.x()
        for value, width, alignment in self.spans(
            option.fontMetrics,
            self.counts(record),
            warnings if isinstance(warnings, str) else "",
        ):
            available = rect.right() - x + 1
            if available <= 0:
                break
            painter.drawText(
                QRect(x, rect.y(), min(width, available), rect.height()),
                alignment | Qt.AlignmentFlag.AlignVCenter,
                option.fontMetrics.elidedText(
                    value, Qt.TextElideMode.ElideRight, available
                ),
            )
            x += width
        painter.restore()


class _ChannelDelegate(QStyledItemDelegate):
    """Two compact lines, keeping business NG and technical ERROR independent."""

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        channel: object = index.data(Qt.ItemDataRole.UserRole)
        if not isinstance(channel, HistoryChannel):
            super().paint(painter, option, index)
            return
        view = QStyleOptionViewItem(option)
        self.initStyleOption(view, index)
        view.text = ""
        option.widget.style().drawControl(
            QStyle.ControlElement.CE_ItemViewItem, view, painter, option.widget
        )
        colors = theme_colors(option.widget)
        rect = option.rect.adjusted(Style.small, 2, -Style.small, -2)
        half = rect.height() // 2
        painter.save()
        business_color = {
            "NG": colors.ng,
            "OK": colors.ok,
            "EMPTY": colors.accent,
        }.get(channel.business, colors.secondary)
        upper = [(f"CH{channel.channel:02d}", colors.primary)]
        lower = [(channel.business, business_color)]
        if channel.technical == "ERROR":
            target = (
                lower
                if option.fontMetrics.horizontalAdvance(channel.business + " · ERR")
                <= rect.width()
                else upper
            )
            target.append((" · ERR", colors.warning))
        for pieces, y, height in (
            (upper, rect.y(), half),
            (lower, rect.y() + half, rect.height() - half),
        ):
            widths = [
                option.fontMetrics.horizontalAdvance(value) for value, _ in pieces
            ]
            x = rect.x() + (rect.width() - sum(widths)) // 2
            for (value, color), width in zip(pieces, widths, strict=True):
                painter.setPen(QColor(color))
                painter.drawText(
                    QRect(x, y, width, height), Qt.AlignmentFlag.AlignVCenter, value
                )
                x += width
        painter.restore()


class HistoryPage(QWidget):
    def __init__(self, owner: ProductionHistory | None) -> None:
        super().__init__()
        self.owner = owner
        self.language: Language = "zh"
        self.busy = False
        self._revision = 0
        self._query = HistoryQuery()
        self._data = HistoryPageData((), (), False)
        self._error = ""
        self._loaded = False
        self._detail_record_id: int | None = None
        self._delivery = FutureDelivery()
        self._delivery.emitter.completed.connect(
            self._completed, Qt.ConnectionType.QueuedConnection
        )
        self.destroyed.connect(
            self._delivery.detach, Qt.ConnectionType.DirectConnection
        )
        outer = QHBoxLayout(self)
        outer.setContentsMargins(Style.space, Style.space, Style.space, Style.space)
        self.workspace = QWidget()
        self.workspace.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        outer.addWidget(self.workspace, 1)
        root = QVBoxLayout(self.workspace)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(Style.small)
        self.toolbar = QWidget()
        self.toolbar_layout = QGridLayout(self.toolbar)
        self.toolbar_layout.setContentsMargins(0, 0, 0, 0)
        self.toolbar_layout.setSpacing(Style.small)
        self.toolbar_layout.setColumnStretch(0, 1)
        self._toolbar_wrapped: bool | None = None
        self.filters = QWidget()
        top = QHBoxLayout(self.filters)
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(Style.small)
        self.date = QDateEdit(QDate.currentDate())
        self.date.setObjectName("historyDate")
        self.date.setCalendarPopup(True)
        self.date.setDisplayFormat("yyyy-MM-dd")
        calendar = self.date.calendarWidget()
        calendar.setObjectName("historyCalendar")
        calendar.setMinimumSize(8 * Style.touch, 8 * Style.touch)
        self.all_dates = QCheckBox()
        self.refresh_button = QPushButton()
        for widget in (self.date, self.all_dates, self.refresh_button):
            widget.setMinimumHeight(Style.touch)
        top.addWidget(self.date)
        top.addWidget(self.all_dates)
        self.batch = QComboBox()
        self.batch.setMinimumHeight(Style.touch)
        self.batch.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.batch.setMinimumContentsLength(12)
        self.result = QComboBox()
        self.result.setMinimumHeight(Style.touch)
        for value in ("all", "OK", "NG", "ERROR", "EMPTY", "UNKNOWN"):
            self.result.addItem(value, value)
        for widget in (self.batch, self.result):
            widget.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
            top.addWidget(widget)
        self.pagination_tools = QWidget()
        actions = QHBoxLayout(self.pagination_tools)
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(Style.small)
        self.newer, self.older = QPushButton(), QPushButton()
        for button in (self.newer, self.older):
            button.setMinimumHeight(Style.touch)
            actions.addWidget(button)
        actions.addWidget(self.refresh_button)
        root.addWidget(self.toolbar)
        self.warning = QLabel()
        self.warning.setTextFormat(Qt.TextFormat.PlainText)
        self.warning.setWordWrap(True)
        root.addWidget(self.warning)
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        root.addWidget(self.status)
        self.cycles = self._table(5, "historyCycles")
        self.cycles.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Fixed
        )
        self.cycles.setItemDelegateForColumn(4, _ResultDelegate(self.cycles))
        self.cycles.viewport().installEventFilter(self)
        root.addWidget(self.cycles, 1)
        self.channels = self._table(7, "historyChannels")
        self.channels.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectItems
        )
        self.channels.setItemDelegate(_ChannelDelegate(self.channels))
        self.channels.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Fixed
        )
        self.channels.viewport().installEventFilter(self)
        root.addWidget(self.channels)
        self.channel_detail = QPlainTextEdit()
        self.channel_detail.setReadOnly(True)
        root.addWidget(self.channel_detail)
        self.date.dateChanged.connect(self.refresh)
        self.all_dates.toggled.connect(self.refresh)
        self.batch.currentIndexChanged.connect(self.refresh)
        self.result.currentIndexChanged.connect(self.refresh)
        self.refresh_button.clicked.connect(self.refresh)
        self.newer.clicked.connect(lambda: self._turn(True))
        self.older.clicked.connect(lambda: self._turn(False))
        self.cycles.itemSelectionChanged.connect(self._selected)
        self.channels.currentCellChanged.connect(self._channel_selected)
        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(
            self._observe
        )  # Scalar diagnostics only, no DB polling.
        self.timer.start()
        self.set_language("zh")

    @staticmethod
    def _table(columns: int, name: str) -> QTableWidget:
        table = QTableWidget(0, columns)
        table.setObjectName(name)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setAlternatingRowColors(True)
        table.setTextElideMode(Qt.TextElideMode.ElideRight)
        table.setWordWrap(False)
        table.verticalHeader().hide()
        table.verticalHeader().setDefaultSectionSize(36)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.setMinimumHeight(150)
        return table

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() is QEvent.Type.Resize and hasattr(self, "channel_detail"):
            self._size_content()
        return super().eventFilter(watched, event)

    def _size_cycles(self) -> None:
        metrics = self.cycles.fontMetrics()
        padding = 2 * Style.small + 8
        time_width = (
            max(
                metrics.horizontalAdvance("12-31 23:59:59.999"),
                max(
                    (
                        metrics.horizontalAdvance(item.text())
                        for row in range(self.cycles.rowCount())
                        if (item := self.cycles.item(row, 0)) is not None
                    ),
                    default=0,
                ),
            )
            + padding
        )
        cycle_width = (
            max(
                max(
                    metrics.horizontalAdvance(text("history_cycle", lang))
                    for lang in ("zh", "en")
                ),
                min(
                    max(
                        (
                            metrics.horizontalAdvance(str(r.cycle_id))
                            for r in self._data.records
                        ),
                        default=0,
                    ),
                    metrics.horizontalAdvance("99999999"),
                ),
            )
            + padding
        )
        counts_width = metrics.horizontalAdvance("OK 36 · NG 36 · EMPTY 36")
        result_width = counts_width
        for record in self._data.records:
            counts = _ResultDelegate.counts(record)
            counts_width = max(
                counts_width,
                sum(width for _, width, _ in _ResultDelegate.spans(metrics, counts)),
            )
            for lang in ("zh", "en"):
                result_width = max(
                    result_width,
                    sum(
                        width
                        for _, width, _ in _ResultDelegate.spans(
                            metrics,
                            counts,
                            " · ".join(self._cycle_warnings(record, lang)),
                        )
                    ),
                )
        names = [
            max(
                max(
                    metrics.horizontalAdvance(text(key, lang)) for lang in ("zh", "en")
                ),
                min(
                    max(
                        (
                            metrics.horizontalAdvance(item.text())
                            for row in range(self.cycles.rowCount())
                            if (item := self.cycles.item(row, column)) is not None
                        ),
                        default=0,
                    ),
                    metrics.horizontalAdvance("M" * 22),
                ),
            )
            + padding
            for column, key in ((2, "history_batch"), (3, "parameters"))
        ]
        minimums = [
            time_width,
            cycle_width,
            max(
                max(
                    metrics.horizontalAdvance(text("history_batch", lang))
                    for lang in ("zh", "en")
                ),
                metrics.horizontalAdvance("000000"),
            )
            + padding,
            max(
                max(
                    metrics.horizontalAdvance(text("parameters", lang))
                    for lang in ("zh", "en")
                ),
                metrics.horizontalAdvance("胶囊A"),
            )
            + padding,
            counts_width + padding,
        ]
        preferred = [time_width, cycle_width, *names, result_width + padding]
        preferred = [max(a, b) for a, b in zip(preferred, minimums, strict=True)]
        available = self.cycles.viewport().width()
        if available >= sum(preferred):
            widths = preferred
            # Surplus benefits every column; results get the largest share.
            weights = [2, 1, 3, 3, 4]
        else:
            widths = minimums
            weights = [p - m for p, m in zip(preferred, minimums, strict=True)]
        surplus = max(0, available - sum(widths))
        total = max(1, sum(weights))
        accumulated = 0
        for column, (width, weight) in enumerate(zip(widths, weights, strict=True)):
            extra = (
                surplus * (accumulated + weight) // total
                - surplus * accumulated // total
            )
            accumulated += weight
            width += extra
            self.cycles.setColumnWidth(column, width)

    def _size_content(self) -> None:
        self._size_cycles()
        metrics = self.cycles.fontMetrics()
        padding = 2 * Style.small + 8
        self.date.setFixedWidth(metrics.horizontalAdvance("2000-12-31") + Style.touch)
        self.all_dates.setFixedWidth(
            max(
                metrics.horizontalAdvance(text("history_all_dates", lang))
                for lang in ("zh", "en")
            )
            + 30
        )
        filter_width = (
            max(
                metrics.horizontalAdvance("12-31 23:59:59"),
                *(
                    metrics.horizontalAdvance(text(key, lang))
                    for key in (
                        "history_all_batches",
                        "history_all",
                        "history_ok",
                        "history_ng",
                        "history_error",
                        "history_empty",
                        "history_unknown",
                    )
                    for lang in ("zh", "en")
                ),
            )
            + 40
        )
        self.batch.setFixedWidth(filter_width)
        self.result.setFixedWidth(filter_width)
        button_width = (
            max(
                metrics.horizontalAdvance(text(key, lang))
                for key in ("history_newer", "history_older", "history_refresh")
                for lang in ("zh", "en")
            )
            + 24
        )
        for button in (self.newer, self.older, self.refresh_button):
            button.setFixedSize(button_width, Style.touch)
        wrapped = (
            self.workspace.width()
            < self.filters.minimumSizeHint().width()
            + self.pagination_tools.minimumSizeHint().width()
            + Style.small
        )
        if wrapped != self._toolbar_wrapped:
            self._toolbar_wrapped = wrapped
            self.toolbar_layout.removeWidget(self.filters)
            self.toolbar_layout.removeWidget(self.pagination_tools)
            self.toolbar_layout.addWidget(
                self.filters, 0, 0, 1, 2 if wrapped else 1, Qt.AlignmentFlag.AlignLeft
            )
            self.toolbar_layout.addWidget(
                self.pagination_tools,
                1 if wrapped else 0,
                0 if wrapped else 1,
                1,
                2 if wrapped else 1,
                Qt.AlignmentFlag.AlignRight,
            )
        row_height = max(Style.touch, 2 * self.channels.fontMetrics().height() + 4)
        self.channels.verticalHeader().setDefaultSectionSize(row_height)
        matrix_metrics = self.channels.fontMetrics()
        self.channels.horizontalHeader().setMinimumSectionSize(
            matrix_metrics.horizontalAdvance("CH36") + Style.small
        )
        camera_width = min(
            max(
                (
                    matrix_metrics.horizontalAdvance(c.camera)
                    for r in self._data.records[:1]
                    for c in r.cameras
                ),
                default=max(
                    matrix_metrics.horizontalAdvance(text("history_camera", lang))
                    for lang in ("zh", "en")
                ),
            )
            + padding,
            matrix_metrics.horizontalAdvance("M" * 10) + padding,
        )
        camera_width = max(
            camera_width,
            min(
                max(2 * Style.touch, self.channels.viewport().width() // 12),
                matrix_metrics.horizontalAdvance("M" * 14) + padding,
            ),
        )
        self.channels.setColumnWidth(0, camera_width)
        cell_minimum = (
            max(
                matrix_metrics.horizontalAdvance("UNKNOWN"),
                matrix_metrics.horizontalAdvance("CH36 · ERR"),
            )
            + 2 * Style.small
        )
        available = self.channels.viewport().width() - camera_width
        total_width = camera_width
        for column in range(1, 7):
            cell_width = max(
                cell_minimum, available * column // 6 - available * (column - 1) // 6
            )
            self.channels.setColumnWidth(column, cell_width)
            total_width += cell_width
        scroll_height = (
            self.channels.horizontalScrollBar().sizeHint().height()
            if total_width > self.channels.viewport().width()
            else 0
        )
        self.channels.setFixedHeight(
            self.channels.horizontalHeader().height()
            + self.channels.rowCount() * row_height
            + 2 * self.channels.frameWidth()
            + scroll_height
        )
        self._size_detail()

    def _size_detail(self) -> None:
        value = self.channel_detail.toPlainText()
        self.channel_detail.setVisible(bool(value))
        if not value:
            return
        metrics = self.channel_detail.fontMetrics()
        margin = int(self.channel_detail.document().documentMargin())
        padding = 2 * self.channel_detail.frameWidth() + 2 * margin + Style.small
        height = metrics.boundingRect(
            QRect(
                0, 0, max(1, self.channel_detail.viewport().width() - 2 * margin), 10000
            ),
            Qt.TextFlag.TextWrapAnywhere,
            value,
        ).height()
        self.channel_detail.setFixedHeight(
            max(metrics.lineSpacing(), min(height, 4 * metrics.lineSpacing())) + padding
        )

    def set_language(self, language: Language) -> None:
        self.language = language
        locale = QLocale("zh_CN" if language == "zh" else "en_US")
        self.date.setLocale(locale)
        calendar = self.date.calendarWidget()
        calendar.setLocale(locale)
        calendar.setFirstDayOfWeek(locale.firstDayOfWeek())
        calendar.setPalette(self.palette())
        header = QTextCharFormat()
        header.setForeground(QColor(theme_colors(self).secondary))
        header.setBackground(QColor(theme_colors(self).raised))
        calendar.setHeaderTextFormat(header)
        for day in (Qt.DayOfWeek.Saturday, Qt.DayOfWeek.Sunday):
            calendar.setWeekdayTextFormat(day, QTextCharFormat())
        for name, icon in (
            ("qt_calendar_prevmonth", "previous"),
            ("qt_calendar_nextmonth", "next"),
        ):
            button = calendar.findChild(QToolButton, name)
            if button is not None:
                button.setIcon(image_icon(icon, theme_colors(self).primary))
                button.setIconSize(QSize(Style.icon, Style.icon))
        self.all_dates.setText(text("history_all_dates", language))
        self.refresh_button.setText(text("history_refresh", language))
        self.date.setAccessibleName(text("history_date", language))
        self.date.setToolTip(text("history_clock", language))
        self.batch.setAccessibleName(text("history_batch", language))
        self.result.setAccessibleName(text("history_result", language))
        self.newer.setText(text("history_newer", language))
        self.older.setText(text("history_older", language))
        self.newer.setToolTip(text("history_newer", language))
        self.older.setToolTip(text("history_older", language))
        for index, key in enumerate(
            (
                "history_all",
                "history_ok",
                "history_ng",
                "history_error",
                "history_empty",
                "history_unknown",
            )
        ):
            self.result.setItemText(index, text(key, language))
        self.cycles.setHorizontalHeaderLabels(
            [
                text(k, language)
                for k in (
                    "history_time",
                    "history_cycle",
                    "history_batch",
                    "parameters",
                    "history_result",
                )
            ]
        )
        self.channels.setHorizontalHeaderLabels(
            [text("history_camera", language), *(str(i + 1) for i in range(6))]
        )
        self.channels.setToolTip(text("history_matrix_key", language))
        self.channel_detail.setAccessibleName(text("history_channel", language))
        self.channel_detail.setPlaceholderText(text("history_select_channel", language))
        self._batches()
        self._present()

    def activate(self) -> None:
        if not self._loaded:
            self.refresh()

    @Slot()
    def refresh(self) -> None:
        self._revision += 1
        self._query = self._filters()
        self.date.setEnabled(not self.all_dates.isChecked())
        self.batch.setToolTip(
            str(
                self.batch.currentData(Qt.ItemDataRole.ToolTipRole)
                or self.batch.currentText()
            )
        )
        if not self.busy:
            self._submit()

    def _filters(self) -> HistoryQuery:
        since = until = None
        if not self.all_dates.isChecked():
            value = self.date.date()
            start = datetime(value.year(), value.month(), value.day())
            since, until = start.timestamp(), (start + timedelta(days=1)).timestamp()
        batch: object = self.batch.currentData()
        result: object = self.result.currentData()
        # All options are owned here; do not interpret arbitrary display text.
        filters: tuple[ResultFilter, ...] = (
            "all",
            "OK",
            "NG",
            "ERROR",
            "EMPTY",
            "UNKNOWN",
        )
        selected: ResultFilter = "all"
        for value in filters:
            if value == result:
                selected = value
                break
        return HistoryQuery(
            since, until, batch if isinstance(batch, str) else None, selected
        )

    def _turn(self, newer: bool) -> None:
        if self.busy or not self._data.records:
            return
        record = self._data.records[0 if newer else -1]
        self._revision += 1
        self._query = replace(
            self._filters(),
            cursor=(record.context.completed_at, record.record_id),
            newer=newer,
        )
        self._submit()

    def _submit(self) -> None:
        if self.owner is None:
            self._error = text("history_unavailable", self.language)
            self._present()
            return
        self.busy = True
        self._error = ""
        self._delivery.watch(str(self._revision), self.owner.query(self._query))
        self._present()

    @Slot(str, object, object)
    def _completed(self, revision: str, data: object, error: object) -> None:
        self.busy = False
        if revision != str(self._revision):
            self._submit()  # Only the latest user filter, never an automatic retry.
            return
        self._loaded = True
        self._error = str(error) if error else ""
        self._data = (
            data
            if isinstance(data, HistoryPageData)
            else HistoryPageData((), (), False)
        )
        self._batches()
        self._present()

    def _batches(self) -> None:
        selected: object = self.batch.currentData()
        self.batch.blockSignals(True)
        self.batch.clear()
        self.batch.addItem(text("history_all_batches", self.language), None)
        stamps = Counter(
            datetime.fromtimestamp(first).strftime("%m-%d %H:%M:%S")
            for _, _, first in self._data.batches
        )
        for segment, label, completed in self._data.batches:
            stamp = datetime.fromtimestamp(completed).strftime("%m-%d %H:%M:%S")
            display = stamp
            if stamps[stamp] > 1:
                length = 6
                while length < len(segment) and any(
                    other != segment and other.startswith(segment[:length])
                    for other, _, _ in self._data.batches
                ):
                    length += 1
                display += f" · {segment[:length]}"
            self.batch.addItem(display, segment)
            self.batch.setItemData(
                self.batch.count() - 1,
                f"{label or '—'}\n{datetime.fromtimestamp(completed).isoformat(timespec='milliseconds')}\n{text('history_segment_observed', self.language)}\nID: {segment}",
                Qt.ItemDataRole.ToolTipRole,
            )
        if isinstance(selected, str):
            index = self.batch.findData(selected)
            if index < 0:
                self.batch.addItem(f"ID: {selected[:8]}", selected)
                index = self.batch.count() - 1
                self.batch.setItemData(index, selected, Qt.ItemDataRole.ToolTipRole)
            self.batch.setCurrentIndex(index)
        self.batch.blockSignals(False)
        current = str(
            self.batch.currentData(Qt.ItemDataRole.ToolTipRole)
            or self.batch.currentText()
        )
        self.batch.setToolTip(current)

    def _present(self) -> None:
        language = self.language
        self.status.setText(
            text(
                "history_loading"
                if self.busy
                else "history_unavailable"
                if self._error
                else "history_none",
                language,
            )
            if self.busy or self._error or not self._data.records
            else ""
        )
        self.status.setVisible(self.busy or bool(self._error) or not self._data.records)
        self.status.setToolTip(self._error or text("history_clock", language))
        self.refresh_button.setEnabled(not self.busy and self.owner is not None)
        self.newer.setEnabled(
            not self.busy
            and bool(self._data.records)
            and (
                self._data.more if self._query.newer else self._query.cursor is not None
            )
        )
        self.older.setEnabled(
            not self.busy
            and bool(self._data.records)
            and (
                self._query.cursor is not None if self._query.newer else self._data.more
            )
        )
        selected = self.cycles.currentRow()
        self.cycles.blockSignals(True)
        self.cycles.setRowCount(len(self._data.records))
        for row, record in enumerate(self._data.records):
            values = (
                datetime.fromtimestamp(record.context.completed_at).strftime(
                    "%m-%d %H:%M:%S.%f"
                )[:-3],
                str(record.cycle_id),
                record.context.batch_label or "—",
                record.context.recipe_name or f"ID: {record.context.active.recipe_id}",
                self._summary(record),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                if column == 1:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    context = record.context
                    item.setToolTip(
                        f"#{record.record_id} · {record.cycle_id}\n{text('history_run', language)} {context.run_id}\n{text('history_batch', language)} {context.batch_label or '—'} · {context.batch_segment}\n{context.recipe_name or text('history_recipe_legacy', language)}\nID: {context.active.recipe_id} · {context.active.model_id}\nModel SHA-256 {context.active.model_sha256}"
                    )
                if column == 0:
                    item.setToolTip(
                        datetime.fromtimestamp(record.context.completed_at).isoformat(
                            timespec="milliseconds"
                        )
                    )
                if column == 4:
                    item.setData(Qt.ItemDataRole.UserRole, record)
                    item.setData(
                        _ResultDelegate.warnings_role,
                        " · ".join(self._cycle_warnings(record)),
                    )
                if column == 3:
                    item.setToolTip(
                        (
                            record.context.recipe_name
                            or text("history_recipe_legacy", language)
                        )
                        + f"\nID: {record.context.active.recipe_id}"
                    )
                self.cycles.setItem(row, column, item)
        if self._data.records:
            self.cycles.selectRow(max(0, min(selected, len(self._data.records) - 1)))
        self.cycles.blockSignals(False)
        self._selected()
        self._size_content()
        self._observe()

    def _summary(self, record: HistoryRecord) -> str:
        parts = [f"{state} {count}" for state, count in _ResultDelegate.counts(record)]
        return " · ".join((*parts, *self._cycle_warnings(record)))

    def _cycle_warnings(
        self, record: HistoryRecord, language: Language | None = None
    ) -> tuple[str, ...]:
        language = language or self.language
        warnings: list[str] = []
        if record.status != "COMPLETE":
            warnings.append(record.status)
        if record.technical == "ERROR" and not any(
            c.technical == "ERROR" for c in record.channels
        ):
            warnings.append(text("history_cycle_error", language))
        elif record.technical == "DISABLED":
            warnings.append(text("history_cycle_disabled", language))
        if record.judgment == "NG" and not any(
            c.business == "NG" for c in record.channels
        ):
            warnings.append(text("history_ng", language))
        elif record.judgment == "UNKNOWN" and not any(
            c.business == "UNKNOWN" for c in record.channels
        ):
            warnings.append(text("history_cycle_unknown", language))
        return tuple(warnings)

    @Slot()
    def _selected(self) -> None:
        index = self.cycles.currentRow()
        if not 0 <= index < len(self._data.records):
            self._detail_record_id = None
            self.channels.setRowCount(0)
            self.channel_detail.clear()
            self._size_detail()
            return
        record = self._data.records[index]
        old_item = self.channels.currentItem()
        old_channel: object = (
            old_item.data(Qt.ItemDataRole.UserRole) if old_item else None
        )
        same_record = self._detail_record_id == record.record_id
        self._detail_record_id = record.record_id
        selected = (0, 1)
        self.channels.blockSignals(True)
        self.channels.clearContents()
        self.channels.setRowCount(len(record.cameras))
        for row, camera in enumerate(record.cameras):
            item = QTableWidgetItem(camera.camera)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            camera_details = [camera.camera]
            if camera.technical != "SUCCESS":
                camera_details.append(camera.technical)
            if camera.judgment != "OK":
                camera_details.append(camera.judgment)
            camera_details.extend(camera.reasons)
            if camera.unassigned_labels:
                camera_details.append(
                    f"{text('history_unassigned', self.language)}: {', '.join(camera.unassigned_labels)}"
                )
            item.setToolTip(" · ".join(camera_details))
            self.channels.setItem(row, 0, item)
            for channel in record.channels:
                if channel.camera == camera.camera and 0 <= channel.slot < 6:
                    if same_record and channel == old_channel:
                        selected = (row, channel.slot + 1)
                    item = QTableWidgetItem(
                        f"CH{channel.channel:02d}\n{channel.business}"
                        + (" · ERR" if channel.technical == "ERROR" else "")
                    )
                    item.setData(Qt.ItemDataRole.UserRole, channel)
                    item.setToolTip(self._channel_text(channel))
                    self.channels.setItem(row, channel.slot + 1, item)
        self.channels.setCurrentCell(*selected)
        self.channels.blockSignals(False)
        self._channel_selected()
        self._size_content()

    def _channel_text(self, channel: HistoryChannel) -> str:
        parts = [channel.camera, f"CH{channel.channel:02d}", channel.business]
        if channel.technical != "SUCCESS":
            parts.append(channel.technical)
        if channel.business == "UNKNOWN" and channel.judgment != "UNKNOWN":
            parts.append(
                f"{text('history_judgment', self.language)}: {channel.judgment}"
            )
        value = " · ".join(parts)
        if channel.reasons:
            value += "\n" + ", ".join(channel.reasons)
        return value

    @Slot()
    def _channel_selected(self) -> None:
        item = self.channels.currentItem()
        channel: object = item.data(Qt.ItemDataRole.UserRole) if item else None
        self.channel_detail.setPlainText(
            self._channel_text(channel) if isinstance(channel, HistoryChannel) else ""
        )
        self._size_detail()

    @Slot()
    def _observe(self) -> None:
        observation = self.owner.snapshot() if self.owner else None
        warning = observation is not None and (
            observation.dropped or observation.failed or observation.error
        )
        self.warning.setVisible(bool(warning))
        self.warning.setText(
            text("history_warning", self.language).format(
                lost=observation.dropped + observation.failed if observation else 0,
                saved=observation.persisted if observation else 0,
            )
        )
        self.warning.setToolTip(observation.error if observation else "")
        self.warning.setStyleSheet(f"color: {theme_colors(self).warning};")

    def dispose(self) -> None:
        self.timer.stop()
        self._delivery.detach()
