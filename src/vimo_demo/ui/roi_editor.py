"""Single-channel RecipeDraft editor; no saving, activation or runtime ownership."""

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from vimo_demo.configuration import ConfigurationError
from vimo_demo.inference import BoundingRect
from vimo_demo.inspection import CameraId
from vimo_demo.inspection.production import ChannelId

from .display_frames import DisplayFrame, LatestDisplayFrameStore
from .production_style import Language, Style, text
from .recipe_editor import RecipeEditorSession
from .roi_canvas import RoiCanvas
from .roi_geometry import fits


class RoiEditorDialog(QDialog):
    def __init__(
        self,
        session: RecipeEditorSession,
        camera: CameraId,
        channel: ChannelId,
        frames: LatestDisplayFrameStore,
        allowed: Callable[[], bool],
        changed: Callable[[], None],
        *,
        language: Language,
        simulation: bool,
        parent: QWidget,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("roiEditor")
        self.session, self.camera, self.channel = session, camera, channel
        self._draft = session.draft
        self._frames, self._allowed, self._changed = frames, allowed, changed
        self.language: Language = language
        self.simulation = simulation
        self.frame: DisplayFrame | None = None
        self._effective: BoundingRect | None = None
        self.setWindowTitle(text("roi_editor", language))
        self.setMinimumSize(700, 620)
        self.resize(
            min(1100, parent.window().width() - 40),
            min(1000, parent.window().height() - 80),
        )
        layout = QVBoxLayout(self)
        layout.setSpacing(Style.space)
        self.identity = QLabel(
            f"{camera} · CH{channel:02d} · {session.draft.display_name}"
        )
        self.identity.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.identity)
        self.reference = QLabel()
        self.reference.setWordWrap(True)
        layout.addWidget(self.reference)
        self.canvas = RoiCanvas(self.editable)
        layout.addWidget(self.canvas, 1)
        actions = QHBoxLayout()
        self.buttons: dict[str, QPushButton] = {}
        for key in ("roi_fit", "roi_minus", "roi_actual", "roi_plus", "roi_refresh"):
            button = self._button(key)
            actions.addWidget(button)
        layout.addLayout(actions)
        fields = QHBoxLayout()
        self.values = tuple(QLineEdit() for _ in range(4))
        for key, line in zip(
            ("roi_x", "roi_y", "roi_width", "roi_height"), self.values, strict=True
        ):
            column = QVBoxLayout()
            label = text(key, language)
            column.addWidget(QLabel(label))
            line.setAccessibleName(label)
            line.setMinimumHeight(Style.touch)
            column.addWidget(line)
            fields.addLayout(column, 1)
            line.textEdited.connect(self.numeric_changed)
        layout.addLayout(fields)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        hint = QLabel(text("roi_gestures", language))
        hint.setWordWrap(True)
        layout.addWidget(hint)
        actions = QHBoxLayout()
        for key in ("roi_create", "roi_reset", "roi_close"):
            actions.addWidget(self._button(key))
        layout.addLayout(actions)
        self.buttons["roi_fit"].clicked.connect(lambda: self.canvas.zoom(None))
        self.buttons["roi_actual"].clicked.connect(lambda: self.canvas.zoom(1))
        self.buttons["roi_minus"].clicked.connect(lambda: self.canvas.zoom_step(0.5))
        self.buttons["roi_plus"].clicked.connect(lambda: self.canvas.zoom_step(2))
        self.buttons["roi_refresh"].clicked.connect(self.refresh_frame)
        self.buttons["roi_create"].clicked.connect(self.create_override)
        self.buttons["roi_reset"].clicked.connect(self.reset_override)
        self.buttons["roi_close"].clicked.connect(self.accept)
        self.canvas.geometry_changed.connect(self.commit_geometry)
        self.refresh_frame()

    def _button(self, key: str) -> QPushButton:
        button = QPushButton(text(key, self.language))
        button.setMinimumHeight(Style.touch)
        self.buttons[key] = button
        return button

    def editable(self) -> bool:
        return self._allowed() and self.session.draft is self._draft

    def refresh_frame(self) -> None:
        # Store pixels are immutable byte-backed copies. Retain ONE; no history.
        self.frame = next(
            (
                f
                for f in self._frames.snapshot().frames
                if f.frame_id.camera_id == self.camera
            ),
            None,
        )
        self.canvas.set_frame(self.frame)
        self.refresh()

    def refresh(self) -> None:
        language = self.language
        override = self.channel in self._draft.rois
        error = ""
        self._effective = None
        try:
            self._effective = next(
                c.roi
                for c in self.session.resolve().channels
                if c.channel_id == self.channel
            )
        except (ConfigurationError, ValueError) as exc:
            error = str(exc)
        roi = self._effective
        values = self._draft.rois.get(self.channel)
        if values is None and roi is not None:
            values = str(roi.x), str(roi.y), str(roi.width), str(roi.height)
        if values is not None:
            for line, value in zip(self.values, values, strict=True):
                if line.text() != value:
                    line.setText(value)
        for line in self.values:
            line.setReadOnly(not (self.editable() and override))
        self.buttons["roi_create"].setEnabled(
            self.editable() and not override and roi is not None
        )
        self.buttons["roi_reset"].setEnabled(self.editable() and override)
        model = self.canvas.model
        previous = model.rect, model.editable
        if roi is not None:
            model.rect = roi
        model.editable = self.editable() and override and roi is not None
        if not model.can_drag:
            self.canvas.cancel_roi_drag()
        if roi is not None and model.size is not None and not fits(roi, model.size):
            error = text("roi_outside", language)
        state = text("override" if override else "inherit", language)
        self.status.setText(
            state
            + " · "
            + text("unsaved" if self.session.dirty else "saved", language)
            + (" · " + text("read_only", language) if not self.editable() else "")
            + ("\n" + error if error else "")
        )
        prefix = text("simulation", language) + " · " if self.simulation else ""
        if self.frame is None:
            reference = text("roi_no_frame", language)
        else:
            frame = self.frame
            reference = f"{text('roi_reference', language)} · {frame.frame_id.camera_id} · {text('roi_generation', language)} {frame.generation} · {text('roi_cycle', language)} {frame.frame_id.cycle_id} · {text('roi_sequence', language)} {frame.frame_id.sequence} · {frame.image.shape[1]} × {frame.image.shape[0]} px"
        self.reference.setText(prefix + reference)
        if previous != (model.rect, model.editable):
            self.canvas.update()

    def _notify(self) -> None:
        self._changed()
        self.refresh()

    def create_override(self) -> None:
        self.refresh()
        roi = self._effective
        if self.editable() and self.channel not in self._draft.rois and roi is not None:
            self._draft.rois[self.channel] = (
                str(roi.x),
                str(roi.y),
                str(roi.width),
                str(roi.height),
            )
            self._notify()

    def reset_override(self) -> None:
        if self.editable() and self.channel in self._draft.rois:
            self._draft.rois.pop(self.channel)
            self._notify()

    def numeric_changed(self) -> None:
        if self.editable() and self.channel in self._draft.rois:
            self.canvas.cancel_gesture()
            x, y, width, height = (line.text() for line in self.values)
            self._draft.rois[self.channel] = x, y, width, height
            self._notify()

    def commit_geometry(self, value: object) -> None:
        if (
            self.editable()
            and self.canvas.model.can_drag
            and self.channel in self._draft.rois
            and isinstance(value, BoundingRect)
            and self.canvas.model.size is not None
            and fits(value, self.canvas.model.size)
        ):
            self._draft.rois[self.channel] = (
                str(value.x),
                str(value.y),
                str(value.width),
                str(value.height),
            )
            self._notify()

    def done(self, arg__1: int) -> None:
        self.frame = None
        self.canvas.set_frame(None)
        super().done(arg__1)
