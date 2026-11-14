"""Shared navigation and compact status presentation, without runtime ownership."""

from math import ceil

from PySide6.QtCore import QEvent, QObject, QPoint, QSize, Qt, Signal, Slot
from PySide6.QtGui import QFontMetricsF, QPaintEvent, QResizeEvent
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QStyle,
    QStyleOptionButton,
    QStyleOptionComboBox,
    QStylePainter,
    QWidget,
)

from capsule_vision.localization import languages as language_identity
from capsule_vision.localization.catalog import text

from .style import Style, Theme, navigation_icon, shell_icon, theme_colors

# Full page names remain the accessible destinations; only the visible captions
# are shortened. Insertion order is also the existing page stack/group order.
PAGE_CAPTIONS = {
    "page_production": "nav_inspect",
    "page_recipe": "page_recipe",
    "page_communication": "nav_plc",
    "page_history": "nav_records",
    "page_vision": "nav_cameras",
}
PAGES = tuple(PAGE_CAPTIONS)
# Inspect and PLC fit the 116px base slot. These small allowances fit longer
# localized captions while leaving a gap before the portrait window's utilities.
_PAGE_WIDTHS = {"page_recipe": 128, "page_history": 132, "page_vision": 132}


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
    def showPopup(self) -> None:
        # The closed control is deliberately narrower than the native names.
        # Keep the native view/model and its keyboard/scroll behavior.
        view = self.view()
        popup = view.window()
        available = self.screen().availableGeometry()
        popup.setMinimumWidth(
            min(
                max(
                    self.fontMetrics().horizontalAdvance(self.itemText(index))
                    for index in range(self.count())
                )
                + 60,
                available.width(),
            )
        )
        super().showPopup()
        # Windows may place the list against full screen geometry, including
        # the taskbar. Use the actual work area and all available row space.
        handle = popup.windowHandle()
        rows_height = sum(view.sizeHintForRow(i) for i in range(self.count()))
        overhead = popup.height() - view.viewport().height()
        margins = handle.frameMargins()
        popup.resize(
            min(popup.width(), available.width() - margins.left() - margins.right()),
            min(
                rows_height + overhead,
                available.height() - margins.top() - margins.bottom(),
            ),
        )
        frame = handle.frameGeometry()
        # Native resize events may still be queued; use the requested widget
        # size rather than the handle's previous dimensions for placement.
        frame.setSize(
            QSize(
                popup.width() + margins.left() + margins.right(),
                popup.height() + margins.top() + margins.bottom(),
            )
        )
        left = max(
            available.left(), min(frame.left(), available.right() - frame.width() + 1)
        )
        top = max(
            available.top(), min(frame.top(), available.bottom() - frame.height() + 1)
        )
        popup.move(QPoint(left, top))

    def paintEvent(self, event: QPaintEvent) -> None:
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        # Only the closed label is compact; the native popup/model retains full
        # language names, item identities and keyboard selection behavior.
        option.currentText = language_identity.LANGUAGE_LABELS.get(
            str(self.currentData()), self.currentText()
        )
        painter = QStylePainter(self)
        painter.drawComplexControl(QStyle.ComplexControl.CC_ComboBox, option)
        painter.drawControl(QStyle.ControlElement.CE_ComboBoxLabel, option)
        painter.drawPixmap(
            self.width() - 24,
            (self.height() - 16) // 2,
            shell_icon("chevron", theme_colors(self).primary).pixmap(16, 16),
        )
        painter.end()


class _NavigationButton(QPushButton):
    """One native page action with a language-independent slot and elided paint."""

    def __init__(self, page: str) -> None:
        super().__init__()
        self._caption_key = PAGE_CAPTIONS[page]
        self._font_key = ""
        self._hint = QSize(_PAGE_WIDTHS.get(page, 116), Style.touch)
        self.setCheckable(True)
        self.setProperty("iconAlignment", "center")
        self.setIconSize(QSize(20, 20))
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)

    def sizeHint(self) -> QSize:
        font_key = self.font().key()
        if self._font_key != font_key:
            metrics = QFontMetricsF(self.font())
            captions = (
                text(self._caption_key, lang) for lang in language_identity.LANGUAGES
            )
            line_height = max(
                metrics.boundingRect(label).height() for label in captions
            )
            self._hint.setHeight(max(Style.touch, ceil(line_height) + 12))
            self._font_key = font_key
        return self._hint

    def minimumSizeHint(self) -> QSize:
        # Allow elision below the workstation widths without raising the shell minimum.
        return QSize(80, self.sizeHint().height())

    def paintEvent(self, event: QPaintEvent) -> None:
        option = QStyleOptionButton()
        self.initStyleOption(option)
        contents = self.style().subElementRect(
            QStyle.SubElement.SE_PushButtonContents, option, self
        )
        budget = contents.width() - option.iconSize.width() - Style.icon_text_gap
        option.text = self.fontMetrics().elidedText(
            option.text, Qt.TextElideMode.ElideRight, max(0, budget)
        )
        painter = QStylePainter(self)
        painter.drawControl(QStyle.ControlElement.CE_PushButton, option)
        painter.end()


