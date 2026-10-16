"""Session-local FINS reads/watches; one background owner, independent of Production."""

import re
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, replace
from datetime import datetime
from threading import Event, Thread

from PySide6.QtCore import QSize, Qt, QThread, QTimer, Slot
from PySide6.QtGui import QHideEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
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
    ProductionReportingCommands,
    ReportingWriteProtectedError,
)

from .inference_bridge import FutureDelivery
from .production_style import Language, Style, navigation_icon, text, theme_colors
from .reporting_panel import ReportingPanel


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

        if reporting is not None and reporting.reporting_editable:
            self.reporting_panel = ReportingPanel(reporting)
            layout.addWidget(self.reporting_panel)
        self.settings_button = (
            self.reporting_panel.settings_button
            if self.reporting_panel
            else QPushButton()
        )
        self.settings_button.setIcon(navigation_icon(1))
        self.settings_button.setIconSize(QSize(Style.icon, Style.icon))
        self.settings_button.setFixedSize(Style.touch, Style.touch)
        self.settings_button.clicked.connect(self.open_settings)
        self.settings_dialog = QDialog(self)
        self.settings_dialog.resize(820, 700)
        dialog_layout = QVBoxLayout(self.settings_dialog)
        settings_scroll = QScrollArea()
        settings_scroll.setWidgetResizable(True)
        settings_scroll.setFrameShape(QFrame.Shape.NoFrame)
        settings_body = QWidget()
        settings_body.setObjectName("commBody")
        settings_layout = QVBoxLayout(settings_body)
        if self.reporting_panel is not None:
            settings_layout.addWidget(self.reporting_panel.settings_body)
        settings_scroll.setWidget(settings_body)
        dialog_layout.addWidget(settings_scroll)
        settings = self._panel(settings_layout, "comm_manual_connection")
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
        self.test_button = self._button(
            "comm_test", lambda: self.dispatch(RequestKind.TEST)
        )
        test_row.addWidget(self.test_button)
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
        settings_layout.addStretch()
        self.settings_close = QPushButton()
        self.settings_close.setMinimumHeight(Style.touch)
        self.settings_close.clicked.connect(self.settings_dialog.reject)
        dialog_layout.addWidget(self.settings_close, 0, Qt.AlignmentFlag.AlignRight)

        watch_header = QWidget()
        watch_heading = QHBoxLayout(watch_header)
        watch_heading.setContentsMargins(0, 0, 0, 0)
        watch_heading.addWidget(self._caption("comm_watch"))
        self.manual_endpoint = QLabel()
        watch_heading.addStretch()
        watch_heading.addWidget(self.manual_endpoint)
        if self.reporting_panel is None:
            watch_heading.addWidget(self.settings_button)
        watch = self._panel(layout, "comm_watch", header=watch_header)
        self.watch_editor_button = self._button(
            "comm_watch_edit", self._toggle_watch_editor
        )
        self.watch_editor_button.setCheckable(True)
        self.watch_editor_button.setIcon(navigation_icon(0))
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
        actions = QHBoxLayout()
        self.watch_selector = QComboBox()
        self.watch_selector.setMinimumHeight(Style.touch)
        self.watch_selector.setMinimumContentsLength(12)
        self.watch_selector.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.watch_selector.currentIndexChanged.connect(self._refresh)
        self._inputs.append(self.watch_selector)
        actions.addWidget(self.watch_selector, 1)
        actions.addWidget(self.watch_editor_button)
        self.watch_remove = self._button("comm_watch_remove", self._remove_watch)
        self.watch_once = self._button("comm_watch_once", self.read_watch_once)
        self.watch_auto = self._button("comm_watch_auto", self._toggle_auto)
        self.watch_auto.setCheckable(True)
        for button in (self.watch_remove, self.watch_once, self.watch_auto):
            actions.addWidget(button)
        watch.addLayout(actions)
        watch.addWidget(self.watch_editor)
        self.watch_editor.hide()
        self.watch_status = QLabel()
        self.watch_status.setWordWrap(True)
        self.watch_status.setTextFormat(Qt.TextFormat.PlainText)
        watch.addWidget(self.watch_status)
        self.watch_table = QTableWidget(0, 5)
        self.watch_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.watch_table.verticalHeader().hide()
        self.watch_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self.watch_table.horizontalHeader().setMinimumSectionSize(90)
        self.watch_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Interactive
        )
        self.watch_table.setColumnWidth(0, 160)
        for column in (1, 2, 3):
            self.watch_table.horizontalHeader().setSectionResizeMode(
                column, QHeaderView.ResizeMode.Stretch
            )
        self.watch_table.setMinimumWidth(200)
        self.watch_table.setTextElideMode(Qt.TextElideMode.ElideRight)
        watch.addWidget(self.watch_table)
        self.watch_empty = self._caption("comm_watch_empty")
        watch.addWidget(self.watch_empty)
        self.watch_area.currentIndexChanged.connect(self._watch_area_changed)
        self._watch_area_changed()

        self.manual_button = self._button("comm_registers", self._toggle_manual)
        self.manual_button.setCheckable(True)
        self.manual_button.setIcon(navigation_icon(2))
        operations = self._panel(layout, "comm_registers", header=self.manual_button)
        self.manual_body = QWidget()
        manual = QVBoxLayout(self.manual_body)
        manual.setContentsMargins(0, 0, 0, 0)
        operations.addWidget(self.manual_body)
        self.manual_body.hide()
        register_row = QGridLayout()
        self.area = self._area_selector()
        self.address = self._spin(0, 32767, 100)
        self.address.setPrefix("D")
        self.count = self._spin(1, 999, 1)
        self._field(register_row, "comm_area", self.area, 0, 0)
        self._field(register_row, "comm_address", self.address, 0, 1)
        self._field(register_row, "comm_count", self.count, 0, 2)
        register_row.setColumnStretch(0, 1)
        register_row.setColumnStretch(1, 1)
        self.read_button = self._button(
            "comm_read", lambda: self.dispatch(RequestKind.READ)
        )
        register_row.setColumnStretch(2, 1)
        register_row.addWidget(self.read_button, 1, 3)
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

        self.read_caption = QLabel()
        self.read_caption.setWordWrap(True)
        self.read_caption.setTextFormat(Qt.TextFormat.PlainText)
        manual.addWidget(self.read_caption)
        self.values_table = QTableWidget(0, 3)
        self.values_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.values_table.verticalHeader().hide()
        self.values_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.values_table.setMinimumWidth(200)
        manual.addWidget(self.values_table)

        feedback = self._panel(layout, "comm_feedback")
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        feedback.addWidget(self.status)
        self.success = QLabel()
        self.success.setWordWrap(True)
        self.success.setTextFormat(Qt.TextFormat.PlainText)
        feedback.addWidget(self.success)
        self.log_button = self._button("comm_details", self._toggle_log)
        self.log_button.setCheckable(True)
        self.log_button.setIcon(navigation_icon(3))
        feedback.addWidget(self.log_button)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(100)
        self.log.setMinimumHeight(100)
        self.log.setMaximumHeight(240)
        feedback.addWidget(self.log)
        self.log.hide()
        layout.addStretch(1)
        self.host.textChanged.connect(self._refresh)
        self.protocol.currentIndexChanged.connect(self._refresh)
        self.port.valueChanged.connect(self._refresh)
        self.set_language("zh")

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

    @Slot()
    def _toggle_log(self) -> None:
        self.log.setVisible(self.log_button.isChecked())

    @Slot()
    def open_settings(self) -> None:
        if self._closing or self._disposed:
            return
        self.settings_dialog.show()  # Nonblocking; opening does not dispatch I/O.
        self.settings_dialog.raise_()
        self.settings_dialog.activateWindow()

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
        return self._disposed or (self._closing and not self.busy)

    @property
    def log_count(self) -> int:
        return len(self._log)

    @Slot()
    def _toggle_advanced(self) -> None:
        self.advanced.setVisible(self.advanced_button.isChecked())

    def set_language(self, language: Language) -> None:
        self.language = language
        self.settings_dialog.setWindowTitle(text("comm_settings", language))
        self.settings_button.setToolTip(text("comm_settings", language))
        self.settings_button.setAccessibleName(text("comm_settings", language))
        self.settings_close.setText(text("comm_settings_close", language))
        self.settings_close.setAccessibleName(self.settings_close.text())
        if self.reporting_panel is not None:
            self.reporting_panel.set_language(language)
        for key, caption in self._captions:
            caption.setText(text(key, language))
        for key, button in self._buttons.items():
            button.setText(text(key, language))
            button.setAccessibleName(button.text())
            button.setToolTip(button.text())
        for key, widget in (
            ("comm_protocol", self.protocol),
            ("comm_host", self.host),
            ("comm_port", self.port),
            ("comm_timeout", self.timeout),
            ("comm_address", self.address),
            ("comm_count", self.count),
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
        self.test_button.setToolTip(text("comm_test_hint", language))
        self.watch_editor_button.setToolTip(
            text("comm_watch_edit", language)
            + "\n"
            + text("comm_watch_limit", language)
        )
        self.log_button.setToolTip(text("comm_log", language))
        self.log_button.setAccessibleName(text("comm_log", language))
        self.values_table.setHorizontalHeaderLabels(
            (
                text("comm_word_address", language),
                "DEC",
                "HEX",
            )
        )
        self.watch_table.setHorizontalHeaderLabels(
            tuple(
                text(key, language)
                for key in (
                    "comm_table_group",
                    "comm_word_address",
                    "comm_dec",
                    "comm_table_hex",
                    "comm_table_observed",
                )
            )
        )
        for column, key in enumerate(
            (
                "comm_watch_group",
                "comm_word_address",
                "comm_word",
                "comm_hex",
                "comm_observed",
            )
        ):
            item = self.watch_table.horizontalHeaderItem(column)
            assert item is not None
            item.setToolTip(text(key, language))
        for column, key in enumerate(("comm_word_address", "comm_word", "comm_hex")):
            item = self.values_table.horizontalHeaderItem(column)
            assert item is not None
            item.setToolTip(text(key, language))
        self._render_watch()
        self._refresh()

    def _snapshot(self, kind: RequestKind) -> CommunicationRequest:
        values = (
            tuple(
                int(word, 10)
                for word in re.split(r"[,\s]+", self.write_values.text().strip())
                if word
            )
            if kind is RequestKind.WRITE
            else ()
        )
        return CommunicationRequest(
            kind,
            self.host.text().strip(),
            self.port.value(),
            self.timeout.value(),
            FinsAddress(*(field.value() for field in self.destination)),
            FinsAddress(*(field.value() for field in self.source)),
            self.address.value(),
            len(values)
            if kind is RequestKind.WRITE
            else 1
            if kind is RequestKind.TEST
            else self.count.value(),
            values,
            self._selected_area(self.area),
            FinsTransport.TCP
            if self.protocol.currentIndex() == 1
            else FinsTransport.UDP,
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
            for offset in range(group.count):
                value = (
                    observation.values[offset]
                    if observation and observation.values is not None
                    else None
                )
                observed = (
                    observation.completed_at.astimezone().strftime("%H:%M:%S")
                    + " · "
                    + text(
                        "comm_current"
                        if index in self._watch_fresh
                        else "comm_previous",
                        self.language,
                    )
                    if observation
                    else text("comm_not_read", self.language)
                )
                cells = (
                    name,
                    f"{group.area.prefix}{group.start + offset}",
                    str(value) if value is not None else "--",
                    f"{value:04X}" if value is not None else "--",
                    observed,
                )
                for column, content in enumerate(cells):
                    item = QTableWidgetItem(content)
                    if column in (1, 2, 3):
                        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    item.setToolTip(
                        f"{name} · {address}\n"
                        + self._response_description(observation)
                        + " · "
                        + observed
                        if observation
                        else f"{name} · {address}\n{observed}"
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

    def dispatch(self, kind: RequestKind) -> bool:
        if QThread.currentThread() != self.thread():
            raise RuntimeError("Communication dispatch requires the GUI thread")
        if self.busy or self._auto_refresh or self._closing or self._disposed:
            return False
        request: CommunicationRequest | None = None
        try:
            request = self._snapshot(kind)
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
                    self.values_table.setRowCount(len(value.values))
                    for row, word in enumerate(value.values):
                        self.values_table.setItem(
                            row,
                            0,
                            QTableWidgetItem(
                                f"{value.request.area.prefix}{value.request.start + row}"
                            ),
                        )
                        self.values_table.setItem(row, 1, QTableWidgetItem(str(word)))
                        self.values_table.setItem(
                            row, 2, QTableWidgetItem(f"{word:04X}")
                        )
                self._append(request, self._state_key, value.elapsed_ms)
            else:
                self._error(RuntimeError("Invalid communication result"), request)
                if was_watch:
                    self._auto_refresh = False
                    self._auto_timer.stop()
                    self._watch_state = "comm_watch_failed"
        self._refresh()
        if self._closing:
            callback, self._after_close = self._after_close, None
            if callback is not None:
                callback()

    def _finish_watch(self, outcome: _WatchOutcome) -> None:
        for index, response in enumerate(outcome.responses):
            self._watch_observations[index] = response
            self._watch_fresh.add(index)
            self._watch_success = response.completed_at
            self.last_response = response
        if outcome.error is not None:
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
        self.protocol.setToolTip(
            text("comm_protocol" if udp else "comm_tcp_nodes", self.language)
        )
        for button in (
            self.settings_button,
            self.watch_editor_button,
            self.manual_button,
            self.log_button,
        ):
            button.setEnabled(not (self._closing or self._disposed))
        self.manual_endpoint.setText(
            f"{self.protocol.currentText()} · {self.host.text().strip()}:{self.port.value()}"
        )
        self.manual_endpoint.setToolTip(text("comm_manual_connection", self.language))
        self.watch_remove.setEnabled(editable and bool(self._watch_groups))
        self.watch_once.setEnabled(editable and bool(self._watch_groups))
        self.watch_auto.setEnabled(
            not (self._closing or self._disposed)
            and (self._auto_refresh or (not self.busy and bool(self._watch_groups)))
        )
        self.watch_auto.setChecked(self._auto_refresh)
        self.watch_auto.setText(
            text(
                "comm_watch_stop" if self._auto_refresh else "comm_watch_auto",
                self.language,
            )
        )
        self.watch_auto.setToolTip(self.watch_auto.text())
        self.watch_auto.setAccessibleName(self.watch_auto.text())
        self.write_button.setEnabled(editable)
        self.write_button.setToolTip(text("comm_local_write", self.language))
        self.permission.setText(text("comm_local_write", self.language))
        key = "comm_closing" if self._closing else self._state_key
        status = text(key, self.language)
        if key == "comm_busy" and self._request is not None:
            status = status.format(
                operation=text("comm_" + self._request.kind.value, self.language)
            )
        if key == "comm_plc_error" and self._detail:
            status += " · " + self._detail.rsplit(" · ", 1)[-1]
        self.status.setText(status)
        self.status.setToolTip(self._detail)
        self.status.setStyleSheet(
            f"color: {theme_colors(self).warning if self._detail else theme_colors(self).accent};"
        )
        self.success.setText(
            self._response_description(self.last_response)
            if self.last_response
            else text("comm_no_success", self.language)
        )
        self.success.setToolTip(
            text("comm_observation", self.language)
            + (
                "\n"
                + text(
                    "comm_" + self.last_response.request.kind.value + "_ok",
                    self.language,
                )
                + " · "
                + self._response_description(self.last_response)
                if self.last_response
                else ""
            )
        )
        self.success.setVisible(self.last_response is not None)
        self.read_caption.setText(
            text("comm_last_read", self.language)
            + (
                " · " + self._response_description(self.last_read)
                if self.last_read
                else ""
            )
        )
        self._size_table(self.values_table, 8)
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
        lines: list[str] = []
        for entry in self._log:
            elapsed = (
                f" · {entry.elapsed_ms:.1f} ms" if entry.elapsed_ms is not None else ""
            )
            detail = " · " + entry.detail.replace("\n", " ") if entry.detail else ""
            lines.append(
                f"{entry.time} · {text(entry.operation, self.language)} · {entry.target} {entry.words} · {text(entry.outcome, self.language)}{elapsed}{detail}"
            )
        self.log.setPlainText("\n".join(lines))
        self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())

    def request_close(self, after: Callable[[], None]) -> None:
        if self._disposed:
            after()
            return
        if self._closing:
            return
        self.settings_dialog.hide()
        self.stop_monitoring()
        self._closing = True
        self._after_close = after
        self._refresh()
        if not self.busy:
            self._after_close = None
            after()

    def resume(self) -> None:
        """Keep the live workspace usable if the application's close fails."""
        if self._disposed:
            raise RuntimeError("Communication workspace is disposed")
        self._closing = False
        self._after_close = None
        if not self.busy:
            self._state_key = "comm_idle"
        self._refresh()

    def dispose(self) -> None:
        """Delivery revocation is allowed only after owned work actually exits."""
        if self.busy:
            raise RuntimeError("Cannot dispose an active communication owner")
        if not self._disposed:
            self._disposed = True
            self._auto_timer.stop()
            self._monitor.stop()
            self._delivery.detach()

    def hideEvent(self, event: QHideEvent) -> None:
        self.settings_dialog.hide()
        self.stop_monitoring()
        super().hideEvent(event)
