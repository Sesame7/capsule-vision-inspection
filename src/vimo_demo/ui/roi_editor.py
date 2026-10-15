"""Canvas-first machine geometry workspace; no inference or activation ownership."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Literal

import numpy as np
from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QIcon,
    QPainter,
    QPen,
    QPixmap,
    QResizeEvent,
)
from PySide6.QtWidgets import (
    QButtonGroup,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QTabBar,
    QVBoxLayout,
    QWidget,
)

from vimo_demo.application.folder_replay import (
    FolderReplay,
    FolderSourceError,
    decode_image,
    folder_images,
)
from vimo_demo.configuration import ConfigurationError
from vimo_demo.inference import BoundingRect
from vimo_demo.inspection import CameraId, CycleId, FrameId

from .camera_source_dialog import CameraSourceDialog
from .display_frames import DisplayFrame, LatestDisplayFrameStore
from .production_style import Language, Style, camera_icon, text
from .recipe_editor import RecipeEditorSession
from .roi_canvas import RoiCanvas


def _image_icon(tool: str) -> QIcon:
    """Local vector tools; checked modes have a check mark, not just a color."""
    if tool == "camera":
        return camera_icon()
    icon = QIcon()
    for checked in (False, True):
        for dpr in (1.0, 1.25, 1.5, 2.0):
            pixmap = QPixmap(round(24 * dpr), round(24 * dpr))
            pixmap.setDevicePixelRatio(dpr)
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QPen(QColor(Style.primary), 1.6))
            if tool == "refresh":
                painter.drawArc(5, 5, 14, 14, 40 * 16, 290 * 16)
                painter.drawLine(19, 4, 19, 10)
                painter.drawLine(19, 10, 13, 10)
            elif tool == "folder":
                for x, y, dx, dy in (
                    (3, 8, 3, 5),
                    (3, 5, 10, 5),
                    (10, 5, 13, 8),
                    (13, 8, 21, 8),
                    (21, 8, 21, 19),
                    (21, 19, 3, 19),
                    (3, 19, 3, 8),
                ):
                    painter.drawLine(x, y, dx, dy)
            elif tool == "runtime":
                painter.drawRect(3, 4, 18, 13)
                painter.drawLine(9, 20, 15, 20)
                painter.drawLine(12, 17, 12, 20)
                painter.drawLine(9, 7, 16, 11)
                painter.drawLine(16, 11, 9, 14)
                painter.drawLine(9, 14, 9, 7)
            elif tool == "loop":
                painter.drawLine(4, 7, 20, 7)
                painter.drawLine(20, 7, 16, 3)
                painter.drawLine(20, 7, 16, 11)
                painter.drawLine(20, 17, 4, 17)
                painter.drawLine(4, 17, 8, 13)
                painter.drawLine(4, 17, 8, 21)
            elif tool == "apply":
                painter.drawLine(4, 12, 9, 17)
                painter.drawLine(9, 17, 20, 6)
            elif tool == "common":
                painter.drawRect(3, 3, 12, 12)
                painter.drawRect(8, 8, 12, 12)
            elif tool == "distribute":
                for x, height in ((3, 12), (10, 8), (17, 12)):
                    painter.drawRect(x, (24 - height) // 2, 4, height)
                painter.drawLine(3, 21, 21, 21)
                painter.drawLine(3, 19, 3, 23)
                painter.drawLine(21, 19, 21, 23)
            elif tool in ("previous", "next"):
                x, dx = (15, -7) if tool == "previous" else (9, 7)
                painter.drawLine(x, 5, x + dx, 12)
                painter.drawLine(x + dx, 12, x, 19)
            else:
                for x, y, dx, dy in (
                    (4, 4, 1, 1),
                    (20, 4, -1, 1),
                    (4, 20, 1, -1),
                    (20, 20, -1, -1),
                ):
                    painter.drawLine(x, y, x + dx * 5, y)
                    painter.drawLine(x, y, x, y + dy * 5)
            if checked and tool in ("folder", "runtime"):
                painter.fillRect(13, 13, 11, 11, QColor(Style.raised))
                painter.drawLine(14, 18, 17, 21)
                painter.drawLine(17, 21, 23, 15)
            painter.end()
            state = QIcon.State.On if checked else QIcon.State.Off
            icon.addPixmap(pixmap, QIcon.Mode.Normal, state)
            dimmed = QPixmap(pixmap.size())
            dimmed.setDevicePixelRatio(dpr)
            dimmed.fill(Qt.GlobalColor.transparent)
            muted = QPainter(dimmed)
            muted.setOpacity(0.35)
            muted.drawPixmap(0, 0, pixmap)
            muted.end()
            icon.addPixmap(dimmed, QIcon.Mode.Disabled, state)
    return icon


type ReferenceMode = Literal["folder", "runtime"]


@dataclass(frozen=True, slots=True)
class _FolderReference:
    frame: DisplayFrame | None
    paths: tuple[Path, ...]
    index: int
    error: str = ""


class _RoiToolbar(QWidget):
    """Align three compact groups; wrap whole groups without shrinking controls."""

    def __init__(self, leading: QWidget, center: QWidget, trailing: QWidget) -> None:
        super().__init__()
        self._groups = leading, center, trailing
        for group in self._groups:
            group.setSizePolicy(
                QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Preferred
            )
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(Style.space)
        self._grid.setVerticalSpacing(Style.small)
        # Equal flexible side columns center the middle group when both sides fit.
        self._grid.setColumnStretch(0, 1)
        self._grid.setColumnStretch(2, 1)
        self._rows = 0
        policy = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self._arrange(self.width())

    def _sizes(self) -> tuple[QSize, ...]:
        return tuple(
            group.minimumSizeHint().expandedTo(group.minimumSize())
            for group in self._groups
        )

    def _row_width(self) -> int:
        return sum(size.width() for size in self._sizes()) + 2 * Style.space

    def _row_count(self, width: int) -> int:
        if width >= self._row_width():
            return 1
        remaining = sum(size.width() for size in self._sizes()[1:])
        return 2 if width >= remaining + Style.space else 3

    def minimumSizeHint(self) -> QSize:
        sizes = self._sizes()
        return QSize(
            max(size.width() for size in sizes), max(size.height() for size in sizes)
        )

    def sizeHint(self) -> QSize:
        return QSize(self._row_width(), self.heightForWidth(self._row_width()))

    def heightForWidth(self, width: int) -> int:
        heights = [size.height() for size in self._sizes()]
        rows = self._row_count(width)
        if rows == 1:
            return max(heights)
        if rows == 2:
            return heights[0] + max(heights[1:]) + Style.small
        return sum(heights) + 2 * Style.small

    def sync_metrics(self) -> None:
        # Recompute after localized labels/status change; retain all camera tabs.
        for group in self._groups:
            layout = group.layout()
            if layout is not None:
                layout.invalidate()
                group.setMinimumWidth(layout.minimumSize().width())
            else:
                group.setMinimumWidth(group.sizeHint().width())
        self._arrange(self.width())

    def _arrange(self, width: int) -> None:
        rows = self._row_count(width)
        if rows != self._rows:
            for group in self._groups:
                self._grid.removeWidget(group)
            leading, center, trailing = self._groups
            left = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
            middle = Qt.AlignmentFlag.AlignCenter
            right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            if rows == 1:
                self._grid.addWidget(leading, 0, 0, left)
                self._grid.addWidget(center, 0, 1, middle)
                self._grid.addWidget(trailing, 0, 2, right)
            elif rows == 2:
                self._grid.addWidget(leading, 0, 0, 1, 3, left)
                self._grid.addWidget(center, 1, 1, middle)
                self._grid.addWidget(trailing, 1, 2, right)
            else:
                for row, (group, alignment) in enumerate(
                    zip(self._groups, (left, middle, right), strict=True)
                ):
                    self._grid.addWidget(group, row, 0, 1, 3, alignment)
            self._rows = rows
        required_height = self.heightForWidth(width)
        if self.minimumHeight() != required_height:
            self.setMinimumHeight(required_height)
        self.updateGeometry()

    def resizeEvent(self, event: QResizeEvent) -> None:
        self._arrange(event.size().width())
        super().resizeEvent(event)


class MachineRoiWorkspace(QWidget):
    sources_verified = Signal()

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
        # UI-session metadata only. No image history, replay state or disk persistence.
        self._folder_paths: dict[CameraId, tuple[Path, ...]] = {}
        self._positions: dict[CameraId, int] = {}
        self._reference_modes: dict[CameraId, ReferenceMode] = {}
        self.language: Language = language
        self.simulation = simulation
        self.frame: DisplayFrame | None = None
        self.selected_index = 0
        self._error = ""
        self._error_key = "vision_error"
        self._feedback = ""
        self._preparing = False
        self._save_failed = False
        self._decision: QMessageBox | None = None
        self.source_dialog: CameraSourceDialog | None = None
        self.buttons: dict[str, QPushButton] = {}
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(Style.small)
        self.camera_tabs = QTabBar()
        self.camera_tabs.setExpanding(False)
        self.camera_tabs.setDrawBase(False)
        self.camera_tabs.setStyleSheet(
            f"QTabBar::tab {{ padding: 12px 10px; min-height: 20px; "
            f"color: {Style.primary}; background: {Style.surface}; border: none; }} "
            f"QTabBar::tab:selected {{ color: {Style.accent}; background: {Style.raised}; }}"
        )
        self.camera_tabs.setIconSize(QSize(Style.icon, Style.icon))
        camera_icon = _image_icon("camera")
        for identity in self.draft.definition.layout.camera_ids:
            self.camera_tabs.addTab(camera_icon, str(identity).removeprefix("CAM"))
        self.camera_tabs.setCurrentIndex(
            self.draft.definition.layout.camera_ids.index(camera)
        )
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.folder = QLineEdit()
        self.browse = QPushButton()
        self.loop = QPushButton()
        self.loop.setCheckable(True)
        self.loop.setIcon(_image_icon("loop"))
        self.loop.setIconSize(QSize(Style.icon, Style.icon))
        self.source_notice = QLabel()
        self.source_notice.setWordWrap(True)
        self.source_settings = self.button("source_settings")
        source_row = QHBoxLayout()
        source_row.setSpacing(Style.small)
        source_row.addWidget(self.folder, 1)
        source_row.addWidget(self.browse)
        source_row.addWidget(self.loop)
        source_row.addWidget(self.source_settings)
        if self.sources is not None:
            self.folder.setMinimumHeight(Style.touch)
            self.browse.setFixedSize(Style.touch, Style.touch)
            self.browse.setIcon(_image_icon("folder"))
            self.browse.setIconSize(QSize(24, 24))
            self.loop.setMinimumHeight(Style.touch)
            source_row.addWidget(self.source_notice)
        else:
            for widget in (
                self.folder,
                self.browse,
                self.loop,
                self.source_notice,
                self.source_settings,
            ):
                widget.hide()  # Fake demo keeps its synthetic source; no real folder execution.
        self.folder.textEdited.connect(self.source_edited)
        self.folder.editingFinished.connect(self.refresh_frame)
        self.browse.clicked.connect(self.browse_folder)
        self.loop.toggled.connect(self.loop_changed)
        self.source_settings.clicked.connect(self.open_source_settings)
        reference_controls = QWidget()
        references = QHBoxLayout(reference_controls)
        references.setContentsMargins(0, 0, 0, 0)
        references.setSpacing(Style.small)
        self._reference_group = QButtonGroup(self)
        self._reference_group.setExclusive(True)
        self.reference_modes: dict[ReferenceMode, QPushButton] = {}
        modes: tuple[ReferenceMode, ...] = ("folder", "runtime")
        for mode in modes:
            button = QPushButton()
            button.setFixedSize(Style.touch, Style.touch)
            button.setIcon(_image_icon(mode))
            button.setIconSize(QSize(Style.icon, Style.icon))
            button.setCheckable(True)
            self._reference_group.addButton(button)

            def choose(_checked: bool = False, m: ReferenceMode = mode) -> None:
                self.set_reference_mode(m)

            button.clicked.connect(choose)
            self.reference_modes[mode] = button
            references.addWidget(button)
        for key, tool in (
            ("reference_previous", "previous"),
            ("reference_next", "next"),
        ):
            button = self.button(key)
            button.setText("")
            button.setFixedSize(Style.touch, Style.touch)
            button.setIcon(_image_icon(tool))
            button.setIconSize(QSize(24, 24))
            references.addWidget(button)
        self.reference = QLabel()
        self.reference.setTextFormat(Qt.TextFormat.PlainText)
        self.reference.setProperty("role", "secondary")
        references.addWidget(self.reference)
        references.addWidget(self.icon_button("roi_refresh", "refresh"))
        display_controls = QWidget()
        display = QHBoxLayout(display_controls)
        display.setContentsMargins(0, 0, 0, 0)
        display.setSpacing(Style.small)
        for key, symbol in (
            ("roi_fit", "⛶"),
            ("roi_actual", "1:1"),
        ):
            button = self.button(key)
            button.setText(symbol if key == "roi_actual" else "")
            if key != "roi_actual":
                button.setIcon(_image_icon("fit"))
                button.setIconSize(QSize(24, 24))
            button.setFixedSize(Style.touch, Style.touch)
            display.addWidget(button)
        self.reference_toolbar = _RoiToolbar(
            self.camera_tabs, reference_controls, display_controls
        )
        root.addWidget(self.reference_toolbar)
        if self.sources is not None:
            root.addLayout(source_row)
        self.canvas = RoiCanvas(self.editable)
        root.addWidget(self.canvas, 1)
        numeric, actions, saving = QWidget(), QWidget(), QWidget()
        geometry = QHBoxLayout(numeric)
        geometry.setContentsMargins(0, 0, 0, 0)
        geometry.setSpacing(Style.small)
        self.channel = QLabel()
        geometry.addWidget(self.channel)
        self.coordinates: dict[str, QLineEdit] = {}
        for key in ("X", "Y", "W", "H"):
            geometry.addWidget(QLabel(key))
            line = QLineEdit()
            line.setFixedWidth(72)
            line.setMinimumHeight(Style.touch)
            line.returnPressed.connect(self.apply_geometry)
            self.coordinates[key] = line
            geometry.addWidget(line)
        self.common_width, self.common_height = (
            self.coordinates["W"],
            self.coordinates["H"],
        )
        apply_geometry = self.button("roi_set_rect")
        apply_geometry.setIcon(_image_icon("apply"))
        apply_geometry.setIconSize(QSize(Style.icon, Style.icon))
        geometry.addWidget(apply_geometry)
        bulk = QHBoxLayout(actions)
        bulk.setContentsMargins(0, 0, 0, 0)
        bulk.setSpacing(Style.small)
        bulk.addWidget(self.icon_button("roi_common", "common"))
        bulk.addWidget(self.icon_button("roi_distribute", "distribute"))
        save_row = QHBoxLayout(saving)
        save_row.setContentsMargins(0, 0, 0, 0)
        save_row.setSpacing(Style.small)
        save_row.addWidget(self.status)
        primary = self.button("roi_save_apply")
        primary.setObjectName("start")
        save_row.addWidget(primary)
        self.geometry_toolbar = _RoiToolbar(numeric, actions, saving)
        root.addWidget(self.geometry_toolbar)
        self.message = QLabel()
        self.message.setWordWrap(True)
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        # Error text stays explicit; hidden when empty rather than reserving a prose row.
        root.addWidget(self.message)
        self.buttons["roi_fit"].clicked.connect(lambda: self.canvas.zoom(None))
        self.buttons["roi_actual"].clicked.connect(lambda: self.canvas.zoom(1))
        self.buttons["roi_refresh"].clicked.connect(self.refresh_frame)
        self.buttons["reference_previous"].clicked.connect(
            lambda: self.browse_reference(-1)
        )
        self.buttons["reference_next"].clicked.connect(lambda: self.browse_reference(1))
        self.buttons["roi_set_rect"].clicked.connect(self.apply_geometry)
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

    def icon_button(self, key: str, tool: str) -> QPushButton:
        button = self.button(key)
        button.setText("")
        button.setFixedSize(Style.touch, Style.touch)
        button.setIcon(_image_icon(tool))
        button.setIconSize(QSize(Style.icon, Style.icon))
        return button

    def set_language(self, language: Language) -> None:
        self.language = language
        self.folder.setAccessibleName(text("source_folder", language))
        self.folder.setPlaceholderText(text("source_folder", language))
        if self.source_dialog is not None:
            self.source_dialog.set_language(language)
        self.browse.setAccessibleName(text("browse", language))
        self.browse.setToolTip(text("browse", language))
        self.loop.setToolTip(text("source_loop", language))
        self.source_notice.setText(text("cli_sources_short", language))
        self.source_notice.setToolTip(text("cli_sources", language))
        self.source_notice.setAccessibleName(text("cli_sources", language))
        self.source_notice.setVisible(self.session.cli_sources)
        for mode, button in self.reference_modes.items():
            button.setText("")
            button.setAccessibleName(text("reference_" + mode, language))
            button.setToolTip(text("reference_" + mode + "_tip", language))
        self.camera_tabs.setAccessibleName(text("camera_setup", language))
        for index in range(self.camera_tabs.count()):
            name = text("camera_number", language).format(
                number=self.camera_tabs.tabText(index)
            )
            self.camera_tabs.setTabToolTip(index, name)
            self.camera_tabs.setAccessibleTabName(index, name)
        for key, button in self.buttons.items():
            button.setAccessibleName(text(key, language))
            button.setToolTip(text(key, language))
            if key not in (
                "roi_fit",
                "roi_actual",
                "roi_refresh",
                "reference_previous",
                "reference_next",
                "roi_common",
                "roi_distribute",
            ):
                button.setText(text(key, language))
        numeric_apply = self.buttons["roi_set_rect"]
        numeric_apply.setMinimumWidth(
            numeric_apply.fontMetrics().horizontalAdvance(numeric_apply.text())
            + Style.icon
            + 2 * Style.space
            + Style.small
        )
        for key, line in self.coordinates.items():
            line.setAccessibleName(text("roi_" + key.lower(), language))
            line.setToolTip(text("roi_" + key.lower(), language))
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
        self._request_reference(reload=False)

    def source_edited(self, folder: str) -> None:
        if not self._allowed() or self.sources is None:
            return
        previous_key = self.sources.definition.camera(self.camera).key(
            self.sources.path.parent
        )
        self.sources.set_folder(self.camera, folder)
        if (
            self.sources.definition.camera(self.camera).key(self.sources.path.parent)
            != previous_key
        ):
            self._folder_paths.pop(self.camera, None)
            self._positions.pop(self.camera, None)
            self._reference_modes[self.camera] = "folder"
        self._save_failed = False
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

    def open_source_settings(self) -> None:
        if (
            not self._allowed()
            or self._preparing
            or self.sources is None
            or self._run_io is None
        ):
            return
        if self.source_dialog is None:
            self.source_dialog = CameraSourceDialog(
                self.sources,
                self.draft.definition.layout.camera_ids,
                self._allowed,
                self._run_io,
                language=self.language,
                parent=self,
            )
            self.source_dialog.accepted.connect(self._source_identity_changed)
        self.source_dialog.begin()

    def _source_identity_changed(self) -> None:
        self._save_failed = False
        if self.reference_mode == "runtime" and self._runtime_frame() is None:
            self._reference_modes[self.camera] = "folder"
            self._request_reference(reload=False)
        self._changed()
        self.refresh()

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
                self._report_error(exc, "roi_geometry_error")
                self.refresh()  # Visible incompatible geometry; never rescale/repair it.

    def _runtime_frame(self) -> DisplayFrame | None:
        source = (
            self.sources.definition.camera(self.camera)
            if self.sources is not None
            else None
        )
        key = (
            self.sources.definition.source_key(self.camera, self.sources.path.parent)
            if source is not None and self.sources is not None
            else None
        )
        return next(
            (
                f
                for f in self._frames.snapshot().frames
                if f.frame_id.camera_id == self.camera
                and (source is None or (key and f.source_key == key))
            ),
            None,
        )

    @property
    def reference_mode(self) -> ReferenceMode:
        if self.camera not in self._reference_modes:
            self._reference_modes[self.camera] = (
                "runtime" if self._runtime_frame() is not None else "folder"
            )
        return self._reference_modes[self.camera]

    def set_reference_mode(self, mode: ReferenceMode) -> None:
        if self._reading or self._preparing:
            self.refresh()  # Restore selected styling if a checkable control was clicked.
            return
        if mode == "runtime" and self._runtime_frame() is None:
            self.refresh()
            return
        if mode == "folder" and self.sources is None:
            return
        self._reference_modes[self.camera] = mode
        self._request_reference(reload=False)

    def browse_reference(self, delta: int) -> None:
        if self.reference_mode != "folder" or self._reading or self._preparing:
            return
        paths = self._folder_paths.get(self.camera, ())
        index = self._positions.get(self.camera, 0) + delta
        if not 0 <= index < len(paths):
            return  # No implicit wrap, decode, or geometry changes at a boundary.
        self._positions[self.camera] = index
        self._request_reference(reload=False)

    def refresh_frame(self) -> None:
        self._request_reference(reload=True)

    def _request_reference(self, *, reload: bool) -> None:
        self._reference_revision += 1
        if self._reading:
            self._pending_reference = True
            self._set_reference(None)
            return
        if self._preparing:
            return
        self._error = ""
        if self.reference_mode == "runtime":
            frame = self._runtime_frame()
            self._set_reference(frame)
            if frame is None:
                self._report_error(
                    text("reference_unavailable", self.language),
                    "reference_unavailable",
                )
                self.refresh()
            return
        self._set_reference(None)
        if self.sources is None or self._run_io is None:
            return
        source = self.sources.definition.camera(self.camera)
        if not source.folder:
            return
        camera, revision = self.camera, self._reference_revision
        key = source.key(self.sources.path.parent)
        folder = source.path(self.sources.path.parent)
        cached = self._folder_paths.get(camera, ())
        index = self._positions.get(camera, 0)
        current = cached[index] if 0 <= index < len(cached) else None

        def read() -> _FolderReference:
            paths = folder_images(folder) if reload or not cached else cached
            position = index
            if reload and current is not None:
                if current not in paths:
                    raise ValueError(f"Reference image no longer exists: {current}")
                position = paths.index(current)  # Refresh is never Next.
            try:
                image = decode_image(paths[position])
            except FolderSourceError as exc:
                return _FolderReference(None, paths, position, str(exc))
            owned = np.frombuffer(image.tobytes(), dtype=np.uint8).reshape(image.shape)
            frame = DisplayFrame(
                FrameId(CycleId(0), camera, position), 0, monotonic(), 0, owned, key
            )
            return _FolderReference(frame, paths, position)

        def completed(value: object, error: BaseException | None) -> None:
            self._reading = False
            if revision == self._reference_revision:
                if error is not None:
                    self._report_error(error, "folder_reference_error")
                    self.refresh()
                elif isinstance(value, _FolderReference):
                    self._folder_paths[camera] = value.paths
                    self._positions[camera] = value.index
                    self._set_reference(value.frame)
                    if value.error:
                        self._report_error(value.error, "folder_reference_error")
                        self.refresh()
            if self._pending_reference:
                self._pending_reference = False
                self._request_reference(reload=False)

        self._reading = True
        if not self._run_io(read, completed):
            self._reading = False
            self._report_error(text("preparing", self.language), "preparing")
        self.refresh()

    def select_roi(self, index: int) -> None:
        self.canvas.cancel_gesture()
        self.selected_index = index
        self._error = ""
        self.sync_size()
        self.refresh()

    def sync_size(self) -> None:
        rect = self.draft.camera(self.camera).slots[self.selected_index].rect
        for key, value in zip(
            ("X", "Y", "W", "H"), (rect.x, rect.y, rect.width, rect.height), strict=True
        ):
            self.coordinates[key].setText(str(value))

    def refresh(self) -> None:
        self.canvas.rois = self.draft.camera(self.camera).slots
        slot = self.canvas.rois[self.selected_index]
        rect = slot.rect
        self.canvas.model.rect = rect
        self.canvas.model.editable = self.editable()
        if not self.canvas.model.can_drag:
            self.canvas.cancel_roi_drag()
        self.channel.setText(f"CH{slot.channel_id:02d}")
        for line in self.coordinates.values():
            line.setReadOnly(not self.editable())
        for key in ("roi_common", "roi_distribute", "roi_set_rect"):
            self.buttons[key].setEnabled(self.editable())
        self.camera_tabs.setEnabled(not self._preparing)
        viewing = not self._reading and not self._preparing
        paths = self._folder_paths.get(self.camera, ())
        index = self._positions.get(self.camera, 0)
        for mode, button in self.reference_modes.items():
            button.setChecked(self.reference_mode == mode)
            button.setEnabled(
                viewing
                and (
                    self.sources is not None
                    if mode == "folder"
                    else self._runtime_frame() is not None
                )
            )
        if self._runtime_frame() is None:
            self.reference_modes["runtime"].setToolTip(
                text("reference_unavailable", self.language)
            )
        else:
            self.reference_modes["runtime"].setToolTip(
                text("reference_runtime_tip", self.language)
            )
        self.buttons["reference_previous"].setEnabled(
            viewing and self.reference_mode == "folder" and index > 0
        )
        self.buttons["reference_next"].setEnabled(
            viewing and self.reference_mode == "folder" and index + 1 < len(paths)
        )
        self.buttons["roi_refresh"].setEnabled(viewing)
        self.buttons["roi_save_apply"].setEnabled(
            self._allowed()
            and not self._preparing
            and not self.session.dirty
            and self.session.entry is not None
        )
        for control in (self.folder, self.browse, self.source_settings):
            control.setEnabled(self._allowed() and not self._preparing)
        self.loop.setEnabled(
            self._allowed()
            and not self._preparing
            and (self.sources is None or self.sources.definition.mode == "folder")
        )
        loop_state = text(
            "source_loop_on" if self.loop.isChecked() else "source_loop_off",
            self.language,
        )
        self.loop.setText(loop_state)
        self.loop.setAccessibleName(
            text("source_loop", self.language) + " · " + loop_state
        )
        self.status.setText(
            text(
                "setup_saving"
                if self._preparing
                else "setup_failed"
                if self._save_failed
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
            (text(self._error_key, self.language) if self._error else "")
            or (text("roi_geometry_error", self.language) if bounds_issue else "")
            or (text("roi_recipe_save", self.language) if self.session.dirty else "")
            or (
                text("read_only", self.language)
                if not self._allowed() and not self._preparing
                else ""
            )
        )
        self.message.setToolTip(self._error or bounds_issue)
        self.message.setVisible(bool(self.message.text()))
        self.status.setToolTip(
            text("roi_applied", self.language) if self._feedback else ""
        )
        position = (
            f"{index + 1}/{len(paths)}"
            if paths and self.reference_mode == "folder"
            else text("reference_runtime", self.language)
            if self.reference_mode == "runtime"
            else ""
        )
        info = (
            (position + " · " if position else "") + text("roi_no_frame", self.language)
            if self.frame is None
            else f"{position} · {self.frame.image.shape[1]}×{self.frame.image.shape[0]}"
        )
        self.reference.setToolTip(
            str(paths[index])
            if paths and index < len(paths) and self.reference_mode == "folder"
            else text("reference_runtime_tip", self.language)
        )
        self.reference.setText(
            (text("simulation", self.language) + " · " if self.simulation else "")
            + info
        )
        self.reference_toolbar.sync_metrics()
        self.geometry_toolbar.sync_metrics()
        self.canvas.update()

    def operation(self, action: Callable[[], None]) -> bool:
        if not self.editable():
            return False
        try:
            action()
            self._error = self._feedback = ""
            self._save_failed = False
            self.sync_size()
            self._changed()
        except (ValueError, ConfigurationError) as exc:
            self._report_error(exc, "roi_geometry_error")
        self.refresh()
        return not self._error

    def _report_error(
        self, error: BaseException | str, key: str = "vision_error"
    ) -> None:
        self._error, self._error_key = str(error), key

    def set_rect(self, rect: BoundingRect) -> None:
        rects = [s.rect for s in self.draft.camera(self.camera).slots]
        rects[self.selected_index] = rect
        self.draft.replace_rects(self.camera, tuple(rects))

    def commit_geometry(self, value: object) -> None:
        if isinstance(value, BoundingRect) and self.canvas.model.can_drag:
            self.operation(lambda: self.set_rect(value))

    def apply_geometry(self) -> None:
        self.operation(
            lambda: self.set_rect(
                BoundingRect(
                    *(int(self.coordinates[key].text()) for key in ("X", "Y", "W", "H"))
                )
            )
        )

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
            self._report_error(
                text("roi_recipe_save", self.language), "roi_recipe_save"
            )
            self.refresh()
            return
        self._preparing = True
        self._save_failed = False
        self._error = self._feedback = ""
        self.refresh()
        sources = self.sources.definition if self.sources is not None else None
        layout = self.draft.definition.layout
        base = self.sources.path.parent if self.sources is not None else None

        def validate() -> None:
            if sources is not None and base is not None:
                sources.validate_cameras(layout.camera_ids)
                FolderReplay(layout, sources, base).next_group()

        def validated(_value: object, error: BaseException | None) -> None:
            self._preparing = False
            if error is not None or not self._allowed():
                if error is not None:
                    self._report_error(
                        error,
                        "source_validation_error"
                        if isinstance(error, FolderSourceError)
                        else "vision_validation_error",
                    )
                else:
                    self._report_error(text("read_only", self.language), "read_only")
                self._save_failed = True
                self.refresh()
                return
            saved_machine = False
            try:
                self.draft.save()
                saved_machine = True
                if self.sources is not None:
                    self.sources.save()
            except (ValueError, ConfigurationError) as exc:
                self._report_error(
                    exc, "source_partial_save" if saved_machine else "vision_save_error"
                )
                self._save_failed = True
                self._changed()
                self.refresh()
                return
            self._preparing = True
            self._changed()
            self.refresh()

            def prepared(error: str) -> None:
                self._preparing = False
                self._report_error(error, "recipe_prepare_error")
                self._save_failed = bool(error)
                self._feedback = "" if error else "roi_applied"
                self.refresh()
                if not error and self.sources is not None:
                    # All folders/bounds were checked and saved, and Apply succeeded.
                    self.sources_verified.emit()
                if not error and after is not None:
                    after()

            self._apply(prepared)

        if self.sources is None:
            validated(None, None)
        elif self._run_io is None or not self._run_io(validate, validated):
            self._preparing = False
            self._report_error(text("preparing", self.language), "preparing")
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