class NavigationBar(QFrame):
    page_selected = Signal(int)
    language_selected = Signal(str)
    theme_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._presentation: tuple[language_identity.Language, Theme, int] | None = None
        self._username: str | None = None
        self._user_detail = ""
        self.setObjectName("navigation")
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 8, 12, 8)
        row.setSpacing(6)
        self._pages = QButtonGroup(self)
        self._pages.idClicked.connect(self._select_page)
        self.buttons: list[_NavigationButton] = []
        for index, page in enumerate(PAGES):
            button = _NavigationButton(page)
            self._pages.addButton(button, index)
            row.addWidget(button)
            self.buttons.append(button)
        row.addStretch()
        self.user = QPushButton()
        self.user.setObjectName("current_user")
        self.user.setFixedWidth(192)
        self.user.setMinimumHeight(Style.touch)
        self.user.setIconSize(QSize(20, 20))
        row.addWidget(self.user)
        self.theme = QPushButton()
        self.theme.setFixedSize(Style.touch, Style.touch)
        self.theme.setIconSize(QSize(22, 22))
        self.theme.clicked.connect(self._request_theme)
        row.addWidget(self.theme)
        self.language = _LanguageSelector()
        self.language.setObjectName("language")
        self.language.setMinimumHeight(Style.touch)
        self.language.setIconSize(QSize(20, 20))
        for language in language_identity.LANGUAGE_DISPLAY_ORDER:
            self.language.addItem(language_identity.LANGUAGE_NAMES[language], language)
        # Use the screen's available height before Qt falls back to scrolling.
        self.language.setMaxVisibleItems(len(language_identity.LANGUAGES))
        self.language.activated.connect(self._select_language)
        row.addWidget(self.language)
        self.language.installEventFilter(self)
        self._size_language_selector()

    @Slot(int)
    def _select_page(self, index: int) -> None:
        self.page_selected.emit(index)

    @Slot()
    def _request_theme(self) -> None:
        self.theme_requested.emit()

    @Slot(int)
    def _select_language(self, index: int) -> None:
        self.language_selected.emit(str(self.language.itemData(index)))

    def present(
        self, language: language_identity.Language, theme: Theme, page: int
    ) -> None:
        identity = language, theme, page
        if identity == self._presentation:
            return
        self._presentation = identity
        colors = theme_colors(self)
        # Resolve the inherited stylesheet font before the first width calculation.
        self.ensurePolished()
        for index, (key, button) in enumerate(zip(PAGES, self.buttons, strict=True)):
            caption, destination = (
                text(PAGE_CAPTIONS[key], language),
                text(key, language),
            )
            button.setText(caption)
            button.setToolTip(destination)
            button.setAccessibleName(destination)
            button.setAccessibleDescription(caption if caption != destination else "")
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
        self.language.setToolTip(
            f"{text('interface_language', language)} · {language_identity.LANGUAGE_NAMES[language]}"
        )
        self.language.setAccessibleName(text("interface_language", language))
        self.present_user(self._username, self._user_detail)

    def present_user(self, username: str | None, detail: str) -> None:
        self._username, self._user_detail = username, detail
        self.user.ensurePolished()
        language = self._presentation[0] if self._presentation else "en"
        if username is None:
            caption = "Guest"
            full = detail or text("identity_guest", language)
        else:
            caption = username
            full = f"{caption} · {detail}" if detail else caption
        self.user.setText(
            self.user.fontMetrics().elidedText(
                caption, Qt.TextElideMode.ElideRight, self.user.width() - 64
            )
        )
        self.user.setToolTip(full)
        self.user.setAccessibleName(full)
        self.user.setIcon(shell_icon("user", theme_colors(self).primary))

    def _size_language_selector(self) -> None:
        self.language.ensurePolished()
        self.language.setFixedWidth(
            max(
                self.language.fontMetrics().horizontalAdvance(name)
                for name in language_identity.LANGUAGE_LABELS.values()
            )
            + 70
        )

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.language and event.type() == QEvent.Type.FontChange:
            self._size_language_selector()
        return super().eventFilter(watched, event)


