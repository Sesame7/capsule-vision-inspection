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
    LANGUAGES,
    Language,
    Style,
    Theme,
    navigation_icon,
    shell_icon,
    text,
    theme_colors,
)
from .translations import LANGUAGE_NAMES

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
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        row = QHBoxLayout(self)
        self._row = row
        row.setContentsMargins(12, 8, 12, 8)
        row.setSpacing(6)
        self.brand_icon = QLabel()
        self.brand_icon.setFixedSize(30, 30)
        row.addWidget(self.brand_icon)
        brand = QLabel("ViMo")
        self._brand = brand
        brand.setStyleSheet("font-size: 21px; font-weight: 700;")
        row.addWidget(brand)
        row.addSpacing(10)
        self._pages = QButtonGroup(self)
        self._pages.idClicked.connect(self._select_page)
        self.buttons: list[QPushButton] = []
        for index, _ in enumerate(PAGES):
            button = QPushButton()
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
        self.language.setFixedHeight(Style.touch)
        self.language.setIconSize(QSize(20, 20))
        for language in LANGUAGES:
            self.language.addItem(LANGUAGE_NAMES[language], language)
        self.language.view().setMinimumWidth(200)
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
        self.language.setFixedWidth(
            max(
                self.fontMetrics().horizontalAdvance(name)
                for name in LANGUAGE_NAMES.values()
            )
            + 70
        )
        self.brand_icon.setPixmap(shell_icon("brand", colors.accent).pixmap(30, 30))
        for index, (key, button) in enumerate(zip(PAGES, self.buttons, strict=True)):
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
        target = text("theme_light" if theme == "dark" else "theme_dark", language)
        self.theme.setToolTip(target)
        self.theme.setAccessibleName(target)
        self.language.setCurrentIndex(self.language.findData(language))
        for index in range(self.language.count()):
            self.language.setItemIcon(index, shell_icon("globe", colors.primary))
        self.language.setToolTip(text("interface_language", language))
        self.language.setAccessibleName(self.language.toolTip())
        self._arrange_buttons()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if self._presentation is not None:
            self._arrange_buttons()

    def _arrange_buttons(self) -> None:
        if self._presentation is None:
            return
        language = self._presentation[0]
        margins = self._row.contentsMargins()
        reserved = (
            self.brand_icon.width()
            + self._brand.sizeHint().width()
            + 10  # Existing gap after the logo.
            + self.theme.width()
            + self.language.width()
            + (self._row.count() - 1) * self._row.spacing()
            + margins.left()
            + margins.right()
        )
        preferred = (
            max(
                self.fontMetrics().horizontalAdvance(
                    text("nav_diagnostics" if key == "diagnostics" else key, lang)
                )
                for key in PAGES
                for lang in LANGUAGES
            )
            + 48
        )
        width = max(
            Style.touch,
            min(
                preferred, (self.contentsRect().width() - reserved) // len(self.buttons)
            ),
        )
        for key, button in zip(PAGES, self.buttons, strict=True):
            button.setFixedWidth(width)
            caption = text("nav_diagnostics" if key == "diagnostics" else key, language)
            button.setText(
                button.fontMetrics().elidedText(
                    caption, Qt.TextElideMode.ElideRight, max(0, width - 48)
                )
            )


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
