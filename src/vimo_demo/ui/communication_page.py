"""Persistent endpoints and session-local FINS reads/watches; one background owner, independent of Production."""

import re
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, replace
from datetime import datetime
from math import ceil
from pathlib import Path
from threading import Event, Thread

from PySide6.QtCore import QEvent, QObject, QRect, QSize, Qt, QThread, QTimer, Slot
from PySide6.QtGui import QColor, QFontMetricsF, QHideEvent, QResizeEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStyle,
    QStyleOptionHeader,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from vimo_demo.application.communication_preferences import (
    CommunicationPreferences,
    load_communication_preferences,
    save_communication_preferences,
)
from vimo_demo.communication import (
    FinsAddress,
    FinsMemoryArea,
    FinsPlcError,
    FinsProtocolError,
    FinsTimeoutError,
    FinsTransport,
    FinsTransportError,
)
from vimo_demo.communication.operations import (
    MAX_WATCH_GROUPS,
    MAX_WATCH_WORDS,
    CommunicationRequest,
    CommunicationResponse,
    RegisterWatchGroup,
    RequestKind,
    execute_request,
)
from vimo_demo.communication.reporting import (
    LocalReportingSettings,
    ProductionReportingCommands,
    ReportingWriteProtectedError,
)
from vimo_demo.configuration import ConfigurationError
from vimo_demo.web.runtime import WebRuntime, WebState

from .image_tools import image_icon
from .inference_bridge import FutureDelivery
from .production_style import (
    LANGUAGES,
    Language,
    Style,
    button_width,
    navigation_icon,
    production_icon,
    set_button_caption,
    text,
    theme_colors,
)
from .reporting_panel import ReportingPanel
from .web_settings_panel import WebSettingsPanel


@dataclass(frozen=True, slots=True)
class _LogEntry:
    time: str
    operation: str
    target: str
    words: str
    outcome: str
    elapsed_ms: float | None
    detail: str


@dataclass(frozen=True, slots=True)
class _WatchOutcome:
    responses: tuple[CommunicationResponse, ...]
    failed_request: CommunicationRequest | None = None
    error: BaseException | None = None
    interrupted: bool = False


