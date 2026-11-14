"""Hierarchical Batch/Run review, independent of the Production selection."""

from PySide6.QtCore import (
    QEvent,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QRect,
    QSignalBlocker,
    QSize,
    Qt,
    Signal,
    Slot,
)
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHeaderView,
    QLabel,
    QSizePolicy,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from capsule_vision.application import (
    ProductionApplication,
    ProductionApplicationSnapshot,
)
from capsule_vision.application.batch_review import (
    BatchDetail,
    BatchList,
    BatchRow,
    RunRow,
)
from capsule_vision.application.models import PilotCounters
from capsule_vision.application.records_query import RecordsScope
from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import Language

from .future_delivery import FutureDelivery
from .history_event_table import RecordsPage, fit_record_columns, record_time
from .image_tools import image_icon
from .style import Style, shell_icon, theme_colors

_COLUMNS = (
    ("batch_run", "    LOT-F", "    DEV-LOT-2026-11", 3),
    ("report_started", "11-07 23:59:59", "11-07 23:59:59", 1),
    ("batch_recent_end", "11-07 23:59:59", "11-07 23:59:59", 1),
    ("batch_start_account", "Operator", "DEV-Example-Operator", 2),
    ("report_inspected", "9,999", "999,999", 1),
    ("ok", "9,999", "999,999", 1),
    ("ng", "999", "9,999", 1),
    ("report_pass_rate", "98.56%", "100.00%", 1),
)
_INFO = ("batch_product", "batch_specification", "batch_notes")
_RUN_OUTCOMES = {
    "open": "state_RUNNING",
    "stopped": "report_stopped",
    "interrupted": "report_interrupted",
    "fault": "FAULT",
}
_NUMBER_ALIGN = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter


def _quantities(totals: PilotCounters | None) -> tuple[str, str, str, str]:
    if totals is None:
        return "—", "—", "—", "—"
    return (
        f"{totals.inspected:,}",
        f"{totals.channel_ok:,}",
        f"{totals.channel_ng:,}",
        f"{100 * totals.channel_ok / totals.inspected:.2f}%"
        if totals.inspected
        else "—",
    )


def _time(stamp: float | None) -> tuple[str, str]:
    if stamp is None:
        return "—", "—"
    caption, full = record_time(stamp)
    return caption[5:] if len(caption) == 19 else caption, full


def _outcome(value: str, language: Language) -> str:
    return text(_RUN_OUTCOMES[value], language) if value in _RUN_OUTCOMES else value


def _warnings(detail: BatchDetail, language: Language) -> list[str]:
    return [
        text(key, language)
        for flag, key in (
            (not detail.batch.totals.batches_completed, "batch_no_groups"),
            (detail.uncertain, "batch_uncertain"),
            (detail.expired, "batch_expired"),
        )
        if flag
    ]


class _BatchTree(QTreeWidget):
    def drawBranches(
        self, painter: QPainter, rect: QRect, index: QModelIndex | QPersistentModelIndex
    ) -> None:
        # Native Windows branch glyphs ignore the dark palette. Only paint the
        # disclosure icon; Qt retains branch hit testing, focus and keyboard input.
        item = self.itemFromIndex(index)
        if (
            item.parent() is not None
            or item.childIndicatorPolicy()
            != QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator
        ):
            return
        color = theme_colors(self).primary
        icon = (
            shell_icon("chevron", color)
            if item.isExpanded()
            else image_icon("next", color)
        )
        x = rect.right() - self.indentation() // 2 + 1
        icon.paint(painter, QRect(x - 8, rect.center().y() - 8, 16, 16))


