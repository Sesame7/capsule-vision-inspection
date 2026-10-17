"""Saved Web preferences and independent observations of the one runtime owner."""

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
)

from vimo_demo.application.communication_preferences import WebPreferences
from vimo_demo.web.hosts import local_ipv4_addresses
from vimo_demo.web.runtime import WebRuntime, WebState

from .production_style import (
    Language,
    Style,
    production_icon,
    shell_icon,
    text,
    theme_colors,
)


class WebSettingsPanel(QFrame):
    requested = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("commPanel")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self.language: Language = "zh"
        self.launched: WebPreferences | None = None
        self.available, self.detail = False, ""
        self.runtime: WebRuntime | None = None
        self.saved = WebPreferences()
        self.dirty = False
        self.editable = True
        self.saved_available = False
        self._address_error = ""
        root = QVBoxLayout(self)
        root.setContentsMargins(Style.space, Style.space, Style.space, Style.space)
        root.setSpacing(Style.small)
        header = QHBoxLayout()
        self.title = QLabel()
        self.title.setStyleSheet("font-size: 18px; font-weight: 600;")
        self.enabled = QCheckBox()
        self.enabled.setMinimumHeight(Style.touch)
        header.addWidget(self.title)
        header.addWidget(self.enabled)
        header.addStretch()
        self.actual = QLabel()
        header.addWidget(self.actual)
        root.addLayout(header)
        grid = QGridLayout()
        self.addresses = QComboBox()
        self.addresses.setEditable(True)
        self.addresses.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        line = self.addresses.lineEdit()
        assert line is not None
        self.host: QLineEdit = line
        self.host.setMaxLength(15)
        self.address_refresh = QPushButton()
        self.address_refresh.setFixedSize(Style.touch, Style.touch)
        self.address_refresh.clicked.connect(self.refresh_addresses)
        address_row = QHBoxLayout()
        address_row.setContentsMargins(0, 0, 0, 0)
        address_row.setSpacing(Style.small)
        address_row.addWidget(self.addresses, 1)
        address_row.addWidget(self.address_refresh)
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.interval = QComboBox()
        self.interval.addItems(("500 ms", "1000 ms", "2000 ms"))
        self._captions: list[tuple[str, QLabel]] = []
        for column, (key, widget) in enumerate(
            (
                ("web_bind", self.addresses),
                ("comm_port", self.port),
                ("web_refresh", self.interval),
            )
        ):
            label = QLabel()
            self._captions.append((key, label))
            grid.addWidget(label, 0, column)
            if column == 0:
                grid.addLayout(address_row, 1, column)
            else:
                grid.addWidget(widget, 1, column)
            grid.setColumnStretch(column, 2 if column == 0 else 1)
            widget.setMinimumHeight(Style.touch)
        root.addLayout(grid)
        actions = QHBoxLayout()
        self.runtime_buttons: dict[str, QPushButton] = {}
        for action in ("start", "stop", "apply"):
            button = QPushButton()
            button.setMinimumHeight(Style.touch)
            button.clicked.connect(
                lambda _checked=False, name=action: self.requested.emit(name)
            )
            self.runtime_buttons[action] = button
            actions.addWidget(button)
        actions.addStretch()
        root.addLayout(actions)
        self.open_browser = QPushButton()
        self.open_browser.setMinimumHeight(Style.touch)
        self.open_browser.clicked.connect(self._open_browser)
        root.addWidget(self.open_browser, alignment=Qt.AlignmentFlag.AlignLeft)
        self.actual.setTextFormat(Qt.TextFormat.PlainText)
        self.restart = QLabel()
        self.restart.setProperty("role", "secondary")
        root.addWidget(self.restart)
        self.set_preferences(WebPreferences())
        self.enabled.toggled.connect(self.refresh)
        self.host.textChanged.connect(self.refresh)
        self.port.valueChanged.connect(self.refresh)
        self.interval.currentIndexChanged.connect(self.refresh)
        self.set_language("zh")
        self.refresh_addresses()

    def refresh_addresses(self) -> None:
        current = self.host.text()
        try:
            addresses = local_ipv4_addresses()
        except (OSError, ValueError) as exc:
            addresses = ("127.0.0.1",)
            self._address_error = f"{type(exc).__name__}: {exc}"
        else:
            self._address_error = ""
        # Enumeration never selects an adapter or discards an unavailable saved IP.
        self.addresses.blockSignals(True)
        self.host.blockSignals(True)
        self.addresses.clear()
        self.addresses.addItems(addresses)
        self.addresses.setEditText(current)
        self.host.blockSignals(False)
        self.addresses.blockSignals(False)
        self.refresh()

    def _open_browser(self) -> None:
        if self.runtime is not None:
            observed = self.runtime.snapshot()
            if observed.state is WebState.RUNNING and observed.url:
                QDesktopServices.openUrl(QUrl(observed.url))

    def set_preferences(self, value: WebPreferences) -> None:
        self.enabled.setChecked(value.enabled)
        self.host.setText(value.host)
        self.port.setValue(value.port)
        self.interval.setCurrentIndex((500, 1000, 2000).index(value.interval_ms))

    def preferences(self) -> WebPreferences:
        return WebPreferences(
            self.enabled.isChecked(),
            self.host.text().strip(),
            self.port.value(),
            (500, 1000, 2000)[self.interval.currentIndex()],
        )

    def observe(self, available: bool, detail: str) -> None:
        if self.runtime is not None:
            return
        self.available, self.detail = available, detail
        self.refresh()

    def set_language(self, language: Language) -> None:
        self.language = language
        self.title.setText(text("web_observer", language))
        self.enabled.setText(text("plc_enable", language))
        self.enabled.setAccessibleName(
            text("web_observer", language) + " · " + self.enabled.text()
        )
        for key, label in self._captions:
            label.setText(text(key, language))
        for key, field in (
            ("web_bind", self.host),
            ("comm_port", self.port),
            ("web_refresh", self.interval),
        ):
            field.setAccessibleName(text(key, language))
        self.host.setToolTip(text("web_boundary", language))
        self.refresh()

    def refresh(self) -> None:
        observed = self.runtime.snapshot() if self.runtime else None
        if observed is not None:
            self.available = observed.state is WebState.RUNNING
            self.detail = observed.error or observed.url
        key = (
            "web_live" if self.available else "web_failed" if self.detail else "web_off"
        )
        if observed is not None:
            key = "web_" + observed.state.name.lower()
        self.actual.setText(text(key, self.language))
        self.actual.setToolTip(
            text("web_boundary", self.language)
            + ("\n" + self.detail if self.detail else "")
        )
        self.actual.setAccessibleDescription(self.actual.toolTip())
        try:
            changed = self.preferences() != (
                observed.configuration if observed else self.launched
            )
        except ValueError:
            changed = True
        self.restart.setText(text("web_restart", self.language))
        self.restart.setVisible(
            changed and (observed is None or observed.configuration is not None)
        )
        colors = theme_colors(self)
        self.actual.setStyleSheet(
            f"color: {colors.warning if observed is not None and (observed.state is WebState.FAILED or observed.error) else colors.accent if self.available else colors.secondary};"
        )
        blocked = (
            not self.editable or observed is None or observed.busy or observed.closing
        )
        for action, button in self.runtime_buttons.items():
            button.setText(text("web_action_" + action, self.language))
            button.setIcon(
                production_icon(
                    "reset" if action == "apply" else action, colors.secondary
                )
            )
            button.setAccessibleName(button.text())
            allowed = not blocked
            if action != "stop":
                allowed = allowed and not self.dirty and self.saved_available
                if action == "start":
                    allowed = (
                        allowed
                        and self.saved.enabled
                        and observed is not None
                        and not observed.owned
                    )
            else:
                allowed = allowed and observed is not None and observed.owned
            button.setEnabled(allowed)
            button.setToolTip(
                text(
                    "web_save_first"
                    if self.dirty or not self.saved_available
                    else "web_action_" + action,
                    self.language,
                )
            )
        self.address_refresh.setIcon(production_icon("reset", colors.secondary))
        self.address_refresh.setToolTip(
            text("web_addresses", self.language)
            + ("\n" + self._address_error if self._address_error else "")
        )
        self.address_refresh.setAccessibleName(text("web_addresses", self.language))
        running = (
            observed
            if observed is not None and observed.state is WebState.RUNNING
            else None
        )
        self.open_browser.setText(
            running.url if running is not None else text("web_open", self.language)
        )
        self.open_browser.setIcon(shell_icon("globe", colors.secondary))
        self.open_browser.setToolTip(
            text("web_open", self.language)
            + ("\n" + running.url if running is not None else "")
        )
        self.open_browser.setAccessibleName(text("web_open", self.language))
        self.open_browser.setEnabled(running is not None)
        if observed is not None and observed.configuration is not None:
            actual = observed.configuration
            self.actual.setToolTip(
                self.actual.toolTip()
                + f"\n{observed.url} · {actual.interval_ms} ms"
                + (
                    "\n" + text("web_restored", self.language)
                    if observed.restored
                    else ""
                )
            )
            self.actual.setAccessibleDescription(self.actual.toolTip())
