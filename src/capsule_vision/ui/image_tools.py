"""Shared Vision/Recipe image tools; whole-group wrapping and DPR-aware icons."""

from PySide6.QtCore import QPoint, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap, QResizeEvent
from PySide6.QtWidgets import QGridLayout, QSizePolicy, QWidget

from .style import Style, camera_icon, shell_icon


def image_icon(tool: str, color: str = Style.primary) -> QIcon:
    """Local vector tools with DPR-aware enabled/disabled states."""
    if tool == "capture":
        return camera_icon(color)
    if tool == "folder":
        return shell_icon("folder", color)
    icon = QIcon()
    for checked in (False, True):
        for dpr in (1.0, 1.25, 1.5, 2.0):
            pixmap = QPixmap(round(24 * dpr), round(24 * dpr))
            pixmap.setDevicePixelRatio(dpr)
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QPen(QColor(color), 1.6))
            if tool == "file_import":
                painter.drawPolyline(
                    [
                        QPoint(8, 3),
                        QPoint(16, 3),
                        QPoint(21, 8),
                        QPoint(21, 21),
                        QPoint(8, 21),
                    ]
                )
                painter.drawPolyline([QPoint(16, 3), QPoint(16, 8), QPoint(21, 8)])
                painter.drawLine(2, 13, 14, 13)
                painter.drawPolyline([QPoint(10, 9), QPoint(14, 13), QPoint(10, 17)])
            elif tool == "accelerate":
                painter.drawPolyline(
                    [
                        QPoint(14, 2),
                        QPoint(5, 13),
                        QPoint(11, 13),
                        QPoint(9, 22),
                        QPoint(20, 10),
                        QPoint(14, 10),
                        QPoint(14, 2),
                    ]
                )
            elif tool == "open_folder":
                painter.drawPolyline(
                    [QPoint(3, 9), QPoint(3, 20), QPoint(20, 20), QPoint(20, 14)]
                )
                painter.drawPolyline(
                    [QPoint(3, 9), QPoint(3, 5), QPoint(9, 5), QPoint(12, 8)]
                )
                painter.drawLine(12, 13, 21, 4)
                painter.drawLine(15, 4, 21, 4)
                painter.drawLine(21, 4, 21, 10)
            elif tool == "refresh":
                painter.drawArc(5, 5, 14, 14, 40 * 16, 290 * 16)
                painter.drawLine(19, 4, 19, 10)
                painter.drawLine(19, 10, 13, 10)
            elif tool == "discover":
                painter.drawEllipse(3, 3, 12, 12)
                painter.drawLine(14, 14, 21, 21)
            elif tool in ("connect", "disconnect"):
                painter.drawLine(8, 3, 8, 8)
                painter.drawLine(16, 3, 16, 8)
                painter.drawRect(6, 8, 12, 6)
                painter.drawLine(12, 14, 12, 21)
                if tool == "disconnect":
                    painter.drawLine(3, 21, 21, 3)
            elif tool == "cancel":
                painter.drawLine(5, 5, 19, 19)
                painter.drawLine(19, 5, 5, 19)
            elif tool == "runtime":
                painter.drawRect(3, 4, 18, 13)
                painter.drawLine(9, 20, 15, 20)
                painter.drawLine(12, 17, 12, 20)
                painter.drawLine(9, 7, 16, 11)
                painter.drawLine(16, 11, 9, 14)
                painter.drawLine(9, 14, 9, 7)
            elif tool == "loop":
                painter.drawLine(4, 7, 20, 7)
                painter.drawLine(20, 7, 16, 3)
                painter.drawLine(20, 7, 16, 11)
                painter.drawLine(20, 17, 4, 17)
                painter.drawLine(4, 17, 8, 13)
                painter.drawLine(4, 17, 8, 21)
            elif tool == "apply":
                painter.drawLine(4, 12, 9, 17)
                painter.drawLine(9, 17, 20, 6)
            elif tool == "sample":
                painter.drawPolyline(
                    [
                        QPoint(4, 20),
                        QPoint(5, 15),
                        QPoint(15, 5),
                        QPoint(19, 9),
                        QPoint(9, 19),
                        QPoint(4, 20),
                    ]
                )
                painter.drawLine(12, 4, 20, 12)
                painter.drawLine(16, 5, 19, 2)
                painter.drawLine(19, 2, 22, 5)
                painter.drawLine(22, 5, 19, 8)
            elif tool == "write":
                painter.drawPolyline(
                    [
                        QPoint(4, 20),
                        QPoint(5, 15),
                        QPoint(17, 3),
                        QPoint(21, 7),
                        QPoint(9, 19),
                        QPoint(4, 20),
                    ]
                )
                painter.drawLine(14, 6, 18, 10)
            elif tool == "save":
                painter.drawPolyline(
                    [
                        QPoint(4, 3),
                        QPoint(17, 3),
                        QPoint(21, 7),
                        QPoint(21, 21),
                        QPoint(3, 21),
                        QPoint(3, 3),
                        QPoint(4, 3),
                    ]
                )
                painter.drawRect(7, 3, 9, 6)
                painter.drawRect(7, 14, 10, 7)
            elif tool == "common":
                painter.drawRect(3, 3, 12, 12)
                painter.drawRect(8, 8, 12, 12)
            elif tool == "distribute":
                for x, height in ((3, 12), (10, 8), (17, 12)):
                    painter.drawRect(x, (24 - height) // 2, 4, height)
                painter.drawLine(3, 21, 21, 21)
                painter.drawLine(3, 19, 3, 23)
                painter.drawLine(21, 19, 21, 23)
            elif tool in ("previous", "next"):
                x, dx = (15, -7) if tool == "previous" else (9, 7)
                painter.drawLine(x, 5, x + dx, 12)
                painter.drawLine(x + dx, 12, x, 19)
            elif tool in ("minus", "plus"):
                painter.drawLine(6, 12, 18, 12)
                if tool == "plus":
                    painter.drawLine(12, 6, 12, 18)
            elif tool == "copy":
                painter.drawRect(4, 3, 12, 14)
                painter.drawRect(8, 7, 12, 14)
            elif tool == "delete":
                painter.drawLine(4, 6, 20, 6)
                painter.drawRect(8, 3, 8, 3)
                painter.drawRect(6, 6, 12, 15)
                painter.drawLine(10, 10, 10, 17)
                painter.drawLine(14, 10, 14, 17)
            else:
                for x, y, dx, dy in (
                    (4, 4, 1, 1),
                    (20, 4, -1, 1),
                    (4, 20, 1, -1),
                    (20, 20, -1, -1),
                ):
                    painter.drawLine(x, y, x + dx * 5, y)
                    painter.drawLine(x, y, x, y + dy * 5)
            painter.end()
            state = QIcon.State.On if checked else QIcon.State.Off
            icon.addPixmap(pixmap, QIcon.Mode.Normal, state)
            dimmed = QPixmap(pixmap.size())
            dimmed.setDevicePixelRatio(dpr)
            dimmed.fill(Qt.GlobalColor.transparent)
            muted = QPainter(dimmed)
            muted.setOpacity(0.35)
            muted.drawPixmap(0, 0, pixmap)
            muted.end()
            icon.addPixmap(dimmed, QIcon.Mode.Disabled, state)
    return icon


class ImageToolbar(QWidget):
    """Align three compact groups; wrap whole groups without shrinking controls."""

    def __init__(self, leading: QWidget, center: QWidget, trailing: QWidget) -> None:
        super().__init__()
        self._groups = leading, center, trailing
        for group in self._groups:
            group.setSizePolicy(
                QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Preferred
            )
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(Style.space)
        self._grid.setVerticalSpacing(Style.small)
        # Equal flexible side columns center the middle group when both sides fit.
        self._grid.setColumnStretch(0, 1)
        self._grid.setColumnStretch(2, 1)
        self._rows = 0
        policy = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self._arrange(self.width())

    def _sizes(self) -> tuple[QSize, ...]:
        return tuple(
            QSize(0, 0)
            if group.isHidden()
            else group.minimumSizeHint().expandedTo(group.minimumSize())
            for group in self._groups
        )

    def _row_width(self) -> int:
        return sum(size.width() for size in self._sizes()) + 2 * Style.space

    def _row_count(self, width: int) -> int:
        if width >= self._row_width():
            return 1
        remaining = sum(size.width() for size in self._sizes()[1:])
        return 2 if width >= remaining + Style.space else 3

    def minimumSizeHint(self) -> QSize:
        sizes = self._sizes()
        return QSize(
            max(size.width() for size in sizes), max(size.height() for size in sizes)
        )

    def sizeHint(self) -> QSize:
        return QSize(self._row_width(), self.heightForWidth(self._row_width()))

    def heightForWidth(self, width: int) -> int:
        heights = [size.height() for size in self._sizes()]
        rows = self._row_count(width)
        if rows == 1:
            return max(heights)
        if rows == 2:
            return heights[0] + max(heights[1:]) + Style.small
        return sum(heights) + 2 * Style.small

    def sync_metrics(self) -> None:
        # Recompute after localized labels/status changes.
        for group in self._groups:
            layout = group.layout()
            if layout is not None:
                layout.invalidate()
                group.setMinimumWidth(layout.minimumSize().width())
            else:
                group.setMinimumWidth(group.sizeHint().width())
        self._arrange(self.width())

    def _arrange(self, width: int) -> None:
        rows = self._row_count(width)
        if rows != self._rows:
            for group in self._groups:
                self._grid.removeWidget(group)
            leading, center, trailing = self._groups
            left = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
            middle = Qt.AlignmentFlag.AlignCenter
            right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            if rows == 1:
                self._grid.addWidget(leading, 0, 0, left)
                self._grid.addWidget(center, 0, 1, middle)
                self._grid.addWidget(trailing, 0, 2, right)
            elif rows == 2:
                self._grid.addWidget(leading, 0, 0, 1, 3, left)
                self._grid.addWidget(center, 1, 1, middle)
                self._grid.addWidget(trailing, 1, 2, right)
            else:
                for row, (group, alignment) in enumerate(
                    zip(self._groups, (left, middle, right), strict=True)
                ):
                    self._grid.addWidget(group, row, 0, 1, 3, alignment)
            self._rows = rows
        required_height = self.heightForWidth(width)
        if self.minimumHeight() != required_height:
            self.setMinimumHeight(required_height)
        self.updateGeometry()

    def resizeEvent(self, event: QResizeEvent) -> None:
        self._arrange(event.size().width())
        super().resizeEvent(event)