class BatchesPage(RecordsPage):
    selection_changed = Signal()

    def __init__(self, app: ProductionApplication) -> None:
        super().__init__("batch")
        self.app = app
        self.language: Language = "zh"
        self._delivery = FutureDelivery()
        self._delivery.emitter.completed.connect(
            self._completed, Qt.ConnectionType.QueuedConnection
        )
        self.destroyed.connect(
            self._delivery.detach, Qt.ConnectionType.DirectConnection
        )
        self._page = BatchList((), False)
        self._cursor: tuple[float, str] | None = None
        self._newer = False
        self.scope = RecordsScope()
        self._list_version = self._version = 0
        self._listing = False
        self._reading: tuple[int, str, int] | None = None
        self._selected: str | None = None
        self._expanded: str | None = None
        self._cache: dict[str, BatchDetail] = {}
        self._errors: dict[str, str] = {}
        self._run_rows: list[RunRow] = []
        self._items: dict[str, QTreeWidgetItem] = {}
        self._error = ""
        self._disposed = False
        self._observed_run: str | None = None
        self.kind.hide()
        self.search.hide()
        self.refresh_button.clicked.connect(self.refresh)
        self.newer.clicked.connect(lambda: self._turn(True))
        self.older.clicked.connect(lambda: self._turn(False))
        root = self.layout()
        assert isinstance(root, QVBoxLayout)
        self.table = _BatchTree()
        self.table.setObjectName("batchRecords")
        self.table.setColumnCount(len(_COLUMNS))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setUniformRowHeights(True)
        self.table.setAlternatingRowColors(True)
        self.table.setIndentation(22)
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.table.header().setStretchLastSection(False)
        self.table.header().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.table.header().setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self.table.header().setTextElideMode(Qt.TextElideMode.ElideRight)
        self.table.viewport().installEventFilter(self)
        self.table.itemSelectionChanged.connect(self._selection)
        self.table.itemExpanded.connect(self._expand)
        self.table.itemCollapsed.connect(self._collapse)
        root.addWidget(self.table, 1)
        self.metadata = QFrame()
        self.metadata.setObjectName("batchMetadata")
        card = QVBoxLayout(self.metadata)
        card.setContentsMargins(12, 6, 12, 6)
        card.setSpacing(0)
        self.metadata_labels = tuple(QLabel() for _ in _INFO)
        for label in self.metadata_labels:
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setSizePolicy(
                QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
            )
            card.addWidget(label)
        self.metadata.installEventFilter(self)
        root.addWidget(self.metadata)
        self.set_language("zh")

    @property
    def detail(self) -> BatchDetail | None:
        return self._cache.get(self._selected) if self._selected is not None else None

    @property
    def selected_batch(self) -> tuple[str, str] | None:
        return next(
            ((r.uuid, r.label) for r in self._page.rows if r.uuid == self._selected),
            None,
        )

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            event.type() in (QEvent.Type.Resize, QEvent.Type.FontChange)
            and watched is self.table.viewport()
        ):
            fit_record_columns(self.table, _COLUMNS)
        if watched is self.metadata and event.type() in (
            QEvent.Type.Resize,
            QEvent.Type.FontChange,
        ):
            self._metadata()
        return super().eventFilter(watched, event)

    def set_language(self, language: Language) -> None:
        self.language = language
        self.navigation_language(language)
        self.table.setAccessibleName(text("records_batches", language))
        header = self.table.headerItem()
        for col, (key, _, _, _) in enumerate(_COLUMNS):
            caption = text(key, language)
            header.setText(col, caption)
            header.setToolTip(col, caption)
            header.setData(col, Qt.ItemDataRole.AccessibleTextRole, caption)
            if col >= 4:
                header.setTextAlignment(col, _NUMBER_ALIGN)
        self._render()
        fit_record_columns(self.table, _COLUMNS)

    def refresh(self) -> None:
        self._cursor, self._newer = None, False
        self._request_list()

    def _turn(self, newer: bool) -> None:
        if self._listing or not self._page.rows:
            return
        self._cursor = self._page.rows[0 if newer else -1].cursor
        self._newer = newer
        self._request_list()

    def _request_list(self) -> None:
        self._list_version += 1
        if not self._listing:
            self._load_list()

    def _load_list(self) -> None:
        if self.app.history is None:
            self._render()
            return
        self._listing = True
        self.newer.setEnabled(False)
        self.older.setEnabled(False)
        self._delivery.watch(
            f"list/{self._list_version}",
            self.app.history.batch_page(
                "",
                self._cursor,
                self._newer,
                since=self.scope.since,
                until=self.scope.until,
                batch=self.scope.batch,
                unbatched=self.scope.unbatched,
            ),
        )

    def _selection(self) -> None:
        item = self.table.currentItem()
        uid = str(item.data(0, Qt.ItemDataRole.UserRole)) if item is not None else None
        changed = uid != self._selected
        self._selected = uid
        self._prune()
        self._render()
        self._load_detail()
        if changed:
            self.selection_changed.emit()

    def _prune(self) -> None:
        self._cache = {
            uid: value
            for uid, value in self._cache.items()
            if uid in (self._selected, self._expanded)
        }
        self._errors = {
            uid: value
            for uid, value in self._errors.items()
            if uid in (self._selected, self._expanded)
        }

    def _expand(self, item: QTreeWidgetItem) -> None:
        uid = str(item.data(0, Qt.ItemDataRole.UserRole))
        if item.parent() is not None or uid == self._expanded:
            return
        if self._expanded is not None:
            previous = self._items.get(self._expanded)
            if previous is not None:
                previous.setExpanded(False)
        self._expanded = uid
        data = self._cache.get(uid)
        self._run_rows = list(data.runs) if data else []
        self._errors.pop(uid, None)  # Explicit re-expansion can retry a failed read.
        self._error = ""
        self._prune()
        self._render()
        self._load_detail()

    def _collapse(self, item: QTreeWidgetItem) -> None:
        uid = str(item.data(0, Qt.ItemDataRole.UserRole))
        if uid != self._expanded:
            return
        current = self.table.currentItem()
        if current is not None and current.parent() is item:
            self.table.setCurrentItem(item)
        item.takeChildren()
        self._expanded = None
        self._run_rows = []
        self._prune()

    def _load_detail(self) -> None:
        # One queued read at a time. Pages share the existing Run anchor; only the
        # expanded Batch accumulates children. Selection never changes that scope.
        if self._disposed or self._reading is not None or self.app.history is None:
            return
        for uid in (self._selected, self._expanded):
            if uid is not None and uid not in self._cache and uid not in self._errors:
                self._reading = self._version, uid, 0
                self._delivery.watch("detail", self.app.history.batch_detail(uid))
                return
        data = self._cache.get(self._expanded) if self._expanded is not None else None
        if (
            data is not None
            and data.batch.uuid not in self._errors
            and len(self._run_rows) < data.run_count
        ):
            offset = len(self._run_rows)
            self._reading = self._version, data.batch.uuid, offset
            self._delivery.watch(
                "detail",
                self.app.history.batch_detail(data.batch.uuid, offset, data.run_anchor),
            )

    @Slot(str, object, object)
    def _completed(self, token: str, value: object, error: object) -> None:
        if self._disposed:
            return
        if token.startswith("list/"):
            self._listing = False
            if token != f"list/{self._list_version}":
                self._load_list()
                return
            self._page = value if isinstance(value, BatchList) else BatchList((), False)
            self._error = str(error) if error is not None else ""
            self._version += 1
            self._cache.clear()
            self._errors.clear()
            self._run_rows = []
            self._rebuild()
        else:
            request, self._reading = self._reading, None
            if request is None:
                return
            version, uid, offset = request
            if version == self._version and uid in (self._selected, self._expanded):
                if (
                    isinstance(value, BatchDetail)
                    and error is None
                    and (offset == 0 or value.runs)
                ):
                    if offset == 0:
                        self._cache[uid] = value
                        if uid == self._expanded:
                            self._run_rows = list(value.runs)
                    elif uid == self._expanded and offset == len(self._run_rows):
                        self._run_rows.extend(value.runs)
                else:
                    self._errors[uid] = str(error) if error is not None else ""
        self._render()
        self._load_detail()

    def _rebuild(self) -> None:
        previous = self._selected
        with QSignalBlocker(self.table):
            self.table.clear()
            self._items.clear()
            for row in self._page.rows:
                item = QTreeWidgetItem(self.table)
                item.setData(0, Qt.ItemDataRole.UserRole, row.uuid)
                item.setChildIndicatorPolicy(
                    QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator
                    if row.first_run is not None
                    else QTreeWidgetItem.ChildIndicatorPolicy.DontShowIndicator
                )
                self._items[row.uuid] = item
            if self._selected not in self._items:
                self._selected = self._page.rows[0].uuid if self._page.rows else None
            if self._expanded not in self._items:
                self._expanded = None
            if self._expanded is not None:
                self._items[self._expanded].setExpanded(True)
            if self._selected is not None:
                self.table.setCurrentItem(self._items[self._selected])
        if self._selected != previous:
            self.selection_changed.emit()

    def _values(
        self,
        item: QTreeWidgetItem,
        values: tuple[tuple[str, str], ...],
        *,
        parent: bool,
    ) -> None:
        height = max(36, self.table.fontMetrics().height() + Style.space)
        if item.sizeHint(0).height() != height:
            item.setSizeHint(0, QSize(0, height))
        font = self.table.font()
        font.setBold(parent)
        for col, (caption, tip) in enumerate(values):
            item.setText(col, caption)
            item.setToolTip(col, tip)
            item.setData(col, Qt.ItemDataRole.AccessibleTextRole, tip)
            item.setFont(col, font)
            if col >= 4:
                item.setTextAlignment(col, _NUMBER_ALIGN)

    def _parent(self, item: QTreeWidgetItem, row: BatchRow) -> None:
        recent, full = _time(row.recent_run)
        if row.recent_outcome is not None:
            state = _outcome(row.recent_outcome, self.language)
            full = f"{state} · {full}"
        data = self._cache.get(row.uuid)
        tip = row.label
        if data is not None:
            extras = [
                text(key, self.language) + ": " + value
                for key, value in zip(
                    _INFO,
                    (data.info.product, data.info.specification, data.info.notes),
                    strict=True,
                )
                if value
            ]
            tip = "\n".join([tip, *extras, *_warnings(data, self.language)])
        counts = _quantities(row.totals if row.totals.batches_completed else None)
        self._values(
            item,
            (
                (row.label, tip),
                _time(row.first_run),
                (recent, full),
                ("—", "—"),
                *((v, v) for v in counts),
            ),
            parent=True,
        )

    def _child(self, item: QTreeWidgetItem, run: RunRow, number: int) -> None:
        state = _outcome(run.outcome, self.language)
        title = f"{text('export_run', self.language)} {number}"
        if run.outcome != "stopped":
            title += f" · {state}"
        evidence = text("run_uncertain_totals", self.language) if run.uncertain else ""
        if run.totals is None:
            evidence = " · ".join(
                filter(
                    None, (text("run_statistics_unavailable", self.language), evidence)
                )
            )
        name = run.account.username if run.account is not None else "—"
        self._values(
            item,
            (
                (title, " · ".join(filter(None, (title, state, evidence)))),
                _time(run.started),
                _time(run.terminated),
                (name, name),
                *(
                    (v, " · ".join(filter(None, (v, evidence))))
                    for v in _quantities(run.totals)
                ),
            ),
            parent=False,
        )

    def _render(self) -> None:
        self.newer.setEnabled(
            not self._listing
            and (self._page.more if self._newer else self._cursor is not None)
        )
        self.older.setEnabled(
            not self._listing
            and (self._cursor is not None if self._newer else self._page.more)
        )
        unavailable = text("history_unavailable", self.language)
        read_errors = " · ".join(
            dict.fromkeys(error or unavailable for error in self._errors.values())
        )
        message = (
            self._error
            or read_errors
            or (
                unavailable
                if self.app.history is None
                else text("report_no_batches", self.language)
                if not self._page.rows and not self._listing
                else ""
            )
        )
        self.message.setText(message)
        self.message.setVisible(bool(message))
        for row in self._page.rows:
            self._parent(
                self._items[row.uuid],
                self._cache[row.uuid].batch if row.uuid in self._cache else row,
            )
        if self._expanded is not None and self._expanded in self._cache:
            parent = self._items[self._expanded]
            detail = self._cache[self._expanded]
            for i, run in enumerate(self._run_rows):
                child = parent.child(i)
                if child is None:
                    child = QTreeWidgetItem(parent)
                    child.setData(0, Qt.ItemDataRole.UserRole, self._expanded)
                    child.setData(0, Qt.ItemDataRole.UserRole + 1, run.uuid)
                self._child(child, run, detail.run_count - i)
        self._metadata()
        self.refresh_state()

    def refresh_state(
        self, snapshot: ProductionApplicationSnapshot | None = None
    ) -> None:
        if snapshot is None:
            snapshot = self.app.snapshot()
        if snapshot.run_id != self._observed_run:
            self._observed_run = snapshot.run_id
            if self.isVisible():
                self._request_list()

    def _metadata(self) -> None:
        data = self.detail
        values = (
            (data.info.product, data.info.specification, data.info.notes)
            if data
            else ("", "", "")
        )
        height = max(72, self.fontMetrics().lineSpacing() * 3 + 12)
        if self.metadata.height() != height:
            self.metadata.setFixedHeight(height)
        for label, key, value in zip(self.metadata_labels, _INFO, values, strict=True):
            full = text(key, self.language) + ": " + value if value else ""
            label.setToolTip(full)
            label.setAccessibleName(full)
            label.setText(
                label.fontMetrics().elidedText(
                    full,
                    Qt.TextElideMode.ElideRight,
                    max(0, self.metadata.width() - 24),
                )
            )

    def dispose(self) -> None:
        self._disposed = True
        self._delivery.detach()
