"""Local calendar presentation and the two read-only event tables in Records."""

from datetime import UTC, datetime
from html import escape
from math import ceil

from PySide6.QtCore import (
    QDate,
    QDateTime,
    QEvent,
    QObject,
    QSize,
    Qt,
    QTimeZone,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QTreeWidget,
    QVBoxLayout,
    QWidget,
)

from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import Language
from capsule_vision.localization.record_text import readable_value as readable_value
from capsule_vision.localization.record_text import record_reason as record_reason

from .image_tools import image_icon
from .style import Style, theme_colors


def local_day_bounds(day: QDate) -> tuple[int, int | None]:
    """Half-open local calendar day, including DST gaps, folds and skipped dates."""
    zone = QTimeZone.systemTimeZone()

    def boundary(date: QDate) -> int:
        instant = date.startOfDay(zone)
        while (
            date.isValid() and not instant.isValid()
        ):  # A date can be skipped at the dateline.
            date = date.addDays(1)
            instant = date.startOfDay(zone)
        if not instant.isValid():
            raise ValueError("Invalid local calendar date")
        return instant.toSecsSinceEpoch()

    return boundary(day), None if day == QDate(9999, 12, 31) else boundary(
        day.addDays(1)
    )


def audit_utc_bound(stamp: int | None) -> str:
    if stamp is None:
        return ""
    # Match the owner's ISO +00:00 storage format for its lexical range query.
    return (
        QDateTime.fromSecsSinceEpoch(stamp, QTimeZone.utc()).toString(
            "yyyy-MM-ddTHH:mm:ss"
        )
        + "+00:00"
    )


def record_time(stamp: str | float, *, milliseconds: bool = False) -> tuple[str, str]:
    """Return a compact local caption and its full timestamp with UTC offset."""
    try:
        instant = (
            datetime.fromisoformat(stamp)
            if isinstance(stamp, str)
            else datetime.fromtimestamp(stamp, UTC)
        )
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=UTC)
        local = instant.astimezone()
    except ValueError, OSError, OverflowError:
        return str(stamp), str(stamp)  # Preserve an unreadable stored observation.
    caption = (
        local.strftime("%m-%d %H:%M:%S.%f")[:-3]
        if milliseconds
        else local.strftime("%Y-%m-%d %H:%M:%S")
    )
    return caption, local.isoformat(sep=" ", timespec="microseconds")


# Header, shortest usable sample, common sample, share of spare width.
type RecordColumn = tuple[str, str, str, int]


def fit_record_columns(
    table: QTableWidget | QTreeWidget, columns: tuple[RecordColumn, ...]
) -> None:
    if not columns:
        return
    body = table.fontMetrics()
    header = (
        table.header() if isinstance(table, QTreeWidget) else table.horizontalHeader()
    )
    titles = header.fontMetrics()
    minimums: list[int] = []
    preferred: list[int] = []
    padding = 2 * Style.space
    for key, short, common, _ in columns:
        # Budget representative localized headings independent of the active page.
        # Unusually long captions can elide without displacing business values.
        title = max(
            titles.horizontalAdvance(text(key, lang))
            for lang in ("zh", "en", "de", "el")
        )
        minimum = max(
            header.minimumSectionSize(),
            max(title, body.horizontalAdvance(short)) + padding,
        )
        minimums.append(minimum)
        preferred.append(max(minimum, body.horizontalAdvance(common) + padding))
    weights = tuple(weight for _, _, _, weight in columns)
    available = table.viewport().width()
    if available >= sum(preferred):
        widths = preferred
    elif available >= sum(minimums):
        widths = minimums
        weights = tuple(p - m for m, p in zip(minimums, preferred, strict=True))
    else:
        # Large fonts can make even the short samples exceed the viewport.
        # Let the delegate elide them within Qt's actual section minimum.
        widths = [header.minimumSectionSize()] * len(columns)
        weights = tuple(m - base for m, base in zip(minimums, widths, strict=True))
    extra = max(0, available - sum(widths))
    total, used = max(1, sum(weights)), 0
    for col, (base, weight) in enumerate(zip(widths, weights, strict=True)):
        width = base + extra * (used + weight) // total - extra * used // total
        used += weight
        if table.columnWidth(col) != width:
            table.setColumnWidth(col, width)


