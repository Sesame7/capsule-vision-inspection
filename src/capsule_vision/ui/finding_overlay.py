"""Paint retained findings in the original image coordinate system."""

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen, QRegion

from capsule_vision.inference import BoundingRect

from .production_presentation import ImageTransform
from .style import Style


def finding_caption(label: str, area: int | None) -> str:
    return label if area is None else f"{label} · {area} px²"


def draw_findings(
    painter: QPainter,
    transform: ImageTransform,
    overlays: tuple[tuple[BoundingRect, str], ...],
    filtered_boxes: frozenset[BoundingRect] = frozenset(),
) -> None:
    area = QRectF(transform.x, transform.y, transform.width, transform.height)
    painter.save()
    painter.setClipRect(area)
    solid = QPen(QColor(Style.ng), 2)
    dashed = QPen(QColor(Style.warning), 2, Qt.PenStyle.DashLine)
    boxes = [
        (QRectF(*transform.rectangle(rect)), label, rect in filtered_boxes)
        for rect, label in overlays
    ]
    occupied = QRegion()
    for box, _, filtered in boxes:
        painter.setPen(dashed if filtered else solid)
        painter.drawRect(box)
        outer = QRegion(box.adjusted(-1, -1, 1, 1).toAlignedRect())
        inner = box.adjusted(2, 2, -2, -2).toRect()
        occupied |= outer.subtracted(QRegion(inner)) if inner.isValid() else outer
    metrics = painter.fontMetrics()
    height = metrics.height() + 4
    for box, label, filtered in boxes:
        caption = metrics.elidedText(
            label, Qt.TextElideMode.ElideRight, max(0, min(200, int(area.width()) - 8))
        )
        if not caption or height > area.height():
            continue
        width = metrics.horizontalAdvance(caption) + 8
        x = min(box.left(), area.right() - width)
        for left, top in (
            (x, box.top() - height - 2),
            (x, box.bottom() + 2),
            (box.right() + 2, box.top()),
            (box.left() - width - 2, box.top()),
            (box.left() + 3, box.top() + 3),
        ):
            badge = QRectF(left, top, width, height)
            bounds = badge.toAlignedRect()
            if area.contains(badge) and not occupied.intersects(bounds):
                occupied |= bounds
                painter.fillRect(badge, QColor(Style.background))
                painter.setPen(QColor(Style.warning if filtered else Style.primary))
                painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, caption)
                break
    painter.restore()
