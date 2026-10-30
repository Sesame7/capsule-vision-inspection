"""Compact next-Start preferences and bounded reporting-worker observations."""

from PySide6.QtCore import QEvent, QObject, QSize, Qt, Signal, Slot
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from capsule_vision.application import ApplicationState
from capsule_vision.communication import (
    FinsAddress,
    FinsTcpEndpoint,
    FinsUdpEndpoint,
    ModbusSettings,
)
from capsule_vision.communication.reporting import (
    ProductionReportingCommands,
    ReportingSelection,
    ReportingSettings,
    ReportOutcome,
)
from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import Language

from .style import (
    Style,
    button_width,
    navigation_icon,
    production_icon,
    set_button_caption,
    theme_colors,
)


class ReportingPanel(QFrame):
    edited = Signal()

    def __init__(self, controller: ProductionReportingCommands) -> None:
        super().__init__()
        self.controller = controller
        self.language: Language = "zh"
        self.state = ApplicationState.UNCONFIGURED
        self._syncing = False
        self._modbus = False
        self._port_automatic = True
        self._summary_text = ""
        self._draft = controller.reporting_selection()
        self.setObjectName("commPanel")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        root = QVBoxLayout(self)
        root.setContentsMargins(Style.space, Style.space, Style.space, Style.space)
        root.setSpacing(Style.small)
        header = QHBoxLayout()
        icon = QLabel()
        icon.setPixmap(navigation_icon(2).pixmap(Style.icon, Style.icon))
        header.addWidget(icon)
        self.title = QLabel()
        self.title.setStyleSheet("font-size: 18px; font-weight: 600;")
        header.addWidget(self.title)
        self.summary = QLabel()
        self.summary.setMinimumWidth(0)
        self.summary.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        header.addWidget(self.summary, 1)
        self.service_status = QLabel()
        header.addWidget(self.service_status)
        self.enabled = QPushButton()
        self.enabled.setCheckable(True)
        self.enabled.setObjectName("reportingPower")
        self.enabled.setProperty("compactAction", True)
        self.enabled.setProperty("iconAlignment", "leading")
        self.enabled.setMinimumHeight(Style.touch)
        header.addWidget(self.enabled)
        root.addLayout(header)
        settings = root
        self._captions: list[tuple[str, QLabel]] = []
        self.host = QLineEdit()
        self.host.setMaxLength(64)
        self.protocol = QComboBox()
        self.protocol.addItems(("FINS/UDP", "FINS/TCP", "Modbus TCP"))
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.timeout = QDoubleSpinBox()
        self.timeout.setRange(0.05, 30)
        self.timeout.setDecimals(2)
        self.timeout.setSingleStep(0.1)
        self.base_address = QSpinBox()
        self.base_address.setRange(0, 508)
        self.base_address.setPrefix("W")
        self.advanced_button = QPushButton()
        self.advanced_button.setCheckable(True)
        self.advanced_button.setMinimumHeight(Style.touch)
        self.advanced_button.setIcon(navigation_icon(2))
        self.advanced_button.setIconSize(QSize(Style.icon, Style.icon))
        self.advanced_button.setProperty("iconAlignment", "leading")
        common = self.fields_layout = QGridLayout()
        for column, (key, widget) in enumerate(
            (
                ("comm_protocol", self.protocol),
                ("comm_host", self.host),
                ("comm_port", self.port),
                ("comm_timeout", self.timeout),
                ("plc_base_address", self.base_address),
            )
        ):
            caption = QLabel()
            caption.setWordWrap(True)
            self._captions.append((key, caption))
            common.addWidget(caption, 0, column)
            widget.setMinimumHeight(Style.touch)
            common.addWidget(widget, 1, column)
            common.setColumnStretch(column, 1)
        common.addWidget(self.advanced_button, 2, 0, 1, 5, Qt.AlignmentFlag.AlignRight)
        settings.addLayout(common)
        self.advanced = QWidget()
        grid = QGridLayout(self.advanced)
        grid.setContentsMargins(0, 0, 0, 0)
        self.destination = self._address_fields()
        self.source = self._address_fields()
        for column, key in enumerate(("comm_network", "comm_node", "comm_unit"), 1):
            caption = QLabel()
            self._captions.append((key, caption))
            grid.addWidget(caption, 0, column)
        for row, (key, fields) in enumerate(
            (("comm_destination", self.destination), ("comm_source", self.source)), 1
        ):
            caption = QLabel()
            self._captions.append((key, caption))
            grid.addWidget(caption, row, 0)
            for column, field in enumerate(fields, 1):
                grid.addWidget(field, row, column)
        settings.addWidget(self.advanced)
        self.advanced.hide()
        self.modbus_advanced = QWidget()
        unit_row = QHBoxLayout(self.modbus_advanced)
        unit_row.setContentsMargins(0, 0, 0, 0)
        unit_row.addWidget(QLabel("Unit ID"))
        self.unit_id = QSpinBox()
        self.unit_id.setRange(0, 255)
        self.unit_id.setMinimumHeight(Style.touch)
        self.unit_id.setAccessibleName("Modbus TCP · Unit ID")
        unit_row.addWidget(self.unit_id)
        unit_row.addStretch()
        settings.addWidget(self.modbus_advanced)
        self.modbus_advanced.hide()
        self.advanced_button.toggled.connect(self.refresh)
        self._fields = (
            self.timeout,
            self.base_address,
            self.unit_id,
            *self.destination,
            *self.source,
        )
        for field in self._fields:
            field.valueChanged.connect(self._changed)
        self.port.valueChanged.connect(self._port_edited)
        port_edit = self.port.lineEdit()
        assert port_edit is not None
        port_edit.textEdited.connect(self._port_text_edited)
        self.enabled.toggled.connect(self._changed)
        self.protocol.currentIndexChanged.connect(self._protocol_changed)
        self.host.textChanged.connect(self._changed)
        self._load_selection()
        self.set_language("zh")
        self.summary.installEventFilter(self)

    @staticmethod
    def _address_fields() -> tuple[QSpinBox, QSpinBox, QSpinBox]:
        fields = (QSpinBox(), QSpinBox(), QSpinBox())
        for field, limits in zip(fields, ((0, 127), (1, 254), (0, 255)), strict=True):
            field.setRange(*limits)
            field.setMinimumHeight(Style.touch)
        return fields

    def set_selection(self, selected: ReportingSelection) -> None:
        self._draft = selected
        self._load_selection()
        self.refresh()

    def _load_selection(self) -> None:
        selected = self._draft
        self._syncing = True
        try:
            self.enabled.setChecked(selected.enabled)
            endpoint = selected.settings.endpoint
            self.host.setText(endpoint.host)
            self.protocol.setCurrentIndex(
                2
                if isinstance(endpoint, ModbusSettings)
                else 1
                if isinstance(endpoint, FinsTcpEndpoint)
                else 0
            )
            self.port.setValue(endpoint.port)
            self.timeout.setValue(endpoint.timeout)
            self.base_address.setValue(
                selected.settings.base_address
                if selected.settings.base_address is not None
                else -1
            )
            self.unit_id.setValue(
                endpoint.unit_id if isinstance(endpoint, ModbusSettings) else 255
            )
            self._port_automatic = endpoint.port == (502 if self._modbus else 9600)
            udp = (
                endpoint if isinstance(endpoint, FinsUdpEndpoint) else FinsUdpEndpoint()
            )
            for fields, address in (
                (self.destination, udp.destination),
                (self.source, udp.source),
            ):
                for field, value in zip(
                    fields, (address.network, address.node, address.unit), strict=True
                ):
                    field.setValue(value)
        finally:
            self._syncing = False

    @Slot()
    def _port_edited(self) -> None:
        if not self._syncing:
            self._port_automatic = False
        self._changed()

    @Slot(str)
    def _port_text_edited(self, value: str) -> None:
        if not self._syncing:
            self._port_automatic = False

    @Slot()
    def _protocol_changed(self) -> None:
        syncing = self._syncing
        self._syncing = True
        try:
            modbus = self.protocol.currentIndex() == 2
            if modbus != self._modbus:
                self.base_address.setRange(
                    -1 if modbus else 0, 65532 if modbus else 508
                )
                self.base_address.setPrefix("HR " if modbus else "W")
                if not syncing:
                    self.base_address.setValue(-1 if modbus else 49)
                    if self._port_automatic:
                        self.port.setValue(502 if modbus else 9600)
                self._modbus = modbus
        finally:
            self._syncing = syncing
        self._changed()

    @Slot()
    def _changed(self) -> None:
        if self._syncing:
            return
        if self.state not in (ApplicationState.READY, ApplicationState.UNCONFIGURED):
            self._load_selection()
            return
        try:
            self._draft = self.selection()
        except ValueError:
            pass  # Invalid text remains a draft; Save reports the validation error.
        self.refresh()
        self.edited.emit()

    def selection(self) -> ReportingSelection:
        common = (self.host.text().strip(), self.port.value(), self.timeout.value())
        endpoint = (
            ModbusSettings(*common, self.unit_id.value())
            if self._modbus
            else FinsTcpEndpoint(*common)
            if self.protocol.currentIndex() == 1
            else FinsUdpEndpoint(
                *common,
                FinsAddress(*(f.value() for f in self.destination)),
                FinsAddress(*(f.value() for f in self.source)),
            )
        )
        return ReportingSelection(
            self.enabled.isChecked(),
            ReportingSettings(
                endpoint=endpoint,
                max_pending=self._draft.settings.max_pending,
                base_address=None
                if self._modbus and self.base_address.value() == -1
                else self.base_address.value(),
            ),
        )

    def set_language(self, language: Language) -> None:
        self.language = language
        self.enabled.setIcon(production_icon("power", theme_colors(self).primary))
        self._power_width = button_width(
            self.enabled,
            ("plc_enable", "plc_disable", "web_turn_on", "web_turn_off"),
            padding=2,
        )
        self.title.setText(text("short_reporting", language))
        self.title.setToolTip(text("plc_reporting", language))
        self.title.setAccessibleName(self.title.toolTip())
        self.summary.setMinimumWidth(
            self.summary.fontMetrics().horizontalAdvance(
                f"ACK -- · {text('plc_success', language)} 0 · ERR 0"
            )
        )
        self.advanced_button.setText("")
        self.advanced_button.setFixedSize(Style.touch, Style.touch)
        self.advanced_button.setIcon(navigation_icon(1, theme_colors(self).primary))
        self.advanced_button.setProperty("iconAlignment", None)
        self.advanced_button.setAccessibleName(text("comm_advanced", language))
        self.advanced_button.setToolTip(text("comm_advanced", language))
        self.host.setAccessibleName(text("comm_host", language))
        self.base_address.setSpecialValueText(
            text("plc_modbus_base_required", language) if self._modbus else ""
        )
        for key, caption in self._captions:
            caption.setText(
                "IPv4"
                if key == "comm_host"
                else text("short_timeout", language) + " · s"
                if key == "comm_timeout"
                else text(key, language)
            )
            caption.setToolTip(text(key, language))
            caption.setAccessibleName(text(key, language))
        for key, widget in (
            ("comm_protocol", self.protocol),
            ("comm_port", self.port),
            ("comm_timeout", self.timeout),
            ("plc_base_address", self.base_address),
        ):
            widget.setAccessibleName(text(key, language))
        for key, fields in (
            ("comm_destination", self.destination),
            ("comm_source", self.source),
        ):
            for suffix, field in zip(
                ("comm_network", "comm_node", "comm_unit"), fields, strict=True
            ):
                field.setAccessibleName(
                    f"{text(key, language)} · {text(suffix, language)}"
                )
        self.refresh()

    def update_state(self, state: ApplicationState) -> None:
        self.state = state
        self.refresh()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._render_summary()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.summary and event.type() is QEvent.Type.Resize:
            self._render_summary()
        return super().eventFilter(watched, event)

    def _render_summary(self) -> None:
        self.summary.setText(
            self.summary.fontMetrics().elidedText(
                self._summary_text, Qt.TextElideMode.ElideRight, self.summary.width()
            )
        )

    @Slot()
    def refresh(self) -> None:
        editable = self.state in (ApplicationState.READY, ApplicationState.UNCONFIGURED)
        for widget in (
            self.enabled,
            self.host,
            self.protocol,
            self.port,
            *self._fields,
        ):
            widget.setEnabled(editable)
        self.enabled.setIcon(production_icon("power", theme_colors(self).primary))
        action = text(
            "plc_disable" if self.enabled.isChecked() else "plc_enable", self.language
        )
        set_button_caption(
            self.enabled,
            action,
            self._power_width,
        )
        self.enabled.setToolTip(action + "\n" + text("plc_next_start", self.language))
        selected = self.controller.reporting_selection()
        udp = self.protocol.currentIndex() == 0
        modbus = self.protocol.currentIndex() == 2
        self.advanced.setEnabled(editable and udp)
        self.modbus_advanced.setEnabled(editable and modbus)
        self.modbus_advanced.setVisible(modbus and self.advanced_button.isChecked())
        self.advanced_button.setEnabled(editable and (udp or modbus))
        self.advanced_button.setVisible(udp or modbus)
        self.advanced.setVisible(udp and self.advanced_button.isChecked())
        self.advanced_button.setAccessibleName(
            "Modbus TCP · Unit ID" if modbus else text("comm_advanced", self.language)
        )
        self.advanced_button.setToolTip(self.advanced_button.accessibleName())
        self.unit_id.setToolTip(text("comm_modbus_hint", self.language))
        self.base_address.setSpecialValueText(
            text("plc_modbus_base_required", self.language) if modbus else ""
        )
        self.base_address.setToolTip(
            text(
                "plc_modbus_base_hint" if modbus else "plc_base_address", self.language
            )
        )
        for caption_key, caption in self._captions:
            if caption_key == "plc_base_address":
                caption.setText(
                    text(caption_key, self.language) + (" *" if modbus else "")
                )
        snapshot = self.controller.reporting_snapshot()
        key = "plc_disabled" if not selected.enabled else "plc_enabled"
        if selected.enabled and self.state is ApplicationState.STARTING:
            key = "plc_starting"
        if snapshot is not None and snapshot.thread_alive:
            key = (
                "plc_stopping"
                if self.state is ApplicationState.STOPPING
                else "plc_starting"
                if self.state is ApplicationState.STARTING
                else "plc_stopping"
                if not snapshot.accepting
                else "plc_active"
            )
        elif snapshot is not None and (
            snapshot.failure_issue or snapshot.cleanup_issue
        ):
            key = "plc_report_failed"
        ack = snapshot.last_acknowledged if snapshot else None
        self.service_status.setText(
            "OFF"
            if key == "plc_disabled"
            else text(
                {
                    "plc_enabled": "service_next_start",
                    "plc_starting": "web_starting",
                    "plc_stopping": "STOPPING",
                    "plc_active": "state_RUNNING",
                }.get(key, key),
                self.language,
            )
        )
        self._summary_text = (
            f"ACK {ack.sequence if ack else '--'} · "
            f"{text('plc_success', self.language)} {snapshot.acknowledged_count if snapshot else 0} · "
            f"ERR {snapshot.failed_count if snapshot else 0}"
        )
        warning = key == "plc_report_failed" or bool(
            snapshot
            and snapshot.last_outcome
            and snapshot.last_outcome.outcome
            in (ReportOutcome.FAILED, ReportOutcome.REJECTED)
        )
        self.summary.setStyleSheet(
            f"color: {theme_colors(self).warning if warning else theme_colors(self).secondary};"
        )
        self.service_status.setToolTip("")
        self.service_status.setAccessibleName(text(key, self.language))
        self.summary.setToolTip("")
        self.summary.setAccessibleDescription("")
        self._render_summary()
