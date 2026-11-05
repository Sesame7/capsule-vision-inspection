"""Shared navigation and compact status presentation, without runtime ownership."""

from math import ceil
from typing import Literal

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QSize, Qt, Signal, Slot
from PySide6.QtGui import (
    QColor,
    QFontMetricsF,
    QIcon,
    QPaintEvent,
    QPalette,
    QResizeEvent,
)
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLayout,
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

PAGES = (
    "page_production",
    "page_recipe",
    "page_communication",
    "page_history",
    "page_vision",
)

type NavigationMode = Literal["comfortable", "compact", "two_row"]


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
    """Native button behavior/frame with a stacked icon and caption when compact."""

    stacked = False

    def content_rects(self) -> tuple[QRect, QRect]:
        option = QStyleOptionButton()
        self.initStyleOption(option)
        area = self.style().subElementRect(
            QStyle.SubElement.SE_PushButtonContents, option, self
        )
        icon = QRect(0, area.top(), self.iconSize().width(), self.iconSize().height())
        icon.moveCenter(QPoint(area.center().x(), icon.center().y()))
        caption = area.adjusted(0, self.iconSize().height() + 4, 0, 0)
        return icon, caption

    def paintEvent(self, event: QPaintEvent) -> None:
        if not self.stacked:
            super().paintEvent(event)
            return
        option = QStyleOptionButton()
        self.initStyleOption(option)
        option.text, option.icon = "", QIcon()
        painter = QStylePainter(self)
        # Keep native checked/hover/focus styling, including keyboard focus.
        painter.drawControl(QStyle.ControlElement.CE_PushButton, option)
        icon, caption = self.content_rects()
        painter.drawItemPixmap(
            icon,
            Qt.AlignmentFlag.AlignCenter,
            self.icon().pixmap(self.iconSize(), self.devicePixelRatioF()),
        )
        colors = theme_colors(self)
        option.palette.setColor(
            QPalette.ColorRole.ButtonText,
            QColor(colors.accent if self.isChecked() else colors.primary),
        )
        painter.drawItemText(
            caption,
            Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextSingleLine,
            option.palette,
            self.isEnabled(),
            self.text(),
            QPalette.ColorRole.ButtonText,
        )
        painter.end()


