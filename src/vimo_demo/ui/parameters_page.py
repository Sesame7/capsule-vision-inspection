"""Recipe editing presentation; saved candidate preparation is off the GUI thread."""

from collections.abc import Callable
from concurrent.futures import Future
from threading import Thread
from typing import Literal

from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from vimo_demo.application import ApplicationState, PreparedConfiguration
from vimo_demo.configuration import CheckOverrides, ConfigurationError
from vimo_demo.inspection import CameraId
from vimo_demo.inspection.production import ChannelId

from .inference_bridge import FutureDelivery
from .production_bridge import QtProductionBridge, RecipeChoice
from .production_style import Language, Style, text
from .recipe_catalog import RecipeEntry
from .recipe_editor import GROUPS, CheckDraft, Group, RecipeEditorSession, draft_checks
from .recipe_fields import CheckFields
from .roi_editor import RoiEditorDialog

type DirtyDecision = Literal["save", "discard", "cancel"]


class ParametersPage(QWidget):
    applied = Signal(object)

    def __init__(
        self,
        bridge: QtProductionBridge,
        session: RecipeEditorSession | None,
        *,
        simulation: bool = False,
    ) -> None:
        super().__init__()
        self.setObjectName("parameters")
        self.bridge, self.session = bridge, session
        self.simulation = simulation
        self.roi_dialog: RoiEditorDialog | None = None
        self.language: Language = "zh"
        self._loading = False
        self._entries: tuple[RecipeEntry, ...] = ()
        self._model_ids: tuple[str | None, ...] = (None,)
        self._catalog_error = ""
        self._validation = "unchecked"
        self._error = ""
        self._thread: Thread | None = None
        self._completion: tuple[object, object] | None = None
        self._closing = False
        self._after_busy: Callable[[], None] | None = None
        self._dialog: QDialog | None = None
        self._delivery = FutureDelivery()
        self._delivery.emitter.completed.connect(
            self._prepared, Qt.ConnectionType.QueuedConnection
        )
        self.destroyed.connect(
            self._delivery.detach, Qt.ConnectionType.DirectConnection
        )
        self._monitor = QTimer(self)
        self._monitor.setInterval(20)
        self._monitor.timeout.connect(self._finish_preparation)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        selection = QHBoxLayout()
        self.recipes = QComboBox()
        self.recipes.setMinimumHeight(Style.touch)
        selection.addWidget(self.recipes, 1)
        self.buttons: dict[str, QPushButton] = {}
        for key in ("new_recipe", "reload"):
            button = QPushButton()
            button.setMinimumHeight(Style.touch)
            self.buttons[key] = button
            selection.addWidget(button)
        root.addLayout(selection)
        self.identity = QLabel()
        self.identity.setWordWrap(True)
        self.identity.setTextFormat(Qt.TextFormat.PlainText)
        root.addWidget(self.identity)
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        root.addWidget(self.scroll_area, 1)
        self.editor = QWidget()
        self.editor.setObjectName("recipeEditor")
        self.scroll_area.setWidget(self.editor)
        content = QVBoxLayout(self.editor)
        metadata = QFormLayout()
        self.labels: dict[str, QLabel] = {}
        self.metadata: dict[str, QLineEdit] = {}
        for key in ("recipe_id", "display_name", "product_code"):
            label, line = QLabel(), QLineEdit()
            line.setMinimumHeight(Style.touch)
            self.labels[key], self.metadata[key] = label, line
            metadata.addRow(label, line)
            line.textEdited.connect(self._metadata_changed)
        self.model = QComboBox()
        self.model.setMinimumHeight(Style.touch)
        self.labels["model_choice"] = QLabel()
        metadata.addRow(self.labels["model_choice"], self.model)
        self.model.activated.connect(self._metadata_changed)
        content.addLayout(metadata)
        self.scope = QComboBox()
        self.camera = QComboBox()
        self.channel = QComboBox()
        hierarchy = QHBoxLayout()
        for combo in (self.scope, self.camera, self.channel):
            combo.setMinimumHeight(Style.touch)
            hierarchy.addWidget(combo, 1)
        content.addLayout(hierarchy)
        self.help = QLabel()
        self.help.setWordWrap(True)
        content.addWidget(self.help)
        self.groups: dict[Group, CheckFields] = {
            name: CheckFields(name) for name in GROUPS
        }
        for box in self.groups.values():
            content.addWidget(box)
            box.changed.connect(self._checks_changed)
        self.roi_box = QGroupBox()
        roi_layout = QVBoxLayout(self.roi_box)
        self.roi_help = QLabel()
        self.roi_help.setWordWrap(True)
        roi_layout.addWidget(self.roi_help)
        self.roi_override = QCheckBox()
        self.roi_override.setMinimumHeight(Style.touch)
        roi_layout.addWidget(self.roi_override)
        row = QHBoxLayout()
        self.roi_values = tuple(QLineEdit() for _ in range(4))
        for name, value in zip(
            ("x", "y", "width", "height"), self.roi_values, strict=True
        ):
            column = QVBoxLayout()
            label = QLabel()
            self.labels["roi_" + name] = label
            column.addWidget(label)
            value.setMinimumHeight(Style.touch)
            column.addWidget(value)
            row.addLayout(column, 1)
            value.textEdited.connect(self._roi_changed)
        roi_layout.addLayout(row)
        self.roi_effective = QLabel()
        self.roi_effective.setWordWrap(True)
        roi_layout.addWidget(self.roi_effective)
        content.addWidget(self.roi_box)
        # Outside the disabled numeric group: diagnostics stay viewable while running.
        self.open_roi = QPushButton()
        self.open_roi.setMinimumHeight(Style.touch)
        self.open_roi.clicked.connect(self.open_roi_editor)
        content.addWidget(self.open_roi)
        self.roi_override.toggled.connect(self._roi_changed)
        self.details: dict[str, tuple[QGroupBox, QLabel]] = {}
        for key in ("model_details", "layout_details"):
            group, label = QGroupBox(), QLabel()
            label.setWordWrap(True)
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            group_layout = QVBoxLayout(group)
            group_layout.addWidget(label)
            self.details[key] = (group, label)
            content.addWidget(group)
        content.addStretch()
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        root.addWidget(self.status)
        actions = QHBoxLayout()
        for key in ("validate", "save", "save_as", "apply"):
            button = QPushButton()
            button.setMinimumHeight(Style.touch)
            self.buttons[key] = button
            actions.addWidget(button, 1)
        root.addLayout(actions)
        self.recipes.activated.connect(self._select)
        self.buttons["new_recipe"].clicked.connect(lambda: self.guard_dirty(self.new))
        self.buttons["reload"].clicked.connect(lambda: self.guard_dirty(self.reload))
        self.buttons["validate"].clicked.connect(self.validate)
        self.buttons["save"].clicked.connect(lambda: self.save())
        self.buttons["save_as"].clicked.connect(self.save_as_dialog)
        self.buttons["apply"].clicked.connect(self.apply)
        self.scope.currentIndexChanged.connect(self.show_draft)
        self.camera.currentIndexChanged.connect(self._camera_changed)
        self.channel.currentIndexChanged.connect(self.show_draft)
        bridge.snapshot_changed.connect(self.refresh_state)
        self.set_language("zh")
        self.reload()

    @property
    def busy(self) -> bool:
        return self._thread is not None

    def editable(self) -> bool:
        return (
            self.session is not None
            and not self.busy
            and not self._closing
            and self.bridge.application.snapshot().state
            in (ApplicationState.UNCONFIGURED, ApplicationState.READY)
        )

    def set_language(self, language: Language) -> None:
        self.language = language
        self.recipes.setAccessibleName(text("recipe", language))
        self.camera.setAccessibleName(text("camera_scope", language))
        self.channel.setAccessibleName(text("channel_scope", language))
        self._loading = True
        for key, button in self.buttons.items():
            button.setText(text(key, language))
        for key, label in self.labels.items():
            label.setText(text(key, language))
        index = max(0, self.scope.currentIndex())
        self.scope.clear()
        self.scope.addItems(
            [
                text(key, language)
                for key in ("defaults", "camera_scope", "channel_scope")
            ]
        )
        self.scope.setCurrentIndex(index)
        self.help.setText(
            text("policy_help", language) + "\n" + text("inherit_help", language)
        )
        self.roi_box.setTitle(text("roi_numeric", language))
        self.open_roi.setText(text("roi_editor", language))
        self.roi_help.setText(text("roi_help", language))
        self.roi_override.setText(text("override", language))
        for key, (group, _) in self.details.items():
            group.setTitle(text(key, language))
        self._loading = False
        self.show_draft()

    def reload(self) -> None:
        if self.session is None:
            self.refresh_state()
            return
        try:
            snapshot = self.session.catalog.discover()
            self._entries = snapshot.entries
            self._catalog_error = "\n".join(
                f"{e.path.name}: {e.message}" for e in snapshot.issues
            )
            current = self.session.entry
            candidate = self.bridge.application.snapshot().candidate
            target = next(
                (e for e in self._entries if current and e.path == current.path), None
            )
            if target is None:
                target = next(
                    (
                        e
                        for e in self._entries
                        if candidate and e.recipe.recipe_id == candidate.recipe_id
                    ),
                    None,
                )
            if target is None and self._entries:
                target = self._entries[0]
            if target is not None:
                self.session.load(target)
            else:
                self.session.new()
            self._error = ""
            self._validation = "unchecked"
        except ConfigurationError as exc:
            self._error = str(exc)
        self._loading = True
        self.recipes.clear()
        self.recipes.addItems([e.recipe.display_name for e in self._entries])
        self.camera.clear()
        self.camera.addItems(
            [str(c.camera_id) for c in self.session.machine.layout.cameras]
        )
        self._loading = False
        self._camera_changed()

    def _camera_changed(self) -> None:
        if self._loading or self.session is None:
            return
        self._loading = True
        camera = self.session.machine.layout.cameras[max(0, self.camera.currentIndex())]
        self.channel.clear()
        self.channel.addItems([f"CH{s.channel_id:02d}" for s in camera.slots])
        self._loading = False
        self.show_draft()

    def selected_ids(self) -> tuple[CameraId, ChannelId]:
        assert self.session is not None
        camera = self.session.machine.layout.cameras[max(0, self.camera.currentIndex())]
        return camera.camera_id, camera.slots[
            max(0, self.channel.currentIndex())
        ].channel_id

    def show_draft(self) -> None:
        if self._loading or self.session is None:
            self.refresh_state()
            return
        self._loading = True
        session, draft = self.session, self.session.draft
        self.metadata["recipe_id"].setText(draft.recipe_id)
        self.metadata["display_name"].setText(draft.display_name)
        self.metadata["product_code"].setText(draft.product_code or "")
        self.metadata["recipe_id"].setReadOnly(session.entry is not None)
        self.metadata["recipe_id"].setToolTip(text("id_save_as", self.language))
        self.model.clear()
        self.model.addItem(text("default_model", self.language))
        self.model.addItems([m.display_name for m in session.registry.models])
        self._model_ids = (None, *(m.model_id for m in session.registry.models))
        if draft.model_override_id not in self._model_ids:
            self._model_ids += (draft.model_override_id,)
            self.model.addItem(
                f"{text('invalid', self.language)}: {draft.model_override_id}"
            )
        model_index = self._model_ids.index(draft.model_override_id)
        self.model.setCurrentIndex(model_index)
        camera, channel = self.selected_ids()
        scope = self.scope.currentIndex()
        rows = (
            draft.defaults
            if scope == 0
            else draft.cameras.get(camera)
            if scope == 1
            else draft.channels.get(channel)
        )
        rows = draft_checks(CheckOverrides()) if rows is None else rows
        for key, widget in self.groups.items():
            widget.show_row(rows[key], root=scope == 0, language=self.language)
        base = next(
            s.rect
            for c in session.machine.layout.cameras
            for s in c.slots
            if s.channel_id == channel
        )
        roi = draft.rois.get(channel)
        self.roi_override.setChecked(roi is not None)
        self.roi_override.setText(
            text("override" if roi is not None else "inherit", self.language)
        )
        values = roi or (str(base.x), str(base.y), str(base.width), str(base.height))
        for line, value in zip(self.roi_values, values, strict=True):
            line.setText(value)
            line.setEnabled(scope == 2 and roi is not None)
        self.roi_override.setEnabled(scope == 2)
        if session.entry is not None:
            index = next(
                (
                    i
                    for i, e in enumerate(self._entries)
                    if e.path == session.entry.path
                ),
                -1,
            )
            self.recipes.setCurrentIndex(index)
        else:
            self.recipes.setCurrentIndex(-1)
        self._loading = False
        self.preview()

    def _metadata_changed(self) -> None:
        if self._loading or not self.editable() or self.session is None:
            return
        draft = self.session.draft
        draft.recipe_id = self.metadata["recipe_id"].text()
        draft.display_name = self.metadata["display_name"].text()
        draft.product_code = self.metadata["product_code"].text() or None
        index = self.model.currentIndex()
        draft.model_override_id = self._model_ids[index]
        self.changed()

    def _checks_changed(self) -> None:
        if not self.editable() or self.session is None:
            return
        camera, channel = self.selected_ids()
        rows: dict[Group, CheckDraft] = {
            key: widget.read() for key, widget in self.groups.items()
        }
        scope = self.scope.currentIndex()
        if scope == 0:
            self.session.draft.defaults = rows
        elif scope == 1:
            self.session.draft.cameras[camera] = rows
        else:
            self.session.draft.channels[channel] = rows
        self.changed()

    def _roi_changed(self) -> None:
        if self._loading or not self.editable() or self.session is None:
            return
        _, channel = self.selected_ids()
        self.roi_override.setText(
            text(
                "override" if self.roi_override.isChecked() else "inherit",
                self.language,
            )
        )
        if self.roi_override.isChecked():
            if channel not in self.session.draft.rois:
                try:
                    roi = next(
                        r.roi
                        for r in self.session.resolve().channels
                        if r.channel_id == channel
                    )
                except (ConfigurationError, ValueError) as exc:
                    self._error = str(exc)
                    self.show_draft()
                    return
                for line, value in zip(
                    self.roi_values, (roi.x, roi.y, roi.width, roi.height), strict=True
                ):
                    line.setText(str(value))
            x, y, width, height = (line.text() for line in self.roi_values)
            self.session.draft.rois[channel] = (x, y, width, height)
        else:
            self.session.draft.rois.pop(channel, None)
        for line in self.roi_values:
            line.setEnabled(self.roi_override.isChecked())
        self.changed()

        if not self.roi_override.isChecked():
            self.show_draft()  # Reset displays inherited numbers, not stale override text.

    def changed(self) -> None:
        self._validation, self._error = "unchecked", ""
        self.preview()

    def preview(self) -> None:
        if self.session is None:
            return
        session = self.session
        _, channel = self.selected_ids()
        try:
            resolved = session.resolve()
            row = next(r for r in resolved.channels if r.channel_id == channel)
            checks = row.checks
            for key, value in zip(
                GROUPS,
                (checks.presence, checks.blob, checks.color, checks.vimo),
                strict=True,
            ):
                self.groups[key].show_effective(value)
            self.roi_effective.setText(
                f"CH{channel:02d} · {text('effective', self.language)}: {row.roi}"
            )
        except (ConfigurationError, ValueError) as exc:
            for widget in self.groups.values():
                widget.show_effective(None)
            self.roi_effective.setText(str(exc))
        try:
            model = session.registry.get(
                session.draft.model_override_id or session.default_model_id
            )
            self.details["model_details"][1].setText(
                f"{model.display_name} [{model.model_id}]\n{model.model_path}\nSHA-256: {model.expected_sha256}\n{text('module_id', self.language)}: {model.module_id}"
            )
        except ConfigurationError as exc:
            self.details["model_details"][1].setText(str(exc))
        self.details["layout_details"][1].setText(
            f"{text('camera_count', self.language)}: {len(session.machine.layout.cameras)} · {text('channel_count', self.language)}: {sum(len(c.slots) for c in session.machine.layout.cameras)}\n"
            + "\n".join(
                f"{c.camera_id}: "
                + "; ".join(
                    f"CH{s.channel_id:02d} [{s.rect.x}, {s.rect.y}, {s.rect.width}, {s.rect.height}]"
                    for s in c.slots
                )
                for c in session.machine.layout.cameras
            )
        )
        self.refresh_state()

    def refresh_state(self) -> None:
        session, language = self.session, self.language
        snapshot = self.bridge.application.snapshot()
        allowed = self.editable()
        self.open_roi.setEnabled(
            session is not None and self.scope.currentIndex() == 2 and not self._closing
        )
        if self.roi_dialog is not None:
            self.roi_dialog.refresh()
        # Keep hierarchy navigation and details available while production runs.
        for widget in (
            *self.metadata.values(),
            self.model,
            *self.groups.values(),
            self.roi_box,
        ):
            widget.setEnabled(allowed)
        self.recipes.setEnabled(
            session is not None and not self.busy and not self._closing
        )
        for key, button in self.buttons.items():
            enabled = allowed
            if key == "reload":
                enabled = session is not None and not self.busy and not self._closing
            if key == "save":
                enabled = allowed and session is not None and session.dirty
            if key == "apply":
                enabled = (
                    allowed
                    and session is not None
                    and session.entry is not None
                    and not session.dirty
                )
                if enabled and session is not None:
                    try:
                        session.resolve()
                    except ConfigurationError, ValueError:
                        enabled = False
            button.setEnabled(enabled)
        draft_status = (
            text("no_catalog", language)
            if session is None
            else text(
                "unsaved"
                if session.dirty
                else "saved"
                if session.entry
                else "new_draft",
                language,
            )
        )
        self.identity.setText(
            f"{text('editing', language)}: {session.draft.display_name if session else '--'} · {draft_status}\n{text('candidate', language)}: {snapshot.candidate.recipe_id if snapshot.candidate else '--'} · {text('active', language)}: {snapshot.active.recipe_id if snapshot.active else '--'}"
        )
        note = (
            text("preparing", language)
            if self.busy
            else text(self._validation, language)
        )
        if not allowed:
            note += " · " + text("read_only", language)
        self.status.setText(
            note
            + "\n"
            + (self._error or text("apply_help", language))
            + (
                "\n" + text("catalog_issues", language) + ": " + self._catalog_error
                if self._catalog_error
                else ""
            )
        )

    def open_roi_editor(self) -> None:
        if self.session is None or self.scope.currentIndex() != 2 or self._closing:
            return
        if self.roi_dialog is not None:
            self.roi_dialog.raise_()
            return
        camera, channel = self.selected_ids()
        dialog = RoiEditorDialog(
            self.session,
            camera,
            channel,
            self.bridge.frames,
            self.editable,
            self._graphical_roi_changed,
            language=self.language,
            simulation=self.simulation,
            parent=self,
        )
        self.roi_dialog = dialog
        dialog.finished.connect(self._roi_closed)
        dialog.open()

    def _graphical_roi_changed(self) -> None:
        self.changed()
        self.show_draft()

    def _roi_closed(self) -> None:
        if self.roi_dialog is not None:
            self.roi_dialog.deleteLater()
            self.roi_dialog = None

    def validate(self) -> bool:
        if not self.editable() or self.session is None:
            return False
        try:
            self.session.resolve()
        except (ConfigurationError, ValueError) as exc:
            self._validation, self._error = "invalid", str(exc)
            self.refresh_state()
            return False
        self._validation, self._error = "valid", ""
        self.preview()
        return True

    def save(self, new_identity: tuple[str, str] | None = None) -> bool:
        if not self.editable() or self.session is None:
            return False
        try:
            self.session.save(new_identity=new_identity)
        except (ConfigurationError, ValueError) as exc:
            self._validation, self._error = "invalid", str(exc)
            self.refresh_state()
            return False
        self.reload()
        self._validation = "valid"
        self.refresh_state()
        return True

    def new(self) -> None:
        if self.editable() and self.session is not None:
            self.session.new()
            self.changed()
            self.show_draft()

    def _select(self, index: int) -> None:
        if index < 0 or self.busy:
            return
        entry = self._entries[index]

        def select() -> None:
            assert self.session is not None
            try:
                self.session.load(entry)
                self.changed()
            except ConfigurationError as exc:
                self._error = str(exc)
            self.show_draft()

        self.show_draft()  # Restore current selection while the decision is pending.
        self.guard_dirty(select)

    def guard_dirty(self, action: Callable[[], None]) -> None:
        session = self.session
        if session is None or not session.dirty:
            action()
            return

        def decided(decision: DirtyDecision) -> None:
            if decision == "cancel":
                return
            if decision == "save" and not self.save():
                return
            if decision == "discard":
                try:
                    session.discard()
                except ConfigurationError as exc:
                    self._error = str(exc)
                    self.refresh_state()
                    return
                self.show_draft()
            action()

        self.confirm_dirty(decided)

    def confirm_dirty(self, callback: Callable[[DirtyDecision], None]) -> None:
        if self._dialog is not None:
            return
        dialog = QMessageBox(self)
        dialog.setWindowTitle(text("parameters", self.language))
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
        save.setEnabled(self.editable())
        self._dialog = dialog

        def finished() -> None:
            self._dialog = None
            decision: DirtyDecision = (
                "save"
                if dialog.clickedButton() is save
                else "discard"
                if dialog.clickedButton() is discard
                else "cancel"
            )
            dialog.deleteLater()
            callback(decision)

        dialog.finished.connect(finished)
        dialog.open()

    def save_as_dialog(self) -> None:
        if not self.editable() or self.session is None or self._dialog is not None:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(text("save_as", self.language))
        form = QFormLayout(dialog)
        identity, name = QLineEdit(), QLineEdit(self.session.draft.display_name)
        form.addRow(text("recipe_id", self.language), identity)
        form.addRow(text("display_name", self.language), name)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText(
            text("save", self.language)
        )
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(
            text("cancel", self.language)
        )
        form.addRow(buttons)
        error = QLabel()
        error.setWordWrap(True)
        error.setTextFormat(Qt.TextFormat.PlainText)
        form.addRow(error)

        def save_as() -> None:
            if self.save((identity.text(), name.text())):
                dialog.accept()
            else:
                error.setText(self._error)

        buttons.accepted.connect(save_as)
        buttons.rejected.connect(dialog.reject)

        def finished() -> None:
            self._dialog = None
            dialog.deleteLater()

        dialog.finished.connect(finished)
        self._dialog = dialog
        dialog.open()

    def apply(self) -> None:
        if not self.editable() or self.session is None:
            return
        try:
            recipe = self.session.apply_recipe()
        except ConfigurationError as exc:
            self._error = str(exc)
            self.refresh_state()
            return
        session = self.session
        future: Future[PreparedConfiguration] = Future()

        def prepare() -> None:
            try:
                future.set_result(session.prepare(recipe))
            except BaseException as exc:
                future.set_exception(exc)

        self._delivery.watch("prepare", future)
        self._thread = Thread(target=prepare, name="RecipePreparation", daemon=False)
        self._thread.start()
        self._monitor.start()
        self.refresh_state()

    @Slot(str, object, object)
    def _prepared(self, name: str, value: object, error: object) -> None:
        self._completion = (value, error)
        self._finish_preparation()

    def _finish_preparation(self) -> None:
        if self._thread is None or self._thread.is_alive() or self._completion is None:
            return
        self._thread.join(0)
        self._thread = None
        self._monitor.stop()
        value, error = self._completion
        self._completion = None
        if not self._closing:
            try:
                if isinstance(error, BaseException):
                    raise ConfigurationError(str(error)) from error
                if not isinstance(value, PreparedConfiguration):
                    raise ConfigurationError("Missing prepared configuration")
                self.bridge.application.configure(
                    value
                )  # Authoritative state check again.
                self.applied.emit(RecipeChoice(value, value.configuration.display_name))
                self._error = ""
                self.bridge.refresh()
            except Exception as exc:
                self._error = str(exc)
        self.refresh_state()
        if self._after_busy is not None:
            callback, self._after_busy = self._after_busy, None
            self._closing = False
            callback()

    def request_close(self, callback: Callable[[], None]) -> None:
        if self.busy:
            self._closing = True  # Do not apply a late preparation while closing.
            self._after_busy = lambda: self.guard_dirty(callback)
            self.refresh_state()
        else:
            self.guard_dirty(callback)
