"""Saved auxiliary status controls and read-only evidence; no file or PLC I/O."""

from datetime import UTC, datetime

from PySide6.QtCore import QEvent, QObject, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from capsule_vision.application.alarm_monitor import AlarmSnapshot
from capsule_vision.communication.fins import FinsMemoryArea
from capsule_vision.configuration.alarm_definitions import AlarmDefinitions
from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import Language

from .image_tools import ImageToolbar, image_icon
from .style import (
    Style,
    button_width,
    production_icon,
    set_button_caption,
    set_service_status,
    set_service_title,
    shell_icon,
    theme_colors,
)


class AlarmMonitorPanel(QFrame):
    preview_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.language: Language = "zh"
        self.snapshot = AlarmSnapshot()
        self.preview: AlarmDefinitions | None = None
        self.preview_issue = ""
        self.can_manage = True
        self._columns = (0, 0)
        self.setObjectName("commPanel")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        root = QVBoxLayout(self)
        root.setContentsMargins(Style.space, Style.space, Style.space, Style.space)
        root.setSpacing(Style.small)
        header = QHBoxLayout()
        header.setSpacing(Style.small)
        self.title_icon = QLabel()
        header.addWidget(self.title_icon)
        self.title = QLabel()
        self.title.setStyleSheet("font-size: 18px; font-weight: 600;")
        self.status_indicator = QLabel()
        self.status = QLabel()
        self.active_count = QLabel()
        self.enabled = QPushButton()
        self.enabled.setCheckable(True)
        self.enabled.setObjectName("alarmPower")
        self.enabled.setProperty("compactAction", True)
        self.enabled.setProperty("iconAlignment", "leading")
        self.enabled.setMinimumHeight(Style.touch)
        self.expand = QPushButton()
        self.expand.setCheckable(True)
        self.expand.setFixedSize(Style.touch, Style.touch)
        self.expand.setIconSize(QSize(Style.icon, Style.icon))
        self.expand.setProperty("compactAction", True)
        header.addWidget(self.title)
        header.addWidget(self.status_indicator)
        header.addWidget(self.status)
        header.addStretch()
        header.addWidget(self.enabled)
        root.addLayout(header)

        self.protocol = QLineEdit("FINS/TCP")
        self.protocol.setReadOnly(True)
        self.host = QLineEdit("127.0.0.1")
        self.host.setMaxLength(64)
        self.host.setMinimumWidth(164)
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(9600)
        self.address = QSpinBox()
        self.address.setRange(0, FinsMemoryArea.W.word_count - 1)
        self.address.setValue(18)
        self.address.setPrefix("W")
        self.heartbeat_address = QSpinBox()
        self.heartbeat_address.setRange(0, FinsMemoryArea.W.word_count - 1)
        self.heartbeat_address.setValue(46)
        self.heartbeat_address.setPrefix("W")
        self.fault_address = QSpinBox()
        self.fault_address.setRange(0, FinsMemoryArea.W.word_count - 1)
        self.fault_address.setValue(47)
        self.fault_address.setPrefix("W")
        self.running_address = QSpinBox()
        self.running_address.setRange(0, FinsMemoryArea.W.word_count - 1)
        self.running_address.setValue(16)
        self.running_address.setPrefix("W")
        self.running_address.setSuffix(".1")
        self._captions: list[tuple[str, QLabel]] = []
        self.controls = QWidget()
        settings = QVBoxLayout(self.controls)
        settings.setContentsMargins(0, 0, 0, 0)
        settings.setSpacing(Style.space)
        self.settings_layout = QGridLayout()
        self.address_layout = QGridLayout()
        for grid in (self.settings_layout, self.address_layout):
            grid.setContentsMargins(0, 0, 0, 0)
            grid.setSpacing(Style.space)
            settings.addLayout(grid)
        self._groups: list[QWidget] = []
        for key, widget in (
            ("comm_protocol", self.protocol),
            ("comm_host", self.host),
            ("comm_port", self.port),
            ("status_heartbeat", self.heartbeat_address),
            ("status_fault", self.fault_address),
            ("status_alarm_address", self.address),
            ("status_running", self.running_address),
        ):
            group = QWidget()
            column = QVBoxLayout(group)
            column.setContentsMargins(0, 0, 0, 0)
            column.setSpacing(Style.small)
            caption = QLabel()
            caption.setWordWrap(True)
            self._captions.append((key, caption))
            column.addWidget(caption)
            widget.setMinimumHeight(Style.touch)
            column.addWidget(widget)
            self._groups.append(group)
        self.controls.installEventFilter(self)
        root.addWidget(self.controls)

        self.applied_target = QLabel()
        self.applied_target.setWordWrap(True)
        self.applied_target.setTextFormat(Qt.TextFormat.PlainText)
        root.addWidget(self.applied_target)

        counts = QWidget()
        count_row = QHBoxLayout(counts)
        count_row.setContentsMargins(0, 0, 0, 0)
        self.signal_count = QLabel()
        count_row.addWidget(self.signal_count)
        count_row.addWidget(self.active_count)
        self.observation = QLabel()
        actions = QWidget()
        action_row = QHBoxLayout(actions)
        action_row.setContentsMargins(0, 0, 0, 0)
        action_row.setSpacing(Style.small)
        self.refresh_preview = QPushButton()
        self.refresh_preview.setFixedSize(Style.touch, Style.touch)
        action_row.addWidget(self.refresh_preview)
        action_row.addWidget(self.expand)
        self.signal_header = ImageToolbar(counts, self.observation, actions)
        root.addWidget(self.signal_header)

        self.details = QWidget()
        detail_layout = QVBoxLayout(self.details)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        self.evidence_status = QLabel()
        self.evidence_status.setWordWrap(True)
        self.evidence_status.setTextFormat(Qt.TextFormat.PlainText)
        detail_layout.addWidget(self.evidence_status)
        self.table = QTableWidget(0, 4)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setWordWrap(True)
        self.table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.table.horizontalHeader().setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self.table.viewport().installEventFilter(self)
        detail_layout.addWidget(self.table)
        root.addWidget(self.details)
        self.details.hide()
        self.expand.toggled.connect(self._expanded)
        self.enabled.toggled.connect(self._render_power)
        self.host.textChanged.connect(self._refresh_applied_target)
        self.port.valueChanged.connect(self._refresh_applied_target)
        self.address.valueChanged.connect(self._refresh_applied_target)
        self.heartbeat_address.valueChanged.connect(self._refresh_applied_target)
        self.fault_address.valueChanged.connect(self._refresh_applied_target)
        self.refresh_preview.clicked.connect(self.preview_requested)
        self.set_language(self.language)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() in (
            QEvent.Type.Resize,
            QEvent.Type.Show,
            QEvent.Type.LayoutRequest,
            QEvent.Type.FontChange,
        ):
            if watched is self.controls:
                self._arrange_settings()
            elif watched is self.table.viewport():
                self._size_table()
        return super().eventFilter(watched, event)

    def _arrange_settings(self) -> None:
        connection_width = (
            sum(group.minimumSizeHint().width() for group in self._groups[:3])
            + 2 * Style.space
        )
        address_width = max(
            group.minimumSizeHint().width() for group in self._groups[3:]
        )
        columns = (
            3 if self.controls.width() >= connection_width else 2,
            4 if self.controls.width() >= 4 * address_width + 3 * Style.space else 2,
        )
        if columns == self._columns:
            return
        self._columns = columns
        for grid, groups, count in (
            (self.settings_layout, self._groups[:3], columns[0]),
            (self.address_layout, self._groups[3:], columns[1]),
        ):
            for group in groups:
                grid.removeWidget(group)
            for column in range(4):
                grid.setColumnStretch(column, 0)
            for index, group in enumerate(groups):
                row, column = divmod(index, count)
                grid.addWidget(group, row, column)
                grid.setColumnStretch(
                    column, 2 if grid is self.settings_layout and column == 1 else 1
                )

    def set_editable(self, editable: bool, *, can_manage: bool) -> None:
        self.can_manage = can_manage
        self.controls.setEnabled(editable)
        self.enabled.setEnabled(editable)
        self._render_power()

    def _render_power(self) -> None:
        snapshot = self.snapshot
        warning = (
            snapshot.issue
            or snapshot.state in ("partial", "unavailable")
            or (snapshot.read_ok and snapshot.active)
        )
        set_service_title(self.title_icon, self.title, "alarm" if warning else "status")
        self.enabled.setIcon(production_icon("power", theme_colors(self).primary))
        action = text(
            "plc_disable" if self.enabled.isChecked() else "plc_enable", self.language
        )
        changed = self.enabled.isChecked() != self.snapshot.selection.enabled
        set_button_caption(
            self.enabled,
            action + (" *" if changed else ""),
            button_width(
                self.enabled,
                ("plc_enable", "plc_disable", "web_turn_on", "web_turn_off"),
                padding=2,
            ),
        )
        tip = text(
            "status_enable_tip" if self.can_manage else "permission_engineer",
            self.language,
        )
        self.enabled.setToolTip(action + "\n" + tip)
        self.enabled.setAccessibleDescription(
            tip + (" · " + text("unsaved", self.language) if changed else "")
        )

    def _refresh_applied_target(self) -> None:
        selected = self.snapshot.selection
        address = self.address.value()
        different = self.snapshot.state != "off" and (
            self.host.text().strip() != selected.endpoint.host
            or self.port.value() != selected.endpoint.port
            or address != selected.word_address
            or self.heartbeat_address.value() != selected.heartbeat_address
            or self.fault_address.value() != selected.fault_address
        )
        value = (
            f"{text('alarm_applied', self.language)} · "
            f"{selected.endpoint.host}:{selected.endpoint.port} · "
            f"{text('status_heartbeat', self.language)} W{selected.heartbeat_address} · "
            f"{text('status_fault', self.language)} W{selected.fault_address} · "
            f"{text('status_alarm_address', self.language)} W{selected.word_address}"
            if different
            else ""
        )
        self.applied_target.setText(value)
        self.applied_target.setVisible(different)
        self.applied_target.setToolTip(value)
        self.applied_target.setAccessibleName(value)
        self.applied_target.setStyleSheet(f"color: {theme_colors(self).secondary};")

    def _expanded(self, expanded: bool) -> None:
        self.details.setVisible(expanded)
        self.refresh()
        if expanded and self.snapshot.state == "off":
            self.preview_requested.emit()

    def set_snapshot(self, snapshot: AlarmSnapshot) -> None:
        if snapshot != self.snapshot:
            self.snapshot = snapshot
            self.refresh()

    def set_preview(
        self, definitions: AlarmDefinitions | None, issue: str = ""
    ) -> None:
        self.preview, self.preview_issue = definitions, issue
        self.refresh()

    def set_language(self, language: Language) -> None:
        self.language = language
        self.title.setText(text("status_service", language))
        self.title.setToolTip(text("status_service_tip", language))
        self.title.setAccessibleName(self.title.toolTip())
        for key, caption in self._captions:
            caption.setText(
                "IPv4" if key == "comm_host" else text(key, language) if key else " "
            )
            caption.setToolTip(text(key, language) if key else "")
            caption.setAccessibleName(caption.toolTip())
        for widget, key in (
            (self.protocol, "comm_protocol"),
            (self.host, "comm_host"),
            (self.port, "comm_port"),
            (self.address, "alarm_word_tip"),
            (self.heartbeat_address, "status_heartbeat_tip"),
            (self.fault_address, "status_fault_tip"),
            (self.running_address, "status_running_tip"),
            (self.expand, "alarm_details"),
            (self.refresh_preview, "alarm_preview_refresh"),
            (self.table, "alarm_details"),
        ):
            widget.setToolTip(text(key, language))
            widget.setAccessibleName(text(key, language))
        self._arrange_settings()
        self.refresh()

    def refresh(self) -> None:
        snapshot, language = self.snapshot, self.language
        offline = snapshot.state == "off"
        definitions = self.preview if offline else snapshot.definitions
        issue = self.preview_issue if offline else snapshot.issue
        status_key = (
            "service_off"
            if offline
            else "service_online"
            if snapshot.state == "online"
            else "service_partial"
            if snapshot.state == "partial"
            else "unavailable"
            if snapshot.state == "unavailable"
            else "service_waiting"
        )
        colors = theme_colors(self)
        set_service_status(
            self.status_indicator,
            self.status,
            text(status_key, language),
            colors.secondary
            if offline
            else colors.ok
            if snapshot.state == "online"
            else colors.warning,
        )
        self.status.setToolTip(snapshot.issue)
        self.status.setAccessibleName(self.status.text())
        self.status.setAccessibleDescription(snapshot.issue)
        fresh = (
            snapshot.read_ok
            and snapshot.raw_word is not None
            and snapshot.observed_at is not None
        )
        self.active_count.setFixedWidth(
            self.active_count.fontMetrics().horizontalAdvance(
                text("alarm_active_count", language).format(count=16)
            )
            + Style.small
        )
        self.active_count.setText(
            text("alarm_active_count", language).format(count=len(snapshot.active))
            if fresh
            else ""
        )
        self.active_count.setStyleSheet(f"color: {colors.secondary};")
        self._render_power()
        self._refresh_applied_target()
        self.expand.setIcon(
            shell_icon("chevron", colors.primary)
            if self.expand.isChecked()
            else image_icon("next", colors.primary)
        )
        count = len(definitions.signals) if definitions else "—"
        disclosure = text("alarm_signals", language).format(count=count)
        self.signal_count.setText(disclosure)
        self.signal_count.setFixedWidth(
            self.signal_count.fontMetrics().horizontalAdvance(
                text("alarm_signals", language).format(count=16)
            )
            + Style.small
        )
        self.expand.setToolTip(disclosure + " · " + text("alarm_details", language))
        self.expand.setAccessibleName(self.expand.toolTip())
        self.refresh_preview.setIcon(image_icon("refresh", colors.primary))
        self.refresh_preview.setIconSize(QSize(Style.icon, Style.icon))
        self.refresh_preview.setVisible(offline and self.expand.isChecked())
        detail = text("alarm_preview", language) if offline else ""
        if offline and definitions is None and not issue:
            detail += " · " + text("alarm_preview_waiting", language)
        elif not offline and snapshot.observed_at is not None:
            detail = (
                text("comm_last_read", language)
                + ": "
                + (
                    datetime.fromtimestamp(snapshot.observed_at, tz=UTC)
                    .astimezone()
                    .strftime("%Y-%m-%d %H:%M:%S")
                )
            )
        self.observation.setText(detail)
        self.observation.setVisible(bool(detail))
        self.observation.setStyleSheet(f"color: {colors.secondary};")
        stale = not offline and snapshot.raw_word is not None and not snapshot.read_ok
        evidence = (
            text("alarm_stale_values", language)
            if stale
            else text("alarm_preview_failed", language)
            if offline and issue
            else issue
        )
        self.evidence_status.setText(evidence)
        self.evidence_status.setVisible(bool(evidence))
        self.evidence_status.setToolTip(issue)
        self.evidence_status.setAccessibleDescription(issue)
        self.evidence_status.setStyleSheet(f"color: {colors.warning};")
        provenance = (
            f"{definitions.origin}/{definitions.version} · SHA-256 {definitions.digest}"
            if definitions
            else ""
        )
        self.signal_count.setToolTip(provenance)
        self.signal_count.setAccessibleDescription(provenance)
        self.observation.setToolTip(detail)
        self.observation.setAccessibleName(detail)
        self.signal_header.sync_metrics()
        headers = tuple(
            text(key, language)
            for key in (
                "alarm_signal",
                "records_bit",
                "export_value",
                "alarm_state",
            )
        )
        self.table.setHorizontalHeaderLabels(headers)
        signals = definitions.signals if definitions else ()
        self.table.setRowCount(len(signals))
        for row, signal in enumerate(signals):
            raw = (
                None
                if offline or snapshot.raw_word is None
                else (snapshot.raw_word >> signal.bit) & 1
            )
            state = (
                "—"
                if raw is None
                else text(
                    "alarm_active"
                    if bool(raw) == signal.active_high
                    else "alarm_inactive",
                    language,
                )
            )
            values = (
                signal.label(language),
                str(signal.bit),
                "—" if raw is None else str(raw),
                state,
            )
            for column, value in enumerate(values):
                item = self.table.item(row, column)
                if item is None:
                    item = QTableWidgetItem()
                    self.table.setItem(row, column, item)
                item.setText(value)
                meaning = (
                    f"W{snapshot.selection.word_address}.{signal.bit:02}"
                    if column == 1
                    else value
                )
                item.setToolTip(meaning)
                item.setData(Qt.ItemDataRole.AccessibleTextRole, meaning)
                item.setData(
                    Qt.ItemDataRole.AccessibleDescriptionRole,
                    text("alarm_stale_values", language) if stale else "",
                )
                item.setTextAlignment(
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                )
                item.setForeground(
                    QColor(
                        colors.warning
                        if column == 3 and fresh and bool(raw) == signal.active_high
                        else colors.secondary
                        if stale or raw is None
                        else colors.primary
                    )
                )
        self.table.setAccessibleDescription(self.evidence_status.text())
        self._size_table()

    def _size_table(self) -> None:
        metrics = self.table.fontMetrics()
        headers = tuple(
            text(key, self.language)
            for key in (
                "alarm_signal",
                "records_bit",
                "export_value",
                "alarm_state",
            )
        )
        for column, sample in (
            (1, "15"),
            (2, headers[2]),
            (
                3,
                text("alarm_inactive", self.language),
            ),
        ):
            self.table.setColumnWidth(
                column,
                max(
                    metrics.horizontalAdvance(headers[column]),
                    metrics.horizontalAdvance(sample),
                )
                + 28,
            )
        minimum = max(32, metrics.height() + 12)
        heights: list[int] = []
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            required = metrics.boundingRect(
                QRect(0, 0, max(1, self.table.columnWidth(0) - 12), 1000),
                Qt.TextFlag.TextWordWrap,
                item.text() if item else "",
            ).height()
            height = max(minimum, min(required, metrics.height() * 2) + 12)
            self.table.setRowHeight(row, height)
            heights.append(height)
        self.table.setFixedHeight(
            self.table.horizontalHeader().sizeHint().height()
            + (sum(heights[:8]) if heights else minimum)
            + 2 * self.table.frameWidth()
        )
