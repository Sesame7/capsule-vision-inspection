"""Compact Recipe workflow; configuration and activation remain backend-owned."""

from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import replace
from threading import Thread
from typing import Literal
from uuid import uuid4

from PySide6.QtCore import QSize, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import (
    QBoxLayout,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from vimo_demo.application import ApplicationState, PreparedConfiguration
from vimo_demo.configuration import (
    CheckConfiguration,
    ConfigurationError,
    ConfigurationIOError,
    ModelArtifactError,
    RecipeDefinition,
)

from .image_tools import image_icon
from .inference_bridge import FutureDelivery
from .production_bridge import QtProductionBridge, RecipeChoice
from .production_style import (
    Language,
    Style,
    production_icon,
    shell_icon,
    text,
    theme_colors,
)
from .recipe_catalog import RecipeEntry
from .recipe_editor import RecipeDraft, RecipeEditorSession
from .roi_editor import MachineRoiWorkspace
from .traditional_preview import PreviewMode, TraditionalPreview

type DirtyDecision = Literal["save", "discard", "cancel"]


class ParametersPage(QWidget):
    applied = Signal(object)
    preview_changed = Signal()
    setup_changed = Signal()
    sources_verified = Signal()

    def __init__(
        self,
        bridge: QtProductionBridge,
        session: RecipeEditorSession | None,
        *,
        simulation: bool = False,
        configuration_messages: tuple[str, ...] = (),
        configuration_details: str = "",
    ) -> None:
        super().__init__()
        self.setObjectName("parameters")
        self.bridge, self.session = bridge, session
        self.simulation = simulation
        self.configuration_messages = configuration_messages
        self.configuration_details = configuration_details
        self.roi_workspace: MachineRoiWorkspace | None = None
        self.preview: TraditionalPreview | None = None
        self._setup_finished: Callable[[object, BaseException | None], None] | None = (
            None
        )
        self._apply_finished: Callable[[str], None] | None = None
        self.language: Language = "zh"
        self._loading = False
        self._entries: tuple[RecipeEntry, ...] = ()
        self._model_ids: tuple[str | None, ...] = ()
        self._catalog_error = ""
        self._error = ""
        self._error_key = "recipe_error"
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
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(Style.small)
        self.buttons: dict[str, QPushButton] = {}
        self.labels: dict[str, QLabel] = {}
        self.summary_scroll = QScrollArea(self)
        self.summary_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.summary_scroll.setWidgetResizable(True)
        self.summary_scroll.setSizeAdjustPolicy(
            QScrollArea.SizeAdjustPolicy.AdjustToContents
        )
        self.summary_scroll.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum
        )
        summary = QWidget()
        summary.setObjectName("recipeSummary")
        sections = QVBoxLayout(summary)
        sections.setContentsMargins(0, 0, 0, 0)
        sections.setSpacing(Style.small)
        self.summary_scroll.setWidget(summary)
        root.addWidget(self.summary_scroll)
        self.recipe_section = QFrame(self)
        self.model_section = QFrame(self)
        self.inspection_section = QFrame(self)
        self.tuning_section = QFrame(self)
        for section in (
            self.recipe_section,
            self.model_section,
            self.inspection_section,
            self.tuning_section,
        ):
            section.setObjectName("recipeSection")
        recipe = QVBoxLayout(self.recipe_section)
        recipe.setContentsMargins(10, 8, 10, 8)
        recipe.setSpacing(Style.small)
        selection = QHBoxLayout()
        selection.setSpacing(Style.small)
        self.title, self.identity = QLabel(), QLabel()
        self.identity.setTextFormat(Qt.TextFormat.PlainText)
        selection.addWidget(self.title)
        self.recipes = QComboBox()
        self.recipes.setMinimumHeight(Style.touch)
        self.recipes.setMinimumContentsLength(18)
        self.recipes.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.recipes.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        selection.addWidget(self.recipes, 1)
        for key in ("new_recipe", "duplicate_recipe", "delete_recipe", "reload"):
            button = QPushButton(self.recipe_section)
            button.setFixedSize(Style.touch, Style.touch)
            button.setIconSize(QSize(Style.icon, Style.icon))
            self.buttons[key] = button
            selection.addWidget(button)
        recipe.addLayout(selection)
        self._metadata_row = QHBoxLayout()
        self._metadata_row.setSpacing(Style.space)
        self._metadata_fields: list[QWidget] = []
        self.metadata: dict[str, QLineEdit] = {}
        for key in ("display_name", "product_code"):
            field = QWidget()
            column = QVBoxLayout(field)
            column.setContentsMargins(0, 0, 0, 0)
            column.setSpacing(2)
            label, line = QLabel(), QLineEdit()
            line.setMinimumHeight(Style.touch)
            line.setMinimumWidth(180)
            line.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            self.labels[key], self.metadata[key] = label, line
            column.addWidget(label)
            column.addWidget(line)
            self._metadata_fields.append(field)
            self._metadata_row.addWidget(field, 1)
            line.textEdited.connect(self._metadata_changed)
        recipe.addLayout(self._metadata_row)
        sections.addWidget(self.recipe_section)
        model_layout = QVBoxLayout(self.model_section)
        model_layout.setContentsMargins(10, 8, 10, 8)
        model_layout.setSpacing(2)
        model_row = QHBoxLayout()
        self.labels["inspection_model"] = QLabel()
        model_row.addWidget(self.labels["inspection_model"])
        self.model = QComboBox()
        self.model.setMinimumHeight(Style.touch)
        self.model.setMinimumContentsLength(18)
        self.model.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.model.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        model_row.addWidget(self.model, 1)
        self.model_more = QPushButton(self.model_section)
        self.model_more.setFixedSize(Style.touch, Style.touch)
        self.model_more.setCheckable(True)
        self.model_more.setIcon(shell_icon("chevron"))
        model_row.addWidget(self.model_more)
        model_layout.addLayout(model_row)
        self.model_status, self.model_details = QLabel(), QLabel()
        for label in (self.model_status, self.model_details):
            label.setWordWrap(True)
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setProperty("role", "secondary")
            model_layout.addWidget(label)
        self.model_details.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.model_details.hide()
        self.model_more.toggled.connect(self.model_details.setVisible)
        sections.addWidget(self.model_section)
        inspection = QVBoxLayout(self.inspection_section)
        inspection.setContentsMargins(10, 8, 10, 8)
        inspection.setSpacing(2)
        header = QHBoxLayout()
        self.inspection_title, self.occupancy = QLabel(), QLabel()
        header.addWidget(self.inspection_title)
        header.addStretch()
        header.addWidget(self.occupancy)
        inspection.addLayout(header)
        inspections = QGridLayout()
        inspections.setColumnStretch(1, 1)
        inspections.setHorizontalSpacing(Style.space)
        inspections.setVerticalSpacing(0)
        self.capsule_enabled: dict[PreviewMode, QCheckBox] = {}
        self.capsule_summary: dict[PreviewMode, QLabel] = {}
        self.expand_tuning: dict[PreviewMode, QPushButton] = {}
        modes: tuple[PreviewMode, ...] = ("size", "color")
        for index, mode in enumerate(modes):
            enabled, summary = QCheckBox(), QLabel()
            enabled.setMinimumHeight(Style.touch)
            summary.setProperty("role", "secondary")
            summary.setWordWrap(True)
            expand = QPushButton(self.inspection_section)
            expand.setCheckable(True)
            expand.setFixedSize(Style.touch, Style.touch)
            expand.setIcon(shell_icon("chevron"))
            (
                self.capsule_enabled[mode],
                self.capsule_summary[mode],
                self.expand_tuning[mode],
            ) = enabled, summary, expand
            inspections.addWidget(enabled, index, 0)
            inspections.addWidget(summary, index, 1)
            inspections.addWidget(expand, index, 2)

            def toggle(checked: bool, m: PreviewMode = mode) -> None:
                self._capsule_changed(m, checked)

            def expand_mode(_checked: bool, m: PreviewMode = mode) -> None:
                if self.preview is not None and self.preview.mode == m:
                    self.leave_preview(lambda: None)
                else:
                    self.open_preview(m)

            enabled.toggled.connect(toggle)
            expand.clicked.connect(expand_mode)
        inspection.addLayout(inspections)
        self.rule, self.notice = QLabel(), QLabel()
        for label in (self.rule, self.notice):
            label.setWordWrap(True)
            label.setProperty("role", "secondary")
        self.advanced = QPushButton(self.inspection_section)
        self.advanced.setCheckable(True)
        self.advanced.setMinimumHeight(Style.touch)
        self.advanced.toggled.connect(self.notice.setVisible)
        advanced_row = QHBoxLayout()
        advanced_row.addWidget(self.rule, 1)
        advanced_row.addWidget(self.advanced)
        inspection.addLayout(advanced_row)
        inspection.addWidget(self.notice)
        sections.addWidget(self.inspection_section)
        self._tuning_layout = QVBoxLayout(self.tuning_section)
        self._tuning_layout.setContentsMargins(6, 4, 6, 4)
        self._tuning_layout.setSpacing(0)
        root.addWidget(self.tuning_section, 1)
        self.tuning_section.hide()
        self._empty_space = QWidget()
        root.addWidget(self._empty_space, 1)
        actions = QHBoxLayout()
        actions.setSpacing(Style.small)
        actions.addWidget(self.identity)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        actions.addWidget(self.status, 1)
        discard = QPushButton(self)
        discard.setFixedSize(Style.touch, Style.touch)
        discard.setIcon(production_icon("reset"))
        self.buttons["recipe_discard"] = discard
        actions.addWidget(discard)
        primary = QPushButton(self)
        primary.setObjectName("start")
        primary.setMinimumHeight(Style.touch)
        self.buttons["save_apply"] = primary
        actions.addWidget(primary)
        root.addLayout(actions)

        # Vision hosts the existing ROI workspace; it shares preparation, not Recipe UI.
        self.settings = QWidget()
        self._settings_layout = QVBoxLayout(self.settings)
        self._settings_layout.setContentsMargins(0, 0, 0, 0)

        self.recipes.activated.connect(self._select)
        self.model.activated.connect(self._model_changed)
        self.buttons["new_recipe"].clicked.connect(
            lambda: self.guard_dirty(lambda: self.create_dialog(False))
        )
        self.buttons["duplicate_recipe"].clicked.connect(
            lambda: self.guard_dirty(lambda: self.create_dialog(True))
        )
        self.buttons["reload"].clicked.connect(lambda: self.guard_dirty(self.reload))
        self.buttons["delete_recipe"].clicked.connect(self.request_delete)
        self.buttons["recipe_discard"].clicked.connect(self.request_discard)
        primary.clicked.connect(lambda: self.save_and_apply())
        bridge.snapshot_changed.connect(self.refresh_state)
        bridge.source_connection_changed.connect(self.refresh_state)
        bridge.frames_changed.connect(
            self.refresh_state
        )  # Eligibility only; references stay frozen.
        self.set_language("zh")
        self.reload()
        if self.configuration_messages:
            # Initial discovery errors already have localized bootstrap summaries
            # and detailed tooltips. Later user-operation errors keep precedence.
            self._error = ""
            self.refresh_state()

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
        if self.preview is not None:
            self.preview.set_language(language)
        if self.roi_workspace is not None:
            self.roi_workspace.set_language(language)
        self.title.setText(text("parameters", language))
        self.inspection_title.setText("检测" if language == "zh" else "Inspection")
        self.occupancy.setText(
            "● 占位检测 · 常开" if language == "zh" else "● Occupancy · Always on"
        )
        self.model_more.setToolTip("模型详情" if language == "zh" else "Model details")
        self.model_more.setAccessibleName(self.model_more.toolTip())
        self.advanced.setText("高级策略" if language == "zh" else "Advanced policies")
        for mode, button in self.expand_tuning.items():
            button.setToolTip(
                ("调整尺寸" if mode == "size" else "调整颜色")
                if language == "zh"
                else ("Tune Size" if mode == "size" else "Tune Color")
            )
            button.setAccessibleName(button.toolTip())
        self.recipes.setAccessibleName(text("recipe", language))
        self.model.setAccessibleName(text("inspection_model", language))
        for key, button in self.buttons.items():
            button.setText(text(key, language) if key == "save_apply" else "")
            button.setToolTip(text(key, language))
            button.setAccessibleName(text(key, language))
        for key, label in self.labels.items():
            label.setText(text(key, language))
        for key, line in self.metadata.items():
            line.setAccessibleName(text(key, language))
        self._arrange_fields()
        self.show_draft()

    def refresh_theme(self) -> None:
        colors = theme_colors(self)
        self.model_more.setIcon(shell_icon("chevron", colors.primary))
        self.buttons["recipe_discard"].setIcon(production_icon("reset", colors.primary))
        for key, tool in (
            ("new_recipe", "plus"),
            ("duplicate_recipe", "copy"),
            ("delete_recipe", "delete"),
            ("reload", "refresh"),
        ):
            self.buttons[key].setIcon(image_icon(tool, colors.primary))
        for button in self.expand_tuning.values():
            button.setIcon(
                shell_icon(
                    "chevron", colors.accent if button.isChecked() else colors.primary
                )
            )

    def _arrange_fields(self) -> None:
        self._metadata_row.setDirection(
            QBoxLayout.Direction.LeftToRight
            if self.width() >= 560
            else QBoxLayout.Direction.TopToBottom
        )

    def resizeEvent(self, event: QResizeEvent) -> None:
        self._arrange_fields()
        super().resizeEvent(event)

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
            if target is None and self._entries and self.session.auto_select_first:
                target = self._entries[0]
            if target is not None:
                self.session.load(target)
            else:
                self.session.new()
            self._error = self._feedback = ""
        except ConfigurationError as exc:
            self._report_error(exc)
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
        if self.preview is not None:
            self.preview.show_settings()
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
        self.setup_changed.emit()
        self._error = self._feedback = ""
        self.refresh_state()

    def refresh_state(self) -> None:
        self.refresh_theme()
        session, language = self.session, self.language
        allowed = self.editable()
        if self.roi_workspace is not None:
            self.roi_workspace.refresh()
        if self.preview is not None:
            self.preview.refresh_state()
        for mode in ("size", "color"):
            check = self.capsule_enabled[mode]
            check.setText(
                ("尺寸检测" if mode == "size" else "颜色检测")
                if language == "zh"
                else ("Size inspection" if mode == "size" else "Color inspection")
            )
            check.setEnabled(allowed)
            if session is not None:
                settings = session.draft.capsule
                check.blockSignals(True)
                check.setChecked(
                    settings.size.enabled if mode == "size" else settings.color.enabled
                )
                check.blockSignals(False)
                self.capsule_summary[mode].setText(
                    f"W {settings.size.target_width} ± {settings.size.width_tolerance} · H {settings.size.target_height} ± {settings.size.height_tolerance} px"
                    if mode == "size"
                    else (
                        ("颜色容差 " if language == "zh" else "Color tolerance ")
                        + str(settings.color.hue_tolerance)
                    )
                )
        for mode, button in self.expand_tuning.items():
            button.setEnabled(
                session is not None
                and not self.busy
                and not self._closing
                and not session.machine_editor.dirty
            )
            button.setChecked(self.preview is not None and self.preview.mode == mode)
        self._present_model()
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
            elif key == "recipe_discard":
                enabled = allowed and session is not None and session.dirty
            elif key == "delete_recipe":
                enabled = (
                    allowed
                    and session is not None
                    and session.entry is not None
                    and session.entry.recipe.recipe_id not in self._protected_recipes()
                )
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
        self.identity.setText("" if state == "saved" else text(state, language))
        self.identity.setToolTip(text(state, language))
        self.identity.setAccessibleName(text(state, language))
        advanced = session is not None and bool(
            session.draft.cameras or session.draft.channels
        )
        self.advanced.setVisible(session is not None)
        self.notice.setVisible(self.advanced.isChecked())
        if session is not None:
            draft = session.draft
            self.notice.setText(
                str(draft.defaults.vimo)
                + "\n"
                + "\n".join(
                    f"{key}: {value.vimo}"
                    for key, value in (*draft.cameras.items(), *draft.channels.items())
                )
            )
        # Never claim all labels participate when persisted policies say otherwise.
        if session is not None:
            simple = (
                session.draft.defaults.vimo == RecipeDraft("", "").defaults.vimo
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
        startup_message = " · ".join(
            text(key, language) for key in self.configuration_messages
        )
        feedback = text(self._feedback, language) if self._feedback else ""
        self.status.setText(
            (text(self._error_key, language) if self._error else "")
            or (
                " · ".join(part for part in (feedback, startup_message) if part)
                if startup_message
                else ""
            )
            or (text("recipe_error", language) if self._catalog_error else "")
            or (text(self._feedback, language) if self._feedback else "")
            or (
                text("save_machine_first", language)
                if session and session.machine_editor.dirty
                else ""
            )
        )
        self.status.setToolTip(
            self._error or self.configuration_details or self._catalog_error
        )

    def _report_error(
        self, error: BaseException | str, key: str = "recipe_error"
    ) -> None:
        # Keep the original details for diagnostics and existing completion callbacks.
        self._error, self._error_key = str(error), key

    def _protected_recipes(self) -> frozenset[str]:
        snapshot = self.bridge.application.snapshot()
        return frozenset(
            identity.recipe_id
            for identity in (snapshot.active, snapshot.candidate)
            if identity is not None
        )

    def _confirm_recipe_action(self, key: str, action: Callable[[], None]) -> None:
        if not self.editable() or self.session is None or self._dialog is not None:
            return
        dialog = QMessageBox(self)
        dialog.setTextFormat(Qt.TextFormat.PlainText)
        dialog.setWindowTitle(text(key, self.language))
        draft = self.session.draft
        if key == "delete_recipe" and self.session.entry is not None:
            draft = RecipeDraft.from_recipe(self.session.entry.recipe)
        identity = (
            f"{draft.display_name}\nID: {draft.recipe_id}"
            if draft.recipe_id
            else text("new_draft", self.language)
        )
        dialog.setText(text(key + "_question", self.language) + "\n\n" + identity)
        accept = dialog.addButton(
            text(key, self.language), QMessageBox.ButtonRole.AcceptRole
        )
        cancel = dialog.addButton(
            text("cancel", self.language), QMessageBox.ButtonRole.RejectRole
        )
        dialog.setDefaultButton(cancel)
        self._dialog = dialog

        def finished() -> None:
            chosen = dialog.clickedButton() is accept
            self._dialog = None
            dialog.deleteLater()
            if chosen and self.editable():
                action()

        dialog.finished.connect(finished)
        dialog.open()

    def request_discard(self) -> None:
        session = self.session
        if session is None or not session.dirty:
            return
        entry = session.entry

        def discard() -> None:
            if session.entry != entry:
                self._report_error("Recipe selection changed; retry Discard")
            else:
                try:
                    session.discard()
                    self._error = self._feedback = ""
                except ConfigurationError as exc:
                    self._report_error(exc, "recipe_discard_error")
            self.show_draft()

        self._confirm_recipe_action("recipe_discard", discard)

    def request_delete(self) -> None:
        session = self.session
        if not self.editable() or session is None or session.entry is None:
            return
        entry = session.entry
        if entry.recipe.recipe_id in self._protected_recipes():
            self._report_error(text("recipe_delete_protected", self.language))
            self.refresh_state()
            return

        def delete() -> None:
            if not self.editable() or session.entry != entry:
                return
            try:
                session.delete(protected_ids=self._protected_recipes())
                self._entries = tuple(e for e in self._entries if e.path != entry.path)
                self.recipes.clear()
                self.recipes.addItems([e.recipe.display_name for e in self._entries])
                self._error = self._feedback = ""
            except ConfigurationError as exc:
                self._report_error(exc, "recipe_delete_error")
            self.show_draft()

        self._confirm_recipe_action("delete_recipe", lambda: self.leave_preview(delete))

    def _present_model(self) -> None:
        session = self.session
        if session is None:
            self.model_status.clear()
            self.model_details.clear()
            return
        try:
            model = session.registry.get(
                session.draft.model_override_id or session.default_model_id
            )
        except ConfigurationError:
            self.model_status.setText(text("unavailable_model", self.language))
            self.model_details.clear()
            return
        snapshot = self.bridge.application.snapshot()
        candidate = snapshot.candidate
        verified = (
            candidate is not None
            and candidate.model_id == model.model_id
            and candidate.model_sha256 == model.expected_sha256
        )
        running = (
            snapshot.state is ApplicationState.RUNNING
            and snapshot.active is not None
            and snapshot.active.model_id == model.model_id
            and snapshot.active.model_sha256 == model.expected_sha256
        )
        phase = text(
            "model_running"
            if running
            else "model_verified"
            if verified
            else "model_configured",
            self.language,
        )
        self.model_status.setText(f"{model.model_id} · {phase}")
        self.model_details.setText(
            f"ID {model.model_id} · {text('model_module', self.language)} {model.module_id}\n{text('model_interface', self.language)}\n{model.model_path}\nSHA-256: {model.expected_sha256}\n{text('model_verification_tip', self.language)}"
        )
        self.model_status.setToolTip(text("model_verification_tip", self.language))

    def _capsule_changed(self, mode: PreviewMode, enabled: bool) -> None:
        if self.session is None or not self.editable():
            return
        settings = self.session.draft.capsule
        self.session.draft.capsule = (
            replace(settings, size=replace(settings.size, enabled=enabled))
            if mode == "size"
            else replace(settings, color=replace(settings.color, enabled=enabled))
        )
        self.changed()
        if self.preview is not None:
            self.preview.show_settings()

    def open_preview(self, mode: PreviewMode | None = None) -> None:
        if (
            self.session is None
            or self.busy
            or self._closing
            or self.session.machine_editor.dirty
        ):
            return
        if self.preview is not None:
            if mode is not None:
                self.preview.set_mode(mode)
            self.refresh_state()
            return
        if mode is None:
            settings = self.session.draft.capsule
            mode = (
                "color"
                if settings.color.enabled and not settings.size.enabled
                else "size"
            )
        self.preview = TraditionalPreview(
            self.session,
            self.bridge.frames,
            self.editable,
            self.changed,
            self._preview_closed,
            mode=mode,
            language=self.language,
            parent=self,
            connection=self.bridge.source_connection,
        )
        self._tuning_layout.addWidget(self.preview)
        self.preview.mode_changed.connect(self.refresh_state)
        self.tuning_section.show()
        self._empty_space.hide()
        self.refresh_state()
        self.preview_changed.emit()

    def leave_preview(self, after: Callable[[], None]) -> None:
        def closed() -> None:
            self._preview_closed()
            after()

        if self.preview is None:
            after()
        else:
            self.preview.request_close(closed)

    def _preview_closed(self) -> None:
        if self.preview is not None:
            workspace, self.preview = self.preview, None
            self._tuning_layout.removeWidget(workspace)
            self.tuning_section.hide()
            self._empty_space.show()
            workspace.deleteLater()
        self.preview_changed.emit()
        self.show_draft()

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
            run_io=self.run_setup_io,
            connection=self.bridge.source_connection,
        )
        self.roi_workspace = workspace
        workspace.sources_verified.connect(self.sources_verified.emit)
        workspace.feedback_changed.connect(self.setup_changed.emit)
        self._settings_layout.addWidget(workspace, 1)

    def _roi_closed(self) -> None:
        if self.roi_workspace is not None:
            workspace, self.roi_workspace = self.roi_workspace, None
            self._settings_layout.removeWidget(workspace)
            workspace.deleteLater()

    def leave_workspace(self, after: Callable[[], None]) -> None:
        if self.busy:
            self._after_busy = lambda: self.leave_workspace(after)
            return
        if self.roi_workspace is None:
            after()
        else:
            self.roi_workspace.request_back(after)

    def save_and_apply(self, after: Callable[[], None] | None = None) -> None:
        if not self.editable() or self.session is None:
            return
        if self.session.machine_editor.dirty or (
            self.session.camera_sources is not None
            and self.session.camera_sources.dirty
        ):
            self._report_error(
                text("save_machine_first", self.language), "save_machine_first"
            )
            self.refresh_state()
            return
        try:
            if self.session.dirty or self.session.entry is None:
                self.session.save()
                self.reload()
            # A clean draft is re-read by apply_recipe; never overwrite external edits.
        except (ConfigurationError, ValueError) as exc:
            self._report_error(
                exc,
                "recipe_save_error"
                if isinstance(exc, ConfigurationIOError)
                else "recipe_validation_error",
            )
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
                self._report_error(exc)
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
                    self._report_error(exc)
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
                error.setText(text("recipe_validation_error", self.language))
                error.setToolTip(str(exc))

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
            self._report_error(exc, "recipe_error")
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

    def run_setup_io(
        self,
        work: Callable[[], object],
        completed: Callable[[object, BaseException | None], None],
    ) -> bool:
        """Reuse the single preparation slot; no IO queue or GUI-thread decoding."""
        if self.busy or self._closing:
            return False
        self._setup_finished = completed
        future: Future[object] = Future()

        def read() -> None:
            try:
                future.set_result(work())
            except BaseException as exc:
                future.set_exception(exc)

        self._delivery.watch("camera_setup", future)
        self._thread = Thread(target=read, name="CameraSetupIO", daemon=False)
        self._thread.start()
        self._monitor.start()
        self.refresh_state()
        self.setup_changed.emit()
        return True

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
        setup, self._setup_finished = self._setup_finished, None
        completed, self._apply_finished = self._apply_finished, None
        # A setup callback may start preparation. Only finish THIS job's callback.
        if setup is not None:
            setup(
                value,
                RuntimeError("Closing")
                if self._closing
                else error
                if isinstance(error, BaseException)
                else None,
            )
        elif not self._closing:
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
                self._report_error(
                    exc,
                    "model_prepare_error"
                    if isinstance(error, ModelArtifactError)
                    else "recipe_prepare_error",
                )
        self.refresh_state()
        if completed is not None:
            completed(
                text("read_only", self.language) if self._closing else self._error
            )
        self.setup_changed.emit()
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
                        self._report_error(
                            text("read_only", self.language), "read_only"
                        )
                        self.refresh_state()
                        return
                    draft.save()
                else:
                    draft.discard()
            except (ConfigurationError, ValueError) as exc:
                self._report_error(exc, "vision_save_error")
                self.refresh_state()
                return
            callback()

        self.confirm_dirty(decided)

    def request_close(self, final: Callable[[], None]) -> None:
        if self.preview is not None:
            self.leave_preview(lambda: self.request_close(final))
            return
        if self.roi_workspace is not None and not self.busy:
            self.leave_workspace(lambda: self.request_close(final))
            return

        def callback() -> None:
            def finalized() -> None:
                # All preparation/reference jobs have finished by this point.
                # Revoke the unparented sender before QApplication destroys Qt
                # objects in an unspecified order during process exit.
                self._delivery.detach()
                final()

            self.guard_machine(finalized)

        if self.busy:
            self._closing = True  # Do not apply a late preparation while closing.
            self._after_busy = lambda: self.request_close(final)
            self.refresh_state()
        else:
            self.guard_dirty(callback)
