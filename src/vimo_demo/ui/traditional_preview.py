"""Frozen-image advisory tuning. One bounded background measurement job, no runtime."""

from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Thread
from time import monotonic
from typing import Literal

import numpy as np
from PySide6.QtCore import QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
)
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
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

from vimo_demo.application.basler_connection import (
    ConnectionState,
    SourceConnectionCommands,
)
from vimo_demo.application.folder_replay import (
    FolderSourceError,
    decode_image,
    folder_images,
)
from vimo_demo.inference import BoundingRect
from vimo_demo.inspection import CameraId, CycleId, FrameId
from vimo_demo.inspection.production import (
    CameraLayout,
    ChannelROI,
    CheckResult,
    ProductJudgment,
    TechnicalStatus,
)
from vimo_demo.inspection.production.capsule import (
    CapsuleMeasurement,
    CapsuleSettings,
    judge_capsule,
    measure_capsule,
    median_hue,
)

from .display_frames import DisplayFrame, LatestDisplayFrameStore
from .image_tools import ImageToolbar, image_icon
from .production_style import (
    Language,
    Style,
    button_width,
    camera_icon,
    set_button_caption,
    text,
    theme_colors,
)
from .recipe_editor import RecipeEditorSession
from .roi_canvas import RoiCanvas

type PreviewMode = Literal["size", "color"]
type ReferenceKind = Literal["folder", "runtime", "import"]

_REASON_LABELS = {
    leaf: f"preview_{leaf}"
    for leaf in (
        "missing_body",
        "competing_bodies",
        "truncated_body",
        "invalid_geometry",
        "insufficient_foreground",
        "unusable",
        "width",
        "height",
        "tolerance",
    )
}


@dataclass(frozen=True, slots=True)
class _Reference:
    frame: DisplayFrame
    kind: ReferenceKind
    path: Path | None = None
    paths: tuple[Path, ...] = ()
    index: int = 0


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


