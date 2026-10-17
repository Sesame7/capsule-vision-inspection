"""Next-launch Web preferences and separate observations of the current owner."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
)

from vimo_demo.application.communication_preferences import WebPreferences

from .production_style import Language, Style, text


class WebSettingsPanel(QFrame):
    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("commPanel")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self.language: Language = "zh"
        self.launched: WebPreferences | None = None
        self.available, self.detail = False, ""
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
        self.host = QLineEdit()
        self.host.setMaxLength(15)
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.interval = QComboBox()
        self.interval.addItems(("500 ms", "1000 ms", "2000 ms"))
        self._captions: list[tuple[str, QLabel]] = []
        for column, (key, widget) in enumerate(
            (
                ("web_bind", self.host),
                ("comm_port", self.port),
                ("web_refresh", self.interval),
            )
        ):
            label = QLabel()
            self._captions.append((key, label))
            grid.addWidget(label, 0, column)
            grid.addWidget(widget, 1, column)
            grid.setColumnStretch(column, 2 if column == 0 else 1)
            widget.setMinimumHeight(Style.touch)
        root.addLayout(grid)
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
        key = (
            "web_live" if self.available else "web_failed" if self.detail else "web_off"
        )
        self.actual.setText(text(key, self.language))
        self.actual.setToolTip(
            text("web_boundary", self.language)
            + ("\n" + self.detail if self.detail else "")
        )
        self.actual.setAccessibleDescription(self.actual.toolTip())
        try:
            changed = self.launched is not None and self.preferences() != self.launched
        except ValueError:
            changed = True
        self.restart.setText(text("web_restart", self.language))
        self.restart.setVisible(changed)
