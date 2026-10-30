"""Canvas-first machine geometry workspace; no inference or activation ownership."""

from collections.abc import Callable
from concurrent.futures import CancelledError, Future
from dataclasses import dataclass, replace
from pathlib import Path
from time import monotonic
from typing import Literal, cast

import numpy as np
from PySide6.QtCore import QRectF, QSize, Qt, Signal, Slot
from PySide6.QtGui import (
    QColor,
    QPainter,
    QPaintEvent,
    QPen,
    QResizeEvent,
    QStandardItemModel,
)
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from capsule_vision.application.basler import BaslerDevice, discover_basler_devices
from capsule_vision.application.basler_connection import (
    CameraSnapshot,
    ConnectionState,
    SourceConnectionCommands,
)
from capsule_vision.application.editing import SavedVision
from capsule_vision.application.folder_replay import (
    FolderSourceError,
    decode_image,
    folder_images,
)
from capsule_vision.configuration import (
    CameraFolder,
    ConfigurationError,
)
from capsule_vision.display import DisplayFrame, LatestDisplayFrameStore
from capsule_vision.inference import BoundingRect
from capsule_vision.inspection import CameraId, CycleId, FrameId
from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import Language

from .editing import EditingCoordinator
from .future_delivery import FutureDelivery
from .image_tools import ImageToolbar, image_icon
from .production_presentation import Indicator
from .roi_canvas import RoiCanvas
from .style import (
    Style,
    button_width,
    camera_icon,
    indicator_icon,
    production_icon,
    set_button_caption,
    theme_colors,
)

type ReferenceMode = Literal["folder", "runtime", "import", "capture"]


class _SourceModeSwitch(QFrame):
    """One painted track/thumb; child buttons retain native keyboard/accessibility."""

    def paintEvent(self, event: QPaintEvent) -> None:
        colors = theme_colors(self)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(colors.border), 1))
        painter.setBrush(QColor(colors.background))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 24, 24)
        for button in self.findChildren(QPushButton):
            if button.isChecked():
                painter.setBrush(QColor(colors.selected))
                painter.setPen(
                    QPen(
                        QColor(colors.accent if button.isEnabled() else colors.border),
                        1,
                    )
                )
                painter.drawRoundedRect(QRectF(button.geometry()), 20, 20)
                break


@dataclass(frozen=True, slots=True)
class _FolderReference:
    frame: DisplayFrame | None
    paths: tuple[Path, ...]
    index: int
    error: str = ""


class _SourceCard(QPushButton):
    """The card selects; its child editors never select or connect a camera."""

    def __init__(self) -> None:
        super().__init__()
        self.title = self.detail = self.summary = ""
        self.status_color = ""
        self.setCheckable(True)
        self.setMinimumWidth(180)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAutoDefault(False)
        self.setObjectName("visionSourceCard")
        contents = QVBoxLayout(self)
        contents.setContentsMargins(10, 34, 10, 8)
        contents.setSpacing(Style.small)
        folder = QHBoxLayout()
        folder.setSpacing(Style.small)
        self.serial_row = QWidget()
        serial = QHBoxLayout(self.serial_row)
        serial.setContentsMargins(0, 0, 0, 0)
        serial.setSpacing(Style.small)
        self.device = QComboBox()
        self.device.setEditable(False)
        self.device.setMinimumContentsLength(12)
        self.device.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.device.setMinimumHeight(Style.touch)
        self.device.setObjectName("visionSourceValue")
        serial.addWidget(self.device, 1)
        folder.addWidget(self.serial_row)
        self.serial_row.hide()
        self.folder_row = QWidget()
        fields = QHBoxLayout(self.folder_row)
        fields.setContentsMargins(0, 0, 0, 0)
        fields.setSpacing(Style.small)
        self.folder = QLineEdit()
        self.folder.setMinimumHeight(Style.touch)
        self.folder.setMinimumWidth(0)
        self.folder.setObjectName("visionSourceValue")
        fields.addWidget(self.folder, 1)
        self.browse = QPushButton()
        self.browse.setFixedSize(Style.touch, Style.touch)
        self.browse.setIconSize(QSize(Style.icon, Style.icon))
        fields.addWidget(self.browse)
        folder.addWidget(self.folder_row, 1)
        self.configuration = QPushButton()
        self.configuration.setFixedSize(Style.touch, Style.touch)
        self.configuration.setIconSize(QSize(Style.icon, Style.icon))
        folder.addWidget(self.configuration)
        self.configuration.hide()
        contents.addLayout(folder)

    def sizeHint(self) -> QSize:
        return QSize(360, 90)

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        colors = theme_colors(self)
        painter = QPainter(self)
        color = (
            colors.secondary
            if not self.isEnabled()
            else colors.accent
            if self.isChecked()
            else colors.primary
        )
        camera_icon(color).paint(painter, 14, 13, Style.icon, Style.icon)
        metrics = self.fontMetrics()
        summary_width = min(
            metrics.horizontalAdvance(self.summary) + 4, self.width() // 3
        )
        if self.status_color:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(self.status_color))
            painter.drawEllipse(self.width() - 24, 16, 8, 8)
        painter.setPen(QColor(colors.secondary))
        painter.drawText(
            self.width() - (34 if self.status_color else 14) - summary_width,
            6,
            summary_width,
            28,
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            metrics.elidedText(
                self.summary, Qt.TextElideMode.ElideRight, summary_width
            ),
        )
        painter.setPen(QColor(color))
        font = painter.font()
        font.setBold(True)
        painter.setFont(font)
        title_width = max(0, self.width() - 70 - summary_width)
        painter.drawText(
            44,
            6,
            title_width,
            28,
            Qt.AlignmentFlag.AlignVCenter,
            metrics.elidedText(self.title, Qt.TextElideMode.ElideRight, title_width),
        )
        painter.end()