class CommunicationPage(QWidget):
    """GUI owns controls/delivery; a retained non-daemon thread owns each client.

    Navigation leaves this owner alive. request_close() disables admission, waits
    nonblockingly for actual exit and calls its callback. Final disposal detaches
    delivery; an application-close failure can resume admission on the live page.
    The optional reporting boundary only commands/presents the existing owner.
    """

    def __init__(
        self,
        *,
        execute: Callable[
            [CommunicationRequest], CommunicationResponse
        ] = execute_request,
        reporting: ProductionReportingCommands | None = None,
    ) -> None:
        super().__init__()
        self.setObjectName("communication")
        self.language: Language = "zh"
        self._execute = execute
        self._reporting = reporting
        self.reporting_panel: ReportingPanel | None = None
        self._thread: Thread | None = None
        self._completion: tuple[object, object] | None = None
        self._request: CommunicationRequest | None = None
        self._sequence = 0
        self._closing = False
        self._disposed = False
        self._after_close: Callable[[], None] | None = None
        self._state_key = "comm_idle"
        self._detail = ""
        self._log: deque[_LogEntry] = deque(maxlen=100)
        self.last_response: CommunicationResponse | None = None
        self.last_read: CommunicationResponse | None = None
        self._watch_groups: list[RegisterWatchGroup] = []
        self._watch_errors: list[str | None] = []
        self._feedback_kind: RequestKind | None = None
        self._write_state, self._write_detail = "comm_idle", ""
        self._last_write: CommunicationResponse | None = None
        self._settings_loading = False
        self._settings_missing = True
        self._settings_message, self._settings_detail = "", ""
        self._discard_dialog: QMessageBox | None = None
        self._watch_observations: list[CommunicationResponse | None] = []
        self._watch_fresh: set[int] = set()
        self._watch_pass = False
        self._pass_auto = False
        self._watch_stop: Event | None = None
        self._auto_refresh = False
        self._watch_state = "comm_watch_idle"
        self._watch_success: datetime | None = None
        self._auto_timer = QTimer(self)
        self._auto_timer.setSingleShot(True)
        self._auto_timer.setInterval(1000)
        self._auto_timer.timeout.connect(self._auto_tick)
        self._delivery = FutureDelivery()
        self._delivery.emitter.completed.connect(
            self._completed, Qt.ConnectionType.QueuedConnection
        )
        self.destroyed.connect(
            self._delivery.detach, Qt.ConnectionType.DirectConnection
        )
        self._monitor = QTimer(self)
        self._monitor.setInterval(20)
        self._monitor.timeout.connect(self._finish)
        self._captions: list[tuple[str, QLabel]] = []
        self._buttons: dict[str, QPushButton] = {}
        self._inputs: list[QWidget] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        scroll = self.scroll_area = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = self.body = QWidget()
        body.setObjectName("commBody")
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(Style.space)
        scroll.setWidget(body)
        root.addWidget(scroll)

        self.service_cards = QWidget()
        self.service_layout = QGridLayout(self.service_cards)
        self.service_layout.setContentsMargins(0, 0, 0, 0)
        self.service_layout.setSpacing(Style.space)
        self.service_layout.setColumnStretch(0, 1)
        if reporting is not None and reporting.reporting_editable:
            self.reporting_panel = ReportingPanel(reporting)
            self.service_layout.addWidget(
                self.reporting_panel, 0, 0, Qt.AlignmentFlag.AlignTop
            )
        self.web_panel = WebSettingsPanel()
        self.web_panel.requested.connect(self._web_action)
        self._web_close_monitor = QTimer(self)
        self._web_close_monitor.setInterval(20)
        self._web_close_monitor.timeout.connect(self._check_closed)
        self.service_layout.addWidget(self.web_panel, 1, 0)
        layout.addWidget(self.service_cards)
        diagnostics = self._panel(layout, "comm_diagnostics")
        settings = diagnostics
        self._preferences_path: Path | None = None
        self._preferences = CommunicationPreferences()
        common = QGridLayout()
        common.setHorizontalSpacing(Style.space)
        self.protocol = QComboBox()
        self.protocol.addItem("FINS/UDP")
        self.protocol.addItem("FINS/TCP")
        self._inputs.append(self.protocol)
        self.host = QLineEdit("127.0.0.1")
        self.host.setMaxLength(64)
        self.port = self._spin(1, 65535, 9600)
        self.timeout = QDoubleSpinBox()
        self.timeout.setRange(0.05, 30)
        self.timeout.setDecimals(2)
        self.timeout.setSingleStep(0.1)
        self.timeout.setValue(1)
        self._inputs.extend((self.host, self.timeout))
        for column, (key, widget) in enumerate(
            (
                ("comm_protocol", self.protocol),
                ("comm_host", self.host),
                ("comm_port", self.port),
                ("comm_timeout", self.timeout),
            )
        ):
            self._field(common, key, widget, 0, column)
            common.setColumnStretch(column, 2 if column == 1 else 1)
            widget.setMinimumHeight(Style.touch)
        settings.addLayout(common)
        test_row = QHBoxLayout()
        test_row.addStretch()
        self.advanced_button = self._button("comm_advanced", self._toggle_advanced)
        self.advanced_button.setCheckable(True)
        test_row.addWidget(self.advanced_button)
        settings.addLayout(test_row)
        self.advanced = QWidget()
        routing = QGridLayout(self.advanced)
        routing.setContentsMargins(0, 0, 0, 0)
        routing.setHorizontalSpacing(Style.space)
        self.destination = (
            self._spin(0, 127, 0),
            self._spin(1, 254, 2),
            self._spin(0, 255, 0),
        )
        self.source = (
            self._spin(0, 127, 0),
            self._spin(1, 254, 1),
            self._spin(0, 255, 0),
        )
        for column, key in enumerate(("comm_network", "comm_node", "comm_unit"), 1):
            routing.addWidget(self._caption(key), 0, column)
            routing.setColumnStretch(column, 1)
        for row, (key, fields) in enumerate(
            (("comm_destination", self.destination), ("comm_source", self.source)), 1
        ):
            routing.addWidget(self._caption(key), row, 0)
            for column, field in enumerate(fields, 1):
                field.setMinimumHeight(Style.touch)
                routing.addWidget(field, row, column)
        settings.addWidget(self.advanced)
        self.advanced.hide()
        watch = diagnostics
        watch.addWidget(self._caption("comm_watch"))
        self.manual_endpoint = QLabel()
        watch.addWidget(self.manual_endpoint)
        self.watch_editor_button = self._button(
            "comm_watch_edit", self._toggle_watch_editor
        )
        self.watch_editor_button.setCheckable(True)
        self.watch_editor = QWidget()
        group_fields = QGridLayout(self.watch_editor)
        group_fields.setContentsMargins(0, 0, 0, 0)
        group_fields.setHorizontalSpacing(Style.space)
        self.watch_area = self._area_selector()
        self.watch_address = self._spin(0, 32767, 0)
        self.watch_count = self._spin(1, 999, 1)
        self.watch_name = QLineEdit()
        self.watch_name.setMaxLength(64)
        self._inputs.append(self.watch_name)
        for column, (key, widget) in enumerate(
            (
                ("comm_area", self.watch_area),
                ("comm_address", self.watch_address),
                ("comm_count", self.watch_count),
                ("comm_watch_name", self.watch_name),
            )
        ):
            self._field(group_fields, key, widget, 0, column)
            group_fields.setColumnStretch(column, 2 if column == 3 else 1)
        self.watch_add = self._button("comm_watch_add", self._add_watch)
        group_fields.addWidget(self.watch_add, 1, 4)
        self.watch_tools = QWidget()
        self.watch_tools.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self.watch_tools_layout = QGridLayout(self.watch_tools)
        self.watch_tools_layout.setContentsMargins(0, 0, 0, 0)
        self.watch_tools_layout.setSpacing(Style.small)
        self.watch_tools_layout.setColumnStretch(0, 1)
        self._watch_wrapped: bool | None = None
        self.watch_selection = QWidget()
        selection = QHBoxLayout(self.watch_selection)
        selection.setContentsMargins(0, 0, 0, 0)
        selection.setSpacing(Style.small)
        self.watch_selector = QComboBox()
        self.watch_selector.setMinimumHeight(Style.touch)
        # Retain readable space for the group name and indexed register range.
        self.watch_selector.setMinimumContentsLength(48)
        self.watch_selector.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.watch_selector.currentIndexChanged.connect(self._refresh)
        self._inputs.append(self.watch_selector)
        selection.addWidget(self.watch_selector, 1)
        selection.addWidget(self.watch_editor_button)
        self.watch_actions = QWidget()
        self.watch_actions.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )
        actions = QHBoxLayout(self.watch_actions)
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(Style.small)
        self.watch_remove = self._button("comm_watch_remove", self._remove_watch)
        self.watch_once = self._button("comm_watch_once", self.read_watch_once)
        self.watch_auto = self._button("comm_watch_auto", self._toggle_auto)
        self.watch_auto.setCheckable(True)
        for button in (self.watch_remove, self.watch_once, self.watch_auto):
            actions.addWidget(button)
        watch.addWidget(self.watch_tools)
        watch.addWidget(self.watch_editor)
        self.watch_editor.hide()
        self.watch_status = QLabel()
        self.watch_status.setWordWrap(True)
        self.watch_status.setTextFormat(Qt.TextFormat.PlainText)
        watch.addWidget(self.watch_status)
        self.watch_table = QTableWidget(0, 5)
        self.watch_table.setObjectName("commWatchTable")
        self.watch_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.watch_table.verticalHeader().hide()
        self.watch_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Fixed
        )
        self.watch_table.horizontalHeader().setMinimumSectionSize(90)
        self.watch_table.setMinimumWidth(200)
        self.watch_table.setTextElideMode(Qt.TextElideMode.ElideRight)
        watch.addWidget(self.watch_table)
        self.watch_empty = self._caption("comm_watch_empty")
        self.watch_empty.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum
        )
        watch.addWidget(self.watch_empty)
        self.watch_area.currentIndexChanged.connect(self._watch_area_changed)
        self._watch_area_changed()

        self.manual_button = self._button("comm_registers", self._toggle_manual)
        self.manual_button.setCheckable(True)
        self.manual_button.setProperty("iconAlignment", "leading")
        diagnostics.addWidget(self.manual_button, 0, Qt.AlignmentFlag.AlignLeft)
        operations = diagnostics
        self.manual_body = QWidget()
        manual = QVBoxLayout(self.manual_body)
        manual.setContentsMargins(0, 0, 0, 0)
        operations.addWidget(self.manual_body)
        self.manual_body.hide()
        register_row = QGridLayout()
        self.area = self._area_selector()
        self.address = self._spin(0, 32767, 100)
        self.address.setPrefix("D")
        self._field(register_row, "comm_area", self.area, 0, 0)
        self._field(register_row, "comm_address", self.address, 0, 1)
        manual.addLayout(register_row)
        write_row = QHBoxLayout()
        self.write_values = QLineEdit("4321")
        self.write_values.setMaxLength(7000)
        self.write_values.setMinimumHeight(Style.touch)
        self._inputs.append(self.write_values)
        write_row.addWidget(self._caption("comm_values"))
        write_row.addWidget(self.write_values, 1)
        self.write_button = self._button(
            "comm_write", lambda: self.dispatch(RequestKind.WRITE)
        )
        write_row.addWidget(self.write_button)
        manual.addLayout(write_row)
        self.permission = QLabel()
        self.permission.setWordWrap(True)
        self.permission.setTextFormat(Qt.TextFormat.PlainText)
        self.permission.setProperty("role", "secondary")
        manual.addWidget(self.permission)
        self.area.currentIndexChanged.connect(self._manual_area_changed)
        self._manual_area_changed()

        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        manual.addWidget(self.status)
        self.success = QLabel()
        self.success.setWordWrap(True)
        self.success.setTextFormat(Qt.TextFormat.PlainText)
        manual.addWidget(self.success)
        preferences_row = QHBoxLayout()
        self.preferences_status = QLabel()
        self.preferences_status.setWordWrap(True)
        self.preferences_status.setTextFormat(Qt.TextFormat.PlainText)
        self.save_settings = QPushButton()
        self.save_settings.setObjectName("start")
        self.save_settings.setMinimumHeight(Style.touch)
        self.save_settings.clicked.connect(self.persist_settings)
        preferences_row.addWidget(self.preferences_status, 1)
        self.discard_settings = QPushButton()
        self.discard_settings.setFixedSize(Style.touch, Style.touch)
        self.discard_settings.clicked.connect(self.request_discard)
        preferences_row.addWidget(self.discard_settings)
        preferences_row.addWidget(self.save_settings)
        self.save_settings.hide()
        self.discard_settings.hide()
        root.addLayout(preferences_row)
        layout.addStretch(1)
        self.host.textChanged.connect(self._refresh)
        self.protocol.currentIndexChanged.connect(self._refresh)
        self.port.valueChanged.connect(self._refresh)
        self.set_language("zh")
        self.service_cards.installEventFilter(self)
        self.scroll_area.viewport().installEventFilter(self)
        self.watch_tools.installEventFilter(self)
        self.watch_table.viewport().installEventFilter(self)

    def _panel(
        self,
        parent: QVBoxLayout,
        title: str,
        *,
        stretch: int = 0,
        header: QWidget | None = None,
    ) -> QVBoxLayout:
        frame = QFrame()
        frame.setObjectName("commPanel")
        frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        parent.addWidget(frame, stretch)
        contents = QVBoxLayout(frame)
        contents.setContentsMargins(Style.space, Style.space, Style.space, Style.space)
        contents.setSpacing(Style.small)
        if header is None:
            header = self._caption(title)
            header.setStyleSheet("font-size: 18px; font-weight: 600;")
        contents.addWidget(header)
        return contents

    def _caption(self, key: str) -> QLabel:
        label = QLabel()
        label.setWordWrap(True)
        label.setProperty("role", "secondary")
        # Several small forms share captions; all participate in language changes.
        self._captions.append((key, label))
        return label

    def _area_selector(self) -> QComboBox:
        selector = QComboBox()
        selector.addItems(("D / DM", "W / WR"))
        selector.setMinimumHeight(Style.touch)
        self._inputs.append(selector)
        return selector

    @staticmethod
    def _selected_area(selector: QComboBox) -> FinsMemoryArea:
        return FinsMemoryArea.DM if selector.currentIndex() == 0 else FinsMemoryArea.W

    @Slot()
    def _manual_area_changed(self) -> None:
        self.address.setPrefix(self._selected_area(self.area).prefix)

    @Slot()
    def _watch_area_changed(self) -> None:
        self.watch_address.setPrefix(self._selected_area(self.watch_area).prefix)

    @Slot()
    def _toggle_manual(self) -> None:
        self.manual_body.setVisible(self.manual_button.isChecked())

    @Slot()
    def _toggle_watch_editor(self) -> None:
        self.watch_editor.setVisible(self.watch_editor_button.isChecked())

    def refresh_icons(self) -> None:
        colors = theme_colors(self)
        for button, tool in (
            (self.watch_editor_button, "plus"),
            (self.watch_add, "apply"),
            (self.watch_remove, "delete"),
            (self.watch_once, "refresh"),
            (self.watch_auto, "loop"),
            (self.manual_button, "write"),
            (self.write_button, "write"),
            (self.save_settings, "save"),
        ):
            button.setIcon(image_icon(tool, colors.primary))
            button.setIconSize(QSize(Style.icon, Style.icon))
        self.advanced_button.setIcon(navigation_icon(1, colors.primary))
        self.discard_settings.setIcon(production_icon("reset", colors.primary))

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._arrange_services()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() in (
            QEvent.Type.Resize,
            QEvent.Type.Show,
            QEvent.Type.LayoutRequest,
        ):
            if watched is self.watch_tools:
                self._arrange_watch()
            elif watched is self.watch_table.viewport():
                self._size_watch_columns()
        if watched in (
            self.service_cards,
            self.scroll_area.viewport(),
        ) and event.type() in (
            QEvent.Type.Resize,
            QEvent.Type.Show,
            QEvent.Type.LayoutRequest,
        ):
            # Observe available space, including after the vertical scrollbar
            # appears. Content minimums can temporarily exceed this viewport.
            self._arrange_services()
        return super().eventFilter(watched, event)

    def _arrange_watch(self) -> None:
        required = (
            self.watch_selection.minimumSizeHint().width()
            + self.watch_actions.minimumSizeHint().width()
            + Style.small
        )
        wrapped = self.watch_tools.width() < required
        if wrapped == self._watch_wrapped:
            return
        self._watch_wrapped = wrapped
        for widget in (self.watch_selection, self.watch_actions):
            self.watch_tools_layout.removeWidget(widget)
        self.watch_tools_layout.addWidget(
            self.watch_selection, 0, 0, 1, 2 if wrapped else 1
        )
        self.watch_tools_layout.addWidget(
            self.watch_actions,
            1 if wrapped else 0,
            0 if wrapped else 1,
            1,
            2 if wrapped else 1,
            Qt.AlignmentFlag.AlignLeft if wrapped else Qt.AlignmentFlag.AlignRight,
        )

    def _size_watch_columns(self) -> None:
        header = self.watch_table.horizontalHeader()
        metrics = QFontMetricsF(header.font())
        widths: list[int] = []
        for column, (key, sample) in enumerate(
            zip(
                (
                    "comm_table_group",
                    "comm_word_address",
                    "comm_dec",
                    "comm_table_hex",
                    "comm_last_read",
                ),
                ("", "W32767", "65535", "0xFFFF", "23:59:59.999"),
                strict=True,
            )
        ):
            option = QStyleOptionHeader()
            header.initStyleOptionForIndex(option, column)
            option.rect = QRect(0, 0, 100, header.height())
            contents = header.style().subElementRect(
                QStyle.SubElement.SE_HeaderLabel, option, header
            )
            widths.append(
                max(
                    header.minimumSectionSize(),
                    ceil(
                        max(
                            metrics.horizontalAdvance(text(key, lang))
                            for lang in LANGUAGES
                        )
                    )
                    + option.rect.width()
                    - contents.width()
                    + Style.small,
                    ceil(metrics.horizontalAdvance(sample)) + 2 * Style.small,
                )
            )
        spare = max(0, self.watch_table.viewport().width() - sum(widths))
        for column, width in enumerate(widths):
            self.watch_table.setColumnWidth(
                column, width + spare // 5 + (column < spare % 5)
            )

    def _arrange_services(self) -> None:
        panel = self.reporting_panel
        if panel is None:
            return
        # The reporting form uses four equally stretched fields. Its minimum
        # hint permits compressed editors, so reserve their natural widths
        # before placing both cards in equally stretched columns.
        margins = panel.contentsMargins()
        fields_width = (
            4
            * max(
                field.sizeHint().width()
                for field in (panel.protocol, panel.host, panel.port, panel.timeout)
            )
            + 3 * panel.fields_layout.horizontalSpacing()
            + 2 * Style.space
            + margins.left()
            + margins.right()
        )
        required = (
            2
            * max(
                fields_width,
                panel.sizeHint().width(),
                self.web_panel.sizeHint().width(),
            )
            + self.service_layout.horizontalSpacing()
        )
        wide = self.scroll_area.viewport().width() >= required
        position = (0, 1) if wide else (1, 0)
        if self.service_layout.itemAtPosition(*position) is not None:
            return
        self.service_layout.removeWidget(self.web_panel)
        self.service_layout.addWidget(
            self.web_panel, *position, Qt.AlignmentFlag.AlignTop
        )
        self.service_layout.setColumnStretch(0, 1)
        self.service_layout.setColumnStretch(1, 1 if wide else 0)
        self.service_cards.updateGeometry()

    def diagnostic_selection(self) -> LocalReportingSettings:
        return LocalReportingSettings(
            host=self.host.text().strip(),
            port=self.port.value(),
            timeout=self.timeout.value(),
            destination=FinsAddress(*(f.value() for f in self.destination)),
            source=FinsAddress(*(f.value() for f in self.source)),
            transport=FinsTransport.TCP
            if self.protocol.currentIndex() == 1
            else FinsTransport.UDP,
        )

    def _draft_preferences(self) -> CommunicationPreferences:
        return CommunicationPreferences(
            self.reporting_panel.selection()
            if self.reporting_panel
            else self._reporting.reporting_selection()
            if self._reporting
            else self._preferences.reporting,
            self.diagnostic_selection(),
            self.web_panel.preferences(),
        )

    @property
    def dirty(self) -> bool:
        try:
            return self._draft_preferences() != self._preferences
        except ValueError:
            return True

    def _show_preferences(self, value: CommunicationPreferences) -> None:
        self._settings_loading = True
        try:
            endpoint = value.diagnostic
            self.host.setText(endpoint.host)
            self.protocol.setCurrentIndex(
                1 if endpoint.transport is FinsTransport.TCP else 0
            )
            self.port.setValue(endpoint.port)
            self.timeout.setValue(endpoint.timeout)
            for fields, address in (
                (self.destination, endpoint.destination),
                (self.source, endpoint.source),
            ):
                for field, number in zip(
                    fields, (address.network, address.node, address.unit), strict=True
                ):
                    field.setValue(number)
            if self.reporting_panel is not None:
                self.reporting_panel.set_selection(value.reporting)
            self.web_panel.set_preferences(value.web)
        finally:
            self._settings_loading = False
        self._refresh()

    def configure_preferences(
        self, path: Path, value: CommunicationPreferences, error: str = ""
    ) -> None:
        if self._reporting is not None and self.reporting_panel is None:
            value = replace(value, reporting=self._reporting.reporting_selection())
        self._preferences_path, self._preferences = path, value
        self._settings_missing = not path.exists()
        self._show_preferences(value)
        self.web_panel.saved_available = not (self._settings_missing or error)
        self.web_panel.refresh()
        self._settings_message = "comm_settings_failed" if error else ""
        self._settings_detail = error
        self.save_settings.show()
        self.discard_settings.show()
        for field in (self.host, self.web_panel.host):
            field.textChanged.connect(self._settings_edited)
        for field in (self.protocol, self.web_panel.interval):
            field.currentIndexChanged.connect(self._settings_edited)
        for field in (
            self.port,
            self.timeout,
            *self.destination,
            *self.source,
            self.web_panel.port,
        ):
            field.valueChanged.connect(self._settings_edited)
        if self.reporting_panel is not None:
            self.reporting_panel.edited.connect(self._settings_edited)
        self._render_preferences()

    def _render_preferences(self) -> None:
        dirty = self.dirty
        self.web_panel.saved = self._preferences.web
        try:
            self.web_panel.dirty = self.web_panel.preferences() != self._preferences.web
        except ValueError:
            self.web_panel.dirty = True
        self.web_panel.editable = not (self._closing or self._disposed)
        self.web_panel.refresh()
        key = self._settings_message or (
            "comm_settings_dirty"
            if dirty
            else "comm_settings_defaults"
            if self._settings_missing
            else "comm_settings_saved"
        )
        self.preferences_status.setText(text(key, self.language))
        self.preferences_status.setToolTip(
            self._settings_detail or str(self._preferences_path or "")
        )
        self.preferences_status.setAccessibleDescription(
            self.preferences_status.toolTip()
        )
        failed = self._settings_message in (
            "comm_settings_failed",
            "comm_reload_failed",
            "web_power_failed",
        )
        self.preferences_status.setStyleSheet(
            f"color: {theme_colors(self).warning if failed else theme_colors(self).secondary};"
        )
        editable = not (
            self.busy or self._auto_refresh or self._closing or self._disposed
        )
        self.save_settings.setEnabled(
            editable and (dirty or failed or self._settings_missing)
        )
        self.discard_settings.setEnabled(editable and dirty)

    @Slot()
    def _settings_edited(self) -> None:
        if self._settings_loading:
            return
        self._settings_message = self._settings_detail = ""
        self._render_preferences()

    def _commit_preferences(
        self, value: CommunicationPreferences, persist: Callable[[], None]
    ) -> None:
        if self._reporting is not None:
            self._reporting.persist_reporting_selection(value.reporting, persist)
        else:
            persist()
        if value.diagnostic != self._preferences.diagnostic:
            self._watch_fresh.clear()
            self._render_watch()
        self._preferences = value

    @Slot()
    def persist_settings(self) -> None:
        if (
            self._preferences_path is None
            or self._closing
            or self._disposed
            or self.busy
            or self._auto_refresh
        ):
            return
        try:
            value = self._draft_preferences()
            path = self._preferences_path
            self._commit_preferences(
                value, lambda: save_communication_preferences(path, value)
            )
        except (ConfigurationError, ValueError, OSError, RuntimeError) as exc:
            self._settings_message, self._settings_detail = (
                "comm_settings_failed",
                str(exc),
            )
        else:
            self._settings_missing = False
            self.web_panel.saved_available = True
            self._settings_message, self._settings_detail = "comm_settings_saved", ""
        self._refresh()
        if self.reporting_panel is not None:
            self.reporting_panel.refresh()

    def request_discard(self) -> None:
        if (
            not self.dirty
            or self._discard_dialog is not None
            or not self.discard_settings.isEnabled()
        ):
            return
        dialog = QMessageBox(self)
        dialog.setWindowTitle(text("comm_discard", self.language))
        dialog.setText(text("comm_discard_question", self.language))
        accept = dialog.addButton(
            text("comm_discard", self.language), QMessageBox.ButtonRole.AcceptRole
        )
        cancel = dialog.addButton(
            text("cancel", self.language), QMessageBox.ButtonRole.RejectRole
        )
        dialog.setDefaultButton(cancel)
        self._discard_dialog = dialog

        def finished() -> None:
            self._discard_dialog = None
            if dialog.clickedButton() is accept:
                self.reload_settings()
            dialog.deleteLater()

        dialog.finished.connect(finished)
        dialog.open()

    def reload_settings(self) -> None:
        if (
            self._preferences_path is None
            or self._closing
            or self._disposed
            or self.busy
            or self._auto_refresh
        ):
            return
        try:
            value = load_communication_preferences(self._preferences_path)
            self._commit_preferences(value, lambda: None)
        except (ConfigurationError, ValueError, OSError, RuntimeError) as exc:
            self._settings_message, self._settings_detail = (
                "comm_reload_failed",
                str(exc),
            )
        else:
            self._show_preferences(value)
            self.web_panel.saved_available = True
            self._settings_missing = False
            self._settings_message, self._settings_detail = "comm_settings_saved", ""
        self._refresh()

    def _field(
        self, layout: QGridLayout, key: str, widget: QWidget, row: int, column: int
    ) -> None:
        label = self._caption(key)
        label.setBuddy(widget)
        layout.addWidget(label, row, column)
        layout.addWidget(widget, row + 1, column)
        widget.setMinimumHeight(Style.touch)

    def _spin(self, minimum: int, maximum: int, value: int) -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(minimum, maximum)
        spin.setValue(value)
        spin.setMinimumHeight(Style.touch)
        self._inputs.append(spin)
        return spin

    def _button(self, key: str, action: Callable[[], object]) -> QPushButton:
        button = QPushButton()
        button.setMinimumHeight(Style.touch)
        button.clicked.connect(action)
        self._buttons[key] = button
        return button

    @property
    def busy(self) -> bool:
        return self._thread is not None

    @property
    def closed(self) -> bool:
        return self._disposed or (
            self._closing
            and not self.busy
            and (self.web_panel.runtime is None or self.web_panel.runtime.closed)
        )

    def bind_web_runtime(self, runtime: WebRuntime) -> None:
        if self.web_panel.runtime is not None:
            raise RuntimeError("Web runtime already attached")
        self.web_panel.runtime = runtime
        self._render_preferences()

    @Slot(str)
    def _web_action(self, action: str) -> None:
        runtime = self.web_panel.runtime
        if (
            runtime is None
            or self._closing
            or self._disposed
            or runtime.snapshot().busy
            or runtime.snapshot().closing
        ):
            return
        if action == "apply" and (
            self.web_panel.dirty or not self.web_panel.saved_available
        ):
            return
        self.web_panel.operation_error = ""
        try:
            if action == "power":
                if self._preferences_path is None:
                    raise ConfigurationError("No communication settings path")
                target = not runtime.snapshot().owned
                path = self._preferences_path
                # Read the actual file; never serialize any UI draft here. A missing
                # first-use file can be established from effective startup defaults.
                saved = (
                    self._preferences
                    if self._settings_missing and not path.exists()
                    else load_communication_preferences(path)
                )
                self._preferences = replace(self._preferences, web=saved.web)
                value = replace(saved, web=replace(saved.web, enabled=target))
                save_communication_preferences(path, value)
                self._preferences = replace(self._preferences, web=value.web)
                self._settings_missing = False
                self.web_panel.saved_available = True
                self._settings_message = self._settings_detail = ""
                if target:
                    runtime.apply(value.web)  # Includes safe cleanup of failed owners.
                else:
                    runtime.stop()
            elif action == "apply":
                observed = runtime.snapshot()
                if (
                    observed.state is WebState.RUNNING
                    and observed.configuration != self._preferences.web
                ):
                    runtime.apply(self._preferences.web)
        except (ConfigurationError, ValueError, OSError, RuntimeError) as exc:
            self.web_panel.operation_error = f"{type(exc).__name__}: {exc}"[:2000]
            self._settings_message, self._settings_detail = (
                "web_power_failed",
                self.web_panel.operation_error,
            )
        self._render_preferences()

    @property
    def log_count(self) -> int:
        return len(self._log)

    @Slot()
    def _toggle_advanced(self) -> None:
        self.advanced.setVisible(
            self.protocol.currentIndex() == 0 and self.advanced_button.isChecked()
        )

    def set_language(self, language: Language) -> None:
        self.language = language
        self.web_panel.set_language(language)
        self.refresh_icons()
        set_button_caption(
            self.save_settings,
            text("save", language),
            button_width(self.save_settings, ("save",)),
        )
        self.save_settings.setToolTip(text("comm_save_settings", language))
        self.save_settings.setAccessibleName(self.save_settings.toolTip())
        self.discard_settings.setToolTip(text("comm_discard", language))
        self.discard_settings.setAccessibleName(text("comm_discard", language))
        if self.reporting_panel is not None:
            self.reporting_panel.set_language(language)
        for key, caption in self._captions:
            caption.setText(
                text("comm_watch_short" if key == "comm_watch" else key, language)
            )
            caption.setToolTip(text(key, language))
            caption.setAccessibleName(text(key, language))
        for key, button in self._buttons.items():
            button.setText(text(key, language))
            button.setAccessibleName(button.text())
            button.setToolTip(button.text())
        for button in (
            self.watch_editor_button,
            self.watch_add,
            self.advanced_button,
        ):
            button.setText("")
            button.setFixedSize(Style.touch, Style.touch)
            button.setProperty("iconAlignment", None)
        set_button_caption(
            self.manual_button,
            text("comm_write", language),
            button_width(self.manual_button, ("comm_write",)),
        )
        self.manual_button.setAccessibleName(text("comm_registers", language))
        self._watch_action_width = button_width(
            self.watch_auto,
            (
                "comm_watch_remove_short",
                "comm_watch_once_short",
                "comm_watch_auto_short",
                "comm_watch_stop_short",
            ),
        )
        for button, key in (
            (self.watch_remove, "comm_watch_remove"),
            (self.watch_once, "comm_watch_once"),
        ):
            set_button_caption(
                button, text(key + "_short", language), self._watch_action_width
            )
            button.setAccessibleName(text(key, language))
            button.setToolTip(
                text("comm_watch", language) + " · " + text(key, language)
            )
            button.setAccessibleDescription(button.toolTip())
        for button in (self.watch_remove, self.watch_once, self.watch_auto):
            button.setFixedHeight(Style.touch)
        for key, widget in (
            ("comm_protocol", self.protocol),
            ("comm_host", self.host),
            ("comm_port", self.port),
            ("comm_timeout", self.timeout),
            ("comm_address", self.address),
            ("comm_area", self.area),
            ("comm_area", self.watch_area),
            ("comm_address", self.watch_address),
            ("comm_count", self.watch_count),
            ("comm_watch_name", self.watch_name),
            ("comm_watch_group", self.watch_selector),
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
        self.write_values.setPlaceholderText(text("comm_values_hint", language))
        self.write_values.setAccessibleName(text("comm_values", language))
        self.watch_editor_button.setToolTip(
            text("comm_watch_edit", language)
            + "\n"
            + text("comm_watch_limit", language)
        )
        self.watch_table.setHorizontalHeaderLabels(
            tuple(
                text(key, language)
                for key in (
                    "comm_table_group",
                    "comm_word_address",
                    "comm_dec",
                    "comm_table_hex",
                    "comm_last_read",
                )
            )
        )
        for column, key in enumerate(
            (
                "comm_watch_group",
                "comm_word_address",
                "comm_word",
                "comm_hex",
                "comm_last_read",
            )
        ):
            item = self.watch_table.horizontalHeaderItem(column)
            assert item is not None
            item.setToolTip(text(key, language))
        self._render_watch()
        self._refresh()
        self._arrange_services()
        self._arrange_watch()
        self._size_watch_columns()

    def _snapshot(self, kind: RequestKind, count: int = 1) -> CommunicationRequest:
        values = (
            tuple(
                int(word, 10)
                for word in re.split(r"[,\s]+", self.write_values.text().strip())
                if word
            )
            if kind is RequestKind.WRITE
            else ()
        )
        endpoint = self._preferences.diagnostic
        return CommunicationRequest(
            kind,
            endpoint.host,
            endpoint.port,
            endpoint.timeout,
            endpoint.destination,
            endpoint.source,
            self.address.value(),
            len(values) if kind is RequestKind.WRITE else count,
            values,
            self._selected_area(self.area),
            endpoint.transport,
        )

    @property
    def watch_groups(self) -> tuple[RegisterWatchGroup, ...]:
        return tuple(self._watch_groups)

    @property
    def auto_refresh(self) -> bool:
        return self._auto_refresh

    def add_watch_group(self, group: RegisterWatchGroup) -> bool:
        if self.busy or self._auto_refresh or self._closing or self._disposed:
            return False
        if (
            len(self._watch_groups) >= MAX_WATCH_GROUPS
            or sum(g.count for g in self._watch_groups) + group.count > MAX_WATCH_WORDS
        ):
            self._error(ValueError("Watch limit: 16 groups / 2048 words"), None)
            self._refresh()
            return False
        self._watch_groups.append(group)
        self._watch_observations.append(None)
        self._watch_errors.append(None)
        self._render_watch()
        self.watch_selector.setCurrentIndex(len(self._watch_groups) - 1)
        self._refresh()
        return True

    @Slot()
    def _add_watch(self) -> None:
        try:
            group = RegisterWatchGroup(
                self._selected_area(self.watch_area),
                self.watch_address.value(),
                self.watch_count.value(),
                self.watch_name.text().strip(),
            )
        except ValueError as exc:
            self._error(exc, None)
            self._refresh()
            return
        self.add_watch_group(group)

    def remove_watch_group(self, index: int) -> bool:
        if (
            self.busy
            or self._auto_refresh
            or self._closing
            or self._disposed
            or not 0 <= index < len(self._watch_groups)
        ):
            return False
        self._watch_groups.pop(index)
        self._watch_observations.pop(index)
        self._watch_errors.pop(index)
        self._watch_fresh = {
            i if i < index else i - 1 for i in self._watch_fresh if i != index
        }
        self._render_watch()
        self._refresh()
        return True

    @Slot()
    def _remove_watch(self) -> None:
        self.remove_watch_group(self.watch_selector.currentIndex())

    def _render_watch(self) -> None:
        selected = self.watch_selector.currentIndex()
        blocked = self.watch_selector.blockSignals(True)
        self.watch_selector.clear()
        self.watch_table.setRowCount(sum(g.count for g in self._watch_groups))
        row = 0
        for index, (group, observation) in enumerate(
            zip(self._watch_groups, self._watch_observations, strict=True)
        ):
            address = f"{group.area.prefix}{group.start}–{group.area.prefix}{group.start + group.count - 1}"
            name = (
                f"{index + 1} · {group.name}"
                if group.name
                else f"{text('comm_watch_group', self.language)} {index + 1}"
            )
            self.watch_selector.addItem(f"{name} · {address}")
            self.watch_selector.setItemData(
                index, f"{name} · {address}", Qt.ItemDataRole.ToolTipRole
            )
            error = self._watch_errors[index]
            for offset in range(group.count):
                value = (
                    observation.values[offset]
                    if observation and observation.values is not None
                    else None
                )
                observed = (
                    observation.completed_at.astimezone().strftime("%H:%M:%S.%f")[:-3]
                    if observation
                    else "--"
                )
                freshness = text(
                    "comm_current"
                    if index in self._watch_fresh
                    else "comm_previous"
                    if observation
                    else "comm_not_read",
                    self.language,
                )
                cells = (
                    name + (" · ERR" if error else ""),
                    f"{group.area.prefix}{group.start + offset}",
                    str(value) if value is not None else "--",
                    f"{value:04X}" if value is not None else "--",
                    observed,
                )
                for column, content in enumerate(cells):
                    item = QTableWidgetItem(content)
                    if column in (1, 2, 3, 4):
                        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    item.setToolTip(
                        f"{name} · {address}\n{freshness}\n"
                        + self._response_description(observation)
                        + " · "
                        + observed
                        if observation
                        else f"{name} · {address}\n{freshness}"
                    )
                    if error:
                        item.setToolTip(item.toolTip() + "\n" + error)
                    colors = theme_colors(self)
                    item.setForeground(
                        QColor(
                            colors.warning
                            if error
                            else colors.secondary
                            if index not in self._watch_fresh
                            else colors.primary
                        )
                    )
                    if column == 0 and (
                        error or (observation and index not in self._watch_fresh)
                    ):
                        item.setIcon(
                            production_icon(
                                "FAULT" if error else "clock",
                                colors.warning if error else colors.secondary,
                            )
                        )
                    self.watch_table.setItem(row, column, item)
                row += 1
        self.watch_selector.setCurrentIndex(
            min(max(0, selected), len(self._watch_groups) - 1)
        )
        self.watch_selector.blockSignals(blocked)
        self.watch_empty.setVisible(not self._watch_groups)
        self._size_table(self.watch_table, 12)

    @staticmethod
    def _size_table(table: QTableWidget, visible_rows: int) -> None:
        """Size to useful rows, with a bounded viewport and internal scrolling."""
        rows = min(table.rowCount(), visible_rows)
        table.setVisible(rows > 0)
        table.setFixedHeight(
            table.horizontalHeader().sizeHint().height()
            + sum(table.rowHeight(row) for row in range(rows))
            + 2 * table.frameWidth()
        )

    def read_watch_once(self) -> bool:
        if self._auto_refresh:
            return False
        return self._start_watch()

    def start_monitoring(self) -> bool:
        if (
            self._auto_refresh
            or self.busy
            or self._closing
            or self._disposed
            or not self.isVisible()
        ):
            return False
        self._auto_refresh = True
        if not self._start_watch():
            self._auto_refresh = False
            self._refresh()
            return False
        return True

    def stop_monitoring(self) -> None:
        was_active = self._auto_refresh or (self.busy and self._watch_pass)
        self._auto_refresh = False
        self._auto_timer.stop()
        if self._watch_stop is not None:
            self._watch_stop.set()  # Only skip unstarted reads; never close active I/O.
        if was_active and self._watch_state != "comm_watch_failed":
            self._watch_state = "comm_watch_stopped"
        self._refresh()

    @Slot()
    def _toggle_auto(self) -> None:
        if self._auto_refresh:
            self.stop_monitoring()
        else:
            self.start_monitoring()

    @Slot()
    def _auto_tick(self) -> None:
        if not self._auto_refresh:
            return
        if not self.isVisible():
            self.stop_monitoring()
        elif not self.busy:
            if not self._start_watch():
                self.stop_monitoring()

    def _start_watch(self) -> bool:
        if self.busy or self._closing or self._disposed:
            return False
        if not self._watch_groups:
            self._watch_state = "comm_watch_empty"
            self._refresh()
            return False
        self._watch_fresh.clear()
        self._feedback_kind = RequestKind.READ
        try:
            # Manual register fields do not affect watch requests. Endpoint and
            # routing are captured once, then each group gets its own read range.
            base = replace(self._snapshot(RequestKind.READ), values=())
            requests = tuple(
                replace(base, start=g.start, count=g.count, area=g.area)
                for g in self._watch_groups
            )
            for request in requests:
                request.validate()
        except ValueError as exc:
            self._error(exc, None)
            self._watch_state = "comm_watch_failed"
            self._render_watch()
            self._refresh()
            return False
        self._watch_state = "comm_watch_busy"
        self._render_watch()
        return self._launch(requests, watch=True)

    def dispatch(self, kind: RequestKind, *, count: int = 1) -> bool:
        if QThread.currentThread() != self.thread():
            raise RuntimeError("Communication dispatch requires the GUI thread")
        if self.busy or self._auto_refresh or self._closing or self._disposed:
            return False
        self._feedback_kind = kind
        request: CommunicationRequest | None = None
        try:
            request = self._snapshot(kind, count)
            request.validate()
        except (ValueError, PermissionError) as exc:
            self._error(exc, request)
            self._refresh()
            return False
        return self._launch((request,))

    def _launch(
        self, requests: tuple[CommunicationRequest, ...], *, watch: bool = False
    ) -> bool:
        if QThread.currentThread() != self.thread():
            raise RuntimeError("Communication dispatch requires the GUI thread")
        if self._reporting is not None:
            try:
                for request in requests:
                    request.validate()
                    self._reporting.validate_manual_request(request)
            except (ValueError, PermissionError) as exc:
                self._error(exc, requests[0])
                self._refresh()
                return False
        self._request = requests[0]
        self._watch_pass = watch
        self._pass_auto = self._auto_refresh
        stop = self._watch_stop = Event() if watch else None
        self._sequence += 1
        self._completion = None
        self._state_key, self._detail = "comm_busy", ""
        if self._feedback_kind is RequestKind.WRITE:
            self._write_state, self._write_detail = "comm_write_busy", ""
        future: Future[CommunicationResponse | _WatchOutcome] = Future()
        execute = self._execute
        guard = (
            self._reporting.validate_manual_request
            if self._reporting is not None
            else None
        )

        def run() -> None:
            # No widgets or page reference: all socket work/cleanup stays here.
            try:
                if not watch:
                    if guard is not None:
                        guard(requests[0])
                    future.set_result(execute(requests[0]))
                    return
                responses: list[CommunicationResponse] = []
                for request in requests:
                    if stop is not None and stop.is_set():
                        break
                    try:
                        value = execute(request)
                        if (
                            value.request != request
                            or value.values is None
                            or len(value.values) != request.count
                        ):
                            raise RuntimeError("Invalid watch response")
                        responses.append(value)
                    except BaseException as exc:
                        future.set_result(_WatchOutcome(tuple(responses), request, exc))
                        return
                future.set_result(
                    _WatchOutcome(
                        tuple(responses), interrupted=bool(stop and stop.is_set())
                    )
                )
            except BaseException as exc:
                future.set_exception(exc)

        self._delivery.watch(str(self._sequence), future)
        self._thread = Thread(target=run, name="FinsCommunication", daemon=False)
        try:
            self._thread.start()
        except RuntimeError as exc:
            self._thread = None
            self._request = None
            self._watch_pass = False
            self._watch_stop = None
            self._error(exc, requests[0])
            if watch:
                self._auto_refresh = False
                self._watch_state = "comm_watch_failed"
            self._refresh()
            return False
        self._monitor.start()
        self._refresh()
        return True

    @Slot(str, object, object)
    def _completed(self, name: str, value: object, error: object) -> None:
        if self._disposed or name != str(self._sequence) or not self.busy:
            return
        self._completion = (value, error)
        self._finish()

    @Slot()
    def _finish(self) -> None:
        if self._thread is None or self._thread.is_alive() or self._completion is None:
            return
        self._thread.join(0)  # Already exited; never waits for active I/O.
        self._thread = None
        self._monitor.stop()
        value, error = self._completion
        self._completion = None
        request, self._request = self._request, None
        was_watch, self._watch_pass = self._watch_pass, False
        self._watch_stop = None
        if not self._closing:
            if was_watch and isinstance(value, _WatchOutcome) and error is None:
                self._finish_watch(value)
            elif isinstance(error, BaseException):
                self._error(error, request)
                if was_watch:
                    self._auto_refresh = False
                    self._auto_timer.stop()
                    self._watch_state = "comm_watch_failed"
            elif isinstance(value, CommunicationResponse) and value.request == request:
                self.last_response = value
                self._state_key = "comm_" + value.request.kind.value + "_ok"
                self._detail = ""
                if value.values is not None:
                    self.last_read = value
                if value.request.kind is RequestKind.WRITE:
                    self._last_write = value
                    self._write_state, self._write_detail = self._state_key, ""
                self._append(request, self._state_key, value.elapsed_ms)
            else:
                self._error(RuntimeError("Invalid communication result"), request)
                if was_watch:
                    self._auto_refresh = False
                    self._auto_timer.stop()
                    self._watch_state = "comm_watch_failed"
        self._refresh()
        self._check_closed()

    def _finish_watch(self, outcome: _WatchOutcome) -> None:
        for index, response in enumerate(outcome.responses):
            self._watch_observations[index] = response
            self._watch_errors[index] = None
            self._watch_fresh.add(index)
            self._watch_success = response.completed_at
            self.last_response = response
        if outcome.error is not None:
            failed = len(outcome.responses)
            if failed < len(self._watch_errors):
                self._watch_errors[failed] = (
                    f"{type(outcome.error).__name__}: {outcome.error}"[:1200]
                )
            self._auto_refresh = False
            self._auto_timer.stop()
            self._watch_state = "comm_watch_failed"
            self._error(outcome.error, outcome.failed_request)
        else:
            self._state_key, self._detail = "comm_read_ok", ""
            self._watch_state = (
                "comm_watch_stopped"
                if outcome.interrupted or self._watch_state == "comm_watch_stopped"
                else "comm_watch_wait"
                if self._auto_refresh
                else "comm_watch_ok"
            )
            if not self._pass_auto and outcome.responses:
                self._append(outcome.responses[-1].request, "comm_watch_ok", None)
            if self._auto_refresh:
                # Single-shot, completion-based cadence. No pending ticks/bursts;
                # future notification alone never schedules another owned thread.
                self._auto_timer.start()
        self._render_watch()

    def _error(
        self, error: BaseException, request: CommunicationRequest | None
    ) -> None:
        if isinstance(error, ReportingWriteProtectedError):
            key = "plc_reserved_write"
        elif isinstance(error, PermissionError):
            key = "comm_permission"
        elif isinstance(error, FinsTimeoutError):
            key = (
                "comm_write_unknown"
                if request and request.kind is RequestKind.WRITE
                else "comm_timeout_error"
            )
        elif isinstance(error, FinsPlcError):
            key = "comm_plc_error"
        elif isinstance(error, FinsProtocolError):
            key = "comm_protocol_error"
        elif isinstance(error, FinsTransportError):
            key = "comm_transport_error"
        elif isinstance(error, ValueError):
            key = "comm_invalid"
        else:
            key = "comm_unexpected_error"
        self._state_key = key
        self._detail = f"{type(error).__name__}: {error}"[:1200]
        if isinstance(error, FinsPlcError):
            self._detail += f" · 0x{error.end_code:04X}"
        if self._feedback_kind is RequestKind.WRITE:
            self._write_state, self._write_detail = key, self._detail
        self._append(request, key, None, self._detail)

    def _append(
        self,
        request: CommunicationRequest | None,
        outcome: str,
        elapsed_ms: float | None,
        detail: str = "",
    ) -> None:
        self._log.append(
            _LogEntry(
                datetime.now().strftime("%H:%M:%S"),
                "comm_" + request.kind.value if request else "comm_registers",
                f"FINS/{request.transport.name} · {request.host}:{request.port}"
                if request
                else self.host.text()[:64],
                f"{request.area.prefix}{request.start}–{request.area.prefix}{request.start + request.count - 1}"
                if request
                else "",
                outcome,
                elapsed_ms,
                detail,
            )
        )

    @staticmethod
    def _response_description(response: CommunicationResponse) -> str:
        request = response.request
        return f"{response.completed_at.astimezone().strftime('%H:%M:%S')} · FINS/{request.transport.name} · {request.host}:{request.port} · {request.area.prefix}{request.start}–{request.area.prefix}{request.start + request.count - 1} · {response.elapsed_ms:.1f} ms"

    @Slot()
    def _refresh(self) -> None:
        editable = not (
            self.busy or self._auto_refresh or self._closing or self._disposed
        )
        for widget in self._inputs:
            widget.setEnabled(editable)
        for button in self._buttons.values():
            button.setEnabled(editable)
        udp = self.protocol.currentIndex() == 0
        self.advanced.setEnabled(editable and udp)
        self.advanced_button.setEnabled(editable and udp)
        self.advanced_button.setVisible(udp)
        self.advanced.setVisible(udp and self.advanced_button.isChecked())
        self.protocol.setToolTip(
            text("comm_protocol" if udp else "comm_tcp_nodes", self.language)
        )
        for button in (
            self.watch_editor_button,
            self.manual_button,
        ):
            button.setEnabled(not (self._closing or self._disposed))
        endpoint = self._preferences.diagnostic
        self.manual_endpoint.setText(
            text("comm_effective_endpoint", self.language)
            + f" · FINS/{endpoint.transport.name} · {endpoint.host}:{endpoint.port}"
        )
        self.manual_endpoint.setToolTip(text("comm_endpoint_hint", self.language))
        self.watch_remove.setEnabled(editable and bool(self._watch_groups))
        self.watch_once.setEnabled(editable and bool(self._watch_groups))
        self.watch_auto.setEnabled(
            not (self._closing or self._disposed)
            and (self._auto_refresh or (not self.busy and bool(self._watch_groups)))
        )
        self.watch_auto.setChecked(self._auto_refresh)
        action_key = "comm_watch_stop" if self._auto_refresh else "comm_watch_auto"
        action = text(action_key, self.language)
        set_button_caption(
            self.watch_auto,
            text(action_key + "_short", self.language),
            self._watch_action_width,
        )
        self.watch_auto.setAccessibleName(action)
        self.watch_auto.setToolTip(text("comm_watch", self.language) + " · " + action)
        self.watch_auto.setAccessibleDescription(self.watch_auto.toolTip())
        self.watch_selector.setToolTip(self.watch_selector.currentText())
        self.watch_selector.setAccessibleDescription(self.watch_selector.currentText())
        self.write_button.setEnabled(editable)
        self.write_button.setToolTip(text("comm_local_write", self.language))
        self.permission.setText(text("comm_local_write", self.language))
        key = "comm_closing" if self._closing else self._write_state
        self.status.setText(text(key, self.language))
        self.status.setToolTip(self._write_detail)
        self.status.setAccessibleDescription(self._write_detail)
        self.status.setStyleSheet(
            f"color: {theme_colors(self).warning if self._write_detail else theme_colors(self).accent};"
        )
        self.success.setText(
            text("comm_last_write_ack", self.language)
            + " · "
            + self._response_description(self._last_write)
            if self._last_write
            else ""
        )
        self.success.setToolTip(text("comm_observation", self.language))
        self.success.setVisible(self._last_write is not None)
        self.watch_status.setText(
            text(self._watch_state, self.language)
            + (
                " · " + self._watch_success.astimezone().strftime("%H:%M:%S")
                if self._watch_success
                else ""
            )
        )
        self.watch_status.setToolTip(
            self._detail
            if self._watch_state == "comm_watch_failed"
            else text("comm_watch_limit", self.language)
        )
        self.watch_status.setVisible(bool(self._watch_groups))
        self.watch_status.setStyleSheet(
            f"color: {theme_colors(self).warning if self._watch_state == 'comm_watch_failed' else theme_colors(self).accent};"
        )
        self._render_preferences()

    @property
    def feedback_text(self) -> str:
        return text(self._state_key, self.language) + (
            " · " + self._detail.rsplit(" · ", 1)[-1]
            if self._state_key == "comm_plc_error" and self._detail
            else ""
        )

    @property
    def feedback_error(self) -> str:
        return self._detail

    @property
    def feedback_details(self) -> str:
        return "\n".join(
            f"{entry.time} · {text(entry.operation, self.language)} · {entry.target} {entry.words} · {text(entry.outcome, self.language)} · {entry.elapsed_ms} ms · {entry.detail}"
            for entry in self._log
        )

    def request_close(self, after: Callable[[], None]) -> None:
        if self._disposed:
            after()
            return
        if self._closing:
            return
        self.stop_monitoring()
        if self._discard_dialog is not None:
            self._discard_dialog.reject()
        self._closing = True
        self._after_close = after
        if self.web_panel.runtime is not None:
            self.web_panel.runtime.request_close()
        self._refresh()
        self.save_settings.setEnabled(False)
        self._web_close_monitor.start()
        self._check_closed()

    def _check_closed(self) -> None:
        self.web_panel.refresh()
        if self.closed:
            self._web_close_monitor.stop()
            callback, self._after_close = self._after_close, None
            if callback is not None:
                callback()

    def resume(self) -> None:
        """Keep the live workspace usable if the application's close fails."""
        if self._disposed:
            raise RuntimeError("Communication workspace is disposed")
        self._closing = False
        self._web_close_monitor.stop()
        self.save_settings.setEnabled(True)
        self._after_close = None
        if not self.busy:
            self._state_key = "comm_idle"
        self._refresh()

    def dispose(self) -> None:
        """Delivery revocation is allowed only after owned work actually exits."""
        if self.busy or (
            self.web_panel.runtime is not None and not self.web_panel.runtime.closed
        ):
            raise RuntimeError("Cannot dispose an active communication owner")
        if not self._disposed:
            self._disposed = True
            self._auto_timer.stop()
            self._monitor.stop()
            self._web_close_monitor.stop()
            self._delivery.detach()

    def hideEvent(self, event: QHideEvent) -> None:
        if not self._disposed:
            self.stop_monitoring()
        super().hideEvent(event)