class StatusBar(QFrame):
    def __init__(self) -> None:
        super().__init__()
        self._appearance: (
            tuple[str, language_identity.Language, tuple[str, ...]] | None
        ) = None
        self.setObjectName("event")
        self.setFixedHeight(44)
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 4, 12, 4)
        row.setSpacing(8)
        self.state_icon, self.state = self._item(row)
        self.plc_icon, self.plc = self._item(row)
        self.alarm_icon, self.alarm = self._item(row)
        self.event_icon, self.event_label = self._item(row, flexible=True)

    @staticmethod
    def _item(row: QHBoxLayout, *, flexible: bool = False) -> tuple[QLabel, QLabel]:
        if row.count():
            separator = QFrame()
            separator.setFrameShape(QFrame.Shape.VLine)
            separator.setFixedHeight(18)
            row.addWidget(separator)
        group = QWidget()
        group.setSizePolicy(
            QSizePolicy.Policy.Expanding if flexible else QSizePolicy.Policy.Fixed,
            QSizePolicy.Policy.Preferred,
        )
        content = QHBoxLayout(group)
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(6)
        icon = QLabel()
        icon.setFixedSize(20, 20)
        content.addWidget(icon)
        label = QLabel() if flexible else _StatusLabel()
        label.setTextFormat(Qt.TextFormat.PlainText)
        # Translation length must not redistribute the icon/text groups.
        label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        content.addWidget(label, 1)
        row.addWidget(group, 1 if flexible else 0)
        return icon, label

    def refresh_icons(self, language: language_identity.Language) -> None:
        colors = theme_colors(self)
        for label in (self.state, self.plc, self.alarm):
            label.ensurePolished()
        appearance = (
            colors.background,
            language,
            tuple(label.font().key() for label in (self.state, self.plc, self.alarm)),
        )
        if self._appearance == appearance:
            return
        self._appearance = appearance
        metrics = self.plc.fontMetrics()
        # Reserve the full supported HR address and 16-bit commit value using
        # the widest actual digit, independently of the retained observation.
        self.plc.setFixedWidth(
            max(metrics.horizontalAdvance("W"), metrics.horizontalAdvance("HR "))
            + 10 * max(metrics.horizontalAdvance(str(digit)) for digit in range(10))
            + metrics.horizontalAdvance(" · ")
            + 2
        )
        self.alarm.setFixedWidth(
            max(
                self.alarm.fontMetrics().horizontalAdvance(
                    text(key, language).format(count=16)
                )
                for key in (
                    "alarm_off",
                    "alarm_waiting",
                    "alarm_unavailable",
                    "alarm_online",
                    "alarm_count",
                )
            )
            + 2
        )
        # Reserve this language's concise role captions across states. Using
        # English clips translations; reserving every language's longest caption
        # wastes feedback space. Recalculate on language/font changes, not status.
        for label, prefix, keys in (
            (
                self.state,
                "",
                (
                    "source_folder_status",
                    "UNCONFIGURED",
                    "READY",
                    "state_STARTING",
                    "state_RUNNING",
                    "state_STOPPING",
                    "state_FAULT",
                    "state_closing",
                ),
            ),
        ):
            label.setFixedWidth(
                max(
                    label.fontMetrics().horizontalAdvance(prefix + text(key, language))
                    for key in keys
                )
                + 2
            )
        for label in (self.state, self.plc, self.alarm):
            group = label.parentWidget()
            if group is not None and (layout := group.layout()) is not None:
                layout.invalidate()
                layout.activate()
                group.updateGeometry()
        if (layout := self.layout()) is not None:
            layout.invalidate()
            layout.activate()
        self.plc_icon.setPixmap(shell_icon("plc", colors.secondary).pixmap(20, 20))
