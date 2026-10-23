"""Saved Web preferences and independent observations of the one runtime owner."""

from PySide6.QtCore import QEvent, QObject, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QPainter, QPaintEvent
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QStyle,
    QStyleOptionComboBox,
    QVBoxLayout,
    QWidget,
)

from vimo_demo.application.communication_preferences import WebPreferences
from vimo_demo.web.hosts import local_ipv4_addresses
from vimo_demo.web.runtime import WebRuntime, WebState

from .production_style import (
    Language,
    Style,
    button_width,
    production_icon,
    set_button_caption,
    shell_icon,
    text,
    theme_colors,
)


class _AddressSelector(QComboBox):
    """Use the shell's painted chevron while retaining native combo hit testing."""

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        area = self.style().subControlRect(
            QStyle.ComplexControl.CC_ComboBox,
            option,
            QStyle.SubControl.SC_ComboBoxArrow,
            self,
        )
        painter = QPainter(self)
        shell_icon("chevron", theme_colors(self).secondary).paint(
            painter, area.adjusted(10, 10, -10, -10)
        )
        painter.end()


class WebSettingsPanel(QFrame):
    requested = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("commPanel")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self.language: Language = "zh"
        self.available, self.detail = False, ""
        self.runtime: WebRuntime | None = None
        self.saved = WebPreferences()
        self.dirty = False
        self.editable = True
        self.saved_available = False
        self._address_error = ""
        self.operation_error = ""
        self._url_text = ""
        self._presentation: tuple[object, ...] | None = None
        root = QVBoxLayout(self)
        root.setContentsMargins(Style.space, Style.space, Style.space, Style.space)
        root.setSpacing(Style.small)
        header = QHBoxLayout()
        self.title = QLabel()
        self.title.setStyleSheet("font-size: 18px; font-weight: 600;")
        header.addWidget(self.title)
        self.open_browser = QPushButton()
        self.open_browser.setMinimumHeight(Style.touch)
        self.open_browser.setMinimumWidth(0)
        self.open_browser.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self.open_browser.clicked.connect(self._open_browser)
        self.open_browser.installEventFilter(self)
        url_area = QWidget()
        url_area.setMinimumWidth(0)
        url_area.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        url_row = QHBoxLayout(url_area)
        url_row.setContentsMargins(0, 0, 0, 0)
        url_row.addWidget(self.open_browser)
        header.addWidget(url_area, 1)
        self.actual = QLabel()
        header.addWidget(self.actual)
        self.power = QPushButton()
        self.power.setMinimumHeight(Style.touch)
        self.power.setObjectName("webPower")
        self.power.setProperty("iconAlignment", "leading")
        self.power.clicked.connect(lambda: self.requested.emit("power"))
        header.addWidget(self.power)
        root.addLayout(header)
        grid = QGridLayout()
        self.addresses = _AddressSelector()
        self.addresses.setEditable(True)
        self.addresses.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.addresses.setObjectName("webAddress")
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
        actions.addStretch()
        self.apply_changes = QPushButton()
        self.apply_changes.setProperty("iconAlignment", "leading")
        self.apply_changes.setMinimumHeight(Style.touch)
        self.apply_changes.clicked.connect(lambda: self.requested.emit("apply"))
        actions.addWidget(self.apply_changes)
        root.addLayout(actions)
        self.actual.setTextFormat(Qt.TextFormat.PlainText)
        self.set_preferences(WebPreferences())
        self.host.textChanged.connect(self.refresh)
        self.port.valueChanged.connect(self.refresh)
        self.interval.currentIndexChanged.connect(self.refresh)
        self.set_language("zh")
        self.refresh_addresses()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.open_browser and event.type() is QEvent.Type.Resize:
            self._render_url()
        return super().eventFilter(watched, event)

    def _render_url(self) -> None:
        width = (
            self.open_browser.width()
            - self.open_browser.iconSize().width()
            - 2 * Style.space
            - Style.small
        )
        self.open_browser.setText(
            self.open_browser.fontMetrics().elidedText(
                self._url_text, Qt.TextElideMode.ElideRight, max(0, width)
            )
        )

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
        self.saved = value
        self.host.setText(value.host)
        self.port.setValue(value.port)
        self.interval.setCurrentIndex((500, 1000, 2000).index(value.interval_ms))

    def preferences(self) -> WebPreferences:
        return WebPreferences(
            self.saved.enabled,
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
        self.title.setText("Web")
        self.title.setToolTip(
            text("web_observer", language) + "\n" + text("web_boundary", language)
        )
        self.title.setAccessibleName(text("web_observer", language))
        for key, label in self._captions:
            label.setText(
                "IPv4"
                if key == "web_bind"
                else text("short_refresh" if key == "web_refresh" else key, language)
            )
            label.setToolTip(text(key, language))
            label.setAccessibleName(text(key, language))
        for key, field in (
            ("web_bind", self.addresses),
            ("comm_port", self.port),
            ("web_refresh", self.interval),
        ):
            field.setAccessibleName(text(key, language))
        self.host.setToolTip(text("web_boundary", language))
        self.refresh()

    def refresh(self) -> None:
        observed = self.runtime.snapshot() if self.runtime else None
        colors = theme_colors(self)
        presentation = (
            observed,
            self.available if observed is None else None,
            self.detail if observed is None else None,
            self.saved,
            self.dirty,
            self.editable,
            self.saved_available,
            self.operation_error,
            self._address_error,
            self.language,
            colors,
            self.host.text(),
            self.port.value(),
            self.interval.currentIndex(),
        )
        if presentation == self._presentation:
            return  # Keep an open address popup stable across unchanged bridge ticks.
        self._presentation = presentation
        if observed is not None:
            self.available = observed.state is WebState.RUNNING
            self.detail = observed.error or observed.url
        key = (
            "web_live" if self.available else "web_failed" if self.detail else "web_off"
        )
        if observed is not None:
            key = "web_" + observed.state.name.lower()
        self.actual.setText(
            "OFF" if key in ("web_off", "web_stopped") else text(key, self.language)
        )
        self.actual.setToolTip(
            text(key, self.language)
            + "\n"
            + text("web_boundary", self.language)
            + ("\n" + self.detail if self.detail else "")
            + "\n"
            + text(
                "web_saved_on" if self.saved.enabled else "web_saved_off", self.language
            )
            + f" · {self.saved.host}:{self.saved.port} · {self.saved.interval_ms} ms"
            + ("\n" + self.operation_error if self.operation_error else "")
        )
        self.actual.setAccessibleDescription(self.actual.toolTip())
        self.actual.setStyleSheet(
            f"color: {colors.warning if self.operation_error or (observed is not None and (observed.state is WebState.FAILED or observed.error)) else colors.accent if self.available else colors.secondary};"
        )
        blocked = (
            not self.editable or observed is None or observed.busy or observed.closing
        )
        self.power.setIcon(production_icon("power", colors.primary))
        action = text(
            "web_turn_off"
            if (observed.owned if observed is not None else self.available)
            else "web_turn_on",
            self.language,
        )
        set_button_caption(
            self.power,
            action,
            button_width(
                self.power,
                ("plc_enable", "plc_disable", "web_turn_on", "web_turn_off"),
                padding=Style.space + Style.small,
            ),
        )
        self.power.setEnabled(not blocked)
        self.power.setToolTip(action + "\n" + text("web_power_tip", self.language))
        changed = (
            observed is not None
            and observed.state is WebState.RUNNING
            and observed.configuration != self.saved
        )
        self.apply_changes.setVisible(changed)
        self.apply_changes.setIcon(production_icon("reset", colors.primary))
        set_button_caption(
            self.apply_changes,
            text("web_apply_changes", self.language),
            button_width(self.apply_changes, ("web_apply_changes",)),
        )
        self.apply_changes.setEnabled(
            not blocked and not self.dirty and self.saved_available
        )
        self.apply_changes.setToolTip(
            text("web_save_first" if self.dirty else "web_apply_changes", self.language)
        )
        self.address_refresh.setIcon(production_icon("reset", colors.secondary))
        self.addresses.setToolTip(text("web_choose_address", self.language))
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
        self._url_text = running.url if running is not None else ""
        self.open_browser.setIcon(shell_icon("globe", colors.secondary))
        self.open_browser.setToolTip(
            text("web_open", self.language)
            + ("\n" + running.url if running is not None else "")
        )
        self.open_browser.setAccessibleName(text("web_open", self.language))
        self.open_browser.setAccessibleDescription(self._url_text)
        self.open_browser.setEnabled(running is not None)
        self.open_browser.setVisible(running is not None)
        self._render_url()
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
