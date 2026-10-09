"""Canvas-first machine geometry workspace; no inference or activation ownership."""

from collections.abc import Callable
from time import monotonic

import numpy as np
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTabBar,
    QVBoxLayout,
    QWidget,
)

from vimo_demo.application.folder_replay import (
    FolderReplay,
    decode_image,
    folder_images,
)
from vimo_demo.configuration import ConfigurationError
from vimo_demo.inference import BoundingRect
from vimo_demo.inspection import CameraId, CycleId, FrameId

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
        run_io: Callable[
            [Callable[[], object], Callable[[object, BaseException | None], None]], bool
        ]
        | None = None,
    ) -> None:
        super().__init__(parent)
        self.session, self.draft = session, session.machine_editor
        self._frames, self._allowed, self._changed, self._apply = (
            frames,
            allowed,
            changed,
            apply,
        )
        self._leave, self._run_io = leave, run_io
        self.sources = session.camera_sources
        self._reference_revision = 0
        self._reading = False
        self._pending_reference = False
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
        self.folder = QLineEdit()
        self.browse = QPushButton()
        self.loop = QCheckBox()
        self.source_label = QLabel()
        self.source_notice = QLabel()
        self.source_notice.setWordWrap(True)
        source_row = QHBoxLayout()
        source_row.addWidget(self.source_label)
        source_row.addWidget(self.folder, 1)
        source_row.addWidget(self.browse)
        source_row.addWidget(self.loop)
        if self.sources is not None:
            self.folder.setMinimumHeight(Style.touch)
            self.browse.setMinimumHeight(Style.touch)
            root.addLayout(source_row)
            root.addWidget(self.source_notice)
        else:
            for widget in (
                self.folder,
                self.browse,
                self.loop,
                self.source_label,
                self.source_notice,
            ):
                widget.hide()  # Fake demo keeps its synthetic source; no real folder execution.
        self.folder.textEdited.connect(self.source_edited)
        self.folder.editingFinished.connect(self.refresh_frame)
        self.browse.clicked.connect(self.browse_folder)
        self.loop.toggled.connect(self.loop_changed)
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
        self.title.setText(text("camera_setup", language))
        self.source_label.setText("Folder")
        self.folder.setAccessibleName(text("source_folder", language))
        self.browse.setText(text("browse", language))
        self.loop.setText(text("source_loop", language))
        self.source_notice.setText(
            text("cli_sources", language) if self.session.cli_sources else ""
        )
        self.source_notice.setVisible(self.session.cli_sources)
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
        if self.sources is not None:
            self.folder.setText(self.sources.definition.camera(self.camera).folder)
            self.loop.blockSignals(True)
            self.loop.setChecked(self.sources.definition.loop)
            self.loop.blockSignals(False)
        self.refresh_frame()

    def source_edited(self, folder: str) -> None:
        if not self._allowed() or self.sources is None:
            return
        self.sources.set_folder(self.camera, folder)
        self._reference_revision += 1
        self.draft.bounds.pop(self.camera, None)
        self._set_reference(None)
        self._changed()

    def browse_folder(self) -> None:
        if not self._allowed() or self.sources is None:
            return
        folder = QFileDialog.getExistingDirectory(
            self, text("source_folder", self.language), self.folder.text()
        )
        if folder:
            self.folder.setText(folder)
            self.source_edited(folder)
            self.refresh_frame()

    def loop_changed(self, enabled: bool) -> None:
        if self._allowed() and self.sources is not None:
            self.sources.set_loop(enabled)
            self._changed()

    def _set_reference(self, frame: DisplayFrame | None) -> None:
        self.frame = frame
        self.canvas.set_frame(frame)
        self.draft.bounds.pop(self.camera, None)
        if self.canvas.model.size is not None:
            self.draft.bounds[self.camera] = self.canvas.model.size
        self.select_roi(self.selected_index)
        if frame is not None:
            try:
                self.draft.camera(self.camera).validate_shape(
                    (frame.image.shape[0], frame.image.shape[1], 3)
                )
            except ValueError as exc:
                self._error = str(exc)
                self.refresh()  # Visible incompatible geometry; never rescale/repair it.

    def refresh_frame(self) -> None:
        self._reference_revision += 1
        if self._reading:
            self._pending_reference = True  # One latest intent, not an IO queue.
            self._set_reference(None)
            return
        camera = self.camera
        source = (
            self.sources.definition.camera(camera) if self.sources is not None else None
        )
        base = self.sources.path.parent if self.sources is not None else None
        key = source.key(base) if source is not None and base is not None else None
        frame = next(
            (
                f
                for f in self._frames.snapshot().frames
                if f.frame_id.camera_id == camera
                and (source is None or (key and f.source_key == key))
            ),
            None,
        )
        self._error = ""
        self._set_reference(frame)
        if (
            frame is not None
            or source is None
            or not source.folder
            or base is None
            or self._run_io is None
        ):
            return
        revision = self._reference_revision
        folder = source.path(base)

        def read() -> DisplayFrame:
            image = decode_image(folder_images(folder)[0])
            owned = np.frombuffer(image.tobytes(), dtype=np.uint8).reshape(image.shape)
            return DisplayFrame(
                FrameId(CycleId(0), camera, 0), 0, monotonic(), 0, owned, key
            )

        def completed(value: object, error: BaseException | None) -> None:
            self._reading = False
            if revision == self._reference_revision:
                if error is not None:
                    self._error = str(error)
                    self.refresh()
                elif isinstance(value, DisplayFrame):
                    self._set_reference(value)
            if self._pending_reference:
                self._pending_reference = False
                self.refresh_frame()

        self._reading = True
        if not self._run_io(read, completed):
            self._reading = False
            self._error = text("preparing", self.language)
            self.refresh()

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
        for control in (self.folder, self.browse, self.loop):
            control.setEnabled(self._allowed() and not self._preparing)
        self.status.setText(
            text(
                "preparing"
                if self._preparing
                else "unsaved"
                if self.draft.dirty or (self.sources is not None and self.sources.dirty)
                else "saved",
                self.language,
            )
        )
        bounds_issue = ""
        if self.frame is not None:
            try:
                self.draft.camera(self.camera).validate_shape(
                    (self.frame.image.shape[0], self.frame.image.shape[1], 3)
                )
            except ValueError as exc:
                bounds_issue = str(exc)
        self.message.setText(
            self._error
            or bounds_issue
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
        self._preparing = True
        self._error = self._feedback = ""
        self.refresh()
        sources = self.sources.definition if self.sources is not None else None
        layout = self.draft.definition.layout
        base = self.sources.path.parent if self.sources is not None else None

        def validate() -> None:
            if sources is not None and base is not None:
                FolderReplay(layout, sources, base).next_group()

        def validated(_value: object, error: BaseException | None) -> None:
            self._preparing = False
            if error is not None or not self._allowed():
                self._error = str(error) if error else text("read_only", self.language)
                self.refresh()
                return
            saved_machine = False
            try:
                self.draft.save()
                saved_machine = True
                if self.sources is not None:
                    self.sources.save()
            except (ValueError, ConfigurationError) as exc:
                self._error = (
                    text("source_partial_save", self.language) + " "
                    if saved_machine
                    else ""
                ) + str(exc)
                self._changed()
                self.refresh()
                return
            self._preparing = True
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

        if self.sources is None:
            validated(None, None)
        elif self._run_io is None or not self._run_io(validate, validated):
            self._preparing = False
            self._error = text("preparing", self.language)
            self.refresh()

    def request_back(self, after: Callable[[], None] | None = None) -> None:
        if self._preparing or self._decision is not None:
            return

        def leave() -> None:
            self.frame = None
            self.canvas.set_frame(None)
            self._leave()
            if after is not None:
                after()

        if not self.draft.dirty and not (
            self.sources is not None and self.sources.dirty
        ):
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
                if self.sources is not None:
                    self.sources.discard()
                self._changed()
                leave()

        dialog.finished.connect(finished)
        dialog.open()
