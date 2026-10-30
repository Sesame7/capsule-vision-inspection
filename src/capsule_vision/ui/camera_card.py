"""Six-channel card with owned QImage and image-space hollow evidence boxes."""

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from capsule_vision.application import ApplicationState, ProductionApplicationSnapshot
from capsule_vision.display import DisplayFrame, matching_camera
from capsule_vision.inference import BoundingRect
from capsule_vision.inspection.production import (
    CameraInspectionResult,
    CameraLayout,
    ProductJudgment,
    TechnicalStatus,
)
from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import Language

from .production_presentation import (
    Indicator,
    channel_indicator,
    image_transform,
)
from .style import (
    Style,
    ThemeColors,
    camera_icon,
    indicator_icon,
    theme_colors,
)


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
        self._indicator_styles: list[tuple[Indicator, bool, ThemeColors] | None] = [
            None
        ] * len(layout.slots)
        self._icon_color = ""
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 6, 8, 8)
        root.setSpacing(Style.small)
        header = QGridLayout()
        header.setHorizontalSpacing(Style.small)
        header.setColumnStretch(0, 1)
        header.setColumnStretch(2, 1)
        self.identity_group = QWidget()
        identity_row = QHBoxLayout(self.identity_group)
        identity_row.setContentsMargins(0, 0, 0, 0)
        identity_row.setSpacing(Style.small)
        self.camera_icon = QLabel()
        self.camera_icon.setFixedSize(Style.icon, Style.icon)
        self.camera_icon.setPixmap(camera_icon().pixmap(Style.icon, Style.icon))
        identity_row.addWidget(self.camera_icon)
        self.identity = QLabel(str(layout.camera_id).removeprefix("CAM"))
        identity_row.addWidget(self.identity)
        header.addWidget(self.identity_group, 0, 0, Qt.AlignmentFlag.AlignLeft)
        self.indicator_group = QWidget()
        slots = QHBoxLayout(self.indicator_group)
        slots.setContentsMargins(0, 0, 0, 0)
        slots.setSpacing(2)
        header.addWidget(self.indicator_group, 0, 1, Qt.AlignmentFlag.AlignCenter)
        self.indicators: list[QLabel] = []
        for slot in layout.slots:
            button = QLabel()
            button.setAlignment(Qt.AlignmentFlag.AlignCenter)
            button.setFixedSize(Style.touch, Style.touch)
            button.setAccessibleName(f"{layout.camera_id} CH{slot.channel_id}")
            slots.addWidget(button)
            self.indicators.append(button)
        self.timing = QLabel("--")
        self.timing.setProperty("role", "secondary")
        self.timing.setStyleSheet("font-size: 13px;")
        header.addWidget(self.timing, 0, 2, Qt.AlignmentFlag.AlignRight)
        root.addLayout(header)
        self.viewport = CameraViewport()
        root.addWidget(self.viewport, 1)

    def present(
        self,
        frame: DisplayFrame | None,
        snapshot: ProductionApplicationSnapshot,
        language: Language,
    ) -> None:
        # Generation zero is launcher reference/warm-up data, never production evidence.
        frame = frame if frame is not None and frame.generation > 0 else None
        self._frame, self._snapshot, self.language = frame, snapshot, language
        # Retain Stop pixels, but hide a previous generation during new runtime work.
        stale = (
            frame is not None
            and frame.generation != snapshot.generation
            and snapshot.state
            in (
                ApplicationState.STARTING,
                ApplicationState.RUNNING,
                ApplicationState.STOPPING,
            )
        )
        self.viewport.set_frame(None if stale else frame)
        self.result = matching_camera(frame, snapshot)
        self._render_card()

    def _render_card(self) -> None:
        palette = theme_colors(self)
        if self._icon_color != palette.primary:
            self._icon_color = palette.primary
            self.camera_icon.setPixmap(
                camera_icon(palette.primary).pixmap(Style.icon, Style.icon)
            )
        colors = {
            Indicator.PENDING: palette.secondary,
            Indicator.DISABLED: "#727985",
            Indicator.OK: palette.ok,
            Indicator.NG: palette.ng,
            Indicator.ERROR: palette.warning,
            Indicator.ERROR_NG: palette.ng,
            Indicator.UNKNOWN: palette.warning,
            Indicator.EMPTY: palette.secondary,
        }
        # A retained result belongs to its own layout, not a newly selected Recipe.
        layout = self.result.layout if self.result else self.camera_layout
        for index, (slot, button) in enumerate(
            zip(layout.slots, self.indicators, strict=True)
        ):
            channel = self.result.channels[index] if self.result else None
            state = channel_indicator(channel)
            error = (
                channel is not None
                and channel.assessment.technical_status is TechnicalStatus.ERROR
            )
            presentation = state, error, palette
            if presentation != self._indicator_styles[index]:
                self._indicator_styles[index] = presentation
                button.setPixmap(indicator_icon(state, colors[state]).pixmap(24, 24))
                button.setStyleSheet(
                    f"border: 1px solid {palette.warning if error else 'transparent'}; border-radius: 8px;"
                )
            description = f"{text('channel', self.language)} {slot.channel_id} · {text(state.value, self.language)}"
            button.setToolTip(f"{layout.camera_id} CH{slot.channel_id}")
            button.setAccessibleName(description)
        result = self.result
        technical = (
            result is not None
            and result.assessment.technical_status is TechnicalStatus.ERROR
        )
        number = str(self.camera_layout.camera_id).removeprefix("CAM")
        self.identity.setText(f"{number}{' !' if technical else ''}")
        name = (
            f"{text('camera_number', self.language).format(number=number)} "
            f"({self.camera_layout.camera_id})"
        )
        for widget in (self.identity_group, self.identity, self.camera_icon):
            widget.setToolTip(name)
        self.identity.setAccessibleName(name)
        self.identity_group.setAccessibleName(name)
        identity_style = f"color: {palette.warning if technical else palette.primary}; font-weight: 600;"
        if self.identity.styleSheet() != identity_style:
            self.identity.setStyleSheet(identity_style)
        card_style = f"QFrame#camera {{ border-color: {palette.warning if technical else palette.border}; }}"
        if self.styleSheet() != card_style:
            self.setStyleSheet(card_style)
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
        self.viewport.message = (
            text("pending", self.language)
            if self._frame is not None
            and self._snapshot is not None
            and self._snapshot.state is ApplicationState.RUNNING
            and self._frame.generation == self._snapshot.generation
            and result is None
            else ""
        )
        self.viewport.setAccessibleName(
            f"{name} · {text('no_display_frame', self.language)}"
            if self.viewport.image.isNull()
            else name
        )
        self.viewport.update()
