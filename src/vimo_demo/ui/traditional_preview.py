"""Frozen-image advisory tuning. One bounded background measurement job, no runtime."""

from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, replace
from threading import Thread
from typing import Literal

import numpy as np
from PySide6.QtCore import QRectF, QSizeF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QTabBar,
    QVBoxLayout,
    QWidget,
)

from vimo_demo.inference import BoundingRect
from vimo_demo.inspection.production import (
    CameraLayout,
    ChannelROI,
    CheckResult,
    ProductJudgment,
)
from vimo_demo.inspection.production.capsule import (
    CapsuleMeasurement,
    judge_capsule,
    measure_capsule,
    median_hue,
)

from .display_frames import DisplayFrame, LatestDisplayFrameStore
from .production_style import Language, Style
from .recipe_editor import RecipeEditorSession
from .roi_canvas import RoiCanvas

type PreviewMode = Literal["size", "color"]


@dataclass(frozen=True, slots=True)
class PreviewMeasurement:
    slot: ChannelROI
    measured: CapsuleMeasurement | None
    error: str = ""


def measure_reference(
    frame: DisplayFrame, camera: CameraLayout
) -> tuple[PreviewMeasurement, ...]:
    """Same production algorithm; retain scalars/boxes, never new display pixels."""
    rows: list[PreviewMeasurement] = []
    for slot in camera.slots:
        try:
            rows.append(
                PreviewMeasurement(slot, measure_capsule(frame.image, slot.rect))
            )
        except Exception as exc:
            rows.append(PreviewMeasurement(slot, None, f"{type(exc).__name__}: {exc}"))
    return tuple(rows)


def measurement_label_rect(
    body: QRectF, available: QRectF, size: QSizeF
) -> QRectF | None:
    """Place a readable label above its body, or inside when space is limited.

    Never bleed into another channel/view or pin labels for offscreen bodies.
    At extreme zoom-out insufficient space requires zooming in, not tiny text.
    """
    if (
        not body.intersects(available)
        or size.width() > available.width()
        or size.height() > available.height()
    ):
        return None
    x = min(max(body.left(), available.left()), available.right() - size.width())
    y = body.top() - size.height() - 3
    if y < available.top():
        y = body.top() + 3
    y = min(max(y, available.top()), available.bottom() - size.height())
    return QRectF(x, y, size.width(), size.height())


class PreviewCanvas(RoiCanvas):
    def __init__(self) -> None:
        super().__init__(lambda: False)
        self.rows: tuple[PreviewMeasurement, ...] = ()
        self.results: tuple[CheckResult | None, ...] = ()
        self.slots: tuple[ChannelROI, ...] = ()
        self.language: Language = "zh"
        self.mode: PreviewMode = "size"

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        transform = self.model.transform(self.width(), self.height())
        if transform is None:
            return
        painter = QPainter(self)
        painter.setClipRect(
            QRectF(transform.x, transform.y, transform.width, transform.height)
        )
        label_font = QFont(self.font())
        label_font.setPixelSize(12)
        metrics = QFontMetricsF(label_font)
        visible = QRectF(self.rect()).intersected(
            QRectF(transform.x, transform.y, transform.width, transform.height)
        )
        for index, slot in enumerate(self.slots):
            result = self.results[index] if index < len(self.results) else None
            judgment = result.assessment.judgment if result else None
            label = (
                judgment.name
                if judgment
                else ("预览不可用" if self.language == "zh" else "Unavailable")
            )
            color = (
                "#41c896"
                if judgment is ProductJudgment.OK
                else "#fa6478"
                if judgment is ProductJudgment.NG
                else "#e2b45d"
            )
            painter.setPen(QPen(QColor(color), 2))
            outline = QRectF(*transform.rectangle(slot.rect))
            painter.drawRect(outline)
            painter.drawText(
                outline.adjusted(5, 5, -2, -2),
                Qt.AlignmentFlag.AlignTop,
                f"CH{slot.channel_id:02d} · {label}",
            )
            if index >= len(self.rows) or self.rows[index].measured is None:
                continue
            measured = self.rows[index].measured
            assert measured is not None
            for view_index, view in enumerate(measured.views):
                if view.body is None:
                    continue
                box = view.body.bounding_rect
                body = QRectF(*transform.rectangle(box))
                painter.setPen(QPen(QColor(color), 1.5))
                painter.drawRect(body)
                if self.mode == "size":
                    caption = f"W {box.width}\nH {box.height}"
                    size = QSizeF(
                        max(
                            metrics.horizontalAdvance(line)
                            for line in caption.splitlines()
                        )
                        + 8,
                        2 * metrics.lineSpacing() + 6,
                    )
                    split = slot.rect.height // 2
                    view_roi = BoundingRect(
                        slot.rect.x,
                        slot.rect.y + (split if view_index else 0),
                        slot.rect.width,
                        slot.rect.height - split if view_index else split,
                    )
                    bounds = QRectF(*transform.rectangle(view_roi)).intersected(visible)
                    bounds = bounds.adjusted(3, 22 if view_index == 0 else 3, -3, -3)
                    label_rect = measurement_label_rect(body, bounds, size)
                    if label_rect is not None:
                        painter.save()
                        painter.setFont(label_font)
                        painter.setPen(Qt.PenStyle.NoPen)
                        painter.setBrush(QColor(20, 24, 30, 225))
                        painter.drawRoundedRect(label_rect, 3, 3)
                        painter.setPen(QColor(Style.primary))
                        painter.drawText(
                            label_rect.adjusted(4, 3, -4, -3),
                            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                            caption,
                        )
                        painter.restore()
                elif view.body.hue is None:
                    painter.drawText(
                        body.adjusted(3, 2, -2, -2), Qt.AlignmentFlag.AlignTop, "—"
                    )


