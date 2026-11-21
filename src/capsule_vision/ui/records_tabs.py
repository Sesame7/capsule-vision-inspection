"""The same Records navigation and filter placement in both window compositions."""

from typing import Literal

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QStyle, QStyleOptionTab, QTabBar, QTabWidget

from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import Language

from .history_event_table import RecordsPage
from .history_page import HistoryPage
from .records_filters import RecordsFilters
from .style import production_icon, shell_icon, theme_colors

type RecordsTab = Literal["batches", "cycles", "ng_capsules", "audit", "alarms"]

_PRESENTATION = {
    "batches": ("records_batches", "records_batches", "folder", shell_icon),
    "cycles": ("short_cycles", "audit_inspection", "brand", shell_icon),
    "ng_capsules": (
        "records_ng_capsules",
        "records_ng_capsules",
        "capsule",
        shell_icon,
    ),
    "audit": ("records_audit", "audit_trail", "cycles", production_icon),
    "alarms": ("records_alarms", "alarm_history", "FAULT", production_icon),
}


class _RecordsTabBar(QTabBar):
    def tabSizeHint(self, index: int) -> QSize:
        return QSize(132, super().tabSizeHint(index).height())

    def minimumTabSizeHint(self, index: int) -> QSize:
        return QSize(80, self.tabSizeHint(index).height())

    def initStyleOption(self, option: QStyleOptionTab, index: int) -> None:
        super().initStyleOption(option, index)
        text_rect = self.style().subElementRect(
            QStyle.SubElement.SE_TabBarTabText, option, self
        )
        inset = (
            text_rect.width() - option.fontMetrics.horizontalAdvance(option.text)
        ) // 2
        # The native tab style adds four pixels beside each occupied side slot.
        # Reserve equal space there so it centers the icon and the elided text together.
        if inset > 4:
            option.leftButtonSize = option.rightButtonSize = QSize(
                inset - 4, option.iconSize.height()
            )


class RecordsTabs(QTabWidget):
    def __init__(
        self, tabs: tuple[tuple[RecordsTab, RecordsPage | HistoryPage], ...]
    ) -> None:
        super().__init__()
        self._tabs = tabs
        self.setObjectName("historyTabs")
        self.setTabBar(_RecordsTabBar(self))
        self.tabBar().setObjectName("recordTypes")
        self.tabBar().setDrawBase(False)
        self.tabBar().setExpanding(False)
        self.setIconSize(QSize(20, 20))
        self.setElideMode(Qt.TextElideMode.ElideRight)
        for _, page in tabs:
            self.addTab(page, "")

    def place_filters(self, index: int, filters: RecordsFilters) -> None:
        # Reparent the one shared filter bar into the current page's first row.
        self._tabs[index][1].filter_bar.insertWidget(0, filters)
        filters.show()

    def set_language(self, language: Language) -> None:
        color = theme_colors(self).primary
        for index, (kind, _) in enumerate(self._tabs):
            caption, destination, symbol, icon = _PRESENTATION[kind]
            label = text(caption, language)
            full = text(destination, language)
            # Use the native label when the longer destination falls back.
            tip = (
                label if language != "en" and full == text(destination, "en") else full
            )
            self.setTabText(
                index, text("ng", language) if kind == "ng_capsules" else label
            )
            self.setTabToolTip(index, tip)
            self.tabBar().setAccessibleTabName(
                index, full if kind == "cycles" else label
            )
            self.setTabIcon(index, icon(symbol, color))
