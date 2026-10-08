"""Compact Recipe workflow; configuration and activation remain backend-owned."""

from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import replace
from threading import Thread
from typing import Literal
from uuid import uuid4

from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QStackedLayout,
    QVBoxLayout,
    QWidget,
)

from vimo_demo.application import ApplicationState, PreparedConfiguration
from vimo_demo.configuration import (
    CheckConfiguration,
    ConfigurationError,
    RecipeDefinition,
)

from .inference_bridge import FutureDelivery
from .production_bridge import QtProductionBridge, RecipeChoice
from .production_style import Language, Style, text
from .recipe_catalog import RecipeEntry
from .recipe_editor import RecipeDraft, RecipeEditorSession
from .roi_editor import MachineRoiWorkspace

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
        self.roi_workspace: MachineRoiWorkspace | None = None
        self._apply_finished: Callable[[str], None] | None = None
        self.language: Language = "zh"
        self._loading = False
        self._entries: tuple[RecipeEntry, ...] = ()
        self._model_ids: tuple[str | None, ...] = ()
        self._catalog_error = ""
        self._error = ""
        self._feedback = ""
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
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(18)
        header = QHBoxLayout()
        self.title, self.identity = QLabel(), QLabel()
        self.title.setProperty("role", "state")
        self.identity.setTextFormat(Qt.TextFormat.PlainText)
        header.addWidget(self.title)
        header.addStretch()
        header.addWidget(self.identity)
        root.addLayout(header)
        selection = QHBoxLayout()
        self.recipes = QComboBox()
        self.recipes.setMinimumHeight(Style.touch)
        selection.addWidget(self.recipes, 1)
        self.buttons: dict[str, QPushButton] = {}
        for key in ("new_recipe", "duplicate_recipe", "reload"):
            button = QPushButton()
            button.setMinimumHeight(Style.touch)
            self.buttons[key] = button
            selection.addWidget(button)
        root.addLayout(selection)
        self.metadata: dict[str, QLineEdit] = {}
        self.labels: dict[str, QLabel] = {}
        for key in ("display_name", "product_code"):
            label, line = QLabel(), QLineEdit()
            line.setMinimumHeight(Style.touch)
            self.labels[key], self.metadata[key] = label, line
            root.addWidget(label)
            root.addWidget(line)
            line.textEdited.connect(self._metadata_changed)
        self.model = QComboBox()
        self.model.setMinimumHeight(Style.touch)
        self.labels["inspection_model"] = QLabel()
        root.addWidget(self.labels["inspection_model"])
        root.addWidget(self.model)
        self.rule = QLabel()
        self.rule.setWordWrap(True)
        root.addWidget(self.rule)
        self.notice = QLabel()
        self.notice.setWordWrap(True)
        root.addWidget(self.notice)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        root.addWidget(self.status)
        actions = QHBoxLayout()
        actions.addStretch()
        primary = QPushButton()
        primary.setObjectName("start")
        primary.setMinimumHeight(Style.touch)
        self.buttons["save_apply"] = primary
        actions.addWidget(primary)
        root.addLayout(actions)
        root.addStretch(1)

        # Settings hosts the existing ROI workspace; it shares preparation, not Recipe UI.
        self.settings = QWidget()
        self._pages = QStackedLayout(self.settings)
        self._pages.setContentsMargins(0, 0, 0, 0)
        self._form = QWidget()
        self._pages.addWidget(self._form)
        settings_layout = QVBoxLayout(self._form)
        self.settings_title = QLabel()
        self.settings_title.setProperty("role", "state")
        settings_layout.addWidget(self.settings_title)
        self.open_roi = QPushButton()
        self.open_roi.setMinimumHeight(Style.touch)
        settings_layout.addWidget(self.open_roi, 0, Qt.AlignmentFlag.AlignLeft)
        settings_layout.addStretch()
        self.open_roi.clicked.connect(self.open_roi_editor)

        self.recipes.activated.connect(self._select)
        self.model.activated.connect(self._model_changed)
        self.buttons["new_recipe"].clicked.connect(
            lambda: self.guard_dirty(lambda: self.create_dialog(False))
        )
        self.buttons["duplicate_recipe"].clicked.connect(
            lambda: self.guard_dirty(lambda: self.create_dialog(True))
        )
        self.buttons["reload"].clicked.connect(lambda: self.guard_dirty(self.reload))
        primary.clicked.connect(lambda: self.save_and_apply())
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
        if self.roi_workspace is not None:
            self.roi_workspace.set_language(language)
        self.title.setText(text("parameters", language))
        self.settings_title.setText(text("settings", language))
        self.open_roi.setText(text("roi_workspace", language))
        self.recipes.setAccessibleName(text("recipe", language))
        self.model.setAccessibleName(text("inspection_model", language))
        for key, button in self.buttons.items():
            button.setText(text(key, language))
        for key, label in self.labels.items():
            label.setText(text(key, language))
        for key, line in self.metadata.items():
            line.setAccessibleName(text(key, language))
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
            self._error = self._feedback = ""
        except ConfigurationError as exc:
            self._error = str(exc)
        self.recipes.clear()
        self.recipes.addItems([e.recipe.display_name for e in self._entries])
        self.show_draft()

    def show_draft(self) -> None:
        if self._loading or self.session is None:
            self.refresh_state()
            return
        self._loading = True
        session, draft = self.session, self.session.draft
        self.metadata["display_name"].setText(draft.display_name)
        self.metadata["product_code"].setText(draft.product_code or "")
        self.model.clear()
        self._model_ids = tuple(
            None if m.model_id == session.default_model_id else m.model_id
            for m in session.registry.models
        )
        self.model.addItems([m.display_name for m in session.registry.models])
        effective = draft.model_override_id
        if effective == session.default_model_id:
            effective = None
        if effective not in self._model_ids:
            self._model_ids += (effective,)
            self.model.addItem(text("unavailable_model", self.language))
        self.model.setCurrentIndex(self._model_ids.index(effective))
        self.recipes.setCurrentIndex(
            next(
                (
                    i
                    for i, e in enumerate(self._entries)
                    if session.entry and e.path == session.entry.path
                ),
                -1,
            )
        )
        self._loading = False
        self.refresh_state()

    def _metadata_changed(self) -> None:
        if self._loading or not self.editable() or self.session is None:
            return
        self.session.draft.display_name = self.metadata["display_name"].text()
        self.session.draft.product_code = self.metadata["product_code"].text() or None
        self.changed()

    def _model_changed(self, index: int) -> None:
        if (
            not self._loading
            and self.editable()
            and self.session is not None
            and index >= 0
        ):
            self.session.draft.model_override_id = self._model_ids[index]
            self.changed()

    def changed(self) -> None:
        self._error = self._feedback = ""
        self.refresh_state()

    def refresh_state(self) -> None:
        session, language = self.session, self.language
        allowed = self.editable()
        self.open_roi.setEnabled(
            session is not None
            and session.machine_path is not None
            and not self._closing
        )
        if self.roi_workspace is not None:
            self.roi_workspace.refresh()
        for line in self.metadata.values():
            line.setEnabled(allowed)
        self.model.setEnabled(allowed and self.model.count() > 1)
        self.recipes.setEnabled(
            session is not None and not self.busy and not self._closing
        )
        for key, button in self.buttons.items():
            enabled = allowed
            if key == "reload":
                enabled = session is not None and not self.busy and not self._closing
            elif key == "duplicate_recipe":
                enabled = (
                    allowed and session is not None and bool(session.draft.display_name)
                )
            elif key == "save_apply":
                enabled = (
                    allowed and session is not None and not session.machine_editor.dirty
                )
            button.setEnabled(enabled)
        state = (
            "preparing"
            if self.busy
            else "no_catalog"
            if session is None
            else "unsaved"
            if session.dirty
            else "saved"
            if session.entry
            else "new_draft"
        )
        self.identity.setText(text(state, language))
        advanced = session is not None and bool(
            session.draft.cameras or session.draft.channels
        )
        self.notice.setVisible(advanced)
        self.notice.setText(text("advanced_recipe_notice", language))
        # Never claim all labels participate when persisted policies say otherwise.
        if session is not None:
            simple = (
                session.draft.defaults["vimo"] == RecipeDraft("", "").defaults["vimo"]
                and not advanced
            )
            self.rule.setText(
                text(
                    "all_model_labels" if simple else "configured_detection_rule",
                    language,
                )
            )
        else:
            self.rule.clear()
        self.status.setText(
            self._error
            or self._catalog_error
            or (text(self._feedback, language) if self._feedback else "")
            or (
                text("save_machine_first", language)
                if session and session.machine_editor.dirty
                else ""
            )
        )

    def open_roi_editor(self) -> None:
        if (
            self.session is None
            or self.session.machine_path is None
            or self._closing
            or self.roi_workspace is not None
        ):
            return
        workspace = MachineRoiWorkspace(
            self.session,
            self.session.machine.layout.camera_ids[0],
            self.bridge.frames,
            self.editable,
            self.changed,
            lambda completed: self.apply(completed=completed),
            self._roi_closed,
            language=self.language,
            simulation=self.simulation,
            parent=self.settings,
        )
        self.roi_workspace = workspace
        self._pages.addWidget(workspace)
        self._pages.setCurrentWidget(workspace)

    def _roi_closed(self) -> None:
        if self.roi_workspace is not None:
            workspace, self.roi_workspace = self.roi_workspace, None
            self._pages.setCurrentWidget(self._form)
            self._pages.removeWidget(workspace)
            workspace.deleteLater()

    def leave_workspace(self, after: Callable[[], None]) -> None:
        if self.roi_workspace is None:
            after()
        else:
            self.roi_workspace.request_back(after)

    def save_and_apply(self, after: Callable[[], None] | None = None) -> None:
        if not self.editable() or self.session is None:
            return
        try:
            if self.session.machine_editor.dirty:
                raise ConfigurationError(text("save_machine_first", self.language))
            if self.session.dirty or self.session.entry is None:
                self.session.save()
                self.reload()
            # A clean draft is re-read by apply_recipe; never overwrite external edits.
        except (ConfigurationError, ValueError) as exc:
            self._error = str(exc)
            self.refresh_state()
            return

        def completed(error: str) -> None:
            self._feedback = "" if error else "roi_applied"
            self.refresh_state()
            if not error and after is not None:
                after()

        self.apply(completed=completed)

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

        self.show_draft()
        self.guard_dirty(select)

    def guard_dirty(self, action: Callable[[], None]) -> None:
        session = self.session
        if session is None or not session.dirty:
            action()
            return

        def decided(decision: DirtyDecision) -> None:
            if decision == "save":
                self.save_and_apply(action)
            elif decision == "discard":
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
            text("save_apply", self.language), QMessageBox.ButtonRole.AcceptRole
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

    def create_dialog(self, duplicate: bool) -> None:
        if not self.editable() or self.session is None or self._dialog is not None:
            return
        session = self.session
        dialog = QDialog(self)
        dialog.setWindowTitle(
            text("duplicate_recipe" if duplicate else "new_recipe", self.language)
        )
        form = QFormLayout(dialog)
        name = QLineEdit(session.draft.display_name if duplicate else "")
        name.setMinimumHeight(Style.touch)
        name.setAccessibleName(text("display_name", self.language))
        form.addRow(text("display_name", self.language), name)
        error = QLabel()
        error.setWordWrap(True)
        error.setTextFormat(Qt.TextFormat.PlainText)
        form.addRow(error)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            text("create_recipe", self.language)
        )
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(
            text("cancel", self.language)
        )
        form.addRow(buttons)

        def create() -> None:
            if not self.editable():
                return
            try:
                identity = uuid4().hex
                recipe = (
                    replace(
                        session.draft.to_recipe(),
                        recipe_id=identity,
                        display_name=name.text(),
                    )
                    if duplicate
                    else RecipeDefinition(identity, name.text(), CheckConfiguration())
                )
                session.new()  # Guard has resolved previous unsaved changes.
                session.draft = RecipeDraft.from_recipe(recipe)
                self.changed()
                self.show_draft()
                dialog.accept()
            except (ConfigurationError, ValueError) as exc:
                error.setText(str(exc))

        buttons.accepted.connect(create)
        buttons.rejected.connect(dialog.reject)

        def finished() -> None:
            self._dialog = None
            dialog.deleteLater()

        dialog.finished.connect(finished)
        self._dialog = dialog
        dialog.open()

    def apply(self, *, completed: Callable[[str], None] | None = None) -> None:
        if not self.editable() or self.session is None:
            if completed is not None:
                completed(text("read_only", self.language))
            return
        try:
            recipe = self.session.apply_recipe()
        except ConfigurationError as exc:
            self._error = str(exc)
            self.refresh_state()
            if completed is not None:
                completed(self._error)
            return
        self._apply_finished = completed
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
        if self._apply_finished is not None:
            completed, self._apply_finished = self._apply_finished, None
            completed(
                text("read_only", self.language) if self._closing else self._error
            )
        if self._after_busy is not None:
            callback, self._after_busy = self._after_busy, None
            self._closing = False
            callback()

    def guard_machine(self, callback: Callable[[], None]) -> None:
        if self.session is None or not self.session.machine_editor.dirty:
            callback()
            return
        draft = self.session.machine_editor

        def decided(decision: DirtyDecision) -> None:
            if decision == "cancel":
                return
            try:
                if decision == "save":
                    if not self.editable():
                        self._error = text("read_only", self.language)
                        self.refresh_state()
                        return
                    draft.save()
                else:
                    draft.discard()
            except (ConfigurationError, ValueError) as exc:
                self._error = str(exc)
                self.refresh_state()
                return
            callback()

        self.confirm_dirty(decided)

    def request_close(self, final: Callable[[], None]) -> None:
        if self.roi_workspace is not None and not self.busy:
            self.leave_workspace(lambda: self.request_close(final))
            return

        def callback() -> None:
            self.guard_machine(final)

        if self.busy:
            self._closing = True  # Do not apply a late preparation while closing.
            self._after_busy = lambda: self.guard_dirty(callback)
            self.refresh_state()
        else:
            self.guard_dirty(callback)
