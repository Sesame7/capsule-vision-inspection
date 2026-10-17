"""Compact presentation of next-Start preferences and the existing H2B owner."""

from PySide6.QtCore import QSize, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox,
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

from vimo_demo.application import ApplicationState
from vimo_demo.communication import FinsAddress, FinsTransport
from vimo_demo.communication.reporting import (
    LocalReportingSettings,
    ProductionReportingCommands,
    ReportingSelection,
    ReportOutcome,
)

from .production_style import Language, Style, navigation_icon, text, theme_colors


class ReportingPanel(QFrame):
    edited = Signal()

    def __init__(self, controller: ProductionReportingCommands) -> None:
        super().__init__()
        self.controller = controller
        self.language: Language = "zh"
        self.state = ApplicationState.UNCONFIGURED
        self._syncing = False
        self._draft = controller.reporting_selection()
        self.setObjectName("commPanel")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        root = QVBoxLayout(self)
        root.setContentsMargins(Style.space, Style.small, Style.space, Style.small)
        root.setSpacing(Style.small)
        header = QHBoxLayout()
        icon = QLabel()
        icon.setPixmap(navigation_icon(2).pixmap(Style.icon, Style.icon))
        header.addWidget(icon)
        self.title = QLabel()
        self.title.setStyleSheet("font-size: 18px; font-weight: 600;")
        header.addWidget(self.title)
        self.enabled = QCheckBox()
        self.enabled.setMinimumHeight(Style.touch)
        header.addWidget(self.enabled)
        header.addStretch()
        self.service_status = QLabel()
        header.addWidget(self.service_status)
        root.addLayout(header)
        settings = root
        self._captions: list[tuple[str, QLabel]] = []
        self.host = QLineEdit()
        self.host.setMaxLength(64)
        self.protocol = QComboBox()
        self.protocol.addItems(("FINS/UDP", "FINS/TCP"))
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.timeout = QDoubleSpinBox()
        self.timeout.setRange(0.05, 30)
        self.timeout.setDecimals(2)
        self.timeout.setSingleStep(0.1)
        self.advanced_button = QPushButton()
        self.advanced_button.setCheckable(True)
        self.advanced_button.setMinimumHeight(Style.touch)
        self.advanced_button.setIcon(navigation_icon(2))
        self.advanced_button.setIconSize(QSize(Style.icon, Style.icon))
        common = QGridLayout()
        for column, (key, widget) in enumerate(
            (
                ("comm_protocol", self.protocol),
                ("comm_host", self.host),
                ("comm_port", self.port),
                ("comm_timeout", self.timeout),
            )
        ):
            caption = QLabel()
            caption.setWordWrap(True)
            self._captions.append((key, caption))
            common.addWidget(caption, 0, column)
            widget.setMinimumHeight(Style.touch)
            common.addWidget(widget, 1, column)
            common.setColumnStretch(column, 1)
        common.addWidget(self.advanced_button, 2, 0, 1, 4, Qt.AlignmentFlag.AlignRight)
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
        self.advanced_button.toggled.connect(self.refresh)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.error = QLabel()
        self.error.setWordWrap(True)
        status_row = QHBoxLayout()
        status_row.addWidget(self.status, 1)
        status_row.addWidget(self.error)
        root.addLayout(status_row)
        self._fields = (self.port, self.timeout, *self.destination, *self.source)
        for field in self._fields:
            field.valueChanged.connect(self._changed)
        self.enabled.toggled.connect(self._changed)
        self.protocol.currentIndexChanged.connect(self._changed)
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
            self.host.setText(selected.settings.host)
            self.protocol.setCurrentIndex(
                1 if selected.settings.transport is FinsTransport.TCP else 0
            )
            self.port.setValue(selected.settings.port)
            self.timeout.setValue(selected.settings.timeout)
            for fields, address in (
                (self.destination, selected.settings.destination),
                (self.source, selected.settings.source),
            ):
                for field, value in zip(
                    fields, (address.network, address.node, address.unit), strict=True
                ):
                    field.setValue(value)
        finally:
            self._syncing = False

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
        return ReportingSelection(
            self.enabled.isChecked(),
            LocalReportingSettings(
                host=self.host.text().strip(),
                port=self.port.value(),
                timeout=self.timeout.value(),
                destination=FinsAddress(*(f.value() for f in self.destination)),
                source=FinsAddress(*(f.value() for f in self.source)),
                max_pending=self._draft.settings.max_pending,
                transport=FinsTransport.TCP
                if self.protocol.currentIndex() == 1
                else FinsTransport.UDP,
            ),
        )

    def set_language(self, language: Language) -> None:
        self.language = language
        self.title.setText(text("plc_reporting", language))
        self.enabled.setText(text("plc_enable", language))
        self.enabled.setAccessibleName(self.enabled.text())
        self.enabled.setToolTip(text("plc_next_start", language))
        self.advanced_button.setText(text("comm_advanced", language))
        self.advanced_button.setAccessibleName(self.advanced_button.text())
        self.host.setAccessibleName(text("comm_host", language))
        self.host.setToolTip(text("plc_next_start", language))
        for key, caption in self._captions:
            caption.setText(text(key, language))
        for key, widget in (
            ("comm_protocol", self.protocol),
            ("comm_port", self.port),
            ("comm_timeout", self.timeout),
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
        editable = self.state in (ApplicationState.READY, ApplicationState.UNCONFIGURED)
        for widget in (self.enabled, self.host, self.protocol, *self._fields):
            widget.setEnabled(editable)
        selected = self.controller.reporting_selection()
        udp = self.protocol.currentIndex() == 0
        self.advanced.setEnabled(editable and udp)
        self.advanced_button.setEnabled(editable and udp)
        self.advanced_button.setVisible(udp)
        self.advanced.setVisible(udp and self.advanced_button.isChecked())
        self.protocol.setToolTip(
            text("comm_protocol" if udp else "comm_tcp_nodes", self.language)
        )
        snapshot = self.controller.reporting_snapshot()
        key = "plc_disabled" if not selected.enabled else "plc_enabled"
        if snapshot is not None and snapshot.thread_alive:
            key = (
                "plc_starting"
                if self.state is ApplicationState.STARTING
                else "plc_active"
            )
            if (
                snapshot.last_outcome
                and snapshot.last_outcome.outcome is ReportOutcome.FAILED
            ):
                key = "plc_report_failed"
        ack = snapshot.last_acknowledged if snapshot else None
        if snapshot and snapshot.thread_alive and snapshot.last_outcome:
            if snapshot.last_outcome.outcome is ReportOutcome.ACKNOWLEDGED:
                key = "plc_acknowledged"
            elif snapshot.last_outcome.outcome in (
                ReportOutcome.FAILED,
                ReportOutcome.REJECTED,
            ):
                key = "plc_report_failed"
        self.service_status.setText(text(key, self.language))
        self.status.setText(
            f"ACK {ack.sequence if ack else '--'} · "
            f"{text('plc_success', self.language)} {snapshot.acknowledged_count if snapshot else 0} · "
            f"ERR {snapshot.failed_count if snapshot else 0}"
        )
        self.status.setStyleSheet(
            f"color: {theme_colors(self).warning if key == 'plc_report_failed' else theme_colors(self).accent};"
        )
        detail = text("plc_ack_hint", self.language)
        if snapshot:
            detail += f"\ngeneration={snapshot.generation}; pending={snapshot.pending}; skipped={snapshot.skipped_count}; rejected={snapshot.rejected_count}"
            if ack:
                detail += f"\nACK cycle={ack.identity.cycle_id}; sequence={ack.sequence}; words={ack.reject_words}"
        self.service_status.setToolTip(detail)
        self.service_status.setAccessibleDescription(detail)
        self.status.setToolTip(detail)
        self.status.setAccessibleDescription(detail)
        failure = snapshot.last_failed if snapshot else None
        issue = failure.issue if failure else None
        self.error.setVisible(issue is not None)
        self.error.setStyleSheet(f"color: {theme_colors(self).warning};")
        self.error.setText(
            f"{text('plc_last_error', self.language)} · {failure.stage.value if failure and failure.stage else '--'}"
        )
        self.error.setToolTip(
            f"generation={failure.identity.generation}; cycle={failure.identity.cycle_id}; sequence={failure.sequence}\n{issue.exception_type}: {issue.message}"
            + (
                f"\n{issue.cause_type}: {issue.cause_message}"
                if issue.cause_type
                else ""
            )
            if failure and issue
            else ""
        )
        self.error.setAccessibleDescription(self.error.toolTip())
