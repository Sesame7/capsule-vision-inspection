"""Compact, persistent multi-selection popup for recorded Cycle states."""

from PySide6.QtCore import (
    QEvent,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QRect,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QStandardItemModel,
)
from PySide6.QtWidgets import (
    QComboBox,
    QListView,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionComboBox,
    QStyleOptionViewItem,
    QStylePainter,
    QWidget,
)

from capsule_vision.application.report_scope import (
    ALL_RESULTS,
    RESULT_STATES,
    ResultFilter,
    result_code,
)
from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import Language

from .style import Style, theme_colors


class _StateDelegate(QStyledItemDelegate):
    def sizeHint(
        self, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex
    ) -> QSize:
        size = super().sizeHint(option, index)
        return QSize(size.width() + Style.touch, max(Style.touch, size.height()))

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        label = QStyleOptionViewItem(option)
        self.initStyleOption(label, index)
        widget = option.widget
        assert widget is not None
        colors = theme_colors(widget)
        painter.save()
        painter.fillRect(
            option.rect,
            QColor(
                colors.raised
                if option.state & QStyle.StateFlag.State_MouseOver
                else colors.surface
            ),
        )
        painter.setPen(QColor(colors.primary))
        painter.setFont(label.font)
        rect = option.rect.adjusted(10, 0, -36, 0)
        painter.drawText(
            rect,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            label.fontMetrics.elidedText(
                label.text, Qt.TextElideMode.ElideRight, rect.width()
            ),
        )
        if index.data(Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked:
            check = QRect(
                option.rect.right() - 29, option.rect.top(), 20, option.rect.height()
            )
            painter.drawText(check, Qt.AlignmentFlag.AlignCenter, "✓")
        # Keyboard focus remains visible without using a selected-row fill or check box.
        if option.state & QStyle.StateFlag.State_HasFocus and widget.hasFocus():
            painter.setPen(QColor(colors.secondary))
            painter.drawLine(
                option.rect.left() + 3,
                option.rect.top() + 10,
                option.rect.left() + 3,
                option.rect.bottom() - 10,
            )
        painter.restore()


class HistoryResultFilter(QComboBox):
    changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.language: Language = "zh"
        self.selected = ALL_RESULTS
        self.setView(QListView())
        self.view().setMouseTracking(True)
        self.setItemDelegate(_StateDelegate(self))
        for state in RESULT_STATES:
            self.addItem("ERR" if state == "ERROR" else state, state)
        model = self.model()
        assert isinstance(model, QStandardItemModel)
        for i in range(len(RESULT_STATES)):
            item = model.item(i)
            assert item is not None
            item.setCheckable(True)
        self.view().installEventFilter(self)
        self.view().viewport().installEventFilter(self)
        self.set_language(self.language)

    def set_selected(self, selected: ResultFilter) -> None:
        if not selected <= ALL_RESULTS:
            raise ValueError("Unknown History result filter")
        changed = self.selected != selected
        self.selected = selected
        for i, state in enumerate(RESULT_STATES):
            self.setItemData(
                i,
                Qt.CheckState.Checked if state in selected else Qt.CheckState.Unchecked,
                Qt.ItemDataRole.CheckStateRole,
            )
        descriptions = [
            text("history_" + ("error" if s == "ERROR" else s.lower()), self.language)
            for s in RESULT_STATES
            if s in selected
        ]
        self.setToolTip(
            text("history_all", self.language)
            if selected == ALL_RESULTS
            else " / ".join(descriptions) or "0 / 5"
        )
        self.setAccessibleDescription(self.toolTip())
        self.update()
        if changed:
            self.changed.emit()

    def set_language(self, language: Language) -> None:
        self.language = language
        self.setAccessibleName(text("history_result", language))
        for i, state in enumerate(RESULT_STATES):
            full = text(
                "history_" + ("error" if state == "ERROR" else state.lower()), language
            )
            self.setItemData(i, full, Qt.ItemDataRole.ToolTipRole)
            self.setItemData(i, full, Qt.ItemDataRole.AccessibleTextRole)
        self.set_selected(self.selected)

    def _toggle(self, index: QModelIndex | QPersistentModelIndex) -> None:
        if index.isValid():
            state = RESULT_STATES[index.row()]
            self.set_selected(self.selected ^ frozenset((state,)))

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            watched is self.view().viewport()
            and isinstance(event, QMouseEvent)
            and event.button() == Qt.MouseButton.LeftButton
        ):
            index = self.view().indexAt(event.position().toPoint())
            if event.type() == QEvent.Type.MouseButtonPress:
                self.view().setCurrentIndex(index)
                return True
            if event.type() == QEvent.Type.MouseButtonRelease:
                self._toggle(index)
                return True
        if (
            watched in (self.view(), self.view().viewport())
            and isinstance(event, QKeyEvent)
            and event.type() == QEvent.Type.KeyPress
            and event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter)
        ):
            self._toggle(self.view().currentIndex())
            return True
        # The native combo container has its own filter. Forward untouched events
        # once through Qt, rather than invoking QComboBox's filter a second time.
        return False

    def paintEvent(self, event: QPaintEvent) -> None:
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        caption = (
            text("history_all", self.language)
            if self.selected == ALL_RESULTS
            else result_code(self.selected)
        )
        option.currentText = self.fontMetrics().elidedText(
            caption, Qt.TextElideMode.ElideRight, max(0, self.width() - 40)
        )
        painter = QStylePainter(self)
        painter.drawComplexControl(QStyle.ComplexControl.CC_ComboBox, option)
        painter.drawControl(QStyle.ControlElement.CE_ComboBoxLabel, option)
