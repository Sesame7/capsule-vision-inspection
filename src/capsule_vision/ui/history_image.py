"""Read-only presentation of one linked archive image and its stored evidence."""

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import (
    QColor,
    QImage,
    QPainter,
    QPaintEvent,
    QPen,
    QRegion,
    QResizeEvent,
    QShowEvent,
)
from PySide6.QtWidgets import QDialog, QLabel, QSizePolicy, QVBoxLayout, QWidget

from capsule_vision.application.history import HistoryFinding
from capsule_vision.inference import BoundingRect

from .production_presentation import image_transform
from .style import Style


class _ImageViewport(QWidget):
    def __init__(
        self, image: QImage, findings: tuple[HistoryFinding, ...], parent: QWidget
    ) -> None:
        super().__init__(parent)
        self.image = image
        labels: dict[BoundingRect, list[str]] = {}
        for finding in findings:
            labels.setdefault(finding.bounding_rect, []).append(finding.label)
        self.overlays = tuple(
            (rect, ", ".join(dict.fromkeys(values))) for rect, values in labels.items()
        )
        self.setAccessibleDescription(
            "\n".join(
                f"{finding.label} · "
                + ", ".join(f"CH{channel:02d}" for channel in finding.channels)
                for finding in findings
            )
        )

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#0d0f12"))
        if min(self.width(), self.height()) > 0:
            transform = image_transform(
                self.image.width(), self.image.height(), self.width(), self.height()
            )
            area = QRectF(transform.x, transform.y, transform.width, transform.height)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawImage(area, self.image)
            painter.setClipRect(area)
            painter.setPen(QPen(QColor(Style.ng), 2))
            boxes = tuple(
                (QRectF(*transform.rectangle(rect)), label)
                for rect, label in self.overlays
            )
            for box, _ in boxes:
                painter.drawRect(box)
            labels = QRegion()
            for box, label in boxes:
                self._draw_label(painter, area, box, label, labels)
        painter.end()

    @staticmethod
    def _draw_label(
        painter: QPainter, area: QRectF, box: QRectF, label: str, occupied: QRegion
    ) -> None:
        metrics = painter.fontMetrics()
        padding = 4
        height = metrics.height() + 4
        caption = metrics.elidedText(
            label,
            Qt.TextElideMode.ElideRight,
            max(0, min(200, int(area.width()) - 2 * padding)),
        )
        if not caption or height > area.height():
            return
        width = metrics.horizontalAdvance(caption) + 2 * padding
        x = min(box.left(), area.right() - width)
        y = max(area.top(), box.top() - height)
        badge = QRectF(x, y, width, height)
        if occupied.intersects(badge.toAlignedRect()):
            badge.moveTop(min(box.bottom(), area.bottom() - height))
        bounds = badge.toAlignedRect()
        if occupied.intersects(bounds):
            return  # Keep the box and its accessible label when neither caption fits.
        occupied |= bounds
        painter.fillRect(badge, QColor(Style.background))
        painter.setPen(QColor(Style.primary))
        painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, caption)


class HistoryImageDialog(QDialog):
    def __init__(
        self,
        parent: QWidget,
        image: QImage,
        findings: tuple[HistoryFinding, ...],
        title: str,
        caption: str,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._positioned = False
        self._caption = caption
        self.viewport = _ImageViewport(image, findings, self)
        self.viewport.setAccessibleName(title)
        self.caption = QLabel()
        self.caption.setTextFormat(Qt.TextFormat.PlainText)
        self.caption.setText(caption)
        self.caption.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.caption.setProperty("role", "secondary")
        self.caption.setToolTip(caption)
        self.caption.setAccessibleName(caption)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(Style.small, Style.small, Style.small, Style.small)
        layout.setSpacing(Style.small)
        layout.addWidget(self.viewport, 1)
        layout.addWidget(self.caption)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if not self._positioned:
            self._positioned = True
            available = self.screen().availableGeometry()
            frame_insets = self.frameGeometry().size() - self.size()
            # The layout minimum accounts for the caption, spacing and margins.
            # The viewport has no intrinsic minimum size.
            layout_insets = self.minimumSizeHint()
            budget = QSize(
                round(available.width() * 0.92), round(available.height() * 0.92)
            )
            image_budget = (budget - frame_insets - layout_insets).expandedTo(
                QSize(1, 1)
            )
            fitted = self.viewport.image.size().scaled(
                image_budget, Qt.AspectRatioMode.KeepAspectRatio
            )
            self.resize(fitted.expandedTo(QSize(1, 1)) + layout_insets)
            frame = self.frameGeometry()
            self.move(
                available.left() + (available.width() - frame.width()) // 2,
                available.top() + (available.height() - frame.height()) // 2,
            )

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self.caption.setText(
            self.caption.fontMetrics().elidedText(
                self._caption,
                Qt.TextElideMode.ElideRight,
                max(0, event.size().width() - 2 * Style.small),
            )
        )
