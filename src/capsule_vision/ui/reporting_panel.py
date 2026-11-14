"""Compact next-Start preferences and bounded reporting-worker observations."""

from PySide6.QtCore import QSignalBlocker, QSize, Qt, Signal, Slot
from PySide6.QtGui import QStandardItemModel
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
    QStackedWidget,
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
    ReportingMode,
    ReportingSelection,
    ReportingSettings,
    ReportOutcome,
)
from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import LANGUAGES, Language

from .style import (
    ChevronComboBox,
    Style,
    button_width,
    navigation_icon,
    production_icon,
    set_button_caption,
    set_service_status,
    set_service_title,
    theme_colors,
)

_MERGE_MODES = {
    0: ReportingMode.PER_CYCLE,
    1: ReportingMode.SINGLE_CYCLE,
    4: ReportingMode.FOUR_CYCLE,
}


class ReportingPanel(QFrame):
    edited = Signal()

    def __init__(self, controller: ProductionReportingCommands) -> None:
        super().__init__()
        self.controller = controller
        self.language: Language = "zh"
        self.state = ApplicationState.UNCONFIGURED
        self.can_manage = True
        self._syncing = False
        self._modbus = False
        self._port_automatic = True
        self._draft = controller.reporting_selection()
        self._merge_index = self._draft.settings.mode.group_size or 0
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
        header.addWidget(self.title)
        self.status_indicator = QLabel()
        header.addWidget(self.status_indicator)
        self.service_status = QLabel()
        header.addWidget(self.service_status)
        self.mode_control = QWidget()
        modes = QHBoxLayout(self.mode_control)
        modes.setContentsMargins(0, 0, 0, 0)
        modes.setSpacing(Style.small)
        modes.addStretch()
        self.merge_label = QLabel()
        self.merge_count = ChevronComboBox()
        self.merge_count.setObjectName("reportingMerge")
        self.merge_count.setMinimumHeight(Style.touch)
        self.merge_count.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.merge_count.setMinimumContentsLength(2)
        self.merge_count.addItems([str(count) for count in range(9)])
        model = self.merge_count.model()
        assert isinstance(model, QStandardItemModel)
        for count in range(9):
            item = model.item(count)
            assert item is not None
            item.setEnabled(count in _MERGE_MODES)
            item.setSelectable(count in _MERGE_MODES)
        self.merge_label.setBuddy(self.merge_count)
        modes.addWidget(self.merge_label)
        modes.addWidget(self.merge_count)
        modes.addStretch()
        header.addWidget(self.mode_control, 1)
        self.enabled = QPushButton()
        self.enabled.setCheckable(True)
        self.enabled.setObjectName("reportingPower")
        self.enabled.setProperty("compactAction", True)
        self.enabled.setProperty("iconAlignment", "leading")
        self.enabled.setMinimumHeight(Style.touch)
        header.addWidget(self.enabled)
        root.addLayout(header)
        self.feedback = QLabel()
        self.feedback.setWordWrap(True)
        self.feedback.setTextFormat(Qt.TextFormat.PlainText)
        root.addWidget(self.feedback)
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
        self.base_choices = QComboBox()
        self.base_choices.addItems(("W49", "W149"))
        self.base_choices.setMinimumHeight(Style.touch)
        self.base_field = QStackedWidget()
        self.base_field.addWidget(self.base_address)
        self.base_field.addWidget(self.base_choices)
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
                ("plc_base_address", self.base_field),
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
        self.merge_count.currentIndexChanged.connect(self._merge_changed)
        self.base_choices.currentIndexChanged.connect(self._base_selected)
        self.host.textChanged.connect(self._changed)
        self._load_selection()
        self.set_language("zh")

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
            self._merge_index = selected.settings.mode.group_size or 0
            self.merge_count.setCurrentIndex(self._merge_index)
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

    @Slot(int)
    def _merge_changed(self, index: int) -> None:
        if self._syncing:
            return
        if (
            index not in _MERGE_MODES
            or not self.can_manage
            or self.state not in (ApplicationState.READY, ApplicationState.UNCONFIGURED)
        ):
            # Native disabled items reject user selection. Also fence direct
            # index changes without discarding an otherwise invalid field draft.
            with QSignalBlocker(self.merge_count):
                self.merge_count.setCurrentIndex(self._merge_index)
            return
        self._merge_index = index
        self._changed()

    @Slot(int)
    def _base_selected(self, index: int) -> None:
        if not self._syncing and index >= 0:
            self.base_address.setValue((49, 149)[index])

    def _refresh_base(self) -> None:
        # A mode change never rewrites the existing address. Unsupported values
        # stay visible as a placeholder until the user explicitly selects a base.
        grouped = self.merge_count.currentIndex() in (1, 4)
        self.base_field.setCurrentIndex(int(grouped))
        syncing, self._syncing = self._syncing, True
        try:
            base = self.base_address.value()
            self.base_choices.setPlaceholderText(f"W{base}" if base >= 0 else "—")
            self.base_choices.setCurrentIndex(
                (49, 149).index(base) if base in (49, 149) else -1
            )
        finally:
            self._syncing = syncing

    def selection(self) -> ReportingSelection:
        mode = _MERGE_MODES.get(self.merge_count.currentIndex())
        if mode is None:
            raise ValueError("Unsupported reporting merge count")
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
                mode=mode,
                max_pending=self._draft.settings.max_pending,
                base_address=None
                if self._modbus and self.base_address.value() == -1
                else self.base_address.value(),
            ),
        )

    def set_language(self, language: Language) -> None:
        self.language = language
        merge_caption = text("plc_merge_count", language)
        merge_tip = (
            text("plc_merge_count_tip", language)
            + "\n"
            + text("plc_next_start", language)
        )
        self.mode_control.setAccessibleName(merge_caption)
        self.merge_label.setText(merge_caption)
        self.merge_label.setToolTip(merge_tip)
        self.merge_label.setFixedWidth(
            max(
                self.merge_label.fontMetrics().horizontalAdvance(
                    text("plc_merge_count", lang)
                )
                for lang in LANGUAGES
            )
            + Style.small
        )
        self.merge_count.setAccessibleName(merge_caption)
        self.merge_count.setAccessibleDescription(merge_tip)
        self.merge_count.setToolTip(merge_tip)
        for count in range(9):
            mode = _MERGE_MODES.get(count)
            caption = str(count)
            meaning = (
                f"{count} · {text('plc_mode_' + mode.value, language)}"
                if mode
                else f"{count} · {text('plc_merge_unsupported', language)}"
            )
            self.merge_count.setItemText(count, caption)
            for role in (
                Qt.ItemDataRole.ToolTipRole,
                Qt.ItemDataRole.AccessibleTextRole,
            ):
                self.merge_count.setItemData(count, meaning, role)
        self.merge_count.ensurePolished()
        self.merge_count.setFixedWidth(
            max(Style.touch, self.merge_count.sizeHint().width())
        )
        self.merge_count.view().setMinimumWidth(
            max(
                self.merge_count.fontMetrics().horizontalAdvance(
                    self.merge_count.itemText(count)
                )
                for count in range(9)
            )
            + 2 * Style.space
        )
        self.enabled.setIcon(production_icon("power", theme_colors(self).primary))
        self._power_width = button_width(
            self.enabled,
            ("plc_enable", "plc_disable", "web_turn_on", "web_turn_off"),
            padding=2,
        )
        self.title.setText(text("short_reporting", language))
        self.title.setToolTip(text("plc_reporting", language))
        self.title.setAccessibleName(self.title.toolTip())
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
            ("plc_base_address", self.base_choices),
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

    @Slot()
    def refresh(self) -> None:
        set_service_title(self.title_icon, self.title, "plc")
        self._refresh_base()
        editable = self.can_manage and self.state in (
            ApplicationState.READY,
            ApplicationState.UNCONFIGURED,
        )
        for widget in (
            self.enabled,
            self.host,
            self.protocol,
            self.mode_control,
            self.base_choices,
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
        self.enabled.setToolTip(
            action
            + "\n"
            + text(
                "plc_next_start" if self.can_manage else "permission_engineer",
                self.language,
            )
        )
        selected = self.controller.reporting_selection()
        udp = self.protocol.currentIndex() == 0
        modbus = self.protocol.currentIndex() == 2
        self.advanced.setEnabled(editable and udp)
        self.modbus_advanced.setEnabled(editable and modbus)
        self.modbus_advanced.setVisible(modbus and self.advanced_button.isChecked())
        self.advanced_button.setEnabled(
            (editable or not self.can_manage) and (udp or modbus)
        )
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
        self.base_choices.setToolTip(text("plc_group_hint", self.language))
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
        if snapshot is not None and snapshot.phase_issue:
            key = "plc_report_failed"
        colors = theme_colors(self)
        status_keys = {
            "plc_disabled": "service_off",
            "plc_enabled": "service_next_start",
            "plc_starting": "web_starting",
            "plc_stopping": "STOPPING",
            "plc_active": "state_RUNNING",
            "plc_report_failed": "service_error",
        }
        set_service_status(
            self.status_indicator,
            self.service_status,
            text(status_keys[key], self.language),
            colors.secondary
            if key == "plc_disabled"
            else colors.ok
            if key == "plc_active"
            else colors.fault
            if key == "plc_report_failed"
            else colors.warning,
        )
        issue = None
        phase = ""
        if snapshot is not None:
            phase = snapshot.phase_issue or ""
            issue = snapshot.failure_issue or snapshot.cleanup_issue
            outcome = snapshot.last_outcome
            if (
                issue is None
                and outcome
                and outcome.outcome in (ReportOutcome.FAILED, ReportOutcome.REJECTED)
            ):
                issue = outcome.issue
        detail = phase or (f"{issue.exception_type}: {issue.message}" if issue else "")
        self.feedback.setText(detail)
        self.feedback.setAccessibleName(detail)
        self.feedback.setVisible(bool(detail))
        self.feedback.setStyleSheet(f"color: {colors.warning};")
        self.service_status.setToolTip("")
        self.service_status.setAccessibleName(text(key, self.language))