class PreviewCanvas(RoiCanvas):
    view_selected = Signal(int, int)

    def __init__(self) -> None:
        super().__init__(lambda: False)
        self.rows: tuple[PreviewMeasurement, ...] = ()
        self.results: tuple[CheckResult | None, ...] = ()
        self.slots: tuple[ChannelROI, ...] = ()
        self.language: Language = "zh"
        self.check_enabled = False
        self.selection: tuple[int, int] | None = None

    def mousePressEvent(self, event: QMouseEvent) -> None:
        transform = self.model.transform(self.width(), self.height())
        if (
            event.button() == Qt.MouseButton.LeftButton
            and transform is not None
            and not self._image.isNull()
        ):
            x, y = transform.source_point(event.position().x(), event.position().y())
            assert self.model.size is not None
            if 0 <= x < self.model.size[0] and 0 <= y < self.model.size[1]:
                for index, slot in enumerate(self.slots):
                    rect = slot.rect
                    if (
                        rect.x <= x < rect.x + rect.width
                        and rect.y <= y < rect.y + rect.height
                    ):
                        self.view_selected.emit(
                            index, int(y >= rect.y + rect.height // 2)
                        )
                        event.accept()
                        return
        super().mousePressEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        transform = self.model.transform(self.width(), self.height())
        if transform is None or self._image.isNull():
            return
        painter = QPainter(self)
        painter.setClipRect(
            QRectF(transform.x, transform.y, transform.width, transform.height)
        )
        for index, slot in enumerate(self.slots):
            result = self.results[index] if index < len(self.results) else None
            error = index < len(self.rows) and bool(self.rows[index].error)
            technical = result.assessment.technical_status if result else None
            judgment = result.assessment.judgment if result else None
            label = (
                text("preview_disabled_short", self.language)
                if not self.check_enabled or technical is TechnicalStatus.DISABLED
                else text("preview_error_short", self.language)
                if error or technical is TechnicalStatus.ERROR
                else judgment.name
                if judgment
                else "—"
            )
            color = (
                theme_colors(self).secondary
                if not self.check_enabled
                or technical is TechnicalStatus.DISABLED
                or (result is None and not error)
                else "#e2b45d"
                if error or technical is TechnicalStatus.ERROR
                else "#41c896"
                if judgment is ProductJudgment.OK
                else "#fa6478"
                if judgment is ProductJudgment.NG
                else "#e2b45d"
            )
            painter.setPen(QPen(QColor(color), 2))
            outline = QRectF(*transform.rectangle(slot.rect))
            painter.drawRect(outline)
            caption = f"CH{slot.channel_id:02d} · {label}"
            if painter.fontMetrics().horizontalAdvance(caption) > outline.width() - 4:
                caption = f"CH{slot.channel_id:02d}\n{label}"
            painter.drawText(
                outline.adjusted(2, 2, -2, -2),
                Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter,
                caption,
            )
            if index >= len(self.rows) or self.rows[index].measured is None:
                continue
            measured = self.rows[index].measured
            assert measured is not None
            for view in measured.views:
                if view.body is None:
                    continue
                box = view.body.bounding_rect
                body = QRectF(*transform.rectangle(box))
                painter.setPen(QPen(QColor(color), 1.5))
                painter.drawRect(body)
        if self.selection is not None:
            index, view = self.selection
            slot = self.slots[index].rect
            middle = slot.height // 2
            rect = BoundingRect(
                slot.x,
                slot.y + (middle if view else 0),
                slot.width,
                slot.height - middle if view else middle,
            )
            painter.setPen(
                QPen(QColor(theme_colors(self).accent), 2, Qt.PenStyle.DashLine)
            )
            painter.drawRect(QRectF(*transform.rectangle(rect)).adjusted(2, 2, -2, -2))


class TraditionalPreview(QWidget):
    """Single GUI owner; cache corresponds only to the currently frozen reference.

    At most one job plus one latest desired reference. Stale completions are
    discarded. Threshold edits only judge cached scalars. Close observes actual
    job termination before releasing the workspace.
    """

    mode_changed = Signal()

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
        connection: SourceConnectionCommands | None = None,
    ) -> None:
        super().__init__(parent)
        self.session, self.frames = session, frames
        self.allowed, self.changed, self.leave = allowed, changed, leave
        self.language: Language = language
        self.connection = connection
        self.mode: PreviewMode = mode
        self.frame: DisplayFrame | None = None
        self.measurements: tuple[PreviewMeasurement, ...] = ()
        self.results: tuple[CheckResult | None, ...] = ()
        self._measurement_flags: tuple[bool, bool] | None = None
        self._revision = 0
        self._thread: Thread | None = None
        self._future: Future[tuple[PreviewMeasurement, ...] | _Reference] | None = None
        self._job_revision = 0
        # One frozen reference per configured camera, one current measurement cache,
        # one IO/measurement job and at most one latest pending read.
        self._references: dict[CameraId, _Reference] = {}
        self._folder_paths: dict[CameraId, tuple[Path, ...]] = {}
        self._positions: dict[CameraId, int] = {}
        self._load_revision = 0
        self._job_load: int | None = None
        self._pending_read: tuple[int, Callable[[], _Reference]] | None = None
        self._reference_error = ""
        self._close: Callable[[], None] | None = None
        self._loading = False
        self._selection_frame: DisplayFrame | None = None
        root = QVBoxLayout(self)
        root.setContentsMargins(Style.space, Style.small, Style.space, Style.small)
        root.setSpacing(Style.small)
        camera_controls = QWidget()
        header = QHBoxLayout(camera_controls)
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(Style.small)
        self.camera_tabs = QTabBar()
        self.camera_tabs.setExpanding(False)
        self.camera_tabs.setUsesScrollButtons(False)
        self.camera_tabs.setDrawBase(False)
        self.camera_tabs.setIconSize(QSize(Style.icon, Style.icon))
        for camera in session.machine.layout.cameras:
            self.camera_tabs.addTab(
                camera_icon(), str(camera.camera_id).removeprefix("CAM")
            )
        self.refresh_button = QPushButton()
        self.fit_button = QPushButton()
        self.actual_button = QPushButton("1:1")
        self.import_button, self.runtime_button = QPushButton(), QPushButton()
        self.previous_button, self.next_button = QPushButton(), QPushButton()
        self.reference = QComboBox()
        self.reference.setMinimumContentsLength(12)
        self.reference.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.reference.setMaximumWidth(300)
        self.reference.setMinimumHeight(Style.touch)
        for button in (
            self.refresh_button,
            self.fit_button,
            self.actual_button,
            self.import_button,
            self.runtime_button,
            self.previous_button,
            self.next_button,
        ):
            button.setFixedSize(Style.touch, Style.touch)
            button.setIconSize(QSize(Style.icon, Style.icon))
        header.addWidget(self.camera_tabs)
        reference_controls, display_controls = QWidget(), QWidget()
        reference_row, display_row = (
            QHBoxLayout(reference_controls),
            QHBoxLayout(display_controls),
        )
        for row in (reference_row, display_row):
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(Style.small)
        for control in (
            self.import_button,
            self.runtime_button,
            self.previous_button,
            self.reference,
            self.next_button,
            self.refresh_button,
        ):
            reference_row.addWidget(control)
        display_row.addWidget(self.fit_button)
        display_row.addWidget(self.actual_button)
        self.image_toolbar = ImageToolbar(
            camera_controls, reference_controls, display_controls
        )
        root.addWidget(self.image_toolbar)
        self.canvas = PreviewCanvas()
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
        self._controls_row.addWidget(self.controls)
        self.selection_detail = QLabel()
        self.selection_detail.setMinimumWidth(230)
        self.selection_detail.setWordWrap(True)
        self.selection_detail.setTextFormat(Qt.TextFormat.PlainText)
        self.selection_detail.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._controls_row.addWidget(self.selection_detail, 1)
        root.addLayout(self._controls_row)
        root.addWidget(self.canvas, 1)
        self.message = QLabel()
        self.message.setWordWrap(True)
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        root.addWidget(self.message)
        self.refresh_button.clicked.connect(self.refresh_reference)
        self.fit_button.clicked.connect(lambda: self.canvas.zoom(None))
        self.actual_button.clicked.connect(lambda: self.canvas.zoom(1.0))
        self.import_button.clicked.connect(self.import_image)
        self.runtime_button.clicked.connect(self.use_runtime)
        self.previous_button.clicked.connect(lambda: self.browse_reference(-1))
        self.next_button.clicked.connect(lambda: self.browse_reference(1))
        self.reference.activated.connect(self.choose_image)
        self.camera_tabs.currentChanged.connect(self.select_camera)
        self.tolerance.valueChanged.connect(self.update_settings)
        self.sample.clicked.connect(self.sample_color)
        self.canvas.view_selected.connect(self.select_view)
        self._timer = QTimer(self)
        self._timer.setInterval(15)
        self._timer.timeout.connect(self._poll)
        self.set_language(language)
        self.select_camera()

    def set_mode(self, mode: PreviewMode) -> None:
        self.mode = mode
        self.show_settings()
        self.mode_changed.emit()

    @property
    def busy(self) -> bool:
        return self._thread is not None

    def refresh_theme(self) -> None:
        color = theme_colors(self).primary
        for index in range(self.camera_tabs.count()):
            self.camera_tabs.setTabIcon(index, camera_icon(color))
        for button, tool in (
            (self.import_button, "folder"),
            (self.runtime_button, "runtime"),
            (self.previous_button, "previous"),
            (self.next_button, "next"),
            (self.refresh_button, "refresh"),
            (self.fit_button, "fit"),
        ):
            button.setIcon(image_icon(tool, color))

    def set_language(self, language: Language) -> None:
        self.language = self.canvas.language = language
        self.refresh_theme()
        for button, key in (
            (self.import_button, "vision_import"),
            (self.runtime_button, "vision_runtime"),
            (self.previous_button, "reference_previous"),
            (self.next_button, "reference_next"),
            (self.refresh_button, "roi_refresh"),
            (self.fit_button, "roi_fit"),
            (self.actual_button, "roi_actual"),
        ):
            button.setToolTip(text(key, language))
            button.setAccessibleName(button.toolTip())
        self.reference.setAccessibleName(text("vision_image_select", language))
        self.camera_tabs.setAccessibleName(text("camera_setup", language))
        for index, camera in enumerate(self.session.machine.layout.cameras):
            name = text("camera_number", language).format(
                number=str(camera.camera_id).removeprefix("CAM")
            )
            self.camera_tabs.setTabToolTip(index, f"{name} ({camera.camera_id})")
            self.camera_tabs.setAccessibleTabName(index, f"{name} ({camera.camera_id})")
        for key in (
            "target_width",
            "width_tolerance",
            "target_height",
            "height_tolerance",
        ):
            caption = text(key, language)
            if key.endswith("tolerance"):
                caption += " (px)"
            # Match Vision's W/H notation; long translated phrases must not
            # squeeze neighboring parameters or silently clip their labels.
            self.labels[key].setText(
                {
                    "target_width": "W (px)",
                    "width_tolerance": "± W (px)",
                    "target_height": "H (px)",
                    "height_tolerance": "± H (px)",
                }[key]
            )
            self.labels[key].setFixedWidth(self.spins[key].width())
            self.labels[key].setToolTip(caption)
            self.labels[key].setAccessibleName(caption)
            self.spins[key].setAccessibleName(caption)
            self.spins[key].setToolTip(caption)
        set_button_caption(
            self.sample,
            text("color_sample", self.language),
            button_width(self.sample, ("color_sample",)),
        )
        self.sample.setToolTip(text("color_sample_tip", self.language))
        self.sample.setAccessibleName(self.sample.toolTip())
        self.show_settings()

    def show_settings(self) -> None:
        self._loading = True
        self.controls.setToolTip(text("preview_scope_tip", self.language))
        settings = self.session.draft.capsule
        self.controls.setCurrentIndex(0 if self.mode == "size" else 1)
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
            text("target_color", self.language) + f" · H {settings.color.target_hue}"
        )
        self.tolerance_label.setText(f"± H {settings.color.hue_tolerance}")
        self.tolerance_label.setFixedWidth(
            self.tolerance_label.fontMetrics().horizontalAdvance("± H 90") + Style.small
        )
        self.tolerance_label.setToolTip(
            text("color_tolerance", self.language) + f" {settings.color.hue_tolerance}"
        )
        self.tolerance_label.setAccessibleName(self.tolerance_label.toolTip())
        self._loading = False
        self.ensure_measurement()
        self.refresh_state()
        self.evaluate()

    def refresh_state(self) -> None:
        allowed = self.allowed() and self._close is None
        self.controls.setEnabled(allowed)
        self.sample.setEnabled(
            allowed
            and not self.busy
            and self.session.draft.capsule.color.enabled
            and any(
                row.measured and row.measured.color_measured
                for row in self.measurements
            )
        )
        viewing = not self.busy and self._close is None
        self.import_button.setEnabled(viewing)
        self.runtime_button.setEnabled(viewing and self._runtime_frame() is not None)
        self.refresh_button.setEnabled(viewing)
        self._present_reference()
        self._present_selection()

    def select_view(self, slot: int, view: int) -> None:
        if self.frame is None or not (
            0 <= slot < len(self.camera.slots) and view in (0, 1)
        ):
            return
        self._selection_frame = self.frame
        self.canvas.selection = slot, view
        self._present_selection()
        self.canvas.update()

    def _present_selection(self) -> None:
        selected = self.canvas.selection
        if (
            selected is None
            or self._selection_frame is not self.frame
            or self.frame is None
        ):
            self.selection_detail.setText(
                text("preview_pending", self.language)
                if self.busy
                else text("preview_no_reference", self.language)
                if self.frame is None
                else text("preview_unavailable", self.language)
                if not self.measurements
                else text("preview_select", self.language)
            )
            self.selection_detail.setToolTip("")
            return
        index, view_index = selected
        slot = self.camera.slots[index]
        name = "upper" if view_index == 0 else "lower"
        caption = text(
            "preview_upper" if view_index == 0 else "preview_lower", self.language
        )
        row = self.measurements[index] if index < len(self.measurements) else None
        measured = row.measured if row else None
        view = measured.views[view_index] if measured else None
        settings = self.session.draft.capsule
        enabled = (
            settings.size.enabled if self.mode == "size" else settings.color.enabled
        )
        available = measured is not None and (
            measured.size_measured if self.mode == "size" else measured.color_measured
        )
        status, reason, explanation = text("unavailable", self.language), "", ""
        if not enabled:
            status = "DISABLED"
        elif row is not None and row.error:
            status, reason = "ERROR", row.error
        elif available and measured is not None:
            result = self.results[index] if index < len(self.results) else None
            if result is not None:
                reasons = [r.code for r in result.reasons if f".{name}." in r.code]
                status = "NG" if reasons else "OK"
                reason = ", ".join(reasons)
                explanation = " · ".join(
                    dict.fromkeys(
                        text(
                            _REASON_LABELS.get(
                                code.rsplit(".", 1)[-1], "preview_unavailable"
                            ),
                            self.language,
                        )
                        for code in reasons
                    )
                )
        check_name = text(self.mode, self.language)
        header = f"{self.camera.camera_id} · CH{slot.channel_id:02d} · {caption} · {check_name} {status}"
        if self.mode == "size":
            rect = (
                view.body.bounding_rect
                if view is not None and view.body is not None and enabled and available
                else None
            )
            width, height = (str(rect.width), str(rect.height)) if rect else ("—", "—")
            s = settings.size
            values = f"W {width} [{s.target_width - s.width_tolerance}, {s.target_width + s.width_tolerance}] · H {height} [{s.target_height - s.height_tolerance}, {s.target_height + s.height_tolerance}] px"
        else:
            hue = (
                view.color.hue
                if view is not None and view.color is not None and enabled and available
                else None
            )
            actual = f"{hue:.2f}" if hue is not None else "—"
            values = f"Hue {actual} · {text('target', self.language)} {settings.color.target_hue} ± {settings.color.hue_tolerance}"
        self.selection_detail.setText(
            f"{header}\n{values}" + (f"\n{explanation}" if explanation else "")
        )
        self.selection_detail.setToolTip(
            reason + ("\n" if reason else "") + text("preview_values", self.language)
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
            self.show_message(text("preview_sample_missing", self.language))
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

    @property
    def camera(self) -> CameraLayout:
        return self.session.machine.layout.cameras[self.camera_tabs.currentIndex()]

    def _runtime_frame(self) -> DisplayFrame | None:
        sources = self.session.camera_sources
        key = (
            sources.definition.source_key(self.camera.camera_id, sources.path.parent)
            if sources
            else None
        )
        return next(
            (
                f
                for f in self.frames.snapshot().frames
                if f.frame_id.camera_id == self.camera.camera_id
                and (sources is None or (key and f.source_key == key))
            ),
            None,
        )

    def _project_blank(self) -> None:
        if self.frame is not None:
            return
        size = None
        sources = self.session.camera_sources
        if (
            self.connection is not None
            and sources is not None
            and sources.definition.mode == "basler"
        ):
            snapshot = self.connection.source_snapshot()
            serial = sources.definition.camera(self.camera.camera_id).basler_serial
            if snapshot.state is ConnectionState.CONNECTED:
                size = next(
                    (
                        (c.width, c.height)
                        for c in snapshot.cameras
                        if c.camera_id == self.camera.camera_id and c.serial == serial
                    ),
                    None,
                )
        self.canvas.rois = self.camera.slots
        self.canvas.set_projection(size)

    def _set_reference(self, reference: _Reference | None) -> None:
        self._revision += 1
        self.measurements, self.results = (), ()
        self.canvas.rows, self.canvas.results = (), ()
        self.canvas.selection = None
        self._selection_frame = None
        self.canvas.setToolTip("")
        self.frame = reference.frame if reference else None
        if reference is not None:
            assert reference.frame.frame_id.camera_id == self.camera.camera_id
            self._references[self.camera.camera_id] = reference
            if reference.kind == "folder":
                self._folder_paths[self.camera.camera_id] = reference.paths
                self._positions[self.camera.camera_id] = reference.index
        else:
            self._references.pop(self.camera.camera_id, None)
        self.canvas.set_frame(self.frame)
        self.canvas.slots = self.camera.slots
        self.canvas.rois = ()
        self._project_blank()
        self.evaluate()
        self.refresh_state()

    def select_camera(self) -> None:
        if self._close is not None:
            return
        self._load_revision += 1
        self._pending_read = None
        self._reference_error = ""
        reference = self._references.get(self.camera.camera_id)
        self._set_reference(reference)
        if reference is None:
            self.refresh_reference()
        elif not self.busy:
            self._start_measurement()

    def use_runtime(self) -> None:
        if self.busy or self._close is not None:
            return
        self._reference_error = ""
        frame = self._runtime_frame()
        self._set_reference(_Reference(frame, "runtime") if frame else None)
        if not self.busy:
            self._start_measurement()

    def refresh_reference(self) -> None:
        if self._close is not None:
            return
        selected = self._references.get(self.camera.camera_id)
        if selected is not None and selected.kind == "import":
            assert selected.path is not None
            self.load_image(selected.path)
        elif selected is not None and selected.kind == "runtime":
            # A measurement may be finishing; its stale completion is rejected.
            self._load_revision += 1
            self._pending_read = None
            self._reference_error = ""
            frame = self._runtime_frame()
            self._set_reference(_Reference(frame, "runtime") if frame else None)
            if not self.busy:
                self._start_measurement()
        elif (
            self.session.camera_sources is not None
            and self.session.camera_sources.definition.mode == "folder"
        ):
            self._read_folder(
                selected.path if selected and selected.kind == "folder" else None
            )
        else:
            # No Folder source is fabricated for Basler or developer-only sessions.
            self._reference_error = ""
            frame = self._runtime_frame()
            self._set_reference(_Reference(frame, "runtime") if frame else None)
            if not self.busy:
                self._start_measurement()

    def import_image(self) -> None:
        if self.busy or self._close is not None:
            return
        path, _ = QFileDialog.getOpenFileName(
            self,
            text("vision_import", self.language),
            "",
            "Images (*.png *.jpg *.jpeg *.bmp *.tif *.tiff)",
        )
        if path:
            self.load_image(Path(path))

    def load_image(self, path: Path) -> None:
        camera = self.camera.camera_id

        def read() -> _Reference:
            return _Reference(
                self._local_frame(path, camera, 0, str(path)), "import", path
            )

        self._queue_read(read)

    @staticmethod
    def _local_frame(
        path: Path, camera: CameraId, index: int, key: str
    ) -> DisplayFrame:
        pixels = decode_image(path)
        owned = np.frombuffer(pixels.tobytes(), dtype=np.uint8).reshape(pixels.shape)
        return DisplayFrame(
            FrameId(CycleId(0), camera, index), 0, monotonic(), 0, owned, key
        )

    def _read_folder(self, path: Path | None) -> None:
        sources = self.session.camera_sources
        if sources is None or sources.definition.mode != "folder":
            return
        camera = self.camera.camera_id
        source = sources.definition.camera(camera)
        folder, key = source.path(sources.path.parent), source.key(sources.path.parent)

        def read() -> _Reference:
            if not source.folder.strip():
                raise FolderSourceError(f"{camera}: no Folder source configured")
            paths = folder_images(folder)
            if path is not None:
                if path not in paths:
                    raise FolderSourceError(f"Reference image no longer exists: {path}")
                index = paths.index(path)
                return _Reference(
                    self._local_frame(path, camera, index, key),
                    "folder",
                    path,
                    paths,
                    index,
                )
            last_error = ""
            for index, candidate in enumerate(paths):
                try:
                    return _Reference(
                        self._local_frame(candidate, camera, index, key),
                        "folder",
                        candidate,
                        paths,
                        index,
                    )
                except FolderSourceError as exc:
                    last_error = str(exc)
            raise FolderSourceError(last_error)

        self._queue_read(read)

    def choose_image(self, index: int) -> None:
        selected = self._references.get(self.camera.camera_id)
        offset = int(selected is None or selected.kind != "folder")
        paths = self._folder_paths.get(self.camera.camera_id, ())
        if not self.busy and 0 <= index - offset < len(paths):
            self._read_folder(paths[index - offset])

    def browse_reference(self, delta: int) -> None:
        paths = self._folder_paths.get(self.camera.camera_id, ())
        index = self._positions.get(self.camera.camera_id, 0) + delta
        if not self.busy and 0 <= index < len(paths):
            self._read_folder(paths[index])

    def _queue_read(self, read: Callable[[], _Reference]) -> None:
        if self._close is not None:
            return
        self._load_revision += 1
        self._revision += 1
        self._reference_error = ""
        self._pending_read = self._load_revision, read
        if not self.busy:
            self._start_read()

    def _start_read(self) -> None:
        request, self._pending_read = self._pending_read, None
        assert request is not None
        self._job_load, read = request
        self._start_job(read)

    def _present_reference(self) -> None:
        self._project_blank()
        selected = self._references.get(self.camera.camera_id)
        folder = (
            self.session.camera_sources is not None
            and self.session.camera_sources.definition.mode == "folder"
        )
        paths = self._folder_paths.get(self.camera.camera_id, ()) if folder else ()
        offset = int(not selected or selected.kind != "folder")
        index = self._positions.get(self.camera.camera_id, 0)
        position = (
            f"{index + 1}/{len(paths)}"
            if paths and not offset
            else text(
                "vision_imported"
                if selected and selected.kind == "import"
                else "reference_runtime",
                self.language,
            )
        )
        if self.frame is not None:
            info = (
                f"{position} · {self.frame.image.shape[1]}×{self.frame.image.shape[0]}"
            )
            tip = (
                str(selected.path)
                if selected and selected.path
                else text("reference_runtime_tip", self.language)
            )
        else:
            size = self.canvas.model.size
            info = (
                text("vision_dimensions_only", self.language).format(
                    width=size[0], height=size[1]
                )
                if size
                else text("vision_schematic", self.language)
            )
            tip = text(
                "roi_no_frame_tip" if size else "vision_schematic_tip", self.language
            )
        # Programmatic item changes never navigate or load an image.
        captions = ([info] if offset or not paths else []) + [
            f"{i + 1}/{len(paths)} · {p.name}" for i, p in enumerate(paths)
        ]
        target = index if paths and not offset else 0
        captions[target] = info
        if captions != [
            self.reference.itemText(i) for i in range(self.reference.count())
        ]:
            self.reference.clear()
            self.reference.addItems(captions)
        if self.reference.currentIndex() != target:
            self.reference.setCurrentIndex(target)
        self.reference.setToolTip(tip)
        self.reference.setAccessibleDescription(info)
        # Size from the localized status, including combo padding/arrow; otherwise
        # "Imported · W×H" can lose the final dimension without an ellipsis.
        self.reference.setMinimumWidth(
            self.reference.fontMetrics().horizontalAdvance(info)
            + 2 * Style.space
            + Style.touch
        )
        self.canvas.setAccessibleDescription(info)
        viewing = not self.busy and self._close is None
        self.reference.setEnabled(viewing and bool(paths))
        self.previous_button.setEnabled(viewing and bool(paths) and index > 0)
        self.next_button.setEnabled(viewing and bool(paths) and index + 1 < len(paths))
        self.image_toolbar.sync_metrics()

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
        self._job_revision = self._revision
        self._job_load = None
        self._start_job(lambda: measure_reference(frame, camera, settings))

    def _start_job(
        self, work: Callable[[], tuple[PreviewMeasurement, ...] | _Reference]
    ) -> None:
        future: Future[tuple[PreviewMeasurement, ...] | _Reference] = Future()
        self._future = future

        def run() -> None:
            try:
                future.set_result(work())
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
            self._references.clear()
            self._pending_read = None
            callback()
            return
        assert future is not None and future.done()
        try:
            value = future.result()
        except BaseException as exc:
            if self._job_load == self._load_revision or (
                self._job_load is None and self._job_revision == self._revision
            ):
                self._reference_error = f"{type(exc).__name__}: {exc}"
        else:
            if isinstance(value, _Reference):
                if self._job_load == self._load_revision:
                    self._set_reference(value)
            elif self._job_revision == self._revision:
                self.measurements = value
        if self._pending_read is not None:
            self._start_read()
        elif self._job_load is not None or self._job_revision != self._revision:
            self._start_measurement()
        self.evaluate()
        self.refresh_state()

    def evaluate(self) -> None:
        settings = self.session.draft.capsule
        selected_settings = replace(
            settings,
            size=replace(
                settings.size, enabled=self.mode == "size" and settings.size.enabled
            ),
            color=replace(
                settings.color, enabled=self.mode == "color" and settings.color.enabled
            ),
        )
        self.canvas.check_enabled = selected_settings.enabled
        self.results = tuple(
            judge_capsule(row.measured, selected_settings) if row.measured else None
            for row in self.measurements
        )
        self.canvas.rows, self.canvas.results = self.measurements, self.results
        self.canvas.update()
        self._present_selection()
        if self._reference_error:
            self.show_message(self._reference_error)
        elif self.frame is None:
            self.show_message("")  # The toolbar owns blank/schematic image status.
        elif self.busy:
            self.show_message(text("preview_measuring", self.language))
        else:
            errors = [
                f"CH{row.slot.channel_id:02d}: {row.error}"
                for row in self.measurements
                if row.error and selected_settings.enabled
            ]
            self.show_message(
                text("preview_measurement_error", self.language).format(
                    count=len(errors)
                )
                if errors
                else ""
            )
            self.message.setToolTip("\n".join(errors))
        self.canvas.setToolTip(
            "\n".join(
                f"CH{row.slot.channel_id:02d}: "
                + (", ".join(r.code for r in result.reasons) if result else row.error)
                for row, result in zip(self.measurements, self.results, strict=True)
                if (row.error and selected_settings.enabled)
                or (result is not None and result.reasons)
            )
        )

    def show_message(self, message: str) -> None:
        """Only transient work/unavailable/error feedback reserves a row."""
        self.message.setText(message)
        self.message.setToolTip(message)
        self.message.setVisible(bool(message))

    def request_close(self, after: Callable[[], None]) -> None:
        self._close = after
        self.setEnabled(False)
        if not self.busy:
            self.frame = None
            self._references.clear()
            self._pending_read = None
            after()
