"""Canvas-first machine geometry workspace; no inference or activation ownership."""

from collections.abc import Callable

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTabBar,
    QVBoxLayout,
    QWidget,
)

from vimo_demo.configuration import ConfigurationError
from vimo_demo.inference import BoundingRect
from vimo_demo.inspection import CameraId

from .display_frames import DisplayFrame, LatestDisplayFrameStore
from .production_style import Language, Style, text
from .recipe_editor import RecipeEditorSession
from .roi_canvas import RoiCanvas


def _image_icon(refresh: bool) -> QIcon:
    """Draw simple tools without relying on optional Unicode font glyphs."""
    pixmap = QPixmap(24, 24)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor(Style.primary), 1.6))
    if refresh:
        painter.drawArc(5, 5, 14, 14, 40 * 16, 290 * 16)
        painter.drawLine(19, 4, 19, 10)
        painter.drawLine(19, 10, 13, 10)
    else:
        for x, y, dx, dy in (
            (4, 4, 1, 1),
            (20, 4, -1, 1),
            (4, 20, 1, -1),
            (20, 20, -1, -1),
        ):
            painter.drawLine(x, y, x + dx * 5, y)
            painter.drawLine(x, y, x, y + dy * 5)
    painter.end()
    return QIcon(pixmap)


class MachineRoiWorkspace(QWidget):
    def __init__(
        self,
        session: RecipeEditorSession,
        camera: CameraId,
        frames: LatestDisplayFrameStore,
        allowed: Callable[[], bool],
        changed: Callable[[], None],
        apply: Callable[[Callable[[str], None]], None],
        leave: Callable[[], None],
        *,
        language: Language,
        simulation: bool,
        parent: QWidget,
    ) -> None:
        super().__init__(parent)
        self.session, self.draft = session, session.machine_editor
        self._frames, self._allowed, self._changed, self._apply = (
            frames,
            allowed,
            changed,
            apply,
        )
        self._leave = leave
        self.language: Language = language
        self.simulation = simulation
        self.frame: DisplayFrame | None = None
        self.selected_index = 0
        self._error = ""
        self._feedback = ""
        self._preparing = False
        self._decision: QMessageBox | None = None
        self.buttons: dict[str, QPushButton] = {}
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        header = QHBoxLayout()
        header.addWidget(self.button("roi_back"))
        self.title = QLabel()
        header.addWidget(self.title)
        self.camera_tabs = QTabBar()
        self.camera_tabs.setExpanding(False)
        self.camera_tabs.setDrawBase(False)
        self.camera_tabs.setStyleSheet(
            f"QTabBar::tab {{ padding: 12px 10px; min-height: 20px; "
            f"color: {Style.primary}; background: {Style.surface}; border: none; }} "
            f"QTabBar::tab:selected {{ color: {Style.accent}; background: {Style.raised}; }}"
        )
        for identity in self.draft.definition.layout.camera_ids:
            self.camera_tabs.addTab(str(identity))
        self.camera_tabs.setCurrentIndex(
            self.draft.definition.layout.camera_ids.index(camera)
        )
        header.addWidget(self.camera_tabs, 1)
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        header.addWidget(self.status)
        root.addLayout(header)
        self.canvas = RoiCanvas(self.editable)
        root.addWidget(self.canvas, 1)
        # Child overlay layouts reserve no workspace height outside the image.
        overlay = QVBoxLayout(self.canvas)
        overlay.setContentsMargins(12, 12, 12, 12)
        self.reference = QLabel()
        self.reference.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.reference.setStyleSheet("color: #a0a8b4; background: transparent;")
        overlay.addWidget(self.reference, 0, Qt.AlignmentFlag.AlignLeft)
        overlay.addStretch()
        corner = QHBoxLayout()
        self.metadata = QLabel()
        self.metadata.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.metadata.setStyleSheet(
            "background: #18202c; padding: 8px; border-radius: 6px;"
        )
        corner.addWidget(self.metadata, 0, Qt.AlignmentFlag.AlignBottom)
        corner.addStretch()
        for key, symbol in (
            ("roi_fit", "⛶"),
            ("roi_actual", "1:1"),
            ("roi_refresh", "↻"),
        ):
            button = self.button(key)
            button.setText(symbol if key == "roi_actual" else "")
            if key != "roi_actual":
                button.setIcon(_image_icon(key == "roi_refresh"))
                button.setIconSize(QSize(24, 24))
            button.setFixedSize(Style.touch, Style.touch)
            corner.addWidget(button, 0, Qt.AlignmentFlag.AlignBottom)
        overlay.addLayout(corner)
        batch = QHBoxLayout()
        self.common_width, self.common_height = QLineEdit(), QLineEdit()
        self.size_labels: list[QLabel] = []
        for key, line in (
            ("roi_width", self.common_width),
            ("roi_height", self.common_height),
        ):
            label = QLabel()
            self.size_labels.append(label)
            batch.addWidget(label)
            line.setAccessibleName(text(key, language))
            line.setFixedWidth(85)
            line.setMinimumHeight(Style.touch)
            batch.addWidget(line)
        batch.addWidget(self.button("roi_common"))
        batch.addWidget(self.button("roi_distribute"))
        batch.addStretch()
        root.addLayout(batch)
        footer = QHBoxLayout()
        self.message = QLabel()
        self.message.setWordWrap(True)
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        footer.addWidget(self.message, 1)
        primary = self.button("roi_save_apply")
        primary.setObjectName("start")
        footer.addWidget(primary)
        root.addLayout(footer)
        self.buttons["roi_back"].clicked.connect(lambda: self.request_back())
        self.buttons["roi_fit"].clicked.connect(lambda: self.canvas.zoom(None))
        self.buttons["roi_actual"].clicked.connect(lambda: self.canvas.zoom(1))
        self.buttons["roi_refresh"].clicked.connect(self.refresh_frame)
        self.buttons["roi_common"].clicked.connect(self.common_size)
        self.buttons["roi_distribute"].clicked.connect(
            lambda: self.operation(lambda: self.draft.distribute(self.camera))
        )
        self.buttons["roi_save_apply"].clicked.connect(lambda: self.save_and_apply())
        self.camera_tabs.currentChanged.connect(self.switch_camera)
        self.canvas.selected.connect(self.select_roi)
        self.canvas.geometry_changed.connect(self.commit_geometry)
        self.set_language(language)
        self.switch_camera()

    @property
    def camera(self) -> CameraId:
        return self.draft.definition.layout.camera_ids[self.camera_tabs.currentIndex()]

    def button(self, key: str) -> QPushButton:
        button = QPushButton(text(key, self.language))
        button.setMinimumHeight(Style.touch)
        self.buttons[key] = button
        return button

    def set_language(self, language: Language) -> None:
        self.language = language
        self.title.setText(text("roi_workspace", language))
        self.camera_tabs.setAccessibleName(text("camera_scope", language))
        for key, button in self.buttons.items():
            button.setAccessibleName(text(key, language))
            button.setToolTip(text(key, language))
            if key not in ("roi_fit", "roi_actual", "roi_refresh"):
                button.setText(text(key, language))
        for label, line, key in zip(
            self.size_labels,
            (self.common_width, self.common_height),
            ("roi_width", "roi_height"),
            strict=True,
        ):
            label.setText(text(key, language))
            line.setAccessibleName(text(key, language))
        self.refresh()

    def editable(self) -> bool:
        return self._allowed() and not self._preparing and self.frame is not None

    def switch_camera(self) -> None:
        self.selected_index = 0
        self.refresh_frame()

    def refresh_frame(self) -> None:
        self.frame = next(
            (
                f
                for f in self._frames.snapshot().frames
                if f.frame_id.camera_id == self.camera
            ),
            None,
        )
        self.canvas.set_frame(self.frame)
        if self.canvas.model.size is not None:
            self.draft.bounds[self.camera] = self.canvas.model.size
        self.select_roi(self.selected_index)

    def select_roi(self, index: int) -> None:
        self.canvas.cancel_gesture()
        self.selected_index = index
        self._error = ""
        self.sync_size()
        self.refresh()

    def sync_size(self) -> None:
        rect = self.draft.camera(self.camera).slots[self.selected_index].rect
        self.common_width.setText(str(rect.width))
        self.common_height.setText(str(rect.height))

    def refresh(self) -> None:
        self.canvas.rois = self.draft.camera(self.camera).slots
        slot = self.canvas.rois[self.selected_index]
        rect = slot.rect
        self.canvas.model.rect = rect
        self.canvas.model.editable = self.editable()
        if not self.canvas.model.can_drag:
            self.canvas.cancel_roi_drag()
        self.metadata.setText(
            f"CH{slot.channel_id:02d} · x{rect.x} y{rect.y} · {rect.width}×{rect.height}"
        )
        for line in (self.common_width, self.common_height):
            line.setReadOnly(not self.editable())
        for key in ("roi_common", "roi_distribute"):
            self.buttons[key].setEnabled(self.editable())
        self.buttons["roi_save_apply"].setEnabled(
            self._allowed()
            and not self._preparing
            and not self.session.dirty
            and self.session.entry is not None
        )
        self.buttons["roi_back"].setEnabled(not self._preparing)
        self.status.setText(
            text(
                "preparing"
                if self._preparing
                else "unsaved"
                if self.draft.dirty
                else "saved",
                self.language,
            )
        )
        self.message.setText(
            self._error
            or (text(self._feedback, self.language) if self._feedback else "")
            or (text("roi_recipe_save", self.language) if self.session.dirty else "")
            or (
                text("read_only", self.language)
                if not self._allowed() and not self._preparing
                else ""
            )
        )
        info = (
            text("roi_no_frame", self.language)
            if self.frame is None
            else f"{self.camera} · {self.frame.image.shape[1]} × {self.frame.image.shape[0]} px"
        )
        self.reference.setText(
            (text("simulation", self.language) + " · " if self.simulation else "")
            + info
        )
        self.canvas.update()

    def operation(self, action: Callable[[], None]) -> bool:
        if not self.editable():
            return False
        try:
            action()
            self._error = self._feedback = ""
            self.sync_size()
            self._changed()
        except (ValueError, ConfigurationError) as exc:
            self._error = str(exc)
        self.refresh()
        return not self._error

    def set_rect(self, rect: BoundingRect) -> None:
        rects = [s.rect for s in self.draft.camera(self.camera).slots]
        rects[self.selected_index] = rect
        self.draft.replace_rects(self.camera, tuple(rects))

    def commit_geometry(self, value: object) -> None:
        if isinstance(value, BoundingRect) and self.canvas.model.can_drag:
            self.operation(lambda: self.set_rect(value))

    def common_size(self) -> None:
        self.operation(
            lambda: self.draft.common_size(
                self.camera,
                int(self.common_width.text()),
                int(self.common_height.text()),
            )
        )

    def save_and_apply(self, after: Callable[[], None] | None = None) -> None:
        if not self._allowed() or self._preparing:
            return
        if self.session.dirty or self.session.entry is None:
            self._error = text("roi_recipe_save", self.language)
            self.refresh()
            return
        try:
            self.draft.save()
        except (ValueError, ConfigurationError) as exc:
            self._error = str(exc)
            self.refresh()
            return
        self._preparing = True
        self._error = self._feedback = ""
        self._changed()
        self.refresh()

        def prepared(error: str) -> None:
            self._preparing = False
            self._error = error
            self._feedback = "" if error else "roi_applied"
            self.refresh()
            if not error and after is not None:
                after()

        self._apply(prepared)

    def request_back(self, after: Callable[[], None] | None = None) -> None:
        if self._preparing or self._decision is not None:
            return

        def leave() -> None:
            self.frame = None
            self.canvas.set_frame(None)
            self._leave()
            if after is not None:
                after()

        if not self.draft.dirty:
            leave()
            return
        dialog = QMessageBox(self)
        dialog.setWindowTitle(text("roi_workspace", self.language))
        dialog.setText(text("dirty_question", self.language))
        save = dialog.addButton(
            text("roi_save_apply", self.language), QMessageBox.ButtonRole.AcceptRole
        )
        discard = dialog.addButton(
            text("discard", self.language), QMessageBox.ButtonRole.DestructiveRole
        )
        dialog.addButton(
            text("cancel", self.language), QMessageBox.ButtonRole.RejectRole
        )
        save.setEnabled(
            self._allowed()
            and not self.session.dirty
            and self.session.entry is not None
        )
        self._decision = dialog

        def finished() -> None:
            self._decision = None
            chosen = dialog.clickedButton()
            dialog.deleteLater()
            if chosen is save:
                self.save_and_apply(leave)
            elif chosen is discard:
                self.draft.discard()
                self._changed()
                leave()

        dialog.finished.connect(finished)
        dialog.open()
