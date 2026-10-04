"""Frozen BGR image painting and thin Qt gesture delivery."""

from collections.abc import Callable

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QImage,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QResizeEvent,
    QWheelEvent,
)
from PySide6.QtWidgets import QWidget

from vimo_demo.inference import BoundingRect
from vimo_demo.inspection.production import ChannelROI

from .display_frames import DisplayFrame
from .production_style import Style
from .roi_geometry import Handle, RoiInteractionModel, drag_rect, handle_points

CURSORS: dict[Handle, Qt.CursorShape] = {
    "move": Qt.CursorShape.SizeAllCursor,
    "n": Qt.CursorShape.SizeVerCursor,
    "s": Qt.CursorShape.SizeVerCursor,
    "e": Qt.CursorShape.SizeHorCursor,
    "w": Qt.CursorShape.SizeHorCursor,
    "nw": Qt.CursorShape.SizeFDiagCursor,
    "se": Qt.CursorShape.SizeFDiagCursor,
    "ne": Qt.CursorShape.SizeBDiagCursor,
    "sw": Qt.CursorShape.SizeBDiagCursor,
}


class RoiCanvas(QWidget):
    geometry_changed = Signal(object)
    selected = Signal(int)

    def __init__(self, allowed: Callable[[], bool]) -> None:
        super().__init__()
        self.model = RoiInteractionModel()
        self.rois: tuple[ChannelROI, ...] = ()
        self._allowed = allowed
        self._image = QImage()
        self._drag: tuple[BoundingRect, Handle, float, float] | None = None
        self._pan_start: tuple[QPointF, tuple[float, float]] | None = None
        self.setMinimumSize(320, 240)
        self.setMouseTracking(True)

    def set_frame(self, frame: DisplayFrame | None) -> None:
        self.cancel_gesture()
        self.model.scale, self.model.pan = None, (0, 0)
        if frame is None:
            self._image = QImage()
            self.model.size = None
        else:
            image = frame.image
            height, width = image.shape[:2]
            # Same detached BGR ownership as Production CameraViewport.
            self._image = QImage(
                image.data, width, height, image.strides[0], QImage.Format.Format_BGR888
            ).copy()
            self.model.size = width, height
        self.update()

    def cancel_gesture(self) -> None:
        self._drag = None
        self._pan_start = None
        self.unsetCursor()

    def cancel_roi_drag(self) -> None:
        """A read-only state revokes edits, but must not interrupt diagnostic pan."""
        self._drag = None
        if self._pan_start is None:
            self.unsetCursor()

    def zoom(self, scale: float | None) -> None:
        self.cancel_gesture()
        self.model.scale, self.model.pan = scale, (0, 0)
        self.update()

    def wheelEvent(self, event: QWheelEvent) -> None:
        transform = self.model.transform(self.width(), self.height())
        if transform is None or event.angleDelta().y() == 0:
            event.ignore()
            return
        self.cancel_gesture()
        point = event.position()
        sx, sy = transform.source_point(point.x(), point.y())
        scale = min(
            8.0, max(0.05, transform.scale * 1.2 ** (event.angleDelta().y() / 120))
        )
        self.model.scale = scale
        assert self.model.size is not None
        width, height = self.model.size
        # Keep the pointed source pixel fixed where bounded pan permits it.
        mx, my = (
            max(0, (width * scale - self.width()) / 2),
            max(0, (height * scale - self.height()) / 2),
        )
        px = point.x() - sx * scale - (self.width() - width * scale) / 2
        py = point.y() - sy * scale - (self.height() - height * scale) / 2
        self.model.pan = min(mx, max(-mx, px)), min(my, max(-my, py))
        self.update()
        event.accept()

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(Style.background))
        transform = self.model.transform(self.width(), self.height())
        if transform is None:
            return
        destination = QRectF(
            transform.x, transform.y, transform.width, transform.height
        )
        painter.drawImage(destination, self._image)
        painter.setClipRect(destination)
        for slot in self.rois:
            outline = QRectF(*transform.rectangle(slot.rect))
            painter.setPen(QPen(QColor(Style.primary), 1))
            painter.drawRect(outline)
            painter.drawText(
                outline.adjusted(3, 3, 0, 0),
                Qt.AlignmentFlag.AlignTop,
                f"CH{slot.channel_id:02d}",
            )
        rect = self.model.rect
        if rect is None:
            return
        outline = QRectF(*transform.rectangle(rect))
        painter.setPen(QPen(QColor("black"), 4))
        painter.drawRect(outline)
        pen = QPen(QColor(Style.accent if self.model.editable else Style.primary), 2)
        if not self.model.editable:
            pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.drawRect(outline)
        if self.model.can_drag:
            # Selected rectangle has handles; the other five retain their labels.
            painter.setPen(QPen(QColor("black"), 2))
            painter.setBrush(QColor(Style.accent))
            for x, y in handle_points(rect, transform):
                painter.drawRect(QRectF(x - 5, y - 5, 10, 10))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        transform = self.model.transform(self.width(), self.height())
        if transform is None:
            return
        point = event.position()
        self.cancel_gesture()
        if event.button() == Qt.MouseButton.MiddleButton:
            if transform.width > self.width() or transform.height > self.height():
                self._pan_start = point, self.model.pan
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
        elif event.button() == Qt.MouseButton.LeftButton:
            sx, sy = transform.source_point(point.x(), point.y())
            # Keep selected handles reachable; otherwise pick the first hit in
            # configured slot order (deterministic even for overlapping ROIs).
            if self.model.hit(point.x(), point.y(), transform) is None:
                for index, slot in enumerate(self.rois):
                    r = slot.rect
                    if r.x <= sx <= r.x + r.width and r.y <= sy <= r.y + r.height:
                        self.selected.emit(index)
                        break
            if not self._allowed():
                return
            handle = self.model.hit(point.x(), point.y(), transform)
            if handle is not None and self.model.rect is not None:
                self._drag = (
                    self.model.rect,
                    handle,
                    *transform.source_point(point.x(), point.y()),
                )
                self.setCursor(CURSORS[handle])

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        point = event.position()
        transform = self.model.transform(self.width(), self.height())
        if transform is None:
            return
        if self._pan_start is not None:
            start, pan = self._pan_start
            # Keep at least the image edges accessible, and center fitting axes.
            px, py = pan[0] + point.x() - start.x(), pan[1] + point.y() - start.y()
            mx, my = (
                max(0, (transform.width - self.width()) / 2),
                max(0, (transform.height - self.height()) / 2),
            )
            self.model.pan = min(mx, max(-mx, px)), min(my, max(-my, py))
            self.update()
        elif self._drag is not None:
            if not self._allowed() or not self.model.can_drag:
                self.cancel_gesture()
                return
            rect, handle, x, y = self._drag
            sx, sy = transform.source_point(point.x(), point.y())
            assert self.model.size is not None
            self.geometry_changed.emit(
                drag_rect(rect, handle, sx - x, sy - y, self.model.size)
            )
        else:
            handle = (
                self.model.hit(point.x(), point.y(), transform)
                if self._allowed()
                else None
            )
            self.setCursor(CURSORS[handle] if handle else Qt.CursorShape.ArrowCursor)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self.mouseMoveEvent(event)
        self.cancel_gesture()

    def resizeEvent(self, event: QResizeEvent) -> None:
        self.cancel_gesture()
        self.model.pan = (0, 0)
        super().resizeEvent(event)
