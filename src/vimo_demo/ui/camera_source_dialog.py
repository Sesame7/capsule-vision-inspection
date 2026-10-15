"""Modal source-draft editing and explicitly requested background discovery."""

from collections.abc import Callable
from dataclasses import replace
from typing import cast

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from vimo_demo.application.basler import BaslerDevice, discover_basler_devices
from vimo_demo.configuration import ConfigurationError
from vimo_demo.inspection import CameraId

from .camera_source_editor import CameraSourceDraft
from .production_style import Language, Style, text


class CameraSourceDialog(QDialog):
    def __init__(
        self,
        draft: CameraSourceDraft,
        cameras: tuple[CameraId, ...],
        allowed: Callable[[], bool],
        run_io: Callable[
            [Callable[[], object], Callable[[object, BaseException | None], None]], bool
        ],
        *,
        language: Language,
        parent: QWidget,
    ) -> None:
        super().__init__(parent)
        self._draft, self._cameras = draft, cameras
        self._allowed, self._run_io = allowed, run_io
        self._devices: tuple[BaslerDevice, ...] = ()
        self._busy = False
        self.language: Language = language
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.mode = QComboBox()
        self.mode.addItem("", "folder")
        self.mode.addItem("", "basler")
        self.mode.setMinimumHeight(Style.touch)
        self.mode_label = QLabel()
        self.hint = QLabel()
        self.hint.setWordWrap(True)
        self.hint.setTextFormat(Qt.TextFormat.PlainText)
        self.refresh_button = QPushButton()
        self.refresh_button.setMinimumHeight(Style.touch)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setTextFormat(Qt.TextFormat.PlainText)
        self.error.setProperty("role", "error")
        root = QVBoxLayout(self)
        header = QHBoxLayout()
        header.addWidget(self.mode_label)
        header.addWidget(self.mode, 1)
        root.addLayout(header)
        root.addWidget(self.hint)
        self.mapping_scroll = QScrollArea()
        self.mapping_scroll.setWidgetResizable(True)
        self.mapping_scroll.setStyleSheet(
            f"QWidget#sourceMappings, QScrollArea {{ background: {Style.background}; border: none; }}"
        )
        contents = QWidget()
        contents.setObjectName("sourceMappings")
        mapping = QFormLayout(contents)
        mapping.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        mapping.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )
        mapping.setVerticalSpacing(Style.small)
        self.assignments: dict[CameraId, QComboBox] = {}
        for camera in cameras:
            combo = QComboBox()
            combo.setEditable(False)
            combo.setMinimumHeight(Style.touch)
            combo.setMinimumContentsLength(18)
            combo.setSizeAdjustPolicy(
                QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
            )
            mapping.addRow(QLabel(str(camera)), combo)
            self.assignments[camera] = combo
            combo.currentTextChanged.connect(combo.setToolTip)
        self.mapping_scroll.setWidget(contents)
        root.addWidget(self.mapping_scroll, 1)
        root.addWidget(self.refresh_button)
        root.addWidget(self.status)
        root.addWidget(self.error)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        for button in self.buttons.buttons():
            button.setMinimumHeight(Style.touch)
        root.addWidget(self.buttons)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.refresh_button.clicked.connect(self.refresh_devices)
        self.set_language(language)

    def set_language(self, language: Language) -> None:
        self.language = language
        self.setWindowTitle(text("source_settings", language))
        self.mode_label.setText(text("source_mode", language))
        self.mode.setAccessibleName(self.mode_label.text())
        self.mode.setToolTip(text("source_mode_tip", language))
        for index, key in enumerate(("source_folder_mode", "source_basler_mode")):
            self.mode.setItemText(index, text(key, language))
        self.hint.setText(text("source_mapping_hint", language))
        self.refresh_button.setText(text("source_discover", language))
        for camera, combo in self.assignments.items():
            combo.setAccessibleName(f"{camera} · {text('source_serial', language)}")
            combo.setWhatsThis(text("source_serial_tip", language))
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            text("source_confirm", language)
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(
            text("cancel", language)
        )

    def begin(self) -> None:
        """Fresh dialog-local edits; no SDK request and no change to the source draft."""
        if self._busy:
            return
        self.mode.setCurrentIndex(1 if self._draft.definition.mode == "basler" else 0)
        self._devices = ()
        self._populate(
            {c: self._draft.definition.camera(c).basler_serial for c in self._cameras}
        )
        self.error.clear()
        self.error.setToolTip("")
        self.status.setText(text("source_discovery_idle", self.language))
        screen = self.screen().availableGeometry()
        parent = self.parentWidget()
        bounds = parent.window().size() if parent is not None else screen.size()
        self.resize(
            min(600, bounds.width() - 32, screen.width() - 32),
            min(660, bounds.height() - 32, screen.height() - 64),
        )
        self.open()

    def _selected(self) -> dict[CameraId, str]:
        return {c: str(combo.currentData()) for c, combo in self.assignments.items()}

    def _populate(self, selected: dict[CameraId, str]) -> None:
        devices = {d.serial: d.model for d in self._devices}
        for camera, combo in self.assignments.items():
            serial = selected[camera]
            combo.clear()
            combo.addItem(text("source_unconfigured", self.language), "")
            for found, model in sorted(devices.items()):
                combo.addItem(f"{found} · {model}", found)
            if serial and serial not in devices:
                combo.addItem(
                    f"{serial} · {text('source_not_discovered', self.language)}", serial
                )
            combo.setCurrentIndex(combo.findData(serial))
            combo.setToolTip(combo.currentText())

    def refresh_devices(self) -> None:
        if self._busy or not self._allowed():
            return
        self._busy = True
        self.error.clear()
        self.error.setToolTip("")
        self.status.setText(text("source_discovering", self.language))
        self.refresh_button.setEnabled(False)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)

        def completed(value: object, error: BaseException | None) -> None:
            self._busy = False
            # Cancel hides this retained dialog; the existing setup owner still waits
            # for actual SDK-call/thread completion before workspace/window release.
            self.refresh_button.setEnabled(True)
            self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(True)
            if not self.isVisible():
                return
            selected = self._selected()
            rows = cast(tuple[object, ...], value) if isinstance(value, tuple) else ()
            if error is None and (
                not isinstance(value, tuple)
                or not all(isinstance(d, BaslerDevice) for d in rows)
            ):
                error = TypeError("Invalid device discovery result")
            if error is not None:
                self._devices = ()
                self.status.setText(text("source_discovery_failed", self.language))
                self.error.setText(f"{type(error).__name__}: {error}")
            else:
                self._devices = tuple(d for d in rows if isinstance(d, BaslerDevice))
                self.status.setText(
                    text("source_discovered", self.language).format(count=len(rows))
                )
            self._populate(selected)

        if not self._run_io(discover_basler_devices, completed):
            completed(None, RuntimeError(text("preparing", self.language)))

    def accept(self) -> None:
        if self._busy or not self._allowed():
            return
        selected = self._selected()
        try:
            # Change only dialog-owned fields, retaining independent Folder/Loop edits.
            definition = replace(
                self._draft.definition,
                mode="basler" if self.mode.currentIndex() == 1 else "folder",
                cameras=tuple(
                    replace(c, basler_serial=selected[c.camera_id])
                    for c in self._draft.definition.cameras
                ),
            )
            definition.validate_cameras(self._cameras)
        except ConfigurationError as exc:
            self.error.setText(text("source_mapping_invalid", self.language))
            self.error.setToolTip(f"{type(exc).__name__}: {exc}")
            return
        self._draft.definition = definition
        super().accept()
