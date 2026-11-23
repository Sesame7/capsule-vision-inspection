"""Four attributed exposures of a rejected or area-filtered capsule."""

from concurrent.futures import Future
from math import ceil
from pathlib import Path
from threading import Event
from typing import cast

from PySide6.QtCore import QPoint, QSignalBlocker, QSize, Qt, Signal, Slot
from PySide6.QtGui import QAction, QHideEvent, QImage, QResizeEvent, QShowEvent
from PySide6.QtWidgets import (
    QLabel,
    QMenu,
    QPushButton,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from capsule_vision.application.history import HistoryReader
from capsule_vision.application.ng_capsules import (
    CapsuleDetail,
    CapsuleExposure,
    CapsuleFilter,
    CapsuleKey,
    CapsulePage,
    CapsuleRow,
)
from capsule_vision.application.records_query import RecordsScope
from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import Language

from .finding_overlay import finding_caption
from .future_delivery import FutureDelivery
from .history_event_table import HistoryEventTable, record_time
from .history_image import HistoryImageViewport
from .history_page import inspection_reason
from .image_tools import image_icon
from .ng_capsule_images import ExposureImage, load_images
from .production_presentation import image_transform
from .style import Style, theme_colors


class _CapsuleButton(QPushButton):
    def hitButton(self, pos: QPoint) -> bool:
        # The style normally excludes the transparent margin from hit testing.
        return self.rect().contains(pos)


class _ExposureGallery(QWidget):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.photo_size: tuple[int, int] | None = None
        self.headers = tuple(QLabel(str(i), self) for i in range(1, 5))
        self.cells = tuple(
            HistoryImageViewport(QImage(), (), self) for _ in self.headers
        )
        for header in self.headers:
            header.setAlignment(Qt.AlignmentFlag.AlignCenter)
            header.setProperty("role", "secondary")
        self.previous = _CapsuleButton(self)
        self.next = _CapsuleButton(self)
        for button in (self.previous, self.next):
            button.setFixedSize(Style.touch, Style.touch)
            button.setIconSize(QSize(18, 18))
            button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def set_language(self, language: Language) -> None:
        for button, icon, key in (
            (self.previous, "previous", "previous_capsule"),
            (self.next, "next", "next_capsule"),
        ):
            button.setIcon(image_icon(icon, theme_colors(self).primary))
            button.setAccessibleName(text(key, language))
            button.setToolTip(button.accessibleName())

    def fit_images(self, width: int, height: int) -> None:
        gap = Style.icon_text_gap
        caption = self.headers[0].sizeHint().height()
        min_face = self.previous.iconSize().width()
        source = self.photo_size
        if source is None:
            cell = self.cells[0]
            source = (
                cell.fontMetrics().horizontalAdvance(cell.empty_text) + 2 * gap,
                Style.touch * 2,
            )

        columns = len(self.cells)
        w = max(1, (width - 2 * (min_face + gap) - (columns - 1) * gap) // columns)
        h = max(1, height - caption - gap)
        if self.photo_size is None:
            w, h = min(w, source[0]), min(h, source[1])
        fit = image_transform(*source, w, h)
        tile = QSize(min(w, ceil(fit.width)), min(h, ceil(fit.height)))
        photos_width = columns * tile.width() + (columns - 1) * gap
        face = max(min_face, min(Style.touch, (width - photos_width) // 2 - gap))
        rail = face + gap
        photo_top = max(0, (Style.touch - tile.height() + 1) // 2)
        caption_y = photo_top + tile.height() + gap
        self.resize(2 * rail + photos_width, max(Style.touch, caption_y + caption))
        # Use spare width for full button faces. When space is tight, only the
        # transparent part of each touch target extends over the photograph.
        for button, inner in ((self.previous, "right"), (self.next, "left")):
            style = (
                f"QPushButton {{ margin-{inner}: {Style.touch - face}px; padding: 0; }}"
                if face < Style.touch
                else ""
            )
            if button.styleSheet() != style:
                button.setStyleSheet(style)
        for col, (header, cell) in enumerate(
            zip(self.headers, self.cells, strict=True)
        ):
            x = rail + col * (tile.width() + gap)
            cell.setGeometry(x, photo_top, tile.width(), tile.height())
            header.setGeometry(x, caption_y, tile.width(), caption)
        middle = photo_top + (tile.height() - Style.touch) // 2
        self.previous.move(0, middle)
        self.next.move(self.width() - Style.touch, middle)


class _CapsulePane(QWidget):
    def __init__(self, table: QTableWidget) -> None:
        super().__init__()
        self.gallery = _ExposureGallery(self)
        self.table = table
        table.setParent(self)
        self.table_min_width = table.sizeHint().width()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self.arrange()

    def arrange(self) -> None:
        if self.gallery.isHidden():
            self.table.setGeometry(self.rect())
            return
        width, height = self.width(), self.height()
        old_table = self.table.geometry()
        gap = Style.small
        if self.window().width() > self.window().height():
            self.gallery.fit_images(
                max(width // 3, width - self.table_min_width - gap),
                height,
            )
            self.gallery.move(0, (height - self.gallery.height()) // 2)
            left = self.gallery.width() + gap
            self.table.setGeometry(left, 0, max(0, width - left), height)
        else:
            row = self.table.verticalHeader().defaultSectionSize()
            table_height = (
                self.table.horizontalHeader().sizeHint().height()
                + min(3, self.table.rowCount()) * row
                + 2 * self.table.frameWidth()
            )
            self.gallery.fit_images(width, height - table_height - gap)
            self.gallery.move((width - self.gallery.width()) // 2, 0)
            top = self.gallery.height() + gap
            self.table.setGeometry(0, top, width, max(0, height - top))
        if self.table.geometry() != old_table and (item := self.table.currentItem()):
            self.table.scrollToItem(item)


def _exposure_text(
    slot: int,
    key: CapsuleKey | None,
    exposure: CapsuleExposure | None,
    image_issue: str,
    language: Language,
) -> tuple[str, str]:
    title = f"{slot}/4"
    if key:
        title += f" · CH{key.channel:02d}"
    lines: list[str] = []
    if exposure:
        title += f" · {text('records_cycle', language)} {exposure.cycle}"
        channel = exposure.channel
        status = channel.business if channel else "—"
        if channel and channel.technical != "SUCCESS":
            status += f" · {channel.technical}"
        lines.append(f"{text('history_result', language)} · {status}")
        if channel and channel.judgment != channel.business:
            lines.append(f"{text('history_judgment', language)} · {channel.judgment}")
        labels = [
            (text("capsule_filtered", language) + " · " if f.filtered else "")
            + finding_caption(f.label, f.area)
            for f in exposure.findings
        ]
        if channel:
            labels.extend(
                inspection_reason(code, language) or code for code in channel.reasons
            )
        if reasons := ", ".join(dict.fromkeys(filter(None, labels))):
            lines.append(reasons)
    if image_issue:
        lines.append(image_issue)
    return title, "\n".join(lines)


def _channel_description(row: CapsuleRow, language: Language) -> str:
    channel = f"CH{row.key.channel:02d}"
    if row.camera:
        channel += f" · {row.camera}"
    if row.filtered:
        channel += f" · OK · {text('capsule_filtered', language)}"
    return channel


class NgCapsulesPage(HistoryEventTable):
    selection_changed = Signal()

    def __init__(
        self, owner: HistoryReader | None, *, image_root: Path | None = None
    ) -> None:
        super().__init__("ngCapsules")
        self.owner = owner
        self._root = image_root
        self.scope = RecordsScope()
        self.language: Language = "zh"
        self._page = CapsulePage()
        self.area_label: str | None = None
        self._labels: tuple[str, ...] | None = None
        self._options_revision = 0
        self._options_future: Future[CapsulePage] | None = None
        self._page_future: Future[CapsulePage] | None = None
        self._cursor: CapsuleRow | None = None
        self._newer = False
        self._page_arrow: _CapsuleButton | None = None
        self._restore_arrow_focus = False
        self._list_revision = self._selection_revision = 0
        self._listing = self._working = self._disposed = False
        self._selected: CapsuleKey | None = None
        self._detail: CapsuleDetail | None = None
        self._views: tuple[ExposureImage, ...] = ()
        self._cancel = Event()
        self._error = ""
        self._delivery = FutureDelivery()
        self._delivery.emitter.completed.connect(
            self._completed, Qt.ConnectionType.QueuedConnection
        )
        self.destroyed.connect(
            self._delivery.detach, Qt.ConnectionType.DirectConnection
        )
        self.search.hide()
        self.sort_button = QPushButton()
        self.sort_button.setObjectName("ngCapsuleSort")
        self.sort_button.setFixedSize(Style.touch, Style.touch)
        self.sort_menu = QMenu(self.sort_button)
        self.filter_bar.insertWidget(
            self.filter_bar.indexOf(self.newer), self.sort_button
        )
        QWidget.setTabOrder(self.kind, self.sort_button)
        QWidget.setTabOrder(self.sort_button, self.newer)
        self.sort_button.clicked.connect(self._open_sort)
        self.sort_menu.triggered.connect(self._choose_sort)
        self.mode = CapsuleFilter.NG
        self.kind.currentIndexChanged.connect(self._change_filter)
        root = self.layout()
        assert isinstance(root, QVBoxLayout)
        position = root.indexOf(self.table)
        root.removeWidget(self.table)
        self._pane = _CapsulePane(self.table)
        self.gallery = self._pane.gallery
        root.insertWidget(position, self._pane, 1)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.gallery.previous.clicked.connect(lambda: self._step(-1))
        self.gallery.next.clicked.connect(lambda: self._step(1))
        self.newer.clicked.connect(lambda: self._turn(True))
        self.older.clicked.connect(lambda: self._turn(False))
        self.refresh_button.clicked.connect(self.refresh)
        self.set_language("zh")

    def set_language(self, language: Language) -> None:
        self.language = language
        self.navigation_language(language)
        self.sort_button.setIcon(image_icon("sort", theme_colors(self).primary))
        self.sort_button.setIconSize(QSize(Style.icon, Style.icon))
        self._sort_caption()
        if self.sort_menu.isVisible():
            self._sort_items()
        self.gallery.set_language(language)
        with QSignalBlocker(self.kind):
            self.kind.clear()
            for label, mode in (
                ("NG", CapsuleFilter.NG),
                (text("capsule_filtered", language), CapsuleFilter.FILTERED),
                (text("export_all", language), CapsuleFilter.ALL),
            ):
                self.kind.addItem(label, mode.value)
            self.kind.setCurrentIndex(self.kind.findData(self.mode.value))
        self.kind.setAccessibleName(text("history_result", language))
        self.kind.setToolTip(self.kind.currentText())
        self.table.setAccessibleName(text("records_ng_capsules", language))
        for idx, row in enumerate(self._page.rows):
            if row.filtered and (item := self.table.item(idx, 3)):
                description = _channel_description(row, language)
                item.setToolTip(description)
                item.setData(Qt.ItemDataRole.AccessibleTextRole, description)
        self._size_columns()
        self._present()

    def _size_columns(self) -> None:
        channel_sample = "CH36" if self.mode == CapsuleFilter.NG else "CH36 · OK"
        self._pane.table_min_width = self.columns(
            self.language,
            (
                ("short_time", "12-31 23:59:59.999", "12-31 23:59:59.999", 0),
                ("batch_id", "LOT-26", "LOT-20261020-003", 4),
                ("page_recipe", "Test", "黄色胶囊-规格B-灯检方案", 6),
                ("history_channel", channel_sample, channel_sample, 0),
                ("history_group", "999999", "999999", 0),
            ),
        )

    def hideEvent(self, event: QHideEvent) -> None:
        super().hideEvent(event)
        self.sort_menu.hide()
        self._select(None)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if self._page.rows and self._selected is None:
            self._selection_changed()

    def refresh(self) -> None:
        self._options_revision += 1
        if self._options_future is not None:
            self._options_future.cancel()
            self._options_future = None
        self._labels = None
        self.sort_menu.hide()
        self._request_page(None, False)

    def _sort_caption(self) -> None:
        title = text("records_sort", self.language)
        value = (
            self.area_label
            if self.area_label is not None
            else text("short_time", self.language)
        )
        self.sort_button.setToolTip(f"{title} · {value}")
        self.sort_button.setAccessibleName(self.sort_button.toolTip())
        self.sort_menu.setAccessibleName(title)

    def _sort_items(self) -> None:
        self.sort_menu.clear()
        for label in (None, *(self._labels or ())):
            caption = text("short_time", self.language) if label is None else label
            action = self.sort_menu.addAction(caption.replace("&", "&&"))
            action.setData(label)
            action.setCheckable(True)
            action.setChecked(label == self.area_label)
        self.sort_menu.adjustSize()

    def _open_sort(self) -> None:
        self._sort_items()
        self.sort_menu.popup(
            self.sort_button.mapToGlobal(QPoint(0, self.sort_button.height()))
        )
        if (
            self._labels is None
            and self.owner is not None
            and self._options_future is None
            and not (self._listing and self.area_label is not None)
        ):
            self._options_future = self.owner.ng_capsule_page(
                self.scope, mode=self.mode, include_area_labels=True
            )
            self._delivery.watch(
                f"options:{self._options_revision}", self._options_future
            )

    def _choose_sort(self, action: QAction) -> None:
        choice: object = action.data()
        if (choice is None or isinstance(choice, str)) and choice != self.area_label:
            self.area_label = choice
            self._sort_caption()
            self._request_page(None, False)

    def _change_filter(self) -> None:
        self.mode = CapsuleFilter(self.kind.currentData())
        self._size_columns()
        self.refresh()

    def _step(self, delta: int) -> None:
        if self._selected is None:
            return
        row = self.table.currentRow() + delta
        if 0 <= row < len(self._page.rows):
            self.table.setCurrentCell(row, 0)
        elif (self.newer if delta < 0 else self.older).isEnabled():
            self._turn(
                delta < 0, self.gallery.previous if delta < 0 else self.gallery.next
            )

    def _turn(self, newer: bool, arrow: _CapsuleButton | None = None) -> None:
        if self._page.rows and not self._listing:
            self._request_page(self._page.rows[0 if newer else -1], newer, arrow)

    def _request_page(
        self,
        cursor: CapsuleRow | None,
        newer: bool,
        arrow: _CapsuleButton | None = None,
    ) -> None:
        self._cursor, self._newer = cursor, newer
        self._page_arrow = arrow
        self._restore_arrow_focus = arrow is not None and arrow.hasFocus()
        if self._restore_arrow_focus:
            self.table.setFocus()
        self._list_revision += 1
        if self._page_future is not None:
            self._page_future.cancel()
        self._error = ""
        self._page = CapsulePage()
        self.table.setRowCount(0)
        self._select(None)
        if not self._listing:
            self._submit_page()

    def _submit_page(self) -> None:
        if self.owner is not None and not self._disposed:
            self._listing = True
            self._page_future = self.owner.ng_capsule_page(
                self.scope, self._cursor, self._newer, self.mode, self.area_label
            )
            self._delivery.watch(f"page:{self._list_revision}", self._page_future)
        self._present()

    def _selection_changed(self) -> None:
        row = self.table.currentRow()
        key = (
            self._page.rows[row].key
            if self.table.selectedItems() and 0 <= row < len(self._page.rows)
            else None
        )
        self._select(key)
        if key is not None and (item := self.table.currentItem()):
            self.table.scrollToItem(item)

    @property
    def selected_capsule(self) -> CapsuleRow | None:
        index = self.table.currentRow()
        if 0 <= index < len(self._page.rows):
            row = self._page.rows[index]
            if row.key == self._selected:
                return row
        return None

    def _select(self, key: CapsuleKey | None) -> None:
        self._cancel.set()
        self._selected = key
        self.selection_changed.emit()
        self._selection_revision += 1
        self._detail = None
        self._views = ()
        self._error = ""
        self._present()
        if not self._working:
            self._submit_detail()

    def _submit_detail(self) -> None:
        if self._selected is not None and self.owner is not None and not self._disposed:
            self._working = True
            self._cancel = Event()
            self._delivery.watch(
                f"detail:{self._selection_revision}",
                self.owner.ng_capsule_detail(self._selected),
            )

    @Slot(str, object, object)
    def _completed(self, token: str, value: object, error: object) -> None:
        if self._disposed:
            return
        kind, revision = token.split(":")
        if kind == "options":
            if int(revision) == self._options_revision:
                self._options_future = None
                if isinstance(value, CapsulePage):
                    self._labels = value.area_labels
                    if self.sort_menu.isVisible():
                        self._sort_items()
                elif error:
                    self._error = str(error)
                    self._present()
            return
        if kind == "page":
            self._listing = False
            self._page_future = None
            if int(revision) != self._list_revision:
                self._submit_page()
                return
            self._page = value if isinstance(value, CapsulePage) else CapsulePage()
            if self._page.area_labels is not None:
                self._labels = self._page.area_labels
                if self.sort_menu.isVisible():
                    self._sort_items()
            self._error = str(error) if error else ""
            with QSignalBlocker(self.table):
                self.table.setRowCount(len(self._page.rows))
                for index, row in enumerate(self._page.rows):
                    stamp, full = record_time(row.observed, milliseconds=True)
                    channel = f"CH{row.key.channel:02d}"
                    if row.filtered:
                        channel += " · OK"
                    self.set_values(
                        index,
                        (
                            stamp,
                            row.batch_label or "—",
                            row.recipe_name or "—",
                            channel,
                            row.key.first_cycle,
                        ),
                        {
                            0: full,
                            3: _channel_description(row, self.language),
                            4: f"{row.key.first_cycle} · Run {row.key.run}",
                        },
                    )
                if self._page.rows:
                    row = (
                        len(self._page.rows) - 1
                        if self._page_arrow is self.gallery.previous
                        else 0
                    )
                    self.table.setCurrentCell(row, 0)
            if self._page.rows and self.isVisible():
                self._selection_changed()
                if (
                    self._restore_arrow_focus
                    and self.table.hasFocus()
                    and self._page_arrow is not None
                    and self._page_arrow.isEnabled()
                ):
                    self._page_arrow.setFocus()
            self._page_arrow = None
        else:
            self._working = False
            if int(revision) != self._selection_revision:
                self._submit_detail()
                return
            self._error = str(error) if error else ""
            if kind == "detail":
                # Only the footprint survives while reading a new selection.
                self.gallery.photo_size = None
                if isinstance(value, CapsuleDetail) and value.key == self._selected:
                    self._detail = value
                    if value.roi is not None:
                        rect = value.roi.rect
                        self.gallery.photo_size = (rect.width, rect.height)
                    self._working = True
                    self._delivery.watch(
                        f"images:{revision}",
                        load_images(value, self._root, self._cancel),
                    )
            elif kind == "images" and isinstance(value, tuple):
                self._views = cast(tuple[ExposureImage, ...], value)
        self._present()

    def _present(self) -> None:
        lang = self.language
        key = (
            "history_unavailable"
            if self.owner is None or self._error
            else "history_loading"
            if self._listing
            else "history_none"
            if not self._page.rows
            else ""
        )
        self.message.setText(text(key, lang) if key else "")
        self.message.setVisible(bool(key))
        self.message.setToolTip(self._error)
        self.message.setAccessibleDescription(self._error)
        self.refresh_button.setEnabled(self.owner is not None)
        self.sort_button.setEnabled(self.owner is not None)
        self.newer.setEnabled(
            bool(self._page.rows)
            and (self._page.more if self._newer else self._cursor is not None)
        )
        self.older.setEnabled(
            bool(self._page.rows)
            and (self._cursor is not None if self._newer else self._page.more)
        )
        self.gallery.setVisible(self._selected is not None)
        row = self.table.currentRow()
        self.gallery.previous.setEnabled(
            self._selected is not None and (row > 0 or self.newer.isEnabled())
        )
        self.gallery.next.setEnabled(
            self._selected is not None
            and (row + 1 < len(self._page.rows) or self.older.isEnabled())
        )
        empty_key = (
            "history_loading" if self._working and not self._views else "unavailable"
        )
        evidence: list[str] = []
        for i, (header, cell) in enumerate(
            zip(self.gallery.headers, self.gallery.cells, strict=True)
        ):
            exposure = self._detail.exposures[i] if self._detail else None
            photo = self._views[i] if self._views else ExposureImage(issue=empty_key)
            cell.empty_text = text(empty_key, lang)
            cell.set_image(photo.image, photo.findings)
            issue = text(photo.issue, lang) if photo.issue else ""
            name, details = _exposure_text(i + 1, self._selected, exposure, issue, lang)
            tooltip = "\n".join(filter(None, (name, details)))
            for widget in (header, cell):
                widget.setToolTip(tooltip)
                widget.setAccessibleName(name)
                widget.setAccessibleDescription(details)
            evidence.append(tooltip)
        selected_evidence = "\n\n".join(evidence) if self._selected else ""
        self.table.setAccessibleDescription(selected_evidence)
        self.table.setWhatsThis(selected_evidence)
        self._pane.arrange()

    def dispose(self) -> None:
        self._disposed = True
        self._cancel.set()
        for future in (self._page_future, self._options_future):
            if future is not None:
                future.cancel()
        self.sort_menu.hide()
        self._delivery.detach()
