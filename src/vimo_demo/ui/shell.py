"""Shared navigation and compact status presentation, without runtime ownership."""

from PySide6.QtCore import QSize, Qt, Signal, Slot
from PySide6.QtGui import QPainter, QPaintEvent, QResizeEvent
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QWidget,
)

from .production_style import (
    Language,
    Style,
    Theme,
    navigation_icon,
    shell_icon,
    text,
    theme_colors,
)

PAGES = ("production", "parameters", "diagnostics", "history", "settings")


class _StatusLabel(QLabel):
    """Keep full status text accessible while fitting its stable shell column."""

    def __init__(self) -> None:
        super().__init__()
        self._full_text = ""

    def setText(self, text: str) -> None:
        self._full_text = text
        self.setAccessibleName(text)
        self._elide()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._elide()

    def _elide(self) -> None:
        super().setText(
            self.fontMetrics().elidedText(
                self._full_text, Qt.TextElideMode.ElideRight, self.width()
            )
        )


class _LanguageSelector(QComboBox):
    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.drawPixmap(
            self.width() - 24,
            (self.height() - 16) // 2,
            shell_icon("chevron", theme_colors(self).primary).pixmap(16, 16),
        )
        painter.end()


class NavigationBar(QFrame):
    page_selected = Signal(int)
    language_selected = Signal(str)
    theme_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._presentation: tuple[Language, Theme, int] | None = None
        self.setObjectName("navigation")
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 8, 12, 8)
        row.setSpacing(6)
        self.brand_icon = QLabel()
        self.brand_icon.setFixedSize(30, 30)
        row.addWidget(self.brand_icon)
        brand = QLabel("ViMo")
        brand.setStyleSheet("font-size: 21px; font-weight: 700;")
        row.addWidget(brand)
        row.addSpacing(10)
        self._pages = QButtonGroup(self)
        self._pages.idClicked.connect(self._select_page)
        self.buttons: list[QPushButton] = []
        for index, _ in enumerate(PAGES):
            button = QPushButton()
            button.setProperty("iconAlignment", "leading")
            button.setCheckable(True)
            button.setMinimumHeight(Style.touch)
            button.setIconSize(QSize(20, 20))
            self._pages.addButton(button, index)
            row.addWidget(button)
            self.buttons.append(button)
        row.addStretch(1)
        self.theme = QPushButton()
        self.theme.setFixedSize(Style.touch, Style.touch)
        self.theme.setIconSize(QSize(22, 22))
        self.theme.clicked.connect(self._request_theme)
        row.addWidget(self.theme)
        self.language = _LanguageSelector()
        self.language.setObjectName("language")
        self.language.setFixedSize(108, Style.touch)
        self.language.setIconSize(QSize(20, 20))
        self.language.addItem("中文", "zh")
        self.language.addItem("EN", "en")
        self.language.activated.connect(self._select_language)
        row.addWidget(self.language)

    @Slot(int)
    def _select_page(self, index: int) -> None:
        self.page_selected.emit(index)

    @Slot()
    def _request_theme(self) -> None:
        self.theme_requested.emit()

    @Slot(int)
    def _select_language(self, index: int) -> None:
        self.language_selected.emit(str(self.language.itemData(index)))

    def present(self, language: Language, theme: Theme, page: int) -> None:
        identity = language, theme, page
        if identity == self._presentation:
            return
        self._presentation = identity
        colors = theme_colors(self)
        # Resolve the inherited stylesheet font before the first width calculation.
        self.ensurePolished()
        # Reserve the longest supported caption, so language changes do not move tabs.
        width = (
            max(
                self.fontMetrics().horizontalAdvance(
                    text(key, lang) if key != "diagnostics" or lang == "zh" else "Comms"
                )
                for key in PAGES
                for lang in ("zh", "en")
            )
            + 48
        )
        for button in self.buttons:
            button.setFixedWidth(width)
        self.brand_icon.setPixmap(shell_icon("brand", colors.accent).pixmap(30, 30))
        for index, (key, button) in enumerate(zip(PAGES, self.buttons, strict=True)):
            label = (
                "Comms"
                if key == "diagnostics" and language == "en"
                else text(key, language)
            )
            button.setText(label)
            button.setToolTip(text(key, language))
            button.setAccessibleName(text(key, language))
            button.setChecked(index == page)
            button.setIcon(
                navigation_icon(
                    index, colors.accent if index == page else colors.secondary
                )
            )
        self.theme.setIcon(
            shell_icon("sun" if theme == "dark" else "moon", colors.primary)
        )
        target = (
            ("切换浅色主题" if theme == "dark" else "切换深色主题")
            if language == "zh"
            else (
                "Switch to light theme" if theme == "dark" else "Switch to dark theme"
            )
        )
        self.theme.setToolTip(target)
        self.theme.setAccessibleName(target)
        self.language.setCurrentIndex(0 if language == "zh" else 1)
        for index in range(2):
            self.language.setItemIcon(index, shell_icon("globe", colors.primary))
        self.language.setToolTip(
            "界面语言" if language == "zh" else "Interface language"
        )
        self.language.setAccessibleName(self.language.toolTip())


class StatusBar(QFrame):
    def __init__(self) -> None:
        super().__init__()
        self._theme: str | None = None
        self.setObjectName("event")
        self.setFixedHeight(44)
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 4, 12, 4)
        row.setSpacing(10)
        self.state_icon, self.state = self._item(row)
        self.plc_icon, self.plc = self._item(row)
        self.web_icon, self.web = self._item(row)
        self.event_icon, self.event_label = self._item(row, flexible=True)

    @staticmethod
    def _item(row: QHBoxLayout, *, flexible: bool = False) -> tuple[QLabel, QLabel]:
        if row.count():
            separator = QFrame()
            separator.setFrameShape(QFrame.Shape.VLine)
            separator.setFixedHeight(18)
            row.addWidget(separator)
        group = QWidget()
        content = QHBoxLayout(group)
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(8)
        icon = QLabel()
        icon.setFixedSize(20, 20)
        content.addWidget(icon)
        label = QLabel() if flexible else _StatusLabel()
        label.setTextFormat(Qt.TextFormat.PlainText)
        # Translation length must not redistribute the icon/text groups.
        label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        content.addWidget(label, 1)
        row.addWidget(group, 3 if flexible else 1)
        return icon, label

    def refresh_icons(self) -> None:
        colors = theme_colors(self)
        if self._theme == colors.background:
            return
        self._theme = colors.background
        for label, kind in ((self.plc_icon, "plc"), (self.web_icon, "globe")):
            label.setPixmap(shell_icon(kind, colors.secondary).pixmap(20, 20))
