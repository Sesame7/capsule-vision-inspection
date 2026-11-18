"""The same Records navigation and filter placement in both window compositions."""

from typing import Literal

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QTabWidget

from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import Language

from .history_event_table import RecordsPage
from .history_page import HistoryPage
from .records_filters import RecordsFilters
from .style import production_icon, shell_icon, theme_colors

type RecordsTab = Literal["batches", "inspection", "audit", "alarms"]

_PRESENTATION = {
    "batches": ("records_batches", "records_batches", "folder", shell_icon),
    "inspection": ("records_inspection", "audit_inspection", "brand", shell_icon),
    "audit": ("records_audit", "audit_trail", "cycles", production_icon),
    "alarms": ("records_alarms", "alarm_history", "FAULT", production_icon),
}


class RecordsTabs(QTabWidget):
    def __init__(
        self, tabs: tuple[tuple[RecordsTab, RecordsPage | HistoryPage], ...]
    ) -> None:
        super().__init__()
        self._tabs = tabs
        self.setObjectName("historyTabs")
        self.tabBar().setObjectName("recordTypes")
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
            self.setTabText(index, label)
            self.setTabToolTip(index, tip)
            self.tabBar().setAccessibleTabName(index, label)
            self.setTabIcon(index, icon(symbol, color))
