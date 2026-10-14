"""Compact presentation of session-local preferences and the existing H2B owner."""

from PySide6.QtCore import QSize, Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from vimo_demo.application import ApplicationState, ApplicationStateError
from vimo_demo.communication import FinsAddress
from vimo_demo.communication.reporting import (
    LocalReportingSettings,
    ProductionReportingCommands,
    ReportingSelection,
)

from .production_style import Language, Style, navigation_icon, text


class ReportingPanel(QFrame):
    def __init__(self, controller: ProductionReportingCommands) -> None:
        super().__init__()
        self.controller = controller
        self.language: Language = "zh"
        self.state = ApplicationState.UNCONFIGURED
        self._syncing = False
        self.setObjectName("commPanel")
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
        header.addStretch()
        self.enabled = QCheckBox()
        self.enabled.setMinimumHeight(Style.touch)
        header.addWidget(self.enabled)
        root.addLayout(header)
        self.host = QLabel("127.0.0.1")  # No editable automatic target.
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
        self._captions: list[tuple[str, QLabel]] = []
        common = QHBoxLayout()
        for key, widget in (
            ("comm_host", self.host),
            ("comm_port", self.port),
            ("comm_timeout", self.timeout),
        ):
            caption = QLabel()
            self._captions.append((key, caption))
            common.addWidget(caption)
            widget.setMinimumHeight(Style.touch)
            common.addWidget(widget, 1)
        common.addWidget(self.advanced_button)
        root.addLayout(common)
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
        root.addWidget(self.advanced)
        self.advanced.hide()
        self.advanced_button.toggled.connect(self.advanced.setVisible)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.error = QLabel()
        self.error.setWordWrap(True)
        root.addWidget(self.status)
        root.addWidget(self.error)
        self._fields = (self.port, self.timeout, *self.destination, *self.source)
        for field in self._fields:
            field.valueChanged.connect(self._changed)
        self.enabled.toggled.connect(self._changed)
        self._load_selection()
        self.set_language("zh")

    @staticmethod
    def _address_fields() -> tuple[QSpinBox, QSpinBox, QSpinBox]:
        fields = (QSpinBox(), QSpinBox(), QSpinBox())
        for field, limits in zip(fields, ((0, 127), (1, 254), (0, 255)), strict=True):
            field.setRange(*limits)
            field.setMinimumHeight(Style.touch)
        return fields

    def _load_selection(self) -> None:
        selected = self.controller.reporting_selection()
        self._syncing = True
        try:
            self.enabled.setChecked(selected.enabled)
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
        previous = self.controller.reporting_selection()
        try:
            self.controller.set_reporting_selection(
                ReportingSelection(
                    self.enabled.isChecked(),
                    LocalReportingSettings(
                        port=self.port.value(),
                        timeout=self.timeout.value(),
                        destination=FinsAddress(
                            *(field.value() for field in self.destination)
                        ),
                        source=FinsAddress(*(field.value() for field in self.source)),
                        max_pending=previous.settings.max_pending,
                    ),
                )
            )
        except ApplicationStateError:
            self._load_selection()  # Includes programmatic edits to disabled widgets.
        self.refresh()

    def set_language(self, language: Language) -> None:
        self.language = language
        self.title.setText(text("plc_reporting", language))
        self.enabled.setText(text("plc_enable", language))
        self.enabled.setAccessibleName(self.enabled.text())
        self.enabled.setToolTip(text("plc_session_only", language))
        self.advanced_button.setText(text("comm_advanced", language))
        self.advanced_button.setAccessibleName(self.advanced_button.text())
        self.host.setAccessibleName(f"{text('comm_host', language)} 127.0.0.1")
        self.host.setToolTip(text("plc_session_only", language))
        for key, caption in self._captions:
            caption.setText(text(key, language))
        for key, widget in (("comm_port", self.port), ("comm_timeout", self.timeout)):
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
        for widget in (self.enabled, *self._fields):
            widget.setEnabled(editable)
        selected = self.controller.reporting_selection()
        snapshot = self.controller.reporting_snapshot()
        key = "plc_disabled" if not selected.enabled else "plc_enabled"
        if snapshot is not None and snapshot.thread_alive:
            key = (
                "plc_starting"
                if self.state is ApplicationState.STARTING
                else "plc_active"
            )
        ack = snapshot.last_acknowledged if snapshot else None
        self.status.setText(
            f"{text(key, self.language)} · ACK {ack.sequence if ack else '--'} · "
            f"{text('plc_success', self.language)} {snapshot.acknowledged_count if snapshot else 0} · "
            f"{text('plc_failed', self.language)} {snapshot.failed_count if snapshot else 0}"
        )
        detail = text("plc_ack_hint", self.language)
        if snapshot:
            detail += f"\ngeneration={snapshot.generation}; pending={snapshot.pending}; skipped={snapshot.skipped_count}; rejected={snapshot.rejected_count}"
            if ack:
                detail += f"\nACK cycle={ack.identity.cycle_id}; sequence={ack.sequence}; words={ack.reject_words}"
        self.status.setToolTip(detail)
        failure = snapshot.last_failed if snapshot else None
        issue = failure.issue if failure else None
        self.error.setVisible(issue is not None)
        self.error.setStyleSheet(f"color: {Style.warning};")
        self.error.setText(
            f"{text('plc_last_error', self.language)} · {failure.stage.value if failure and failure.stage else '--'} · {issue.exception_type if issue else ''}"
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
