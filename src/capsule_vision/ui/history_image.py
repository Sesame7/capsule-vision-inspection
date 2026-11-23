"""Read-only presentation of one linked archive image and its stored evidence."""

from pathlib import Path

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import (
    QColor,
    QImage,
    QImageReader,
    QPainter,
    QPaintEvent,
    QResizeEvent,
    QShowEvent,
    QTextOption,
)
from PySide6.QtWidgets import QDialog, QLabel, QSizePolicy, QVBoxLayout, QWidget

from capsule_vision.application.archive_links import relocated_image_path
from capsule_vision.application.history import HistoryFinding
from capsule_vision.inference import BoundingRect

from .finding_overlay import draw_findings, finding_caption
from .production_presentation import image_transform
from .style import Style, theme_colors


def read_archived_png(
    linked: Path, camera: str, root: Path | None, size: tuple[int, int] | None
) -> tuple[QImage, str]:
    """Use the exact linked PNG, confined to the supplied package in portable mode."""
    path = linked if root is None else relocated_image_path(linked, camera, root)
    if path is None or not path.is_absolute() or path.suffix.lower() != ".png":
        return QImage(), "history_image_missing"
    try:
        if root is not None:
            path = path.resolve(strict=True)
            if not path.is_relative_to(root.resolve(strict=True)):
                return QImage(), "history_image_missing"
        reader = QImageReader(str(path), b"png")
        reader.setAutoDetectImageFormat(False)
        reader.setAutoTransform(False)
        image = reader.read()
        if image.isNull():
            path.stat()
            return image, "history_image_open_failed"
        if size is not None and size != (image.width(), image.height()):
            return QImage(), "history_image_size_mismatch"
    except FileNotFoundError, NotADirectoryError:
        return QImage(), "history_image_missing"
    except OSError, ValueError:
        return QImage(), "history_image_open_failed"
    return image, ""


class HistoryImageViewport(QWidget):
    def __init__(
        self, image: QImage, findings: tuple[HistoryFinding, ...], parent: QWidget
    ) -> None:
        super().__init__(parent)
        self.empty_text = ""
        self.set_image(image, findings)

    def set_image(self, image: QImage, findings: tuple[HistoryFinding, ...]) -> None:
        self.image = image
        self.filtered_boxes = frozenset(f.bounding_rect for f in findings if f.filtered)
        labels: dict[BoundingRect, list[str]] = {}
        for finding in findings:
            labels.setdefault(finding.bounding_rect, []).append(
                finding_caption(finding.label, finding.area)
            )
        self.overlays = tuple(
            (rect, ", ".join(dict.fromkeys(values))) for rect, values in labels.items()
        )
        self.setAccessibleDescription(
            "\n".join(
                finding_caption(finding.label, finding.area)
                + " · "
                + ", ".join(f"CH{channel:02d}" for channel in finding.channels)
                for finding in findings
            )
        )
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#0d0f12"))
        if self.image.isNull():
            colors = theme_colors(self)
            painter.fillRect(self.rect(), QColor(colors.raised))
            painter.setPen(QColor(colors.secondary))
            options = QTextOption(Qt.AlignmentFlag.AlignCenter)
            options.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
            painter.drawText(QRectF(self.rect()), self.empty_text, options)
        elif min(self.width(), self.height()) > 0:
            transform = image_transform(
                self.image.width(), self.image.height(), self.width(), self.height()
            )
            area = QRectF(transform.x, transform.y, transform.width, transform.height)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawImage(area, self.image)
            draw_findings(painter, transform, self.overlays, self.filtered_boxes)
        painter.end()


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
        self.viewport = HistoryImageViewport(image, findings, self)
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