class _SourceCards(QWidget):
    """Reflow whole cards by usable width, retaining configured row-major order."""

    def __init__(self) -> None:
        super().__init__()
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(Style.small)
        self._cards: list[_SourceCard] = []
        self.columns = 2

    def add_card(self, card: _SourceCard) -> None:
        self._cards.append(card)
        index = len(self._cards) - 1
        self.grid.addWidget(card, index // self.columns, index % self.columns)
        for column in range(self.columns):
            self.grid.setColumnStretch(column, 1)
        self._sync_height()

    def _sync_height(self) -> None:
        rows = (len(self._cards) + self.columns - 1) // self.columns
        height = max(
            (
                c.sizeHint().expandedTo(c.minimumSizeHint()).height()
                for c in self._cards
            ),
            default=0,
        )
        self.setFixedHeight(rows * height + max(0, rows - 1) * Style.small)

    def resizeEvent(self, event: QResizeEvent) -> None:
        # Three cards need useful space for a Windows path or model + serial.
        columns = 3 if event.size().width() >= 3 * 360 + 2 * Style.small else 2
        if columns != self.columns:
            self.columns = columns
            for card in self._cards:
                self.grid.removeWidget(card)
            for column in range(3):
                self.grid.setColumnStretch(column, 1 if column < columns else 0)
            for index, card in enumerate(self._cards):
                self.grid.addWidget(card, index // columns, index % columns)
            self.grid.invalidate()
            self._sync_height()
            self.updateGeometry()
        super().resizeEvent(event)


class VisionPage(QWidget):
    sources_verified = Signal()
    feedback_changed = Signal()
    setup_saved = Signal(bool, bool)

    def __init__(
        self,
        editing: EditingCoordinator,
        camera: CameraId,
        frames: LatestDisplayFrameStore,
        leave: Callable[[], None],
        *,
        language: Language,
        parent: QWidget,
        connection: SourceConnectionCommands | None = None,
    ) -> None:
        super().__init__(parent)
        session = editing.session
        assert session is not None
        self.editing = editing
        self.session, self.draft = session, session.machine_editor
        self._frames, self._allowed, self._changed, self._apply = (
            frames,
            editing.editable,
            editing.changed.emit,
            editing.apply,
        )
        self._leave, self._run_io = leave, editing.run_io
        self.sources = session.camera_sources
        self.connection = connection
        self._hardware_bounds: set[CameraId] = set()
        self._reference_revision = 0
        self._reading = False
        self._pending_reference = False
        # UI-session metadata only. No image history, replay state or disk persistence.
        self._folder_paths: dict[CameraId, tuple[Path, ...]] = {}
        self._positions: dict[CameraId, int] = {}
        self._reference_modes: dict[CameraId, ReferenceMode] = {}
        self._selector_key: tuple[bool, tuple[Path, ...], ReferenceMode] | None = None
        self._selector_index = -1
        self.language: Language = language
        self.frame: DisplayFrame | None = None
        self.selected_index = 0
        self._error = ""
        self._error_key = "vision_error"
        self._bounds_issue = ""
        self._last_feedback: tuple[str, str] | None = None
        self._feedback = ""
        self._preparing = False
        self._reloading = False
        self._save_failed = False
        self._decision: QMessageBox | None = None
        self._devices: tuple[BaslerDevice, ...] = ()
        self._discovering = False
        self._capture_future: Future[CameraSnapshot] | None = None
        self._capture_token = 0
        self._capture_timeout = False
        self._capture_delivery = FutureDelivery()
        self._capture_delivery.emitter.completed.connect(
            self._capture_finished, Qt.ConnectionType.QueuedConnection
        )
        self.buttons: dict[str, QPushButton] = {}
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(Style.space)
        self.source_area = QFrame()
        self.source_area.setObjectName("visionSources")
        source_layout = QVBoxLayout(self.source_area)
        source_layout.setContentsMargins(10, 10, 10, 10)
        source_layout.setSpacing(Style.small)
        root.addWidget(self.source_area)
        self._cameras = QButtonGroup(self)
        self.camera_cards: dict[CameraId, _SourceCard] = {}
        self.source_cards = _SourceCards()
        positions = self.draft.camera_ids
        if CameraId("CAM6") not in positions:
            positions = (*positions, CameraId("CAM6"))
        for index, identity in enumerate(positions):
            card = _SourceCard()
            self._cameras.addButton(card, index)
            self.camera_cards[identity] = card
            self.source_cards.add_card(card)
            card.setChecked(identity == camera)

            def edit_folder(value: str, target: CameraId = identity) -> None:
                self.source_edited(value, target)

            def finish_folder(
                target: CameraId = identity, field: QLineEdit = card.folder
            ) -> None:
                modified = field.isModified()
                field.setModified(False)
                if modified and target == self.camera:
                    self.refresh_frame()

            def browse(_checked: bool = False, target: CameraId = identity) -> None:
                self.browse_folder(target)

            card.folder.textEdited.connect(edit_folder)
            card.folder.editingFinished.connect(finish_folder)
            card.browse.clicked.connect(browse)

            def select_device(index: int, target: CameraId = identity) -> None:
                self.select_device(target, index)

            card.device.activated.connect(select_device)
            card.configuration.clicked.connect(self.request_camera_configuration)
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setFixedSize(24, Style.touch)
        self.loop = QPushButton()
        self.loop.setCheckable(True)
        self.loop.setIcon(image_icon("loop"))
        self.loop.setIconSize(QSize(Style.icon, Style.icon))
        self.loop.setFixedSize(Style.touch, Style.touch)
        self.discover = self.icon_button("source_discover", "discover")
        self.discover.clicked.connect(self.discover_devices)
        self.playback_options = QWidget()
        playback_options = QHBoxLayout(self.playback_options)
        playback_options.setContentsMargins(0, 0, 0, 0)
        playback_options.setSpacing(Style.small)
        playback_options.addWidget(self.loop)
        playback_options.addWidget(QLabel("FPS"))
        self.fps_stepper = QFrame()
        self.fps_stepper.setObjectName("visionFps")
        stepper = QHBoxLayout(self.fps_stepper)
        stepper.setContentsMargins(0, 0, 0, 0)
        stepper.setSpacing(0)
        self.fps_decrease, self.fps_increase = QPushButton(), QPushButton()
        self.fps = QLineEdit()
        self.fps.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.fps.setInputMethodHints(Qt.InputMethodHint.ImhDigitsOnly)
        self.fps.setMinimumHeight(Style.touch)
        self.fps.setFixedWidth(64)
        for button in (self.fps_decrease, self.fps_increase):
            button.setFixedSize(Style.touch, Style.touch)
            button.setIconSize(QSize(Style.icon, Style.icon))
        stepper.addWidget(self.fps_decrease)
        stepper.addWidget(self.fps)
        stepper.addWidget(self.fps_increase)
        playback_options.addWidget(self.fps_stepper)
        self.loop.toggled.connect(self.loop_changed)
        self.fps.editingFinished.connect(self.fps_changed)
        self.fps_decrease.clicked.connect(lambda: self._step_fps(-1))
        self.fps_increase.clicked.connect(lambda: self._step_fps(1))
        mode_row = QHBoxLayout()
        mode_row.setSpacing(Style.small)
        self._source_modes = QButtonGroup(self)
        self.mode_switch = _SourceModeSwitch()
        self.mode_switch.setObjectName("visionModeSwitch")
        switch = QHBoxLayout(self.mode_switch)
        switch.setContentsMargins(3, 3, 3, 3)
        switch.setSpacing(2)
        self.source_modes: dict[Literal["folder", "basler"], QPushButton] = {}
        source_modes: tuple[Literal["folder", "basler"], ...] = ("folder", "basler")
        for mode in source_modes:
            button = QPushButton()
            button.setCheckable(True)
            button.setFixedSize(54, Style.touch)
            button.setIconSize(QSize(Style.icon, Style.icon))

            def repaint_switch(_checked: bool) -> None:
                self.mode_switch.update()

            button.toggled.connect(repaint_switch)
            self._source_modes.addButton(button)
            self.source_modes[mode] = button

            def choose_source(
                _checked: bool = False, m: Literal["folder", "basler"] = mode
            ) -> None:
                self.set_source_mode(m)

            button.clicked.connect(choose_source)
            switch.addWidget(button)
        mode_row.addWidget(self.mode_switch)
        mode_row.addStretch(1)
        mode_row.addWidget(self.playback_options)
        # Visibility is set before mode_row is attached: own these as children
        # now so they can never briefly show as native top-level windows.
        self.connection_status = QLabel(self.source_area)
        self.connect_source = QPushButton(self.source_area)
        self.disconnect_source = QPushButton(self.source_area)
        for widget in (
            self.connection_status,
            self.connect_source,
            self.disconnect_source,
        ):
            mode_row.addWidget(widget)
            widget.setVisible(connection is not None)
        for button in (self.connect_source, self.disconnect_source):
            button.setFixedSize(Style.touch, Style.touch)
            button.setIconSize(QSize(Style.icon, Style.icon))
        self.connect_source.clicked.connect(
            lambda: self.connection_operation(connect=True)
        )
        self.disconnect_source.clicked.connect(
            lambda: self.connection_operation(connect=False)
        )
        mode_row.addWidget(self.discover)
        source_layout.addLayout(mode_row)
        source_layout.addWidget(self.source_cards)
        self.loop.setMinimumHeight(Style.touch)
        if self.sources is None:
            for widget in (
                self.discover,
                self.playback_options,
            ):
                widget.hide()  # Fake demo keeps its synthetic source; no real folder execution.
        self.roi_area = QFrame()
        self.roi_area.setObjectName("visionRoi")
        roi = QVBoxLayout(self.roi_area)
        roi.setContentsMargins(10, 10, 10, 10)
        roi.setSpacing(4)
        root.addWidget(self.roi_area, 1)
        reference_controls = QWidget()
        references = QHBoxLayout(reference_controls)
        references.setContentsMargins(0, 0, 0, 0)
        references.setSpacing(Style.small)
        references.addWidget(self.icon_button("vision_import", "folder"))
        references.addWidget(self.icon_button("vision_runtime", "runtime"))
        self.capture = self.icon_button("vision_capture", "capture")
        self.cancel_capture_button = self.icon_button("cancel", "cancel")
        references.addWidget(self.capture)
        references.addWidget(self.cancel_capture_button)
        self.capture.clicked.connect(self.capture_image)
        self.cancel_capture_button.clicked.connect(self.cancel_capture)
        self.buttons["vision_import"].clicked.connect(self.import_image)
        self.buttons["vision_runtime"].clicked.connect(
            lambda: self.set_reference_mode("runtime")
        )
        for key, tool in (
            ("reference_previous", "previous"),
            ("reference_next", "next"),
        ):
            button = self.button(key)
            button.setText("")
            button.setFixedSize(Style.touch, Style.touch)
            button.setIcon(image_icon(tool))
            button.setIconSize(QSize(24, 24))
            references.addWidget(button)
        self.reference = QComboBox()
        self.reference.setMinimumHeight(Style.touch)
        self.reference.setMinimumContentsLength(16)
        self.reference.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.reference.setMaximumWidth(360)
        self.reference.activated.connect(self.choose_image)
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
                button.setIcon(image_icon("fit"))
                button.setIconSize(QSize(24, 24))
            button.setFixedSize(Style.touch, Style.touch)
            display.addWidget(button)
        self.reference_toolbar = ImageToolbar(
            reference_controls, self.reference, display_controls
        )
        roi.addWidget(self.reference_toolbar)
        self.canvas = RoiCanvas(self.editable)
        roi.addWidget(self.canvas, 1)
        numeric, actions, saving = QWidget(), QWidget(), QWidget()
        geometry = QHBoxLayout(numeric)
        geometry.setContentsMargins(0, 0, 0, 0)
        # Spend label/field gaps on localized actions; keep coordinate fields intact.
        geometry.setSpacing(Style.small // 2)
        self.channel = QComboBox()
        self.channel.setMinimumHeight(Style.touch)
        self.channel.activated.connect(self.select_roi)
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
        apply_geometry = self.icon_button("roi_set_rect", "apply")
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
        save_row.addWidget(self.icon_button("vision_discard", "refresh"))
        primary = self.button("save_apply")
        primary.setObjectName("start")
        primary.setProperty("compactAction", True)
        save_row.addWidget(self.icon_button("save", "save"))
        save_row.addWidget(primary)
        self.geometry_toolbar = ImageToolbar(numeric, actions, saving)
        roi.addWidget(self.geometry_toolbar)
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
        self.buttons["save"].clicked.connect(lambda: self.save_setup())
        self.buttons["save_apply"].clicked.connect(lambda: self.save_and_apply())
        self.buttons["vision_discard"].clicked.connect(self.request_discard)
        self._cameras.idToggled.connect(self._camera_selected)
        self.canvas.selected.connect(self.select_roi)
        self.canvas.geometry_changed.connect(self.commit_geometry)
        self.set_language(language)
        self.switch_camera()

    @property
    def camera(self) -> CameraId:
        return next(
            camera for camera, card in self.camera_cards.items() if card.isChecked()
        )

    def request_camera_configuration(self) -> None:
        if (
            not self.source_editable()
            or self.sources is None
            or self.session.cli_sources
        ):
            return
        configured = CameraId("CAM6") in self.draft.camera_ids
        if configured:
            dialog = QMessageBox(self)
            dialog.setWindowTitle(text("vision_remove_cam6", self.language))
            dialog.setText(text("vision_remove_cam6_question", self.language))
            remove = dialog.addButton(
                text("vision_remove_cam6", self.language),
                QMessageBox.ButtonRole.DestructiveRole,
            )
            cancel = dialog.addButton(
                text("cancel", self.language), QMessageBox.ButtonRole.RejectRole
            )
            dialog.setDefaultButton(cancel)
            self._decision = dialog

            def decided(_result: int) -> None:
                chosen = dialog.clickedButton()
                self._decision = None
                dialog.deleteLater()
                if chosen is remove and self.source_editable():
                    self.change_camera_configuration()

            dialog.finished.connect(decided)
            dialog.open()
        else:
            self.change_camera_configuration(restore=True)
        self.refresh()

    def change_camera_configuration(self, *, restore: bool = False) -> None:
        """Confirmed stopped/disconnected draft edit; persistence remains Save/apply."""
        if (
            not self.source_editable()
            or self.sources is None
            or self.session.cli_sources
        ):
            return
        try:
            if not restore:
                self.draft.remove_camera_six()
                cameras = tuple(
                    row
                    for row in self.sources.definition.cameras
                    if row.camera_id != "CAM6"
                )
            else:
                self.draft.restore_camera_six()
                cameras = (
                    *(
                        row
                        for row in self.sources.definition.cameras
                        if row.camera_id != "CAM6"
                    ),
                    CameraFolder(CameraId("CAM6"), ""),
                )
            self.sources.definition = replace(self.sources.definition, cameras=cameras)
            self.draft.bounds.pop(CameraId("CAM6"), None)
            self._folder_paths.pop(CameraId("CAM6"), None)
            self._positions.pop(CameraId("CAM6"), None)
            self._reference_modes.pop(CameraId("CAM6"), None)
            self._populate_devices()
            self._sync_configured_camera()
            self._changed()
            self.refresh()
        except (ValueError, ConfigurationError) as exc:
            self._report_error(exc, "vision_validation_error")
            self.refresh()

    def _sync_configured_camera(self) -> None:
        cameras = self.draft.camera_ids
        if self.camera not in cameras:
            self.camera_cards[cameras[0]].setChecked(True)

    @property
    def folder(self) -> QLineEdit:
        """The selected card's editor, not a second source field."""
        return self.camera_cards[self.camera].folder

    @property
    def browse(self) -> QPushButton:
        return self.camera_cards[self.camera].browse

    def button(self, key: str) -> QPushButton:
        button = QPushButton(text(key, self.language))
        button.setMinimumHeight(Style.touch)
        self.buttons[key] = button
        return button

    def icon_button(self, key: str, tool: str) -> QPushButton:
        button = self.button(key)
        button.setText("")
        button.setFixedSize(Style.touch, Style.touch)
        button.setIcon(image_icon(tool))
        button.setIconSize(QSize(Style.icon, Style.icon))
        return button

    def refresh_theme(self) -> None:
        colors = theme_colors(self)
        for camera, card in self.camera_cards.items():
            card.browse.setIcon(image_icon("folder", colors.primary))
            card.configuration.setIcon(
                image_icon(
                    "minus" if camera in self.draft.camera_ids else "plus",
                    colors.primary,
                )
            )
            card.update()
        for key, tool in (
            ("reference_previous", "previous"),
            ("reference_next", "next"),
            ("roi_refresh", "refresh"),
            ("roi_fit", "fit"),
            ("roi_set_rect", "apply"),
            ("roi_common", "common"),
            ("roi_distribute", "distribute"),
            ("vision_import", "folder"),
            ("vision_runtime", "runtime"),
            ("source_discover", "discover"),
            ("vision_capture", "capture"),
            ("cancel", "cancel"),
            ("vision_discard", "refresh"),
            ("save", "save"),
        ):
            self.buttons[key].setIcon(image_icon(tool, colors.primary))
        self.loop.setIcon(image_icon("loop", colors.primary))
        self.fps_decrease.setIcon(image_icon("minus", colors.primary))
        self.fps_increase.setIcon(image_icon("plus", colors.primary))
        self.source_modes["folder"].setIcon(image_icon("folder", colors.primary))
        self.source_modes["basler"].setIcon(camera_icon(colors.primary))
        self.connect_source.setIcon(image_icon("connect", colors.primary))
        self.disconnect_source.setIcon(image_icon("disconnect", colors.primary))

    def set_language(self, language: Language) -> None:
        self.language = language
        self.refresh_theme()
        self._populate_devices()
        self.loop.setToolTip(text("source_loop", language))
        self.fps.setAccessibleName(text("vision_fps", language))
        for button, key in (
            (self.fps_decrease, "vision_fps_decrease"),
            (self.fps_increase, "vision_fps_increase"),
        ):
            button.setAccessibleName(text(key, language))
            button.setToolTip(text(key, language))
        self.reference.setAccessibleName(text("vision_image_select", language))
        self.channel.setAccessibleName(text("vision_roi_select", language))
        for button, key in (
            (self.connect_source, "source_connect"),
            (self.disconnect_source, "source_disconnect"),
        ):
            button.setText("")
            button.setAccessibleName(text(key, language))
            button.setToolTip(text(key, language))
        self.mode_switch.setToolTip(
            text("cli_sources", language) if self.session.cli_sources else ""
        )
        for mode, button in self.source_modes.items():
            key = "vision_folder_mode" if mode == "folder" else "vision_basler_mode"
            button.setText("")
            button.setAccessibleName(text(key, language))
            button.setToolTip(text(key, language))
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
                "vision_import",
                "vision_runtime",
                "source_discover",
                "vision_capture",
                "cancel",
                "vision_discard",
                "save",
                "roi_set_rect",
            ):
                button.setText(text(key, language))
        primary = self.buttons["save_apply"]
        set_button_caption(
            primary,
            text("save_apply", language),
            button_width(primary, ("save_apply",), padding=2),
        )
        for key, line in self.coordinates.items():
            line.setAccessibleName(text("roi_" + key.lower(), language))
            line.setToolTip(text("roi_" + key.lower(), language))
        self.refresh()

    def editable(self) -> bool:
        return self._allowed() and not self._preparing

    def source_editable(self) -> bool:
        return (
            self._allowed()
            and not self._preparing
            and not (
                self.connection is not None and self.connection.source_snapshot().locked
            )
        )

    def connection_operation(self, *, connect: bool) -> None:
        if self.connection is None or not self._allowed() or self._preparing:
            return
        self.cancel_capture()
        if connect and (self.sources is None or self.sources.dirty):
            self._report_error(
                text("source_save_first", self.language), "vision_validation_error"
            )
            self.refresh()
            return

        def completed(_value: object, error: BaseException | None) -> None:
            if error is not None:
                self._report_error(error, "source_connection_error")
            else:
                self._error = ""
            self.refresh()

        self._run_io(
            self.connection.connect_source
            if connect
            else self.connection.disconnect_source,
            completed,
        )

    def _camera_selected(self, _index: int, checked: bool) -> None:
        if checked:
            self.switch_camera()

    def switch_camera(self) -> None:
        self.cancel_capture()
        self._reference_modes.pop(self.camera, None)
        self.selected_index = 0
        if self.sources is not None:
            self.folder.setText(self.sources.definition.camera(self.camera).folder)
            self.loop.blockSignals(True)
            self.loop.setChecked(self.sources.definition.loop)
            self.loop.blockSignals(False)
        self._request_reference(reload=False)

    def source_edited(self, folder: str, camera: CameraId | None = None) -> None:
        if not self.source_editable() or self.sources is None:
            return
        camera = self.camera if camera is None else camera
        self.cancel_capture()
        previous_key = self.sources.definition.camera(camera).key(
            self.sources.path.parent
        )
        self.sources.set_folder(camera, folder)
        if (
            self.sources.definition.camera(camera).key(self.sources.path.parent)
            != previous_key
        ):
            self._folder_paths.pop(camera, None)
            self._positions.pop(camera, None)
            self._reference_modes[camera] = "folder"
        self._save_failed = False
        self.draft.bounds.pop(camera, None)
        if camera == self.camera:
            self._reference_revision += 1
            self._set_reference(None)
        self._changed()

    def browse_folder(self, camera: CameraId | None = None) -> None:
        if not self.source_editable() or self.sources is None:
            return
        camera = self.camera if camera is None else camera
        folder = QFileDialog.getExistingDirectory(
            self,
            text("source_folder", self.language),
            self.camera_cards[camera].folder.text(),
        )
        if folder:
            self.source_edited(folder, camera)
            if camera == self.camera:
                self.refresh_frame()

    def select_device(self, camera: CameraId, index: int) -> None:
        if not self.source_editable() or self.sources is None:
            return
        combo = self.camera_cards[camera].device
        if not 0 <= index < combo.count():
            return
        serial = str(combo.itemData(index))
        previous = self.sources.definition.camera(camera).basler_serial
        if serial == previous:
            return
        used = {
            c.basler_serial
            for c in self.sources.definition.cameras
            if c.camera_id != camera and c.basler_serial
        }
        if serial in used:
            self._report_error(
                text("source_duplicate", self.language), "source_duplicate"
            )
            self._populate_devices()
            self.refresh()
            return
        if serial and serial not in {d.serial for d in self._devices}:
            self._populate_devices()  # Undiscovered saved values may be retained, not assigned elsewhere.
            return
        try:
            self.sources.set_serial(camera, serial)
        except ConfigurationError as exc:
            self._report_error(exc, "vision_validation_error")
            self.refresh()
            return
        self._populate_devices()
        if self.sources.definition.mode == "folder":
            self._save_failed = False
            self._changed()
            self.refresh()
        else:
            self._source_identity_changed()

    def loop_changed(self, enabled: bool) -> None:
        if self.source_editable() and self.sources is not None:
            self.sources.set_loop(enabled)
            self._changed()

    def fps_changed(self) -> None:
        value = self.fps.text()
        self.fps.setModified(False)
        if (
            not self.source_editable()
            or self.sources is None
            or self.sources.definition.mode != "folder"
        ):
            self.refresh()
            return
        if value == str(self.sources.definition.target_fps):
            self.refresh()  # An untouched saved value remains authoritative.
            return
        if value not in {str(i) for i in range(1, 11)}:
            self._report_error(text("vision_fps_invalid", self.language))
            self.refresh()
            return
        self.sources.set_fps(float(value))
        self._error = ""
        self._changed()
        self.refresh()

    def _step_fps(self, delta: int) -> None:
        if self.sources is None or not self.source_editable():
            return
        value = self.sources.definition.target_fps
        if not (float(value).is_integer() and 1 <= value + delta <= 10):
            return
        # Legacy values are replaced by explicit entry, never by implicit clamping.
        if not 1 <= value <= 10:
            return
        self.fps.setText(str(int(value) + delta))
        self.fps_changed()

    def _sync_fps(self) -> None:
        if self.sources is None:
            return
        value = self.sources.definition.target_fps
        standard = 1 <= value <= 10 and float(value).is_integer()
        if not (self.fps.hasFocus() and self.fps.isModified()):
            self.fps.setText(str(int(value)) if standard else str(value))
        self.fps_decrease.setEnabled(standard and value > 1)
        self.fps_increase.setEnabled(standard and value < 10)
        self.fps.setToolTip(text("vision_fps", self.language) + f": {value}")
        self.fps_stepper.setToolTip(self.fps.toolTip())

    def set_source_mode(self, mode: Literal["folder", "basler"]) -> None:
        if not self.source_editable() or self.sources is None:
            return
        if self.sources.definition.mode != mode:
            self.sources.set_mode(mode)
            self._source_identity_changed()

    def discover_devices(self) -> None:
        if not self.source_editable() or self._discovering:
            return
        self._discovering = True

        def completed(value: object, error: BaseException | None) -> None:
            self._discovering = False
            rows = cast(tuple[object, ...], value) if isinstance(value, tuple) else ()
            if error is None and (
                not isinstance(value, tuple)
                or not all(isinstance(d, BaslerDevice) for d in rows)
            ):
                error = TypeError("Invalid device discovery result")
            if error is not None:
                self._report_error(error, "source_discovery_failed")
            else:
                self._devices = tuple(d for d in rows if isinstance(d, BaslerDevice))
                self._error = ""
            self._populate_devices()
            self.refresh()

        if not self._run_io(discover_basler_devices, completed):
            completed(None, RuntimeError(text("preparing", self.language)))
        self.refresh()

    def _populate_devices(self) -> None:
        devices = {d.serial: d.model for d in self._devices}
        snapshot = self.connection.source_snapshot() if self.connection else None
        connected = (
            {row.camera_id: row for row in snapshot.cameras}
            if snapshot is not None and snapshot.state is ConnectionState.CONNECTED
            else {}
        )
        for camera, card in self.camera_cards.items():
            if camera not in self.draft.camera_ids:
                continue
            selected = (
                self.sources.definition.camera(camera).basler_serial
                if self.sources
                else ""
            )
            labels = {
                serial: f"{serial} · {model}" for serial, model in devices.items()
            }
            actual = connected.get(camera)
            if actual is not None and actual.serial == selected:
                # The application connection survives this workspace. Discovery
                # is only a page-local list of assignable devices, not liveness.
                labels[selected] = " · ".join(
                    part
                    for part in (selected, actual.model or devices.get(selected, ""))
                    if part
                )
            elif selected and selected not in devices:
                labels[selected] = (
                    f"{selected} · {text('source_not_discovered', self.language)}"
                )
            choices = [
                ("", "S/N —"),
                *sorted(labels.items()),
            ]
            card.device.blockSignals(True)
            if choices != [
                (card.device.itemData(i), card.device.itemText(i))
                for i in range(card.device.count())
            ]:
                card.device.clear()
                for serial, label in choices:
                    card.device.addItem(label, serial)
            card.device.setCurrentIndex(card.device.findData(selected))
            model = card.device.model()
            used: set[str] = (
                {
                    c.basler_serial
                    for c in self.sources.definition.cameras
                    if c.camera_id != camera and c.basler_serial
                }
                if self.sources
                else set()
            )
            if isinstance(model, QStandardItemModel):
                for index in range(1, card.device.count()):
                    item = model.item(index)
                    serial = str(card.device.itemData(index))
                    item.setEnabled(serial in devices and serial not in used)
                    item.setToolTip(card.device.itemText(index))
            card.device.blockSignals(False)

    def capture_image(self) -> None:
        if not self._can_capture() or self.connection is None:
            return
        self._reference_revision += 1
        self._capture_token += 1
        self._error = ""
        try:
            self._capture_future = self.connection.capture_image(self.camera)
        except Exception as exc:
            self._report_error(exc, "vision_capture_failed")
            self.refresh()
            return
        self._capture_timeout = False
        self._capture_delivery.watch(str(self._capture_token), self._capture_future)
        self.refresh()

    def _can_capture(self) -> bool:
        return (
            self._allowed()
            and not self._preparing
            and not self._reading
            and self._capture_future is None
            and self.sources is not None
            and self.sources.definition.mode == "basler"
            and self.connection is not None
            and self.connection.source_snapshot().state is ConnectionState.CONNECTED
        )

    def cancel_capture(self) -> None:
        future, self._capture_future = self._capture_future, None
        self._capture_token += 1
        if future is not None:
            future.cancel()
            self._capture_timeout = False
        self.refresh()

    @Slot(str, object, object)
    def _capture_finished(self, token: str, value: object, error: object) -> None:
        if token != str(self._capture_token) or self._capture_future is None:
            return
        self._capture_future = None
        self._capture_timeout = False
        if (
            isinstance(error, CancelledError)
            or not self._allowed()
            or self.connection is None
            or self.connection.source_snapshot().state is not ConnectionState.CONNECTED
            or self.sources is None
            or self.sources.definition.mode != "basler"
        ):
            pass
        elif isinstance(error, TimeoutError):
            self._capture_timeout = True
        elif isinstance(error, BaseException):
            self._report_error(error, "vision_capture_failed")
        elif isinstance(value, CameraSnapshot) and value.camera_id == self.camera:
            self._reference_modes[self.camera] = "capture"
            # Cycle zero is a canvas-only identity, never published to Production.
            self._set_reference(
                DisplayFrame(
                    FrameId(CycleId(0), self.camera, value.metadata.image_number or 0),
                    0,
                    value.metadata.received_at,
                    self._capture_token,
                    value.pixels,
                    "basler:" + value.metadata.serial,
                )
            )
        else:
            self._report_error("Invalid camera snapshot", "vision_capture_failed")
        self.refresh()

    def import_image(self) -> None:
        if self._reading or self._preparing:
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
        if self._reading or self._preparing:
            return
        self.cancel_capture()
        self._reference_revision += 1
        revision, camera = self._reference_revision, self.camera
        self._reading = True

        def read() -> DisplayFrame:
            pixels = decode_image(path)
            owned = np.frombuffer(pixels.tobytes(), dtype=np.uint8).reshape(
                pixels.shape
            )
            return DisplayFrame(
                FrameId(CycleId(0), camera, 0),
                0,
                monotonic(),
                revision,
                owned,
                str(path),
            )

        def completed(value: object, error: BaseException | None) -> None:
            self._reading = False
            if revision == self._reference_revision:
                if error is not None:
                    self._report_error(error, "folder_reference_error")
                elif isinstance(value, DisplayFrame):
                    self._reference_modes[camera] = "import"
                    self._set_reference(value)
            if self._pending_reference:
                self._pending_reference = False
                self._request_reference(reload=False)
            self.refresh()

        if not self._run_io(read, completed):
            self._reading = False
        self.refresh()

    def _source_identity_changed(self) -> None:
        self.cancel_capture()
        self._save_failed = False
        self._reference_modes.clear()
        self._reference_revision += 1
        self._set_reference(None)
        self._request_reference(reload=False)
        self._changed()
        self.refresh()

    def _set_reference(self, frame: DisplayFrame | None) -> None:
        if self._capture_future is None:
            self._capture_timeout = False
        self.frame = frame
        self.canvas.set_frame(frame)
        self.draft.bounds.pop(self.camera, None)
        if self.canvas.model.size is not None:
            self.draft.bounds[self.camera] = self.canvas.model.size
        self._connection_bounds()
        self.select_roi(self.selected_index)
        if frame is not None:
            try:
                if self.editable() and self.draft.initialize(
                    self.camera, frame.image.shape[1], frame.image.shape[0]
                ):
                    self.sync_size()
                    self._changed()
                self.draft.camera(self.camera).validate_shape(
                    (frame.image.shape[0], frame.image.shape[1], 3)
                )
            except ValueError as exc:
                self._report_error(exc, "roi_geometry_error")
            self.refresh()  # Incompatible saved geometry remains visible, never repaired.

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
        self.cancel_capture()
        self._reference_modes[self.camera] = mode
        self._request_reference(reload=False)

    def browse_reference(self, delta: int) -> None:
        if (
            self.sources is None
            or self.sources.definition.mode != "folder"
            or self._reading
            or self._preparing
        ):
            return
        paths = self._folder_paths.get(self.camera, ())
        index = self._positions.get(self.camera, 0) + delta
        if not 0 <= index < len(paths):
            return  # No implicit wrap, decode, or geometry changes at a boundary.
        self._positions[self.camera] = index
        self._reference_modes[self.camera] = "folder"
        self._request_reference(reload=False)

    def choose_reference(self, index: int) -> None:
        if (
            self.sources is None
            or self.sources.definition.mode != "folder"
            or self._reading
            or self._preparing
        ):
            return
        if 0 <= index < len(self._folder_paths.get(self.camera, ())):
            if (
                index != self._positions.get(self.camera, 0)
                or self.reference_mode != "folder"
            ):
                self._positions[self.camera] = index
                self._reference_modes[self.camera] = "folder"
                self._request_reference(reload=False)

    def refresh_frame(self) -> None:
        self.cancel_capture()
        self._request_reference(reload=True)

    def choose_image(self, index: int) -> None:
        # The current temporary image is descriptive, never Folder input/history.
        if self.reference_mode != "folder":
            index -= 1
        self.choose_reference(index)

    def _request_reference(self, *, reload: bool) -> None:
        self._reference_revision += 1
        if self._reading:
            self._pending_reference = True
            self._set_reference(None)
            return
        if self._preparing:
            return
        self._error = ""
        if self.reference_mode in ("capture", "import"):
            self.refresh()
            return
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
        if self.sources is None:
            return
        if self.sources.definition.mode == "basler":
            return  # Persisted Folder paths are not independent Basler references.
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
        rois = self.draft.rois(self.camera)
        if not rois:
            for line in self.coordinates.values():
                line.clear()
            return
        rect = rois[self.selected_index].rect
        for key, value in zip(
            ("X", "Y", "W", "H"), (rect.x, rect.y, rect.width, rect.height), strict=True
        ):
            self.coordinates[key].setText(str(value))

    def refresh(self) -> None:
        colors = theme_colors(self)
        if self._capture_future is not None and (
            not self._allowed()
            or self.connection is None
            or self.connection.source_snapshot().state is not ConnectionState.CONNECTED
        ):
            self.cancel_capture()
        self._connection_bounds()
        self.canvas.rois = self.draft.rois(self.camera)
        if self.frame is None:
            size = self.draft.bounds.get(self.camera)
            self.canvas.set_projection(size)
        self.canvas.model.rect = (
            self.canvas.rois[self.selected_index].rect if self.canvas.rois else None
        )
        geometry_ready = bool(self.canvas.rois)
        self.canvas.model.editable = (
            self.editable() and self.frame is not None and geometry_ready
        )
        if not self.canvas.model.can_drag:
            self.canvas.cancel_roi_drag()
        channels = tuple(f"CH{row.channel_id:02d}" for row in self.canvas.rois)
        if (
            tuple(self.channel.itemText(i) for i in range(self.channel.count()))
            != channels
        ):
            self.channel.clear()
            self.channel.addItems(channels)
        self.channel.setCurrentIndex(self.selected_index)
        self.channel.setEnabled(not self._preparing and geometry_ready)
        for line in self.coordinates.values():
            line.setReadOnly(not self.editable() or not geometry_ready)
        for key in ("roi_common", "roi_distribute", "roi_set_rect"):
            self.buttons[key].setEnabled(self.editable() and geometry_ready)
        self.source_cards.setEnabled(not self._preparing)
        for mode, button in self.source_modes.items():
            button.setChecked(
                self.sources is not None and self.sources.definition.mode == mode
            )
            button.setEnabled(self.sources is not None and self.source_editable())
        self._refresh_source_cards()
        viewing = not self._reading and not self._preparing
        paths = self._folder_paths.get(self.camera, ())
        index = self._positions.get(self.camera, 0)
        self.buttons["vision_runtime"].setEnabled(
            viewing and self._runtime_frame() is not None
        )
        self.buttons["vision_runtime"].setToolTip(
            text(
                "reference_runtime_tip"
                if self._runtime_frame()
                else "reference_unavailable",
                self.language,
            )
        )
        self.buttons["vision_import"].setEnabled(viewing)
        folder_mode = (
            self.sources is not None and self.sources.definition.mode == "folder"
        )
        self.capture.setVisible(not folder_mode and self.connection is not None)
        self.capture.setEnabled(self._can_capture())
        self.cancel_capture_button.setVisible(self._capture_future is not None)
        self.capture.setToolTip(text("vision_capture", self.language))
        self.buttons["reference_previous"].setEnabled(
            viewing and folder_mode and index > 0
        )
        self.buttons["reference_next"].setEnabled(
            viewing and folder_mode and index + 1 < len(paths)
        )
        self.buttons["roi_refresh"].setEnabled(
            viewing and self.reference_mode in ("folder", "runtime")
        )
        self.buttons["save_apply"].setEnabled(
            self._allowed()
            and not self._preparing
            and not self.session.dirty
            and self.session.entry is not None
            and self.draft.complete
        )
        self.buttons["save"].setEnabled(self._allowed() and not self._preparing)
        self.buttons["vision_discard"].setEnabled(self._can_discard())
        source_editable = self.sources is not None and self.source_editable()
        for key in ("reference_previous", "reference_next"):
            self.buttons[key].setVisible(folder_mode)
        self.discover.setVisible(self.sources is not None and not folder_mode)
        self.discover.setEnabled(source_editable and not self._discovering)
        self.discover.setToolTip(
            text("source_discovering", self.language)
            if self._discovering
            else text("source_discover", self.language)
        )
        self.playback_options.setVisible(self.sources is not None and folder_mode)
        folder_options = (
            source_editable
            and self.sources is not None
            and self.sources.definition.mode == "folder"
        )
        self.loop.setEnabled(folder_options)
        self.playback_options.setEnabled(folder_options)
        self.fps.setEnabled(folder_options)
        self.fps_stepper.setEnabled(folder_options)
        if self.sources is not None:
            self.loop.blockSignals(True)
            self.loop.setChecked(self.sources.definition.loop)
            self.loop.blockSignals(False)
            self._sync_fps()
        loop_state = text(
            "source_loop_on" if self.loop.isChecked() else "source_loop_off",
            self.language,
        )
        self.loop.setText("")
        self.loop.setIcon(
            image_icon(
                "loop", colors.accent if self.loop.isChecked() else colors.secondary
            )
        )
        self.loop.setToolTip(text("source_loop", self.language) + " · " + loop_state)
        self.loop.setAccessibleName(
            text("source_loop", self.language) + " · " + loop_state
        )
        status_key = (
            "vision_reloading"
            if self._reloading
            else "setup_saving"
            if self._preparing
            else "setup_failed"
            if self._save_failed
            else "unsaved"
            if self.draft.dirty or (self.sources is not None and self.sources.dirty)
            else "UNCONFIGURED"
            if not self.draft.persisted
            or (self.sources is not None and not self.sources.persisted)
            else "saved"
        )
        status_text = text(status_key, self.language)
        if (
            self.sources is not None
            and self.sources.persisted
            and not self.draft.persisted
        ):
            status_text = text("saved", self.language) + " · " + status_text
        self.status.setAccessibleName(status_text)
        self.status.setToolTip("")
        status_color = (
            colors.warning
            if status_key == "unsaved"
            else colors.fault
            if status_key == "setup_failed"
            else colors.ok
            if status_key == "saved"
            else colors.secondary
        )
        self.status.setPixmap(
            (
                indicator_icon(Indicator.PENDING, status_color)
                if status_key == "unsaved"
                else production_icon(
                    "READY"
                    if status_key == "saved"
                    else "FAULT"
                    if status_key == "setup_failed"
                    else "STARTING",
                    status_color,
                )
            ).pixmap(20, 20)
        )
        bounds_issue = ""
        size = self.draft.bounds.get(self.camera)
        if size is not None and geometry_ready:
            try:
                self.draft.camera(self.camera).validate_shape((size[1], size[0], 3))
            except ValueError as exc:
                bounds_issue = str(exc)
        self._bounds_issue = bounds_issue
        position = (
            f"{index + 1}/{len(paths)}"
            if paths and self.reference_mode == "folder"
            else text("reference_runtime", self.language)
            if self.reference_mode == "runtime"
            else text(
                "vision_imported"
                if self.reference_mode == "import"
                else "vision_captured",
                self.language,
            )
            if self.reference_mode in ("import", "capture")
            else ""
        )
        info = (
            (
                f"— · {self.canvas.model.size[0]}×{self.canvas.model.size[1]}"
                if self.canvas.model.size is not None
                else "ROI ≈"
                if self.canvas.rois
                else "—"
            )
            if self.frame is None
            else f"{position} · {self.frame.image.shape[1]}×{self.frame.image.shape[0]}"
        )
        self.reference.setToolTip(
            str(paths[index])
            if self.frame is not None
            and folder_mode
            and paths
            and index < len(paths)
            and self.reference_mode == "folder"
            else self.frame.source_key or info
            if self.frame is not None and self.reference_mode == "import"
            else text(
                "vision_schematic"
                if self.frame is None
                and self.canvas.model.size is None
                and self.canvas.rois
                else "reference_unavailable"
                if self.frame is None
                else "reference_runtime_tip",
                self.language,
            )
        )
        key = folder_mode, paths, self.reference_mode
        chooser = folder_mode and bool(paths)
        offset = int(self.reference_mode != "folder") if chooser else 0
        target = index + offset if chooser and self.reference_mode == "folder" else 0
        if key != self._selector_key:
            self._selector_key = key
            self.reference.clear()
            if not chooser or offset:
                self.reference.addItem("")
            if chooser:
                self.reference.addItems(
                    [f"{i + 1}/{len(paths)} · {p.name}" for i, p in enumerate(paths)]
                )
        elif chooser and self._selector_index != target:
            old = self._selector_index - offset
            if 0 <= old < len(paths):
                self.reference.setItemText(
                    self._selector_index, f"{old + 1}/{len(paths)} · {paths[old].name}"
                )
        # The selected caption is always derived from current pixels/dimensions,
        # never a reused Folder-list placeholder from another source mode.
        self.reference.setItemText(target, info)
        self.reference.setCurrentIndex(target)
        self._selector_index = target
        self.reference.setEnabled(viewing and chooser)
        self.reference.setVisible(True)
        meaning = (
            text("vision_dimensions_only", self.language).format(
                width=self.canvas.model.size[0], height=self.canvas.model.size[1]
            )
            if self.frame is None and self.canvas.model.size is not None
            else text("reference_unavailable", self.language)
            if self.frame is None and not self.canvas.rois
            else text("vision_schematic", self.language)
            if self.frame is None
            else info
        )
        self.reference.setAccessibleDescription(meaning)
        self.canvas.setAccessibleDescription(meaning)
        self.canvas.setToolTip("")
        self.reference_toolbar.sync_metrics()
        self.geometry_toolbar.sync_metrics()
        self.canvas.update()
        feedback = self.feedback
        if feedback != self._last_feedback:
            self._last_feedback = feedback
            self.feedback_changed.emit()

    @property
    def feedback(self) -> tuple[str, str] | None:
        """Page-scoped error evidence for the existing global status presenter."""
        if self._error:
            return self._error_key, self._error
        if self._bounds_issue:
            return "roi_geometry_error", self._bounds_issue
        if self.connection is not None:
            snapshot = self.connection.source_snapshot()
            if snapshot.state is ConnectionState.FAULT:
                return "source_connection_error", snapshot.error
        if self._capture_timeout:
            return "vision_capture_timeout", text(
                "vision_capture_timeout", self.language
            )
        return None

    @property
    def vision_dirty(self) -> bool:
        return self.draft.dirty or bool(self.sources is not None and self.sources.dirty)

    def _can_discard(self) -> bool:
        return (
            self.editable()
            and self.vision_dirty
            and self.draft.path is not None
            and self._decision is None
            and self._capture_future is None
        )

    def request_discard(self) -> None:
        if not self._can_discard():
            return
        dialog = QMessageBox(self)
        dialog.setWindowTitle(text("vision_discard", self.language))
        dialog.setText(text("vision_discard_question", self.language))
        discard = dialog.addButton(
            text("discard", self.language), QMessageBox.ButtonRole.DestructiveRole
        )
        cancel = dialog.addButton(
            text("cancel", self.language), QMessageBox.ButtonRole.RejectRole
        )
        dialog.setDefaultButton(cancel)
        self._decision = dialog

        def finished() -> None:
            self._decision = None
            chosen = dialog.clickedButton()
            dialog.deleteLater()
            if chosen is discard:
                self._reload_vision()
            else:
                self.refresh()

        dialog.finished.connect(finished)
        dialog.open()
        self.refresh()

    def _reload_vision(self, after: Callable[[], None] | None = None) -> None:
        if not self.editable() or self._capture_future is not None:
            return
        if self.draft.path is None:
            self._report_error(
                "Saved Vision paths or configuration loader unavailable",
                "vision_reload_error",
            )
            self.refresh()
            return

        def completed(value: object, error: BaseException | None) -> None:
            self._preparing = self._reloading = False
            if (
                error is not None
                or not isinstance(value, SavedVision)
                or not self._allowed()
            ):
                self._report_error(
                    error
                    or "Configuration reload unavailable in the current editing state",
                    "vision_reload_error",
                )
                self.refresh()
                return
            old = self.sources.definition if self.sources is not None else None
            try:
                self.session.accept_vision(
                    value,
                    self.connection.source_snapshot() if self.connection else None,
                )
            except ConfigurationError as exc:
                self._report_error(exc, "vision_reload_error")
                self.refresh()
                return
            # All files validate before either draft/baseline is replaced. No save,
            # preparation, source command or application activation is performed.
            if (
                self.sources is not None
                and value.sources is not None
                and old is not None
            ):
                for row in value.sources.cameras:
                    previous = next(
                        (c for c in old.cameras if c.camera_id == row.camera_id), None
                    )
                    if previous is None or previous.folder != row.folder:
                        self._folder_paths.pop(row.camera_id, None)
                        self._positions.pop(row.camera_id, None)
                if (
                    self.frame is not None
                    and (
                        value.machine is None
                        or self.camera in value.machine.layout.camera_ids
                    )
                    and self.reference_mode == "folder"
                    and self.frame.source_key
                    != value.sources.source_key(self.camera, self.sources.path.parent)
                ):
                    self._reference_modes[self.camera] = (
                        "import"  # Keep frozen pixels/provenance, not a stale Folder-list association.
                    )
            self._populate_devices()
            self._sync_configured_camera()
            self._save_failed = False
            self._error = self._feedback = ""
            self._selector_key = None
            self._populate_devices()
            self.sync_size()
            scale, pan = self.canvas.model.scale, self.canvas.model.pan
            self._changed()
            self.refresh()
            self.canvas.model.scale, self.canvas.model.pan = scale, pan
            self.canvas.update()
            if after is not None:
                after()

        self._preparing = self._reloading = True
        if not self._run_io(self.session.read_vision, completed):
            completed(
                None, RuntimeError("Configuration IO is busy; retry after it completes")
            )
        self.refresh()

    def _refresh_source_cards(self) -> None:
        connection = self.connection.source_snapshot() if self.connection else None
        actual = (
            {row.camera_id: row for row in connection.cameras} if connection else {}
        )
        colors = theme_colors(self)
        if connection is not None:
            basler_mode = (
                self.sources is not None and self.sources.definition.mode == "basler"
            )
            for widget in (
                self.connection_status,
                self.connect_source,
                self.disconnect_source,
            ):
                widget.setVisible(basler_mode)
            state_text = text("source_" + connection.state.name.lower(), self.language)
            count = len(actual) if connection.state is ConnectionState.CONNECTED else 0
            self.connection_status.setText(f"{count}/{len(self.draft.camera_ids)}")
            self.connection_status.setToolTip("")
            self.connection_status.setAccessibleName(
                state_text + f" · {count}/{len(self.draft.camera_ids)}"
            )
            self.connect_source.setEnabled(
                self._allowed()
                and not self._preparing
                and not connection.locked
                and self.sources is not None
                and basler_mode
                and not self.sources.dirty
            )
            self.disconnect_source.setEnabled(
                self._allowed() and not self._preparing and connection.locked
            )
        self._populate_devices()
        for camera, card in self.camera_cards.items():
            card.title = str(camera).removeprefix("CAM")
            name = text("camera_number", self.language).format(number=card.title)
            configured = camera in self.draft.camera_ids
            card.setCheckable(configured)
            card.configuration.setVisible(camera == "CAM6" and self.sources is not None)
            card.configuration.setEnabled(
                self.source_editable() and not self.session.cli_sources
            )
            card.configuration.setIcon(
                image_icon("minus" if configured else "plus", colors.primary)
            )
            operation = text(
                "vision_remove_cam6" if configured else "vision_restore_cam6",
                self.language,
            )
            card.configuration.setToolTip(operation)
            card.configuration.setAccessibleName(operation)
            if not configured:
                card.folder_row.hide()
                card.serial_row.hide()
                card.detail = text("source_unconfigured", self.language)
                card.summary = card.detail
                card.status_color = ""
                card.setToolTip("")
                card.setAccessibleName(f"{name} · {card.detail}")
                card.update()
                continue
            card.status_color = ""
            card.summary = ""
            if self.sources is None:
                card.detail = text("simulation", self.language)
            else:
                source = self.sources.definition.camera(camera)
                basler = self.sources.definition.mode == "basler"
                card.detail = (
                    source.basler_serial if basler else source.folder
                ) or text("source_unconfigured", self.language)
                if basler:
                    row = actual.get(camera)
                    matched = row is not None and row.serial == source.basler_serial
                    if connection is not None:
                        card.status_color = (
                            colors.ok
                            if connection.state is ConnectionState.CONNECTED and matched
                            else colors.warning
                            if connection.state
                            in (
                                ConnectionState.FAULT,
                                ConnectionState.CONNECTING,
                                ConnectionState.DISCONNECTING,
                            )
                            else colors.secondary
                        )
                        if (
                            connection.state is ConnectionState.CONNECTED
                            and row is not None
                            and matched
                        ):
                            card.summary = f"{row.width}×{row.height}"
                if card.folder.text() != source.folder:
                    card.folder.setText(source.folder)
                card.device.setCurrentIndex(card.device.findData(source.basler_serial))
                card.serial_row.setVisible(basler)
                card.folder_row.setVisible(not basler)
            allowed = self.sources is not None and self.source_editable()
            card.folder.setEnabled(allowed)
            card.device.setEnabled(allowed)
            card.browse.setEnabled(allowed)
            card.folder.setPlaceholderText(text("source_folder_status", self.language))
            card.folder.setAccessibleName(
                f"{camera} · {text('source_folder', self.language)}"
            )
            card.folder.setToolTip(card.folder.text())
            card.device.setAccessibleName(
                f"{camera} · {text('source_serial', self.language)}"
            )
            card.device.setToolTip(
                card.device.currentText()
                if card.device.currentData()
                else text("source_unconfigured", self.language)
            )
            card.browse.setAccessibleName(f"{camera} · {text('browse', self.language)}")
            card.browse.setToolTip(text("browse", self.language))
            card.setToolTip("")
            card.setAccessibleName(f"{name} · {camera}")
            card.update()

    def _connection_bounds(self) -> None:
        initialized = False
        if self.connection is not None:
            snapshot = self.connection.source_snapshot()
            if snapshot.state is ConnectionState.CONNECTED:
                for row in snapshot.cameras:
                    if row.camera_id not in self._hardware_bounds and self.editable():
                        try:
                            initialized |= self.draft.initialize(
                                row.camera_id, row.width, row.height
                            )
                        except ConfigurationError as exc:
                            self._report_error(
                                f"{row.camera_id}: {exc}", "roi_geometry_error"
                            )
                    self.draft.bounds[row.camera_id] = row.width, row.height
                    self._hardware_bounds.add(row.camera_id)
            elif self._hardware_bounds:
                for identity in self._hardware_bounds:
                    self.draft.bounds.pop(identity, None)
                self._hardware_bounds.clear()
                if self.frame is not None:
                    self.draft.bounds[self.camera] = (
                        self.frame.image.shape[1],
                        self.frame.image.shape[0],
                    )
        if initialized:
            self.sync_size()
            self._changed()

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
        self._error, self._error_key = (
            f"{type(error).__name__}: {error}"
            if isinstance(error, BaseException)
            else error,
            key,
        )

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

    def save_setup(self, after: Callable[[], None] | None = None) -> None:
        """Persist independent definitions; no source IO, model work or activation."""
        if not self._allowed() or self._preparing:
            return
        self._preparing = True
        self._save_failed = False
        self._error = self._feedback = ""
        connection = self.connection.source_snapshot() if self.connection else None
        self.refresh()

        def validated(_value: object, error: BaseException | None) -> None:
            self._preparing = False
            if not self._allowed():
                self._report_error(text("read_only", self.language), "read_only")
                self.refresh()
                return
            result = self.session.save_vision(connection, error)
            errors, machine_saved, source_saved = (
                result.errors,
                result.machine_saved,
                result.sources_saved,
            )
            self._save_failed = bool(errors)
            if errors:
                saved = ", ".join(
                    name
                    for name, succeeded in (
                        ("MachineLayout", machine_saved),
                        ("CameraSources", source_saved),
                    )
                    if succeeded
                )
                self._report_error(
                    "\n".join(
                        ([f"{text('saved', self.language)}: {saved}"] if saved else [])
                        + list(errors)
                    ),
                    "source_partial_save"
                    if machine_saved and not source_saved
                    else "vision_error",
                )
            else:
                self._feedback = "saved"
            self.setup_saved.emit(machine_saved, source_saved)
            self._changed()
            self.refresh()
            if not errors and not self.vision_dirty and after is not None:
                after()

        if not self._run_io(
            lambda: self.session.validate_vision_save(connection), validated
        ):
            self._preparing = False
            self._report_error(text("preparing", self.language), "preparing")
            self.refresh()

    def save_and_apply(self, after: Callable[[], None] | None = None) -> None:
        if not self._allowed() or self._preparing:
            return
        if self.session.dirty or self.session.entry is None:
            self._report_error(
                text("roi_recipe_save", self.language), "roi_recipe_save"
            )
            self.refresh()
            return

        def apply_saved() -> None:
            if not self.draft.persisted:
                self._report_error(
                    text("save_machine_first", self.language), "save_machine_first"
                )
                self.refresh()
                return
            self._preparing = True
            self.refresh()

            def prepared(error: BaseException | None) -> None:
                self._preparing = False
                self._report_error(str(error) if error else "", "recipe_prepare_error")
                self._save_failed = bool(error)
                self._feedback = "" if error else "roi_applied"
                self.refresh()
                if not error and self.sources is not None:
                    self.sources_verified.emit()
                if not error and after is not None:
                    after()

            self._apply(prepared)

        self.save_setup(apply_saved)

    def request_back(self, after: Callable[[], None] | None = None) -> None:
        self.cancel_capture()
        if self._preparing or self._decision is not None:
            return

        def leave() -> None:
            self.cancel_capture()
            self._capture_delivery.detach()
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
            text("save", self.language), QMessageBox.ButtonRole.AcceptRole
        )
        discard = dialog.addButton(
            text("discard", self.language), QMessageBox.ButtonRole.DestructiveRole
        )
        dialog.addButton(
            text("cancel", self.language), QMessageBox.ButtonRole.RejectRole
        )
        save.setEnabled(self._allowed())
        self._decision = dialog

        def finished() -> None:
            self._decision = None
            chosen = dialog.clickedButton()
            dialog.deleteLater()
            if chosen is save:
                self.save_setup(leave)
            elif chosen is discard:
                self._reload_vision(leave)
            else:
                self.editing.changed.emit()

        dialog.finished.connect(finished)
        dialog.open()
