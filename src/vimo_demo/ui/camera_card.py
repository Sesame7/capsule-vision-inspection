"""Six-channel card with owned QImage and image-space hollow evidence boxes."""

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from vimo_demo.application import ProductionApplicationSnapshot
from vimo_demo.inference import BoundingRect
from vimo_demo.inspection.production import (
    CameraInspectionResult,
    CameraLayout,
    ProductJudgment,
    TechnicalStatus,
)

from .display_frames import DisplayFrame
from .production_presentation import (
    Indicator,
    image_transform,
    indicator,
    matching_camera,
)
from .production_style import Language, Style, indicator_icon, text


class CameraViewport(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(120, 120)
        self.image = QImage()
        self.revision = -1
        self.conversions = 0
        self.boxes: tuple[BoundingRect, ...] = ()
        self.message = ""

    def set_frame(self, frame: DisplayFrame | None) -> None:
        revision = frame.revision if frame else -1
        if revision == self.revision:
            return
        self.revision = revision
        if frame is None:
            self.image = QImage()
        else:
            height, width, _ = frame.image.shape
            # Format_BGR888 avoids a color conversion; copy owns Qt's pixel buffer.
            self.image = QImage(
                frame.image.data,
                width,
                height,
                frame.image.strides[0],
                QImage.Format.Format_BGR888,
            ).copy()
            self.conversions += 1
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#0d0f12"))
        if not self.image.isNull() and min(self.width(), self.height()) > 0:
            transform = image_transform(
                self.image.width(), self.image.height(), self.width(), self.height()
            )
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawImage(
                QRectF(transform.x, transform.y, transform.width, transform.height),
                self.image,
            )
            painter.setClipRect(
                QRectF(transform.x, transform.y, transform.width, transform.height)
            )
            painter.setPen(QPen(QColor(Style.ng), 2))
            for box in self.boxes:
                painter.drawRect(QRectF(*transform.rectangle(box)))
            painter.setClipping(False)
        if self.message:
            painter.setPen(QColor(Style.secondary))
            painter.drawText(
                self.rect().adjusted(
                    Style.space, Style.small, -Style.space, -Style.small
                ),
                Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignHCenter,
                self.message,
            )
        painter.end()


class CameraCard(QFrame):
    def __init__(self, layout: CameraLayout, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.camera_layout = layout
        self.setObjectName("camera")
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self._frame: DisplayFrame | None = None
        self._snapshot: ProductionApplicationSnapshot | None = None
        self.result: CameraInspectionResult | None = None
        self.language: Language = "zh"
        self._expanded: set[int] = set()
        root = QVBoxLayout(self)
        root.setContentsMargins(Style.small, Style.small, Style.small, Style.small)
        root.setSpacing(Style.small)
        header = QHBoxLayout()
        header.setSpacing(2)
        self.identity = QLabel(str(layout.camera_id))
        header.addWidget(self.identity)
        self.indicators: list[QPushButton] = []
        for index, slot in enumerate(layout.slots):
            button = QPushButton()
            button.setFixedSize(Style.touch, Style.touch)
            button.clicked.connect(lambda _checked=False, i=index: self._toggle(i))
            button.setAccessibleName(f"{layout.camera_id} CH{slot.channel_id}")
            header.addWidget(button)
            self.indicators.append(button)
        header.addStretch()
        self.timing = QLabel("--")
        self.timing.setProperty("role", "secondary")
        header.addWidget(self.timing)
        root.addLayout(header)
        self.viewport = CameraViewport()
        root.addWidget(self.viewport, 1)

    def _toggle(self, index: int) -> None:
        if index in self._expanded:
            self._expanded.remove(index)
        else:
            self._expanded.add(index)
        self._render_card()

    def present(
        self,
        frame: DisplayFrame | None,
        snapshot: ProductionApplicationSnapshot,
        language: Language,
    ) -> None:
        self._frame, self._snapshot, self.language = frame, snapshot, language
        self.viewport.set_frame(frame)
        self.result = matching_camera(frame, snapshot)
        self._render_card()

    def _render_card(self) -> None:
        colors = {
            Indicator.PENDING: Style.secondary,
            Indicator.DISABLED: "#727985",
            Indicator.OK: Style.ok,
            Indicator.NG: Style.ng,
            Indicator.ERROR: Style.warning,
            Indicator.ERROR_NG: Style.ng,
            Indicator.UNKNOWN: Style.warning,
        }
        # A retained result belongs to its own layout, not a newly selected Recipe.
        layout = self.result.layout if self.result else self.camera_layout
        for index, (slot, button) in enumerate(
            zip(layout.slots, self.indicators, strict=True)
        ):
            assessment = self.result.channels[index].assessment if self.result else None
            state = indicator(assessment)
            show_id = (
                state
                in (
                    Indicator.NG,
                    Indicator.ERROR,
                    Indicator.ERROR_NG,
                    Indicator.UNKNOWN,
                )
                or index in self._expanded
            )
            button.setIcon(indicator_icon(state))
            button.setIconSize(QSize(18, 18))
            button.setText(
                f"{slot.channel_id}{'!' if state is Indicator.ERROR_NG else ''}"
                if show_id
                else ""
            )
            description = f"{text('channel', self.language)} {slot.channel_id} · {text(state.value, self.language)}"
            button.setToolTip(description)
            button.setAccessibleName(description)
            border = Style.warning if state is Indicator.ERROR_NG else "transparent"
            button.setStyleSheet(
                f"color: {colors[state]}; border: 1px solid {border}; padding: 0px; background: {Style.surface}; font-size: 13px;"
            )
        result = self.result
        technical = (
            result is not None
            and result.assessment.technical_status is TechnicalStatus.ERROR
        )
        self.identity.setText(
            f"{self.camera_layout.camera_id}{' !' if technical else ''}"
        )
        self.identity.setStyleSheet(
            f"color: {Style.warning if technical else Style.primary}; font-weight: 600;"
        )
        self.setStyleSheet(
            f"QFrame#camera {{ border-color: {Style.warning if technical else Style.border}; }}"
        )
        self.timing.setText(
            f"{result.timing.total_ms:.1f} ms"
            if result
            and (
                result.timing.vimo_ms is not None
                or result.timing.traditional_ms is not None
            )
            else "--"
        )
        boxes: list[BoundingRect] = []
        if result:
            boxes.extend(
                item.finding.bounding_rect for item in result.assigned_findings
            )
            boxes.extend(item.bounding_rect for item in result.unassigned_findings)
            for row in result.channels:
                for check in row.checks:
                    if check.assessment.judgment is ProductJudgment.NG:
                        boxes.extend(
                            reason.bounding_rect
                            for reason in check.reasons
                            if reason.bounding_rect is not None
                        )
        self.viewport.boxes = tuple(dict.fromkeys(boxes))
        message = ""
        if self._frame is None:
            message = text("no_image", self.language)
        elif self._snapshot and self._frame.generation != self._snapshot.generation:
            message = text("reference", self.language)
        elif result is None:
            message = text("pending", self.language)
        if technical:
            message = text("error", self.language)
        self.viewport.message = message
        self.viewport.update()