class NavigationBar(QFrame):
    page_selected = Signal(int)
    language_selected = Signal(str)
    theme_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._presentation: tuple[language_identity.Language, Theme, int] | None = None
        self.mode: NavigationMode = "comfortable"
        self._arranging = False
        self._metrics: (
            tuple[
                tuple[str, ...], tuple[str, ...], tuple[int, ...], tuple[int, ...], int
            ]
            | None
        ) = None
        self._username: str | None = None
        self._user_detail = ""
        self.setObjectName("navigation")
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._layout = QGridLayout(self)
        self._layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self._layout.setContentsMargins(12, 8, 12, 8)
        self._layout.setSpacing(6)
        self._layout.setColumnStretch(1, 1)
        self.page_row = QWidget()
        self.page_row.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed
        )
        self._row = QHBoxLayout(self.page_row)
        self._row.setContentsMargins(0, 0, 0, 0)
        self._row.setSpacing(6)
        self._layout.addWidget(self.page_row, 0, 0)
        self._pages = QButtonGroup(self)
        self._pages.idClicked.connect(self._select_page)
        self.buttons: list[_NavigationButton] = []
        for index, _ in enumerate(PAGES):
            button = _NavigationButton()
            button.setCheckable(True)
            button.setProperty("iconAlignment", "leading")
            button.setIconSize(QSize(20, 20))
            self._pages.addButton(button, index)
            self._row.addWidget(button)
            button.installEventFilter(self)
            self.buttons.append(button)
        self.utilities = QWidget()
        utilities = QHBoxLayout(self.utilities)
        utilities.setContentsMargins(0, 0, 0, 0)
        utilities.setSpacing(6)
        self._layout.addWidget(self.utilities, 0, 1, Qt.AlignmentFlag.AlignRight)
        self.user = QPushButton()
        self.user.setObjectName("current_user")
        self.user.setFixedSize(144, Style.touch)
        self.user.setIconSize(QSize(20, 20))
        self.user.installEventFilter(self)
        utilities.addWidget(self.user)
        self.theme = QPushButton()
        self.theme.setFixedSize(Style.touch, Style.touch)
        self.theme.setIconSize(QSize(22, 22))
        self.theme.clicked.connect(self._request_theme)
        utilities.addWidget(self.theme)
        self.language = _LanguageSelector()
        self.language.setObjectName("language")
        self.language.setFixedHeight(Style.touch)
        self.language.setIconSize(QSize(20, 20))
        for language in language_identity.LANGUAGE_DISPLAY_ORDER:
            self.language.addItem(language_identity.LANGUAGE_NAMES[language], language)
        # Use the screen's available height before Qt falls back to scrolling.
        self.language.setMaxVisibleItems(len(language_identity.LANGUAGES))
        self.language.activated.connect(self._select_language)
        utilities.addWidget(self.language)
        self.language.installEventFilter(self)

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
        self.language.setToolTip(
            f"{text('interface_language', language)} · {language_identity.LANGUAGE_NAMES[language]}"
        )
        self.language.setAccessibleName(text("interface_language", language))
        self.present_user(self._username, self._user_detail)
        self._arrange_buttons()

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

    @property
    def compact(self) -> bool:
        """Both compact presentations use the same stacked native buttons."""
        return self.mode != "comfortable"

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if self._presentation is not None:
            self._arrange_buttons()

    def minimumSizeHint(self) -> QSize:
        if not hasattr(self, "utilities"):
            return super().minimumSizeHint()
        margins = self._layout.contentsMargins()
        # Fixed row widths describe the current presentation, not the minimum
        # window width. A narrower resize can redistribute the same controls.
        gaps = (len(self.buttons) - 1) * self._row.spacing()
        width = max(len(self.buttons) * Style.touch + gaps, self.utilities.width())
        return QSize(
            width + margins.left() + margins.right() + 2 * self.frameWidth(),
            self.height(),
        )

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            if hasattr(self, "_presentation") and self._presentation is not None:
                self._arrange_buttons()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.FontChange and self._presentation is not None:
            self._arrange_buttons()
        return super().eventFilter(watched, event)

    def _arrange_buttons(self) -> None:
        if self._presentation is None or self._arranging:
            return
        self._arranging = True
        try:
            self._arrange()
        finally:
            self._arranging = False

    def _arrange(self) -> None:
        assert self._presentation is not None
        language = self._presentation[0]
        for widget in (*self.buttons, self.user, self.language):
            widget.ensurePolished()
        fonts = tuple(widget.font().key() for widget in (*self.buttons, self.language))
        captions = tuple(
            "PLC / Web" if key == "page_communication" else text(key, language)
            for key in PAGES
        )
        if self._metrics is None or self._metrics[:2] != (fonts, captions):
            # Measure this language, not the longest translation of every page.
            # Bound unusually long labels to a font-scaled readable span; the
            # complete localized name remains in the tooltip/accessibility name.
            label_widths = tuple(
                min(
                    ceil(QFontMetricsF(button.font()).horizontalAdvance(caption)) + 1,
                    8 * button.fontMetrics().height(),
                )
                for button, caption in zip(self.buttons, captions, strict=True)
            )
            horizontal = tuple(
                max(Style.touch, width + button.iconSize().width() + 28)
                for button, width in zip(self.buttons, label_widths, strict=True)
            )
            stacked = tuple(max(Style.touch, width + 24) for width in label_widths)
            # Reserve a single line across scripts, independently of selection
            # and language. Font changes may legitimately change both row sizes.
            line = (
                self._metrics[4]
                if self._metrics is not None and self._metrics[0] == fonts
                else max(
                    button.fontMetrics().boundingRect(text(key, lang)).height()
                    for key, button in zip(PAGES, self.buttons, strict=True)
                    for lang in language_identity.LANGUAGES
                )
            )
            self._metrics = fonts, captions, horizontal, stacked, line
        _, _, horizontal, stacked, line = self._metrics
        self.language.setFixedWidth(
            max(
                self.language.fontMetrics().horizontalAdvance(name)
                for name in language_identity.LANGUAGE_LABELS.values()
            )
            + 70
        )
        utility_width = (
            self.user.width() + self.theme.width() + 12 + self.language.width()
        )
        utility_height = max(
            Style.touch,
            line + 14,
            self.user.fontMetrics().height() + 14,
            self.language.fontMetrics().height() + 14,
        )
        self.user.setFixedHeight(utility_height)
        self.language.setFixedHeight(utility_height)
        self.utilities.setFixedSize(utility_width, utility_height)
        margins = self._layout.contentsMargins()
        available = self.contentsRect().width() - margins.left() - margins.right()
        gaps = (len(self.buttons) - 1) * self._row.spacing()
        shared = gaps + self._layout.horizontalSpacing() + utility_width
        mode: NavigationMode = (
            "comfortable"
            if sum(horizontal) + shared <= available
            else "compact"
            if sum(stacked) + shared <= available
            else "two_row"
        )
        two_rows = mode == "two_row"
        if two_rows != (self.mode == "two_row"):
            self._layout.removeWidget(self.page_row)
            self._layout.removeWidget(self.utilities)
            self._layout.addWidget(self.page_row, 0, 0, 1, 2 if two_rows else 1)
            self._layout.addWidget(
                self.utilities, int(two_rows), 1, Qt.AlignmentFlag.AlignRight
            )
        self.mode = mode
        widths = horizontal if mode == "comfortable" else stacked
        if two_rows and sum(widths) + gaps > available:
            # Keep natural proportions when even the stacked row needs elision.
            # Each slot retains a touch target, including in the capacity fixture.
            extra = max(0, available - gaps - len(widths) * Style.touch)
            total_extra = sum(width - Style.touch for width in widths)
            widths = tuple(
                Style.touch + (width - Style.touch) * extra // max(1, total_extra)
                for width in widths
            )
        else:
            # Choose the mode from the intrinsic requirements above, then spend
            # spare width on comfortable padding. Growth must not feed the next
            # mode decision or stretch short captions across the entire bar.
            minimum, limit, padding = (
                (128, 200, 32) if mode == "comfortable" else (96, 176, 24)
            )
            headroom = tuple(
                max(0, min(limit, max(minimum, width + padding)) - width)
                for width in widths
            )
            page_budget = available - (gaps if two_rows else shared)
            growth = min(
                sum(headroom), max(0, page_budget - sum(widths) - 2 * Style.space)
            )
            if growth:
                widths = tuple(
                    width + room * growth // sum(headroom)
                    for width, room in zip(widths, headroom, strict=True)
                )
        # Include the focused button's border as well as vertical padding.
        height = max(Style.touch, line + 14) + (24 if self.compact else 0)
        self.page_row.setFixedHeight(height)
        self.page_row.setFixedWidth(sum(widths) + gaps)
        self.setFixedHeight(
            (
                height + utility_height + self._layout.verticalSpacing()
                if two_rows
                else max(height, utility_height)
            )
            + margins.top()
            + margins.bottom()
            + 2 * self.frameWidth()
        )
        for button, width in zip(self.buttons, widths, strict=True):
            button.stacked = self.compact
            button.setFixedSize(width, height)
            button.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self._layout.activate()
        self._row.activate()
        for caption, button in zip(captions, self.buttons, strict=True):
            budget = (
                button.content_rects()[1].width()
                if self.compact
                else button.width() - 48
            )
            metrics = QFontMetricsF(button.font())
            button.setText(
                caption
                if metrics.horizontalAdvance(caption) <= budget
                else metrics.elidedText(
                    caption, Qt.TextElideMode.ElideRight, max(0, budget - 2)
                )
            )
        self.present_user(self._username, self._user_detail)


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
        self.web_icon, self.web = self._item(row)
        self.alarm = _StatusLabel()
        self.alarm.setTextFormat(Qt.TextFormat.PlainText)
        row.addWidget(self.alarm)
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
        appearance = (
            colors.background,
            language,
            tuple(label.font().key() for label in (self.state, self.plc, self.web)),
        )
        if self._appearance == appearance:
            return
        self._appearance = appearance
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
            (
                self.plc,
                "PLC · ",
                ("service_off", "service_next_start", "web_running", "technical_error"),
            ),
            (
                self.web,
                "Web · ",
                (
                    "service_off",
                    "web_starting",
                    "web_running",
                    "STOPPING",
                    "technical_error",
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
            group = label.parentWidget()
            if group is not None and (layout := group.layout()) is not None:
                layout.invalidate()
                layout.activate()
                group.updateGeometry()
        if (layout := self.layout()) is not None:
            layout.invalidate()
            layout.activate()
        for label, kind in ((self.plc_icon, "plc"), (self.web_icon, "globe")):
            label.setPixmap(shell_icon(kind, colors.secondary).pixmap(20, 20))