class TraditionalPreview(QWidget):
    """Single GUI owner; cache corresponds only to the currently frozen reference.

    At most one job plus one latest desired reference. Stale completions are
    discarded. Threshold edits only judge cached scalars. Close observes actual
    job termination before releasing the workspace.
    """

    def __init__(
        self,
        session: RecipeEditorSession,
        frames: LatestDisplayFrameStore,
        allowed: Callable[[], bool],
        changed: Callable[[], None],
        leave: Callable[[], None],
        *,
        mode: PreviewMode,
        language: Language,
        parent: QWidget,
    ) -> None:
        super().__init__(parent)
        self.session, self.frames = session, frames
        self.allowed, self.changed, self.leave = allowed, changed, leave
        self.language = language
        self.setStyleSheet(
            f"QTabBar::tab {{ padding: 12px 10px; min-height: 20px; color: {Style.primary}; background: {Style.surface}; border: none; }}"
            f"QTabBar::tab:selected {{ color: {Style.accent}; background: {Style.raised}; }}"
            f"QSpinBox {{ color: {Style.primary}; background: {Style.raised}; border: 1px solid #424954; padding: 5px; }}"
            "QSlider::groove:horizontal { height: 6px; background: #424954; border-radius: 3px; }"
            f"QSlider::handle:horizontal {{ width: 22px; margin: -8px 0; border-radius: 11px; background: {Style.accent}; }}"
        )
        self.frame: DisplayFrame | None = None
        self.measurements: tuple[PreviewMeasurement, ...] = ()
        self.results: tuple[CheckResult | None, ...] = ()
        self._revision = 0
        self._thread: Thread | None = None
        self._future: Future[tuple[PreviewMeasurement, ...]] | None = None
        self._job_revision = 0
        self._close: Callable[[], None] | None = None
        self._loading = False
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        header = QHBoxLayout()
        self.back = QPushButton()
        self.camera_tabs = QTabBar()
        self.camera_tabs.setExpanding(False)
        self.camera_tabs.setDrawBase(False)
        for camera in session.machine.layout.cameras:
            self.camera_tabs.addTab(str(camera.camera_id))
        self.refresh_button = QPushButton()
        self.fit_button = QPushButton("Fit")
        for button in (self.back, self.refresh_button, self.fit_button):
            button.setMinimumHeight(Style.touch)
        header.addWidget(self.back)
        header.addWidget(self.camera_tabs, 1)
        header.addWidget(self.fit_button)
        header.addWidget(self.refresh_button)
        root.addLayout(header)
        self.canvas = PreviewCanvas()
        root.addWidget(self.canvas, 1)
        self.mode_tabs = QTabBar()
        self.mode_tabs.setDrawBase(False)
        self.mode_tabs.addTab("")
        self.mode_tabs.addTab("")
        self.mode_tabs.setCurrentIndex(0 if mode == "size" else 1)
        root.addWidget(self.mode_tabs)
        self.enabled = QCheckBox()
        root.addWidget(self.enabled)
        self.controls = QStackedWidget()
        size_page, color_page = QWidget(), QWidget()
        size_row, color_row = QHBoxLayout(size_page), QHBoxLayout(color_page)
        self.spins: dict[str, QSpinBox] = {}
        self.labels: dict[str, QLabel] = {}
        for key in (
            "target_width",
            "width_tolerance",
            "target_height",
            "height_tolerance",
        ):
            column = QVBoxLayout()
            label, spin = QLabel(), QSpinBox()
            spin.setRange(1 if key.startswith("target") else 0, 1000000)
            spin.setMinimumHeight(Style.touch)
            spin.valueChanged.connect(self.update_settings)
            self.spins[key], self.labels[key] = spin, label
            column.addWidget(label)
            column.addWidget(spin)
            size_row.addLayout(column)
        self.swatch = QLabel()
        self.swatch.setFixedSize(Style.touch, Style.touch)
        self.sample = QPushButton()
        self.sample.setMinimumHeight(Style.touch)
        self.tolerance = QSlider(Qt.Orientation.Horizontal)
        self.tolerance.setRange(0, 90)
        self.tolerance.setMinimumHeight(Style.touch)
        self.tolerance_label = QLabel()
        color_row.addWidget(self.swatch)
        color_row.addWidget(self.sample)
        color_row.addWidget(self.tolerance_label)
        color_row.addWidget(self.tolerance, 1)
        self.controls.addWidget(size_page)
        self.controls.addWidget(color_page)
        root.addWidget(self.controls)
        self.message = QLabel()
        self.message.setWordWrap(True)
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        root.addWidget(self.message)
        self.back.clicked.connect(lambda: self.request_close(self.leave))
        self.refresh_button.clicked.connect(self.refresh_reference)
        self.fit_button.clicked.connect(lambda: self.canvas.zoom(None))
        self.camera_tabs.currentChanged.connect(self.refresh_reference)
        self.mode_tabs.currentChanged.connect(self.show_settings)
        self.enabled.toggled.connect(self.update_settings)
        self.tolerance.valueChanged.connect(self.update_settings)
        self.sample.clicked.connect(self.sample_color)
        self._timer = QTimer(self)
        self._timer.setInterval(15)
        self._timer.timeout.connect(self._poll)
        self.set_language(language)
        self.refresh_reference()

    @property
    def mode(self) -> PreviewMode:
        return "size" if self.mode_tabs.currentIndex() == 0 else "color"

    @property
    def busy(self) -> bool:
        return self._thread is not None

    def tr_pair(self, zh: str, en: str) -> str:
        return zh if self.language == "zh" else en

    def set_language(self, language: Language) -> None:
        self.language = self.canvas.language = language
        self.back.setText(self.tr_pair("返回配方", "Back to Recipe"))
        self.refresh_button.setText(self.tr_pair("刷新参考图", "Refresh reference"))
        self.mode_tabs.setTabText(0, self.tr_pair("尺寸检测", "Size inspection"))
        self.mode_tabs.setTabText(1, self.tr_pair("颜色检测", "Color inspection"))
        for key, zh, en in (
            ("target_width", "目标宽度 (px)", "Target width (px)"),
            ("width_tolerance", "宽度容差 (px)", "Width tolerance (px)"),
            ("target_height", "目标高度 (px)", "Target height (px)"),
            ("height_tolerance", "高度容差 (px)", "Height tolerance (px)"),
        ):
            self.labels[key].setText(self.tr_pair(zh, en))
            self.spins[key].setAccessibleName(self.labels[key].text())
        self.sample.setText(self.tr_pair("从当前画面取色", "Use current image color"))
        self.show_settings()

    def show_settings(self) -> None:
        self._loading = True
        settings = self.session.draft.capsule
        self.controls.setCurrentIndex(self.mode_tabs.currentIndex())
        self.canvas.mode = self.mode
        self.enabled.setText(self.tr_pair("启用此项检测", "Enable this inspection"))
        self.enabled.setChecked(
            settings.size.enabled if self.mode == "size" else settings.color.enabled
        )
        for key, spin in self.spins.items():
            value = {
                "target_width": settings.size.target_width,
                "width_tolerance": settings.size.width_tolerance,
                "target_height": settings.size.target_height,
                "height_tolerance": settings.size.height_tolerance,
            }[key]
            spin.setValue(value)
        self.tolerance.setValue(settings.color.hue_tolerance)
        self.swatch.setStyleSheet(
            f"background: {QColor.fromHsv(settings.color.target_hue * 2, 220, 220).name()}; border: 1px solid #8090a0;"
        )
        self.swatch.setToolTip(self.tr_pair("目标颜色", "Target color"))
        self.tolerance_label.setText(
            self.tr_pair("颜色容差", "Color tolerance")
            + f" {settings.color.hue_tolerance}"
        )
        self._loading = False
        self.refresh_state()
        self.evaluate()

    def refresh_state(self) -> None:
        allowed = self.allowed() and self._close is None
        self.controls.setEnabled(allowed)
        self.enabled.setEnabled(allowed)
        self.sample.setEnabled(allowed and not self.busy and bool(self.measurements))

    def update_settings(self) -> None:
        if self._loading or not self.allowed() or self._close is not None:
            return
        settings = self.session.draft.capsule
        if self.mode == "size":
            settings = replace(
                settings,
                size=replace(
                    settings.size,
                    enabled=self.enabled.isChecked(),
                    target_width=self.spins["target_width"].value(),
                    width_tolerance=self.spins["width_tolerance"].value(),
                    target_height=self.spins["target_height"].value(),
                    height_tolerance=self.spins["height_tolerance"].value(),
                ),
            )
        else:
            settings = replace(
                settings,
                color=replace(
                    settings.color,
                    enabled=self.enabled.isChecked(),
                    hue_tolerance=self.tolerance.value(),
                ),
            )
        self.session.draft.capsule = settings
        if not self.measurements and not self.busy:
            self._start_measurement()
        self.changed()
        self.show_settings()

    def sample_color(self) -> None:
        if not self.allowed() or self.busy or self._close is not None:
            return
        # Only complete, valid two-view channels contribute; no arbitrary pixel picking.
        hues = [
            v.body.hue
            for row in self.measurements
            if row.measured
            and all(
                v.body is not None and v.body.hue is not None
                for v in row.measured.views
            )
            for v in row.measured.views
            if v.body is not None and v.body.hue is not None
        ]
        target = median_hue(np.array(hues, dtype=np.float64))
        if target is None:
            self.show_message(
                self.tr_pair(
                    "没有可用的双视图颜色，未更改目标颜色。",
                    "No usable two-view color evidence; target unchanged.",
                )
            )
            return
        settings = self.session.draft.capsule
        self.session.draft.capsule = replace(
            settings, color=replace(settings.color, target_hue=round(target) % 180)
        )
        self.changed()
        self.show_settings()

    def refresh_reference(self) -> None:
        if self._close is not None:
            return
        self._revision += 1
        self.measurements = ()
        self.results = ()
        camera = self.session.machine.layout.cameras[self.camera_tabs.currentIndex()]
        self.frame = next(
            (
                f
                for f in self.frames.snapshot().frames
                if f.frame_id.camera_id == camera.camera_id
            ),
            None,
        )
        self.canvas.set_frame(self.frame)
        self.canvas.slots = camera.slots
        self.canvas.rows = ()
        self.canvas.results = ()
        self.evaluate()
        if not self.busy:
            self._start_measurement()

    def _start_measurement(self) -> None:
        frame = self.frame
        if (
            frame is None
            or self._close is not None
            or not self.session.draft.capsule.enabled
        ):
            return
        camera = self.session.machine.layout.cameras[self.camera_tabs.currentIndex()]
        future: Future[tuple[PreviewMeasurement, ...]] = Future()
        self._future = future
        self._job_revision = self._revision

        def run() -> None:
            try:
                future.set_result(measure_reference(frame, camera))
            except BaseException as exc:
                future.set_exception(exc)

        self._thread = Thread(target=run, name="CapsulePreview", daemon=False)
        self._thread.start()
        self._timer.start()
        self.refresh_state()
        self.evaluate()

    def _poll(self) -> None:
        if self._thread is None or self._thread.is_alive():
            return
        self._thread.join(0)
        self._thread = None
        self._timer.stop()
        future, self._future = self._future, None
        if self._close is not None:
            callback = self._close
            self.frame = None
            callback()
            return
        if self._job_revision != self._revision:
            self._start_measurement()
            self.refresh_state()
            return
        assert future is not None and future.done()
        try:
            self.measurements = future.result()
        except BaseException as exc:
            self.show_message(f"{type(exc).__name__}: {exc}")
            self.refresh_state()
            return
        self.evaluate()
        self.refresh_state()

    def evaluate(self) -> None:
        self.results = tuple(
            judge_capsule(row.measured, self.session.draft.capsule)
            if row.measured
            else None
            for row in self.measurements
        )
        self.canvas.rows, self.canvas.results = self.measurements, self.results
        self.canvas.update()
        if self.frame is None:
            self.show_message(
                self.tr_pair("参考图不可用", "Reference image unavailable")
            )
        elif not self.session.draft.capsule.enabled:
            self.show_message(
                self.tr_pair("两项检测均未启用", "Both inspections are disabled")
            )
        elif self.busy:
            self.show_message(
                self.tr_pair("正在测量冻结参考图…", "Measuring frozen reference…")
            )
        else:
            errors = [
                f"CH{row.slot.channel_id:02d}: {row.error}"
                for row in self.measurements
                if row.error
            ]
            self.show_message("\n".join(errors))
            self.canvas.setToolTip(
                "\n".join(
                    f"CH{row.slot.channel_id:02d}: "
                    + (
                        ", ".join(r.code for r in result.reasons)
                        if result
                        else row.error
                    )
                    for row, result in zip(self.measurements, self.results, strict=True)
                    if row.error or (result is not None and result.reasons)
                )
            )

    def show_message(self, message: str) -> None:
        """Only transient work/unavailable/error feedback reserves a row."""
        self.message.setText(message)
        self.message.setVisible(bool(message))

    def request_close(self, after: Callable[[], None]) -> None:
        self._close = after
        self.setEnabled(False)
        if not self.busy:
            self.frame = None
            after()
