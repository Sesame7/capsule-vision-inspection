"""Manual FINS workspace; one background request, independent of Production."""

import re
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime
from threading import Thread

from PySide6.QtCore import Qt, QThread, QTimer, Slot
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
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from vimo_demo.communication import (
    FinsAddress,
    FinsPlcError,
    FinsProtocolError,
    FinsTimeoutError,
    FinsTransportError,
)
from vimo_demo.communication.operations import (
    CommunicationRequest,
    CommunicationResponse,
    RequestKind,
    execute_request,
)

from .inference_bridge import FutureDelivery
from .production_style import Language, Style, text


@dataclass(frozen=True, slots=True)
class _LogEntry:
    time: str
    operation: str
    target: str
    words: str
    outcome: str
    elapsed_ms: float | None
    detail: str


class CommunicationPage(QWidget):
    """GUI owns controls/delivery; a retained non-daemon thread owns each client.

    Navigation leaves this owner alive. request_close() disables admission, waits
    nonblockingly for actual exit and calls its callback. Final disposal detaches
    delivery; an application-close failure can resume admission on the live page.
    No application/worker/session reference is needed by this workspace.
    """

    def __init__(
        self,
        *,
        execute: Callable[
            [CommunicationRequest], CommunicationResponse
        ] = execute_request,
    ) -> None:
        super().__init__()
        self.setObjectName("communication")
        self.language: Language = "zh"
        self._execute = execute
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
        self._captions: dict[str, QLabel] = {}
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
        self.setStyleSheet(
            f"QWidget#commBody, QScrollArea {{ background: {Style.background}; border: none; }}"
            f"QFrame#commPanel {{ background: {Style.surface}; border: 1px solid {Style.border}; border-radius: 8px; }}"
            f"QSpinBox, QDoubleSpinBox {{ background: {Style.raised}; border: 1px solid {Style.border}; padding: 5px; }}"
            f"QTableWidget, QPlainTextEdit {{ background: {Style.background}; border: 1px solid {Style.border}; }}"
            f"QHeaderView::section {{ background: {Style.raised}; padding: 6px; border: none; }}"
            f"QScrollBar:vertical {{ background: {Style.background}; width: 16px; }}"
            f"QScrollBar::handle:vertical {{ background: {Style.raised}; min-height: 40px; border-radius: 6px; }}"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
            "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }"
        )

        settings = self._panel(layout, "comm_settings")
        common = QGridLayout()
        common.setHorizontalSpacing(Style.space)
        self.protocol = QComboBox()
        self.protocol.addItem("FINS/UDP")
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
        test_row.addWidget(self._caption("comm_test_hint"), 1)
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

        operations = self._panel(layout, "comm_registers")
        register_row = QGridLayout()
        self.address = self._spin(0, 32767, 100)
        self.address.setPrefix("D")
        self.count = self._spin(1, 999, 1)
        self._field(register_row, "comm_address", self.address, 0, 0)
        self._field(register_row, "comm_count", self.count, 0, 1)
        register_row.setColumnStretch(0, 1)
        register_row.setColumnStretch(1, 1)
        self.read_button = self._button(
            "comm_read", lambda: self.dispatch(RequestKind.READ)
        )
        register_row.addWidget(self.read_button, 1, 2)
        operations.addLayout(register_row)
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
        operations.addLayout(write_row)
        self.permission = QLabel()
        self.permission.setWordWrap(True)
        self.permission.setTextFormat(Qt.TextFormat.PlainText)
        self.permission.setProperty("role", "secondary")
        operations.addWidget(self.permission)

        feedback = self._panel(layout, "comm_feedback", stretch=1)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        feedback.addWidget(self.status)
        self.success = QLabel()
        self.success.setWordWrap(True)
        self.success.setTextFormat(Qt.TextFormat.PlainText)
        feedback.addWidget(self.success)
        feedback.addWidget(self._caption("comm_observation"))
        results = QHBoxLayout()
        read_column = QVBoxLayout()
        self.read_caption = QLabel()
        self.read_caption.setWordWrap(True)
        self.read_caption.setTextFormat(Qt.TextFormat.PlainText)
        read_column.addWidget(self.read_caption)
        self.values_table = QTableWidget(0, 2)
        self.values_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.values_table.verticalHeader().hide()
        self.values_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.values_table.setMinimumSize(200, 160)
        read_column.addWidget(self.values_table, 1)
        results.addLayout(read_column, 2)
        log_column = QVBoxLayout()
        log_column.addWidget(self._caption("comm_log"))
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(100)
        self.log.setMinimumSize(260, 160)
        log_column.addWidget(self.log, 1)
        results.addLayout(log_column, 3)
        feedback.addLayout(results, 1)
        self.host.textChanged.connect(self._refresh)
        self.set_language("zh")

    def _panel(
        self, parent: QVBoxLayout, title: str, *, stretch: int = 0
    ) -> QVBoxLayout:
        frame = QFrame()
        frame.setObjectName("commPanel")
        parent.addWidget(frame, stretch)
        contents = QVBoxLayout(frame)
        contents.setContentsMargins(Style.space, Style.space, Style.space, Style.space)
        contents.setSpacing(Style.small)
        label = self._caption(title)
        label.setStyleSheet("font-size: 18px; font-weight: 600;")
        contents.addWidget(label)
        return contents

    def _caption(self, key: str) -> QLabel:
        label = QLabel()
        label.setWordWrap(True)
        label.setProperty("role", "secondary")
        self._captions[key] = label
        return label

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
        for key, caption in self._captions.items():
            caption.setText(text(key, language))
        for key, button in self._buttons.items():
            button.setText(text(key, language))
            button.setAccessibleName(button.text())
            button.setToolTip(button.text())
        for key, widget in (
            ("comm_host", self.host),
            ("comm_port", self.port),
            ("comm_timeout", self.timeout),
            ("comm_address", self.address),
            ("comm_count", self.count),
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
        self.values_table.setHorizontalHeaderLabels(
            (text("comm_word_address", language), text("comm_word", language))
        )
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
        )

    def dispatch(self, kind: RequestKind) -> bool:
        if QThread.currentThread() != self.thread():
            raise RuntimeError("Communication dispatch requires the GUI thread")
        if self.busy or self._closing or self._disposed:
            return False
        request: CommunicationRequest | None = None
        try:
            request = self._snapshot(kind)
            request.validate()
        except (ValueError, PermissionError) as exc:
            self._error(exc, request)
            self._refresh()
            return False
        self._request = request
        self._sequence += 1
        self._completion = None
        self._state_key, self._detail = "comm_busy", ""
        future: Future[CommunicationResponse] = Future()
        execute = self._execute

        def run() -> None:
            # No widgets or page reference: all socket work/cleanup stays here.
            try:
                future.set_result(execute(request))
            except BaseException as exc:
                future.set_exception(exc)

        self._delivery.watch(str(self._sequence), future)
        self._thread = Thread(target=run, name="FinsCommunication", daemon=False)
        try:
            self._thread.start()
        except RuntimeError as exc:
            self._thread = None
            self._request = None
            self._error(exc, request)
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
        if not self._closing:
            if isinstance(error, BaseException):
                self._error(error, request)
            elif isinstance(value, CommunicationResponse) and value.request == request:
                self.last_response = value
                self._state_key = "comm_" + value.request.kind.value + "_ok"
                self._detail = ""
                if value.values is not None:
                    self.last_read = value
                    self.values_table.setRowCount(len(value.values))
                    for row, word in enumerate(value.values):
                        self.values_table.setItem(
                            row, 0, QTableWidgetItem(f"D{value.request.start + row}")
                        )
                        self.values_table.setItem(row, 1, QTableWidgetItem(str(word)))
                self._append(request, self._state_key, value.elapsed_ms)
            else:
                self._error(RuntimeError("Invalid communication result"), request)
        self._refresh()
        if self._closing:
            callback, self._after_close = self._after_close, None
            if callback is not None:
                callback()

    def _error(
        self, error: BaseException, request: CommunicationRequest | None
    ) -> None:
        if isinstance(error, PermissionError):
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
                f"{request.host}:{request.port}" if request else self.host.text()[:64],
                f"D{request.start}–D{request.start + request.count - 1}"
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
        return f"{response.completed_at.astimezone().strftime('%H:%M:%S')} · {request.host}:{request.port} · D{request.start}–D{request.start + request.count - 1} · {response.elapsed_ms:.1f} ms"

    @Slot()
    def _refresh(self) -> None:
        editable = not (self.busy or self._closing or self._disposed)
        for widget in self._inputs:
            widget.setEnabled(editable)
        for button in self._buttons.values():
            button.setEnabled(editable)
        local = self.host.text().strip() == "127.0.0.1"
        self.write_button.setEnabled(editable and local)
        self.write_button.setToolTip(
            text("comm_local_write" if local else "comm_remote_read", self.language)
        )
        self.permission.setText(
            text("comm_local_write" if local else "comm_remote_read", self.language)
        )
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
            f"color: {Style.warning if self._detail else Style.accent};"
        )
        self.success.setText(
            text("comm_last_success", self.language)
            + " · "
            + text(
                "comm_" + self.last_response.request.kind.value + "_ok", self.language
            )
            + " · "
            + self._response_description(self.last_response)
            if self.last_response
            else text("comm_no_success", self.language)
        )
        self.read_caption.setText(
            text("comm_last_read", self.language)
            + (
                " · " + self._response_description(self.last_read)
                if self.last_read
                else ""
            )
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
            self._monitor.stop()
            self._delivery.detach()