class RecordsPage(QWidget):
    def __init__(self, name: str) -> None:
        super().__init__()
        self.setObjectName(name + "Page")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(Style.small)
        bar = self.filter_bar = QHBoxLayout()
        bar.setSpacing(Style.small)
        root.addLayout(bar)
        self.search = QLineEdit()
        self.search.setClearButtonEnabled(True)
        self.kind = QComboBox()
        self.kind.setObjectName("recordFilter")
        self.kind.currentTextChanged.connect(self.kind.setToolTip)
        for w in (self.search, self.kind):
            w.setMinimumHeight(Style.touch)
            bar.addWidget(w, 1 if w is self.search else 0)
        self.search.setMinimumWidth(80)
        self.search.setMaximumWidth(112)
        self.kind.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.kind.setFixedWidth(176)
        bar.addStretch(1)
        self.newer, self.older, self.refresh_button = (
            QPushButton(),
            QPushButton(),
            QPushButton(),
        )
        for b in (self.newer, self.older, self.refresh_button):
            b.setFixedSize(Style.touch, Style.touch)
            bar.addWidget(b)
        self.message = QLabel()
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        self.message.hide()
        root.addWidget(self.message)

    def navigation_language(self, language: Language) -> None:
        for b, icon, key in (
            (self.newer, "previous", "previous_page"),
            (self.older, "next", "next_page"),
            (self.refresh_button, "refresh", "history_refresh"),
        ):
            b.setIcon(image_icon(icon, theme_colors(self).primary))
            b.setIconSize(QSize(Style.icon, Style.icon))
            b.setAccessibleName(text(key, language))
            b.setToolTip(b.accessibleName())


class HistoryEventTable(RecordsPage):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        root = self.layout()
        assert isinstance(root, QVBoxLayout)
        self.table = QTableWidget(0, 5)
        self.table.setObjectName(name + "Records")
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.table.horizontalHeader().setTextElideMode(Qt.TextElideMode.ElideRight)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(36)
        self.table.horizontalHeader().setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self._columns: tuple[RecordColumn, ...] = ()
        self.table.viewport().installEventFilter(self)
        root.addWidget(self.table, 3)
        self.details = QTextEdit()
        self.details.setObjectName("recordDetails")
        self.details.setReadOnly(True)
        self.details.setTabChangesFocus(True)
        root.addWidget(self.details)
        self.details.hide()

    def columns(self, language: Language, columns: tuple[RecordColumn, ...]) -> None:
        self._columns = columns
        for col, (key, _, _, _) in enumerate(columns):
            caption = text(key, language)
            item = QTableWidgetItem(caption)
            full_key = {
                "short_time": "audit_time",
                "records_user": "identity_username",
            }.get(key, key)
            full = text(full_key, language)
            item.setToolTip(full)
            item.setData(Qt.ItemDataRole.AccessibleTextRole, full)
            self.table.setHorizontalHeaderItem(col, item)
        fit_record_columns(self.table, columns)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            event.type() in (QEvent.Type.Resize, QEvent.Type.FontChange)
            and watched is self.table.viewport()
        ):
            fit_record_columns(self.table, self._columns)
        return super().eventFilter(watched, event)

    def set_values(
        self, row: int, values: tuple[str, ...], descriptions: dict[int, str]
    ) -> None:
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            full = descriptions.get(column, value)
            item.setToolTip(full)
            item.setData(Qt.ItemDataRole.AccessibleTextRole, full)
            self.table.setItem(row, column, item)

    def clear_details(self) -> None:
        self.details.clear()
        self.details.hide()

    def show_details(
        self, language: Language, fields: tuple[tuple[str, str], ...]
    ) -> None:
        lines: list[str] = []
        for key, value in fields:
            if value:
                label = f"<b>{escape(text(key, language))}</b> " if key else ""
                lines.append(
                    f"<p style='margin: 2px 0'>{label}<span style='white-space: pre-wrap'>{escape(value)}</span></p>"
                )
        if not lines:
            self.clear_details()
            return
        self.details.show()
        self.details.setHtml("".join(lines))
        height = (
            ceil(self.details.document().size().height())
            + 2 * self.details.frameWidth()
        )
        self.details.setFixedHeight(
            min(height, 8 * self.details.fontMetrics().lineSpacing())
        )
        self.details.verticalScrollBar().setValue(0)
