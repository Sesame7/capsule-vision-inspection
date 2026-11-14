"""Bounded read-only history browsing; all database work stays on its owner."""

from collections import Counter
from dataclasses import replace
from math import ceil
from pathlib import Path

from PySide6.QtCore import (
    QEvent,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QRect,
    QRectF,
    QSignalBlocker,
    QSize,
    Qt,
    QTimer,
    Slot,
)
from PySide6.QtGui import (
    QColor,
    QFontMetrics,
    QImageReader,
    QPainter,
    QPainterPath,
    QRegion,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSizePolicy,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from capsule_vision.application import ProductionApplication
from capsule_vision.application.alarm_report import AlarmReportRequest
from capsule_vision.application.audit_report import AuditReportRequest
from capsule_vision.application.history import (
    HistoryCamera,
    HistoryChannel,
    HistoryPageData,
    HistoryQuery,
    HistoryRecord,
    ProductionHistory,
)
from capsule_vision.application.identity_models import Role
from capsule_vision.application.records_query import RecordsScope
from capsule_vision.application.report_scope import ALL_RESULTS
from capsule_vision.localization.catalog import CAPSULE_REASON_KEYS, text
from capsule_vision.localization.languages import Language

from .future_delivery import FutureDelivery
from .history_event_table import (
    fit_record_columns,
    readable_value,
    record_time,
)
from .history_image import HistoryImageDialog
from .history_result_filter import HistoryResultFilter
from .image_tools import image_icon
from .permissions import permits
from .report_dialog import ReportDialog
from .style import Style, button_width, theme_colors

_CYCLE_COLUMNS = (
    ("short_time", "12-31 23:59:59.999", "12-31 23:59:59.999", 1),
    ("records_cycle", "9999", "99999", 1),
    ("history_group", "9999 · 4/4", "9999 · 4/4", 1),
    ("batch_id", "DEV-LOT-F", "LOT-20261020-003", 6),
    ("page_recipe", "Yellow", "黄色胶囊-规格B-灯检方案", 7),
    (
        "audit_outcome",
        "OK 36 · NG 0 · EMPTY 6",
        "OK 12 · NG 13 · EMPTY 10 · UNKNOWN 1 · ERR 12",
        4,
    ),
)


def _inspection_reason(code: str, language: Language) -> str:
    keys = {
        "defect_region": "records_model_defect",
        "execution_error": "execution_error",
        "invalid_result": "invalid_result",
        "result_unavailable": "result_unavailable",
        "cycle_timed_out": "cycle_timeout",
        "cycle_failed": "records_cycle_failed",
        "cycle_shutdown": "cycle_shutdown",
        "basler.shutdown_unfinished": "cycle_shutdown",
        "basler.missing_camera": "records_missing_frame",
        "missing_frame": "records_missing_frame",
        "basler.inspection_deadline": "records_deadline",
    }
    if code in keys:
        return text(keys[code], language)
    if code == "camera_collection_error":
        return f"{text('collection_short', language)} · {text('execution_error', language)}"
    parts = code.split(".")
    if (
        len(parts) == 2
        and parts[0] in ("occupancy", "capsule")
        and parts[1] in ("execution_error", "result_unavailable")
    ):
        check = "records_occupancy" if parts[0] == "occupancy" else "traditional_short"
        return f"{text(check, language)} · {text(parts[1], language)}"
    if (
        parts[0] == "capsule"
        and (len(parts) == 3 or len(parts) == 4 and parts[1] in ("size", "color"))
        and parts[-2] in ("upper", "lower")
        and parts[-1] in CAPSULE_REASON_KEYS
    ):
        return text(CAPSULE_REASON_KEYS[parts[-1]], language)
    # Preserve free-form explanations from older records. Opaque codes stay in History.
    if (
        code.isascii()
        and not any(c.isspace() for c in code)
        and any(c in code for c in "_.:")
    ):
        return ""
    return readable_value(code)


class _ResultDelegate(QStyledItemDelegate):
    """Right-aligned two-digit fields, independent of proportional space widths."""

    warnings_role = Qt.ItemDataRole.UserRole + 1

    @staticmethod
    def counts(record: HistoryRecord) -> tuple[tuple[str, int], ...]:
        counts = Counter(c.business for c in record.channels)
        parts = [(state, counts[state]) for state in ("OK", "NG")]
        parts.extend(
            (state, counts[state]) for state in ("EMPTY", "UNKNOWN") if counts[state]
        )
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
        spans = self.spans(
            option.fontMetrics,
            self.counts(record),
            warnings if isinstance(warnings, str) else "",
        )
        if sum(width for _, width, _ in spans) > rect.width():
            caption = option.fontMetrics.elidedText(
                str(index.data()), Qt.TextElideMode.ElideRight, rect.width()
            )
            painter.drawText(
                rect,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                caption,
            )
            painter.restore()
            return
        x = rect.x()
        for value, width, alignment in spans:
            painter.drawText(
                QRect(x, rect.y(), width, rect.height()),
                alignment | Qt.AlignmentFlag.AlignVCenter,
                value,
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
    def __init__(
        self,
        owner: ProductionHistory | None,
        application: ProductionApplication | None = None,
    ) -> None:
        super().__init__()
        self.owner = owner
        self.application = application
        self.export_available = True
        self.export_batch: tuple[str, str] | None = None
        self.export_audit: AuditReportRequest | None = None
        self.export_audit_label = ""
        self.export_alarm: AlarmReportRequest | None = None
        self.export_alarm_label = ""
        self.export_requires_batch = False
        self.scope = RecordsScope()
        self.report_dialog: ReportDialog | None = None
        self.image_dialog: HistoryImageDialog | None = None
        self.language: Language = "zh"
        self.busy = False
        self._revision = 0
        self._query = HistoryQuery()
        self._data = HistoryPageData((), (), False)
        self._error = ""
        self._loaded = False
        self._detail_record_id: int | None = None
        self._image_issue: tuple[int, str, Path, str] | None = None
        self._delivery = FutureDelivery()
        self._delivery.emitter.completed.connect(
            self._completed, Qt.ConnectionType.QueuedConnection
        )
        self.destroyed.connect(
            self._delivery.detach, Qt.ConnectionType.DirectConnection
        )
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.workspace = QWidget()
        self.workspace.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        outer.addWidget(self.workspace, 1)
        root = QVBoxLayout(self.workspace)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(Style.small)
        self.toolbar = QWidget()
        # Wide filter allocations must not raise a hidden page's window minimum.
        self.toolbar.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self.toolbar_layout = QGridLayout(self.toolbar)
        self.toolbar_layout.setContentsMargins(0, 0, 0, 0)
        self.toolbar_layout.setSpacing(Style.small)
        self.toolbar_layout.setColumnStretch(0, 1)
        self._toolbar_wrapped: bool | None = None
        self.filters = QWidget()
        top = self.filter_bar = QHBoxLayout(self.filters)
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(Style.small)
        self.refresh_button = QPushButton()
        self.refresh_button.setMinimumHeight(Style.touch)
        self.result = HistoryResultFilter()
        self.result.setMinimumHeight(Style.touch)
        for widget in (self.result,):
            widget.setObjectName("recordFilter")
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
        self.export_button = QPushButton(self)
        self.export_button.setMinimumHeight(Style.touch)
        self.export_button.clicked.connect(self._open_export)
        self.export_button.hide()  # The Records tab widget hosts this shared action.
        root.addWidget(self.toolbar)
        self.warning = QLabel()
        self.warning.setTextFormat(Qt.TextFormat.PlainText)
        self.warning.setWordWrap(True)
        root.addWidget(self.warning)
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        root.addWidget(self.status)
        self.cycles = self._table(6, "historyCycles")
        self.cycles.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.cycles.horizontalHeader().setTextElideMode(Qt.TextElideMode.ElideRight)
        self.cycles.horizontalHeader().setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self.cycles.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Fixed
        )
        self.cycles.setItemDelegateForColumn(5, _ResultDelegate(self.cycles))
        root.addWidget(self.cycles, 1)
        self.channels = self._table(7, "historyChannels")
        self.channels.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectItems
        )
        self.channels.setItemDelegate(_ChannelDelegate(self.channels))
        self.channels.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Fixed
        )
        root.addWidget(self.channels)
        self.channel_detail = QTextEdit()
        self.channel_detail.setObjectName("channelEvidence")
        self.channel_detail.setTabChangesFocus(True)
        self.channel_detail.setReadOnly(True)
        self.channel_detail.document().documentLayout().documentSizeChanged.connect(
            self._size_detail, Qt.ConnectionType.QueuedConnection
        )
        detail = QHBoxLayout()
        detail.addWidget(self.channel_detail, 1)
        self.image_button = QPushButton()
        self.image_button.setMinimumHeight(Style.touch)
        self.image_button.clicked.connect(self._open_image)
        detail.addWidget(
            self.image_button,
            0,
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop,
        )
        root.addLayout(detail)
        for table in (self.cycles, self.channels):
            for child in table.findChildren(
                QWidget, options=Qt.FindChildOption.FindDirectChildrenOnly
            ):
                child.installEventFilter(self)
        self.result.changed.connect(self.refresh)
        self.refresh_button.clicked.connect(self.refresh)
        self.newer.clicked.connect(lambda: self._turn(True))
        self.older.clicked.connect(lambda: self._turn(False))
        self.cycles.itemSelectionChanged.connect(self._selected)
        self.channels.currentCellChanged.connect(self._channel_selected)
        self.channels.itemSelectionChanged.connect(self._channel_selected)
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
        if hasattr(self, "channel_detail"):
            if event.type() in (
                QEvent.Type.Resize,
                QEvent.Type.FontChange,
            ) and watched in (
                self.cycles.viewport(),
                self.channels.viewport(),
            ):
                self._size_content()
            if event.type() in (QEvent.Type.Resize, QEvent.Type.Move):
                for table in (self.cycles, self.channels):
                    if watched.parent() is table:
                        self._clip_table_contents(table)
        return super().eventFilter(watched, event)

    @staticmethod
    def _clip_table_contents(table: QTableWidget) -> None:
        # QSS rounds the frame background, but does not clip child headers,
        # viewports or scrollbar containers. Keep them inside the same border
        # without adding padding or changing the accepted table geometry.
        inset = table.frameWidth()
        path = QPainterPath()
        radius = max(0, Style.radius - inset)
        path.addRoundedRect(
            QRectF(table.rect().adjusted(inset, inset, -inset, -inset)),
            radius,
            radius,
        )
        interior = QRegion(path.toFillPolygon().toPolygon())
        for child in table.findChildren(
            QWidget, options=Qt.FindChildOption.FindDirectChildrenOnly
        ):
            child.setMask(
                interior.translated(-child.x(), -child.y()).intersected(child.rect())
            )

    def _size_content(self) -> None:
        fit_record_columns(self.cycles, _CYCLE_COLUMNS)
        padding = 2 * Style.small + 8
        width = Style.touch
        for button, key in (
            (self.newer, "history_newer"),
            (self.older, "history_older"),
            (self.refresh_button, "history_refresh"),
        ):
            button.setText("")
            button.setFixedSize(Style.touch, Style.touch)
            button.setIconSize(QSize(Style.icon, Style.icon))
            button.setIcon(
                image_icon(
                    "previous"
                    if button is self.newer
                    else "next"
                    if button is self.older
                    else "refresh",
                    theme_colors(self).primary,
                )
            )
            button.setAccessibleName(text(key, self.language))
            button.setToolTip(text(key, self.language))
        self.result.setFixedWidth(176)
        desired_row = self.filter_bar.sizeHint().width()
        self.image_button.setFixedWidth(
            button_width(self.image_button, ("history_open_image",))
        )
        action_width = 3 * width + 3 * Style.small
        wrapped = self.workspace.width() < desired_row + action_width
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
                    for lang in ("zh", "en", self.language)
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

    @Slot()
    def _size_detail(self) -> None:
        detail = self.channel_detail
        detail.setVisible(bool(detail.toPlainText()))
        if detail.isHidden():
            return
        document = detail.document()
        max_height = 4 * detail.fontMetrics().lineSpacing() + ceil(
            2 * document.documentMargin()
        )
        detail.setFixedHeight(
            min(ceil(document.size().height()), max_height)
            + 2 * detail.frameWidth()
            + Style.small
        )

    def set_language(self, language: Language) -> None:
        self.language = language
        self.result.set_language(language)
        self.export_button.setText(text("report_export", language))
        self.export_button.setFixedWidth(
            button_width(self.export_button, ("report_export",))
        )
        self.image_button.setText(text("history_open_image", language))
        self.image_button.setAccessibleName(self.image_button.text())
        self.identity_changed()
        if self.report_dialog is not None:
            self.report_dialog.set_language(language)
        full_headers = (
            "history_time",
            "history_cycle",
            "history_group",
            "batch_id",
            "page_recipe",
            "history_result",
        )
        for column, ((key, _, _, _), full) in enumerate(
            zip(_CYCLE_COLUMNS, full_headers, strict=True)
        ):
            item = QTableWidgetItem(text(key, language))
            if column == 1:
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            description = text(full, language) + (
                "\n" + text("history_clock", language) if column == 0 else ""
            )
            item.setToolTip(description)
            item.setData(Qt.ItemDataRole.AccessibleTextRole, description)
            self.cycles.setHorizontalHeaderItem(column, item)
        self.channels.setHorizontalHeaderLabels(
            ["CAM", *(str(i + 1) for i in range(6))]
        )
        camera_header = self.channels.horizontalHeaderItem(0)
        assert camera_header is not None
        camera_header.setToolTip(text("history_camera", language))
        self.channel_detail.setAccessibleName(text("history_channel", language))
        self.channel_detail.setPlaceholderText(text("history_select_channel", language))
        self._present()

    def activate(self) -> None:
        if not self._loaded and not self.busy:
            self.refresh()

    def view_batch(self, batch: str, label: str) -> None:
        """Open this exact Batch without changing the shared calendar bounds."""
        self.scope = replace(self.scope, batch=batch, unbatched=False)
        with QSignalBlocker(self.result):
            self.result.set_selected(ALL_RESULTS)
        self.refresh()

    def identity_changed(self) -> None:
        app = self.application
        audit = self.export_audit is not None
        allowed = app is not None and permits(
            app, Role.ENGINEER if audit else Role.OPERATOR
        )
        available = (
            (app is not None and app.identity is not None)
            if audit
            else self.owner is not None
        )
        self.export_button.setEnabled(
            available
            and allowed
            and self.export_available
            and (not self.export_requires_batch or self.export_batch is not None)
        )
        self.export_button.setToolTip(
            text(
                "history_unavailable"
                if not available or app is None
                else "customer_deferred"
                if not self.export_available and allowed
                else "customer_select_batch"
                if self.export_requires_batch and self.export_batch is None and allowed
                else "report_export"
                if allowed
                else "permission_engineer"
                if audit
                else "permission_operator",
                self.language,
            )
        )
        if (
            self.report_dialog is not None
            and app is not None
            and self.report_dialog.revision != app.session_revision
        ):
            self.report_dialog.reject()

    def _open_export(self) -> None:
        if self.application is None or not self.export_button.isEnabled():
            return
        if self.report_dialog is None:
            self.report_dialog = ReportDialog(
                self.application,
                self.language,
                self,
                self._filters(),
                self.export_batch,
                audit=self.export_audit,
                audit_label=self.export_audit_label,
                alarm=self.export_alarm,
                alarm_label=self.export_alarm_label,
            )
            self.report_dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            self.report_dialog.finished.connect(self._report_closed)
        self.report_dialog.show()
        self.report_dialog.raise_()
        self.report_dialog.activateWindow()

    def _report_closed(self) -> None:
        self.report_dialog = None

    @Slot()
    def refresh(self) -> None:
        self._revision += 1
        self._query = self._filters()
        if not self.busy:
            self._submit()

    def _filters(self) -> HistoryQuery:
        return HistoryQuery(
            self.scope.since,
            self.scope.until,
            self.scope.batch,
            self.result.selected,
            unbatched=self.scope.unbatched,
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
        self._present()

    def _present(self) -> None:
        language = self.language
        unavailable = self.owner is None or bool(self._error)
        self.status.setText(
            text(
                "history_loading"
                if self.busy
                else "history_unavailable"
                if unavailable
                else "history_none",
                language,
            )
            if self.busy or unavailable or not self._data.records
            else ""
        )
        self.status.setVisible(bool(self.status.text()))
        self.status.setToolTip(self._error)
        self.status.setAccessibleDescription(self._error)
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
            clock, full_time = record_time(
                record.context.completed_at, milliseconds=True
            )
            values = (
                clock,
                str(record.cycle_id),
                self._group_caption(record),
                readable_value(record.context.batch_label) or "—",
                readable_value(record.context.recipe_name) or "—",
                self._summary(record),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setToolTip(full_time)
                if column == 3:
                    item.setToolTip(record.context.batch_label or value)
                if column == 2:
                    description = (
                        f"{text('records_run', language)} ID: {record.context.run_id}\n"
                        f"{text('history_group', language)}: {value}"
                    )
                    item.setToolTip(description)
                if column == 1:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    item.setToolTip(value)
                if column == 5:
                    item.setToolTip(value)
                    item.setData(Qt.ItemDataRole.AccessibleTextRole, value)
                    item.setData(Qt.ItemDataRole.UserRole, record)
                    item.setData(
                        _ResultDelegate.warnings_role,
                        " · ".join(self._cycle_warnings(record)),
                    )
                if column == 4:
                    item.setToolTip(
                        (
                            record.context.recipe_name
                            or text("history_recipe_name_missing", language)
                        )
                        + f"\nID: {record.context.active.recipe_id}"
                    )
                if item.toolTip():
                    item.setData(
                        Qt.ItemDataRole.AccessibleDescriptionRole, item.toolTip()
                    )
                self.cycles.setItem(row, column, item)
        if self._data.records:
            self.cycles.selectRow(max(0, min(selected, len(self._data.records) - 1)))
        self.cycles.blockSignals(False)
        self._selected()
        self._size_content()
        self._observe()

    @staticmethod
    def _group_caption(record: HistoryRecord) -> str:
        if (
            record.group_first_cycle is None
            or record.group_slot is None
            or record.group_size not in (1, 4)
        ):
            return "—"
        return f"{record.group_first_cycle} · {record.group_slot}/{record.group_size}"

    def _summary(self, record: HistoryRecord) -> str:
        parts = [f"{state} {count}" for state, count in _ResultDelegate.counts(record)]
        return " · ".join((*parts, *self._cycle_warnings(record)))

    def _cycle_warnings(
        self, record: HistoryRecord, language: Language | None = None
    ) -> tuple[str, ...]:
        language = language or self.language
        warnings: list[str] = []
        if record.status != "COMPLETE":
            status_key = {
                "FAILED": "records_cycle_failed",
                "TIMED_OUT": "cycle_timeout",
                "SHUTDOWN": "cycle_shutdown",
            }.get(record.status)
            warnings.append(text(status_key, language) if status_key else record.status)
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
        if not self.cycles.selectedItems() or not 0 <= index < len(self._data.records):
            self._detail_record_id = None
            self.channels.setRowCount(0)
            self.channel_detail.clear()
            self._channel_selected()
            return
        record = self._data.records[index]
        old_item = self.channels.currentItem()
        old_channel: object = (
            old_item.data(Qt.ItemDataRole.UserRole) if old_item else None
        )
        old_camera = old_item.text() if old_item and old_item.column() == 0 else None
        same_record = self._detail_record_id == record.record_id
        self._detail_record_id = record.record_id
        selected = (0, 1)
        self.channels.blockSignals(True)
        self.channels.clearContents()
        self.channels.setRowCount(len(record.cameras))
        for row, camera in enumerate(record.cameras):
            item = QTableWidgetItem(camera.camera)
            if same_record and old_camera == camera.camera:
                selected = (row, 0)
            item.setData(Qt.ItemDataRole.UserRole, camera)
            item.setToolTip(camera.camera)
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
                    self.channels.setItem(row, channel.slot + 1, item)
        self.channels.setCurrentCell(*selected)
        self.channels.blockSignals(False)
        self._channel_selected()
        self._size_content()

    @Slot()
    def _channel_selected(self) -> None:
        item = self.channels.currentItem()
        selected: object = (
            item.data(Qt.ItemDataRole.UserRole) if item and item.isSelected() else None
        )
        reasons: list[str] = []
        source = self._selected_camera()
        if isinstance(selected, (HistoryChannel, HistoryCamera)):
            labels: tuple[str, ...] = ()
            if isinstance(selected, HistoryChannel):
                evidence = source[1].image_evidence if source else None
                if evidence is not None:
                    labels = tuple(
                        finding.label
                        for finding in evidence.findings
                        if selected.channel in finding.channels
                    )
            reasons.extend(labels)
            reasons.extend(
                _inspection_reason(code, self.language)
                for code in selected.reasons
                if code != "defect_region" or not labels
            )
            if isinstance(selected, HistoryCamera) and selected.unassigned_labels:
                unassigned = ", ".join(selected.unassigned_labels)
                reasons.append(
                    text("records_unassigned", self.language).format(labels=unassigned)
                )
        separator = "\n"
        if isinstance(selected, HistoryChannel):
            separator = "、" if self.language == "zh" else ", "
        detail = separator.join(dict.fromkeys(filter(None, reasons)))
        if self._image_issue is not None:
            record_id, camera, path, key = self._image_issue
            if source and (
                source[0].record_id,
                source[1].camera,
                source[1].image_path,
            ) == (
                record_id,
                camera,
                path,
            ):
                detail = "\n".join(filter(None, (detail, text(key, self.language))))
            else:
                self._image_issue = None
        self.channel_detail.setPlainText(detail)
        self._image_selection_changed()
        self._size_detail()

    @Slot()
    def _image_selection_changed(self) -> None:
        source = self._selected_camera()
        path = source[1].image_path if source is not None and not self.busy else None
        self.image_button.setEnabled(path is not None)
        description = (
            str(path) if path is not None else text("history_no_image", self.language)
        )
        self.image_button.setToolTip(description)
        self.image_button.setAccessibleDescription(description)

    def _selected_camera(self) -> tuple[HistoryRecord, HistoryCamera] | None:
        cycle, camera = self.cycles.currentRow(), self.channels.currentRow()
        if (
            not self.cycles.selectedItems()
            or not self.channels.selectedItems()
            or not 0 <= cycle < len(self._data.records)
        ):
            return None
        record = self._data.records[cycle]
        if 0 <= camera < len(record.cameras):
            return record, record.cameras[camera]
        return None

    @Slot()
    def _open_image(self) -> None:
        source = self._selected_camera()
        if source is None or self.busy or self.image_dialog is not None:
            return
        record, camera = source
        path = camera.image_path
        if path is None:
            return
        self._image_issue = None
        self._channel_selected()
        if not path.is_absolute() or path.suffix.lower() != ".png":
            self._image_error(
                record.record_id, camera.camera, path, "history_image_missing"
            )
            return
        reader = QImageReader(str(path), b"png")
        reader.setAutoDetectImageFormat(False)
        reader.setAutoTransform(False)
        image = reader.read()
        if image.isNull():
            issue = "history_image_open_failed"
            try:
                path.stat()
            except FileNotFoundError:
                issue = "history_image_missing"
            except OSError:
                pass
            self._image_error(record.record_id, camera.camera, path, issue)
            return
        evidence = camera.image_evidence
        if (
            evidence is not None
            and evidence.size is not None
            and evidence.size != (image.width(), image.height())
        ):
            self._image_error(
                record.record_id, camera.camera, path, "history_image_size_mismatch"
            )
            return
        language = self.language
        title = (
            f"{text('history_archived_image', language)} · {camera.camera} · "
            f"{text('history_cycle', language)} {record.cycle_id}"
        )
        time_caption, _ = record_time(record.context.completed_at, milliseconds=True)
        item = self.channels.currentItem()
        channel: object = item.data(Qt.ItemDataRole.UserRole) if item else None
        parts = [time_caption]
        if isinstance(channel, HistoryChannel):
            parts.append(f"CH{channel.channel:02d}")
        parts.extend(self.channel_detail.toPlainText().splitlines())
        self.image_dialog = HistoryImageDialog(
            self,
            image,
            evidence.findings
            if evidence is not None and evidence.size is not None
            else (),
            title,
            " · ".join(parts),
        )
        self.image_dialog.finished.connect(self._image_closed)
        self.image_dialog.open()

    def _image_error(self, record_id: int, camera: str, path: Path, key: str) -> None:
        self._image_issue = record_id, camera, path, key
        self._channel_selected()

    def _image_closed(self) -> None:
        self.image_dialog = None

    @Slot()
    def _observe(self) -> None:
        observation = self.owner.snapshot() if self.owner else None
        warning = observation is not None and (
            observation.dropped or observation.failed or observation.error
        )
        identity = self.application.identity if self.application else None
        lost, issue = identity.audit.observation() if identity else (0, "")
        image_issue = observation.image_link_error if observation else ""
        self.warning.setVisible(bool(warning or issue or image_issue))
        history_text = (
            text("history_warning", self.language).format(
                lost=observation.dropped + observation.failed if observation else 0,
                saved=observation.persisted if observation else 0,
            )
            if warning
            else ""
        )
        self.warning.setText(
            "\n".join(
                x
                for x in (
                    history_text,
                    text("history_image_link_warning", self.language).format(
                        count=observation.image_links_lost
                    )
                    if image_issue and observation
                    else "",
                    text("audit_warning", self.language).format(count=lost)
                    if issue
                    else "",
                )
                if x
            )
        )
        self.warning.setToolTip(
            "\n".join(
                x
                for x in (observation.error if observation else "", image_issue, issue)
                if x
            )
        )
        self.warning.setStyleSheet(f"color: {theme_colors(self).warning};")

    def dispose(self) -> None:
        if self.image_dialog is not None:
            self.image_dialog.reject()
        if self.report_dialog is not None:
            self.report_dialog.reject()
        self.timer.stop()
        self._delivery.detach()
