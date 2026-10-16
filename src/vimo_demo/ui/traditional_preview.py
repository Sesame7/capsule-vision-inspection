"""Frozen-image advisory tuning. One bounded background measurement job, no runtime."""

from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, replace
from threading import Thread
from typing import Literal

import numpy as np
from PySide6.QtCore import QRectF, QSize, QSizeF, Qt, QTimer
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QPainter,
    QPaintEvent,
    QPen,
    QResizeEvent,
)
from PySide6.QtWidgets import (
    QBoxLayout,
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
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
    CapsuleSettings,
    judge_capsule,
    measure_capsule,
    median_hue,
)

from .display_frames import DisplayFrame, LatestDisplayFrameStore
from .production_style import Language, Style, camera_icon, text, theme_colors
from .recipe_editor import RecipeEditorSession
from .roi_canvas import RoiCanvas

type PreviewMode = Literal["size", "color"]


@dataclass(frozen=True, slots=True)
class PreviewMeasurement:
    slot: ChannelROI
    measured: CapsuleMeasurement | None
    error: str = ""


def measure_reference(
    frame: DisplayFrame, camera: CameraLayout, settings: CapsuleSettings
) -> tuple[PreviewMeasurement, ...]:
    """Same production algorithm; retain scalars/boxes, never new display pixels."""
    rows: list[PreviewMeasurement] = []
    for slot in camera.slots:
        try:
            rows.append(
                PreviewMeasurement(
                    slot, measure_capsule(frame.image, slot.rect, settings)
                )
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
        self.frame: DisplayFrame | None = None
        self.measurements: tuple[PreviewMeasurement, ...] = ()
        self.results: tuple[CheckResult | None, ...] = ()
        self._measurement_flags: tuple[bool, bool] | None = None
        self._revision = 0
        self._thread: Thread | None = None
        self._future: Future[tuple[PreviewMeasurement, ...]] | None = None
        self._job_revision = 0
        self._close: Callable[[], None] | None = None
        self._loading = False
        root = QVBoxLayout(self)
        root.setContentsMargins(Style.space, Style.small, Style.space, Style.small)
        root.setSpacing(Style.small)
        header = QHBoxLayout()
        header.setSpacing(Style.small)
        self.back = QPushButton()
        self.camera_tabs = QTabBar()
        self.camera_tabs.setExpanding(False)
        self.camera_tabs.setDrawBase(False)
        self.camera_tabs.setIconSize(QSize(Style.icon, Style.icon))
        for camera in session.machine.layout.cameras:
            self.camera_tabs.addTab(
                camera_icon(), str(camera.camera_id).removeprefix("CAM")
            )
        self.refresh_button = QPushButton()
        self.fit_button = QPushButton("Fit")
        for button in (self.back, self.refresh_button, self.fit_button):
            button.setMinimumSize(Style.touch, Style.touch)
        header.addWidget(self.back)
        header.addWidget(self.camera_tabs)
        header.addStretch()
        header.addWidget(self.fit_button)
        header.addWidget(self.refresh_button)
        root.addLayout(header)
        self.canvas = PreviewCanvas()
        root.addWidget(self.canvas, 1)
        self.mode_tabs = QTabBar()
        self.mode_tabs.setDrawBase(False)
        self.mode_tabs.setExpanding(False)
        self.mode_tabs.setUsesScrollButtons(False)
        self.mode_tabs.addTab("")
        self.mode_tabs.addTab("")
        self.mode_tabs.setCurrentIndex(0 if mode == "size" else 1)
        self._mode_group = QWidget()
        mode_column = QVBoxLayout(self._mode_group)
        mode_column.setContentsMargins(0, 0, 0, 0)
        mode_column.setSpacing(Style.small)
        mode_column.addWidget(self.mode_tabs)
        self.enabled = QCheckBox()
        self.enabled.setMinimumHeight(Style.touch)
        mode_column.addWidget(self.enabled)
        self.controls = QStackedWidget()
        size_page, color_page = QWidget(), QWidget()
        size_row, color_row = QHBoxLayout(size_page), QHBoxLayout(color_page)
        for row in (size_row, color_row):
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(Style.space)
        self.spins: dict[str, QSpinBox] = {}
        self.labels: dict[str, QLabel] = {}
        for key in (
            "target_width",
            "width_tolerance",
            "target_height",
            "height_tolerance",
        ):
            column = QVBoxLayout()
            column.setSpacing(Style.small)
            label, spin = QLabel(), QSpinBox()
            spin.setRange(1 if key.startswith("target") else 0, 1000000)
            spin.setMinimumHeight(Style.touch)
            spin.setFixedWidth(
                spin.fontMetrics().horizontalAdvance(str(spin.maximum()))
                + 2 * Style.space
                + Style.icon
            )
            spin.valueChanged.connect(self.update_settings)
            self.spins[key], self.labels[key] = spin, label
            column.addWidget(label)
            column.addWidget(spin)
            size_row.addLayout(column)
        size_row.addStretch()
        self.swatch = QLabel()
        self.swatch.setFixedSize(Style.touch, Style.touch)
        self.sample = QPushButton()
        self.sample.setMinimumHeight(Style.touch)
        self.tolerance = QSlider(Qt.Orientation.Horizontal)
        self.tolerance.setRange(0, 90)
        self.tolerance.setMinimumHeight(Style.touch)
        self.tolerance.setMinimumWidth(5 * Style.touch)
        self.tolerance_label = QLabel()
        color_row.addWidget(self.swatch)
        color_row.addWidget(self.sample)
        color_row.addWidget(self.tolerance_label)
        color_row.addWidget(self.tolerance)
        color_row.addStretch()
        self.controls.addWidget(size_page)
        self.controls.addWidget(color_page)
        self.controls.setSizePolicy(
            QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred
        )
        self._controls_row = QHBoxLayout()
        self._controls_row.setSpacing(Style.space)
        self._controls_row.addWidget(self._mode_group)
        self._controls_row.addWidget(self.controls)
        self._controls_row.addStretch()
        root.addLayout(self._controls_row)
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

    def refresh_theme(self) -> None:
        for index in range(self.camera_tabs.count()):
            self.camera_tabs.setTabIcon(index, camera_icon(theme_colors(self).primary))

    def set_language(self, language: Language) -> None:
        self.language = self.canvas.language = language
        self.refresh_theme()
        self.back.setText(self.tr_pair("返回配方", "Back to Recipe"))
        self.refresh_button.setText(self.tr_pair("刷新", "Refresh"))
        self.refresh_button.setToolTip(text("roi_refresh", language))
        self.refresh_button.setAccessibleName(self.refresh_button.toolTip())
        self.fit_button.setToolTip(text("roi_fit", language))
        self.fit_button.setAccessibleName(self.fit_button.toolTip())
        self.back.setAccessibleName(self.back.text())
        self.camera_tabs.setAccessibleName(text("camera_setup", language))
        for index, camera in enumerate(self.session.machine.layout.cameras):
            name = text("camera_number", language).format(
                number=str(camera.camera_id).removeprefix("CAM")
            )
            self.camera_tabs.setTabToolTip(index, f"{name} ({camera.camera_id})")
            self.camera_tabs.setAccessibleTabName(index, f"{name} ({camera.camera_id})")
        self.mode_tabs.setTabText(0, self.tr_pair("尺寸", "Size"))
        self.mode_tabs.setTabText(1, self.tr_pair("颜色", "Color"))
        self.mode_tabs.setAccessibleName(self.tr_pair("检测项目", "Inspection mode"))
        for key, zh, en in (
            ("target_width", "目标宽度 (px)", "Target W (px)"),
            ("width_tolerance", "宽度容差", "W tolerance"),
            ("target_height", "目标高度 (px)", "Target H (px)"),
            ("height_tolerance", "高度容差", "H tolerance"),
        ):
            self.labels[key].setText(self.tr_pair(zh, en))
            caption = self.labels[key].text()
            if key.endswith("tolerance"):
                caption += " (px)"
            self.spins[key].setAccessibleName(caption)
            self.spins[key].setToolTip(caption)
        self.sample.setText(self.tr_pair("画面取色", "Sample image color"))
        self.sample.setToolTip(
            self.tr_pair("从当前画面取色", "Use current image color")
        )
        self.sample.setAccessibleName(self.sample.toolTip())
        self.show_settings()

    def show_settings(self) -> None:
        self._loading = True
        settings = self.session.draft.capsule
        self.controls.setCurrentIndex(self.mode_tabs.currentIndex())
        self.canvas.mode = self.mode
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
        self.swatch.setToolTip(
            self.tr_pair("目标颜色", "Target color")
            + f" · H {settings.color.target_hue}"
        )
        self.tolerance_label.setText(
            self.tr_pair("颜色容差", "Color tolerance")
            + f" {settings.color.hue_tolerance}"
        )
        self.enabled.setText(
            self.tr_pair("已启用", "Enabled")
            if self.enabled.isChecked()
            else self.tr_pair("未启用", "Disabled")
        )
        self.enabled.setAccessibleName(
            self.mode_tabs.tabText(self.mode_tabs.currentIndex())
            + " · "
            + self.enabled.text()
        )
        self.enabled.setToolTip(
            self.tr_pair(
                "通道 OK/NG 为所有已启用检测的组合结果",
                "Channel OK/NG combines all enabled checks",
            )
        )
        self._arrange_controls()
        self._loading = False
        self.ensure_measurement()
        self.refresh_state()
        self.evaluate()

    def _arrange_controls(self) -> None:
        required = (
            self._mode_group.minimumSizeHint().width()
            + self.controls.minimumSizeHint().width()
            + Style.space
        )
        self._controls_row.setDirection(
            QBoxLayout.Direction.LeftToRight
            if required <= self.width() - 2 * Style.space
            else QBoxLayout.Direction.TopToBottom
        )

    def resizeEvent(self, event: QResizeEvent) -> None:
        self._arrange_controls()
        super().resizeEvent(event)

    def refresh_state(self) -> None:
        allowed = self.allowed() and self._close is None
        self.controls.setEnabled(allowed)
        self.enabled.setEnabled(allowed)
        self.sample.setEnabled(
            allowed
            and not self.busy
            and self.session.draft.capsule.color.enabled
            and any(
                row.measured and row.measured.color_measured
                for row in self.measurements
            )
        )

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
        self.changed()
        self.show_settings()

    def sample_color(self) -> None:
        if not self.allowed() or self.busy or self._close is not None:
            return
        # Both view colors must be usable. Body geometry is not a sampling gate.
        if not self.session.draft.capsule.color.enabled:
            return
        hues = [
            v.color.hue
            for row in self.measurements
            if row.measured
            and all(
                v.color is not None and v.color.hue is not None
                for v in row.measured.views
            )
            for v in row.measured.views
            if v.color is not None and v.color.hue is not None
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

    def ensure_measurement(self) -> None:
        """Enabled capabilities identify cached evidence; thresholds do not."""
        settings = self.session.draft.capsule
        flags = settings.size.enabled, settings.color.enabled
        if flags == self._measurement_flags or self._close is not None:
            return
        self._measurement_flags = flags
        self._revision += 1
        self.measurements, self.results = (), ()
        self.canvas.rows, self.canvas.results = (), ()
        if not self.busy:
            self._start_measurement()

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
        settings = (
            self.session.draft.capsule
        )  # Immutable capability snapshot for this job.
        future: Future[tuple[PreviewMeasurement, ...]] = Future()
        self._future = future
        self._job_revision = self._revision

        def run() -> None:
            try:
                future.set_result(measure_reference(frame, camera, settings))
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
