"""Dedicated Records presentation for an already validated History copy."""

from html import escape

from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QCloseEvent, QFont, QResizeEvent
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from capsule_vision.application.history_review import HistoryReview
from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import Language, is_language

from .alarm_page import AlarmPage
from .batches_page import BatchesPage
from .history_page import HistoryPage
from .records_filters import RecordsFilters
from .records_tabs import RecordsTabs
from .shell import LanguageSelector
from .style import Style, apply_theme


class _PackagePath(QLabel):
    def __init__(self, path: str) -> None:
        super().__init__(path)
        self._path = path
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setToolTip(f"<span style='white-space: pre-wrap'>{escape(path)}</span>")
        self.setAccessibleDescription(path)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self.setText(
            self.fontMetrics().elidedText(
                self._path, Qt.TextElideMode.ElideMiddle, self.contentsRect().width()
            )
        )


class HistoryReviewWindow(QMainWindow):
    def __init__(self, reader: HistoryReview) -> None:
        super().__init__()
        self._disposed = False
        self.setMinimumSize(*Style.minimum)
        font = QFont()
        font.setFamilies(Style.font_families)
        self.setFont(font)

        central = QWidget()
        central.setObjectName("shell")
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(Style.space, Style.space, Style.space, Style.space)
        root.setSpacing(Style.small)
        header = QHBoxLayout()
        root.addLayout(header)
        self.mode = QLabel()
        self.mode.setProperty("role", "state")
        self.mode.setTextFormat(Qt.TextFormat.PlainText)
        header.addWidget(self.mode)
        header.addStretch()
        self.language_selector = LanguageSelector()
        self.language_selector.activated.connect(self._select_language)
        header.addWidget(self.language_selector)
        self.directory = _PackagePath(str(reader.root))
        root.addWidget(self.directory)

        self.batches = BatchesPage(None, history=reader)
        self.inspection = HistoryPage(reader, image_root=reader.root)
        self.alarms = AlarmPage(reader)
        self.filters = RecordsFilters(reader)
        self.tabs = RecordsTabs(
            (
                ("batches", self.batches),
                ("inspection", self.inspection),
                ("alarms", self.alarms),
            )
        )
        root.addWidget(self.tabs, 1)
        self.tabs.currentChanged.connect(self._refresh_tab)
        self.filters.changed.connect(
            lambda: self._refresh_tab(self.tabs.currentIndex())
        )
        apply_theme(self, "dark")
        self.set_language("zh")
        self._refresh_tab(self.tabs.currentIndex())

    @Slot(int)
    def _select_language(self, index: int) -> None:
        language: object = self.language_selector.itemData(index)
        if is_language(language):
            self.set_language(language)

    def set_language(self, language: Language) -> None:
        caption = text("history_review_mode", language)
        self.setWindowTitle(f"Capsule Vision Inspection · {caption}")
        self.mode.setText(caption)
        self.directory.setAccessibleName(text("archive_folder", language))
        self.language_selector.present(language)
        self.tabs.set_language(language)
        self.batches.set_language(language)
        self.inspection.set_language(language)
        self.alarms.set_language(language)
        self.filters.set_language(language)

    @Slot(int)
    def _refresh_tab(self, index: int) -> None:
        page = (self.batches, self.inspection, self.alarms)[index]
        page.scope = self.filters.scope
        self.tabs.place_filters(index, self.filters)
        page.refresh()

    def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        self.filters.dispose()
        self.batches.dispose()
        self.inspection.dispose()
        self.alarms.dispose()

    def closeEvent(self, event: QCloseEvent) -> None:
        self.dispose()
        super().closeEvent(event)
