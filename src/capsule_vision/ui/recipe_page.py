"""Compact Recipe workflow; configuration and activation remain backend-owned."""

from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path
from threading import Event, Thread
from typing import Literal, Protocol, cast

from PySide6.QtCore import (
    QEvent,
    QObject,
    QPoint,
    QRect,
    QSize,
    Qt,
    QTimer,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtGui import QDesktopServices, QResizeEvent, QShowEvent
from PySide6.QtWidgets import (
    QBoxLayout,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLayoutItem,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from capsule_vision.application import (
    ModelPreparationRequest,
    ModelPreparationState,
)
from capsule_vision.application.identity_models import (
    IdentityError,
    Role,
    StaleSessionError,
)
from capsule_vision.application.process_preview import ProcessObservation
from capsule_vision.configuration import (
    CheckConfiguration,
    ConfigurationError,
    ConfigurationIOError,
    ModelDefinition,
    RecipeDefinition,
)
from capsule_vision.configuration.catalog import RecipeEntry
from capsule_vision.configuration.drafts import RecipeDraft
from capsule_vision.configuration.process_parameters import ProcessCatalog
from capsule_vision.inference import read_model_labels
from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import Language

from .editing import EditingCoordinator
from .future_delivery import FutureDelivery
from .image_tools import image_icon
from .model_controls import ModelControls
from .permissions import failure_key, permits
from .process_parameters import ProcessParametersCard
from .production_bridge import QtProductionBridge
from .recipe_tuning import PreviewMode, RecipeTuning
from .style import (
    Style,
    button_width,
    production_icon,
    set_button_caption,
    shell_icon,
    theme_colors,
)

type DirtyDecision = Literal["save", "discard", "cancel"]


class ModelLabelReader(Protocol):
    def __call__(
        self, path: Path, /, *, module_id: str, expected_sha256: str
    ) -> tuple[str, ...]: ...


class ProcessPreviewReader(Protocol):
    def __call__(
        self,
        catalog: ProcessCatalog,
        catalog_path: Path | None,
        cancelled: Event,
        completed: Callable[[ProcessObservation | None, BaseException | None], None],
    ) -> None: ...


class _RecipeSummary(QScrollArea):
    """Recompute the preferred height when inline content changes/wraps."""

    def sizeHint(self) -> QSize:
        hint = super().sizeHint()
        content = self.widget()
        if content is not None:
            height = content.heightForWidth(self.viewport().width())
            hint.setHeight(
                (height if height >= 0 else content.sizeHint().height())
                + 2 * self.frameWidth()
            )
        return hint

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.widget() and event.type() == QEvent.Type.LayoutRequest:
            self.updateGeometry()
        return super().eventFilter(watched, event)


class _LabelFlow(QLayout):
    """Natural-width label controls; height follows the available row width."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self.setContentsMargins(0, 0, 0, 0)
        self.setSpacing(Style.small)

    def addItem(self, item: QLayoutItem) -> None:
        self._items.append(item)
        self.invalidate()

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int) -> QLayoutItem | None:
        if 0 <= index < len(self._items):
            item = self._items.pop(index)
            self.invalidate()
            return item
        return None

    def expandingDirections(self) -> Qt.Orientation:
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._arrange(QRect(0, 0, width, 0), measure=True)

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        return size

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._arrange(rect, measure=False)

    def _arrange(self, rect: QRect, *, measure: bool) -> int:
        x, y, height = rect.x(), rect.y(), 0
        for item in self._items:
            size = item.sizeHint()
            if x > rect.x() and x + size.width() > rect.x() + rect.width():
                x, y, height = rect.x(), y + height + self.spacing(), 0
            if not measure:
                item.setGeometry(QRect(QPoint(x, y), size))
            x += size.width() + self.spacing()
            height = max(height, size.height())
        return y + height - rect.y()


class RecipePage(QWidget):
    preview_changed = Signal()
    setup_changed = Signal()

    def __init__(
        self,
        bridge: QtProductionBridge,
        editing: EditingCoordinator,
        *,
        label_reader: ModelLabelReader | None = None,
        configuration_messages: tuple[str, ...] = (),
        configuration_details: str = "",
    ) -> None:
        super().__init__()
        self.setObjectName("page_recipe")
        self.bridge, self.editing, self.session = bridge, editing, editing.session
        self._label_reader = label_reader
        self.configuration_messages = configuration_messages
        self.configuration_details = configuration_details
        self.preview: RecipeTuning | None = None
        self.language: Language = "zh"
        self._loading = False
        self._entries: tuple[RecipeEntry, ...] = ()
        self._browse_entry: RecipeEntry | None = None
        self._model_ids: tuple[str | None, ...] = ()
        self._catalog_error = ""
        self._error = ""
        self._error_key = "recipe_error"
        self._feedback = ""
        self._closing = False
        self._after_labels: Callable[[], None] | None = None
        self._dialog: QDialog | None = None
        self._labels_model: ModelDefinition | None = None
        self._labels_read_model: ModelDefinition | None = None
        self._labels_thread: Thread | None = None
        self._labels_completion: tuple[object, object] | None = None
        self._labels_error = ""
        self.label_checks: dict[str, QPushButton] = {}
        self._process_reader: ProcessPreviewReader | None = None
        self._process_stop: Event | None = None
        self._delivery = FutureDelivery()
        self._delivery.emitter.completed.connect(
            self._model_observed, Qt.ConnectionType.QueuedConnection
        )
        self.destroyed.connect(
            self._delivery.detach, Qt.ConnectionType.DirectConnection
        )
        self._monitor = QTimer(self)
        self._monitor.setInterval(20)
        self._monitor.timeout.connect(self._finish_label_read)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(Style.space)
        self.buttons: dict[str, QPushButton] = {}
        self.labels: dict[str, QLabel] = {}
        self.summary_scroll = _RecipeSummary(self)
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
        sections.setSpacing(Style.space)
        sections.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.summary_scroll.setWidget(summary)
        summary.installEventFilter(self.summary_scroll)
        root.addWidget(self.summary_scroll)
        self.recipe_section = QFrame(self)
        self.model_section = QFrame(self)
        self.inspection_section = QFrame(self)
        self.tuning_section = QFrame(self)
        for section in (
            self.recipe_section,
            self.model_section,
            self.inspection_section,
        ):
            section.setObjectName("recipeSection")
        for section in (
            self.recipe_section,
            self.model_section,
            self.inspection_section,
        ):
            section.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum
            )
        recipe = QVBoxLayout(self.recipe_section)
        recipe.setContentsMargins(Style.space, Style.space, Style.space, Style.space)
        recipe.setSpacing(Style.small)
        selection = QHBoxLayout()
        selection.setSpacing(Style.small)
        self.identity = QLabel()
        self.identity.setTextFormat(Qt.TextFormat.PlainText)
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
            column.setSpacing(Style.small)
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
        model_layout = QVBoxLayout(self.model_section)
        model_layout.setContentsMargins(
            Style.space, Style.space, Style.space, Style.space
        )
        model_layout.setSpacing(Style.small)
        model_row = QHBoxLayout()
        model_row.setSpacing(Style.small)
        self.model = QComboBox()
        self.model.setMinimumHeight(Style.touch)
        self.model.setMinimumContentsLength(18)
        self.model.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.model.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        model_row.addWidget(self.model, 1)
        self.cache_status = QLabel(self.model_section)
        self.cache_status.setFixedSize(Style.icon, Style.touch)
        self.cache_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        model_row.addWidget(self.cache_status)
        cache_prepare = QPushButton(self.model_section)
        cache_prepare.setFixedSize(Style.touch, Style.touch)
        cache_prepare.setIconSize(QSize(Style.icon, Style.icon))
        self.buttons["cache_prepare"] = cache_prepare
        cache_prepare.clicked.connect(self.prepare_model_cache)
        model_row.addWidget(cache_prepare)
        self.model_controls = ModelControls(self, editing)
        model_row.addWidget(self.model_controls)
        model_layout.addLayout(model_row)
        self.label_choices = QWidget(self.model_section)
        self.label_choices.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum
        )
        self._label_flow = _LabelFlow(self.label_choices)
        model_layout.addWidget(self.label_choices)
        self.label_choices.hide()
        inspection = QVBoxLayout(self.inspection_section)
        inspection.setContentsMargins(
            Style.space, Style.space, Style.space, Style.space
        )
        inspection.setSpacing(Style.small)
        inspections = QHBoxLayout()
        inspections.setSpacing(Style.space)
        self.capsule_enabled: dict[PreviewMode, QPushButton] = {}
        self.expand_tuning: dict[PreviewMode, QPushButton] = {}
        modes: tuple[PreviewMode, ...] = ("size", "color")
        for mode in modes:
            group = QHBoxLayout()
            group.setSpacing(Style.small)
            enabled = QPushButton()
            enabled.setObjectName("recipeChoice")
            enabled.setCheckable(True)
            enabled.setMinimumWidth(2 * Style.touch)
            enabled.setMinimumHeight(Style.touch)
            enabled.setStyleSheet("font-size: 18px; font-weight: 600;")
            expand = QPushButton(self.inspection_section)
            expand.setCheckable(True)
            expand.setMinimumHeight(Style.touch)
            expand.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            expand.setObjectName("recipeRule")
            expand.setIconSize(QSize(Style.icon, Style.icon))
            (
                self.capsule_enabled[mode],
                self.expand_tuning[mode],
            ) = enabled, expand
            group.addWidget(enabled)
            group.addWidget(expand, 1)
            inspections.addLayout(group, 1)

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
        self.process_parameters = ProcessParametersCard()
        self.process_parameters.edited.connect(self._process_edited)
        self.process_parameters.plc_requested.connect(self._read_process)
        self.process_parameters.plc_ended.connect(self._cancel_process_read)
        self.archive_section = QFrame(self)
        self.archive_section.setObjectName("recipeSection")
        self.archive_section.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum
        )
        self._archive_layout = QGridLayout(self.archive_section)
        self._archive_layout.setContentsMargins(
            Style.space, Style.space, Style.space, Style.space
        )
        self._archive_layout.setSpacing(Style.small)
        self._archive_stacked: bool | None = None
        self._archive_conditions = QWidget(self.archive_section)
        self._archive_conditions.setSizePolicy(
            QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed
        )
        conditions = QHBoxLayout(self._archive_conditions)
        conditions.setContentsMargins(0, 0, 0, 0)
        conditions.setSpacing(Style.small)
        self.labels["image_archive"] = QLabel()
        conditions.addWidget(self.labels["image_archive"])
        self.archive_checks: dict[str, QPushButton] = {}
        for name in ("ok", "ng", "error", "empty"):
            check = QPushButton(name.upper())
            check.setObjectName("recipeChoice")
            check.setCheckable(True)
            check.setMinimumHeight(Style.touch)
            check.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
            check.toggled.connect(self._archive_edited)
            self.archive_checks[name] = check
            conditions.addWidget(check)
        self._archive_path = QWidget(self.archive_section)
        self._archive_path.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        path_row = QHBoxLayout(self._archive_path)
        path_row.setContentsMargins(0, 0, 0, 0)
        path_row.setSpacing(Style.small)
        self.archive_directory = QLineEdit()
        self.archive_directory.setMinimumHeight(Style.touch)
        self.archive_directory.setMinimumWidth(6 * Style.touch)
        self.archive_directory.textEdited.connect(self._archive_edited)
        path_row.addWidget(self.archive_directory, 1)
        for key, callback in (
            ("archive_browse", self._browse_archive),
            ("archive_open", self._open_archive),
        ):
            button = QPushButton(self.archive_section)
            button.setFixedSize(Style.touch, Style.touch)
            button.clicked.connect(callback)
            self.buttons[key] = button
            path_row.addWidget(button)
        self.archive_section.installEventFilter(self)
        for section in (
            self.recipe_section,
            self.archive_section,
            self.process_parameters,
            self.model_section,
        ):
            sections.addWidget(section)
        self._tuning_layout = QVBoxLayout(self.tuning_section)
        self._tuning_layout.setContentsMargins(0, 0, 0, 0)
        self._tuning_layout.setSpacing(0)
        # One traditional-inspection surface, with the large shared canvas outside
        # the compact summary scroll. No additional scroll or measurement owner.
        inspection.addWidget(self.tuning_section, 1)
        root.addWidget(self.inspection_section, 1)
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
        primary.setProperty("compactAction", True)
        primary.setMinimumHeight(Style.touch)
        self.buttons["save"] = primary
        actions.addWidget(primary)
        root.addLayout(actions)
        self._tab_order()

        self.recipes.activated.connect(self._select)
        self.model.activated.connect(self._model_changed)
        self.buttons["new_recipe"].clicked.connect(
            lambda: self.guard_dirty(lambda: self.create_dialog(False))
        )
        self.buttons["duplicate_recipe"].clicked.connect(
            lambda: self.guard_dirty(lambda: self.create_dialog(True))
        )
        self.buttons["reload"].clicked.connect(
            lambda: self.reload() if self.browsing else self.guard_dirty(self.reload)
        )
        self.buttons["delete_recipe"].clicked.connect(self.request_delete)
        self.buttons["recipe_discard"].clicked.connect(self.request_discard)
        primary.clicked.connect(lambda: self.save())
        editing.changed.connect(self.refresh_state)
        editing.recipe_refresh_changed.connect(self.refresh_state)
        editing.applied.connect(self._configuration_applied)
        bridge.snapshot_changed.connect(self.refresh_state)
        bridge.source_connection_changed.connect(self.refresh_state)
        bridge.model_preparation_changed.connect(self.refresh_state)
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
        return self.editing.busy

    @property
    def closing(self) -> bool:
        return self._closing or self.editing.closing

    def selected_model(self) -> ModelDefinition | None:
        return self._selected_model()

    def _tab_order(self) -> None:
        """Follow the visible card order, including late-arriving label buttons."""
        controls: list[QWidget] = [
            self.recipes,
            *(
                self.buttons[k]
                for k in ("new_recipe", "duplicate_recipe", "delete_recipe", "reload")
            ),
            *self.metadata.values(),
            *self.archive_checks.values(),
            self.archive_directory,
            self.buttons["archive_browse"],
            self.buttons["archive_open"],
            self.process_parameters.view_plc,
            self.process_parameters.expand,
            *(
                self.process_parameters.fields[p.id]
                for group in self.process_parameters.groups
                for p in self.process_parameters.catalog.parameters
                if p.group == group
            ),
            self.model,
            *(
                self.buttons[k]
                for k in (
                    "cache_prepare",
                    "model_import",
                    "model_rename",
                    "model_delete",
                )
            ),
            *self.label_checks.values(),
            self.capsule_enabled["size"],
            self.expand_tuning["size"],
            self.capsule_enabled["color"],
            self.expand_tuning["color"],
        ]
        preview = self.preview
        if preview is not None:
            controls.extend(
                (
                    preview.camera_tabs,
                    preview.import_button,
                    preview.runtime_button,
                    preview.previous_button,
                    preview.reference,
                    preview.next_button,
                    preview.refresh_button,
                    preview.fit_button,
                    preview.actual_button,
                    preview.spins["target_length"],
                    preview.length_sample,
                    preview.spins["length_tolerance"],
                    preview.length_slider,
                    preview.hue,
                    preview.sample,
                    preview.tolerance,
                )
            )
        controls.extend((self.buttons["recipe_discard"], self.buttons["save"]))
        for previous, following in zip(controls, controls[1:], strict=False):
            QWidget.setTabOrder(previous, following)

    def model_operation_failed(self, error: BaseException) -> None:
        self._report_error(error, "model_action_error")
        self.refresh_state()

    @property
    def browsing(self) -> bool:
        return not permits(self.bridge.application, Role.ENGINEER)

    def _displayed_recipe(self) -> RecipeDefinition | RecipeDraft:
        if self.browsing:
            return (
                self._browse_entry.recipe if self._browse_entry else RecipeDraft("", "")
            )
        return self.session.draft if self.session else RecipeDraft("", "")

    def editable(self) -> bool:
        return not self.browsing and self.editing.editable() and not self.closing

    def identity_changed(self) -> None:
        self.process_parameters.end_plc_preview()
        if self._dialog is not None:
            self._dialog.reject()
        if self.model_controls.dialog is not None:
            self.model_controls.dialog.reject()
        if self.browsing:
            if self.preview is not None:
                self.tuning_section.hide()
                if not self.preview.closing and not self.editing.closing:
                    self.leave_preview(lambda: None)
            self.reload()
        else:
            self._browse_entry = None
            self.show_draft()

    @Slot(object)
    def _configuration_applied(self, _prepared: object) -> None:
        self._error = ""
        self.refresh_state()

    @Slot(object, str)
    def _operation_failed(self, error: object, key: str) -> None:
        self._report_error(
            error if isinstance(error, BaseException) else str(error), key
        )
        self.refresh_state()

    def set_language(self, language: Language) -> None:
        self.language = language
        self.process_parameters.set_language(language)
        if self.preview is not None:
            self.preview.set_language(language)
        self.archive_directory.setPlaceholderText(text("archive_folder", language))
        self.archive_directory.setAccessibleName(text("archive_directory", language))
        for key, check in self.archive_checks.items():
            check.setToolTip(text(f"archive_{key}", language))
            check.setAccessibleName(check.toolTip())
        self.label_choices.setAccessibleName(text("recipe_labels", language))
        for mode, button in self.expand_tuning.items():
            button.setToolTip(text("tune_" + mode, language))
            button.setAccessibleName(text("tune_" + mode, language))
        self.recipes.setAccessibleName(text("recipe", language))
        self.model.setAccessibleName(text("inspection_model", language))
        for key, button in self.buttons.items():
            button.setText(text(key, language) if key == "save" else "")
            button.setToolTip(text(key, language))
            button.setAccessibleName(text(key, language))
        primary = self.buttons["save"]
        set_button_caption(
            primary,
            text("save", language),
            button_width(primary, ("save",), padding=2),
        )
        for key, label in self.labels.items():
            label.setText(
                text(
                    {"product_code": "recipe_product_code"}.get(key, key),
                    language,
                )
            )
            label.setToolTip(text(key, language))
            label.setAccessibleName(text(key, language))
        archive = self.labels["image_archive"]
        archive.setText(text("archive_save_images", language))
        archive.setToolTip(text("image_archive", language) + " · PNG")
        archive.setAccessibleDescription(archive.toolTip())
        for key, line in self.metadata.items():
            line.setAccessibleName(text(key, language))
        self._arrange_fields()
        self.show_draft()

    def refresh_theme(self) -> None:
        self.process_parameters.refresh_theme()
        self.model_controls.refresh_theme()
        colors = theme_colors(self)
        self.buttons["recipe_discard"].setIcon(production_icon("reset", colors.primary))
        self.buttons["cache_prepare"].setIcon(image_icon("accelerate", colors.primary))
        self.buttons["archive_browse"].setIcon(image_icon("folder", colors.primary))
        self.buttons["archive_open"].setIcon(image_icon("open_folder", colors.primary))
        for key, tool in (
            ("new_recipe", "plus"),
            ("duplicate_recipe", "copy"),
            ("delete_recipe", "delete"),
            ("reload", "refresh"),
        ):
            self.buttons[key].setIcon(image_icon(tool, colors.primary))
        for button in self.expand_tuning.values():
            button.setIcon(
                shell_icon("chevron", colors.primary)
                if button.isChecked()
                else image_icon("next", colors.primary)
            )

    def _arrange_fields(self) -> None:
        self._metadata_row.setDirection(
            QBoxLayout.Direction.LeftToRight
            if self.width() >= 560
            else QBoxLayout.Direction.TopToBottom
        )
        self._arrange_archive()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.archive_section and event.type() in (
            QEvent.Type.Resize,
            QEvent.Type.Show,
            QEvent.Type.LayoutRequest,
        ):
            self._arrange_archive()
        return super().eventFilter(watched, event)

    def _arrange_archive(self) -> None:
        margins = self._archive_layout.contentsMargins()
        required = (
            self._archive_conditions.sizeHint().width()
            + self._archive_path.minimumSizeHint().width()
            + margins.left()
            + margins.right()
            + self._archive_layout.horizontalSpacing()
        )
        stacked = self.archive_section.contentsRect().width() < required
        if stacked != self._archive_stacked:
            self._archive_stacked = stacked
            self._archive_layout.removeWidget(self._archive_conditions)
            self._archive_layout.removeWidget(self._archive_path)
            self._archive_layout.addWidget(self._archive_conditions, 0, 0)
            self._archive_layout.addWidget(
                self._archive_path, 1 if stacked else 0, 0 if stacked else 1
            )
            self._archive_layout.setColumnStretch(0, 1 if stacked else 0)
            self._archive_layout.setColumnStretch(1, 0 if stacked else 1)
            self.archive_section.updateGeometry()
            self.summary_scroll.updateGeometry()

    def resizeEvent(self, event: QResizeEvent) -> None:
        self._arrange_fields()
        super().resizeEvent(event)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if not self.labels_loading:
            self._labels_model = (
                None  # Re-entry reads the artifact, never a timer loop.
            )
        self._ensure_model_labels()

    def reload(self) -> None:
        if self.session is None:
            self.refresh_state()
            return
        browsing = self.browsing
        revision = self.bridge.application.session_revision
        try:
            if self.editable() or browsing:
                self.session.reload_models()
            snapshot = self.session.catalog.discover()
            self._entries = snapshot.entries
            self._catalog_error = "\n".join(
                (
                    *self.session.model_issues,
                    *(f"{e.path.name}: {e.message}" for e in snapshot.issues),
                )
            )
            current = (
                self._browse_entry
                if browsing and self._browse_entry
                else self.session.entry
            )
            candidate = self.bridge.application.snapshot().candidate
            target = next(
                (e for e in self._entries if current and e.path == current.path), None
            )
            if target is None and (not browsing or current is None):
                target = next(
                    (
                        e
                        for e in self._entries
                        if candidate and e.recipe.recipe_id == candidate.recipe_id
                    ),
                    None,
                )
            if (
                target is None
                and (not browsing or current is None)
                and self._entries
                and self.session.auto_select_first
            ):
                target = self._entries[0]
            if browsing:
                self._browse_entry = target
            elif revision != self.bridge.application.session_revision:
                raise StaleSessionError("Session changed during Recipe reload")
            elif target is not None:
                self.session.load(target)
            else:
                self.session.new()
            self._error = self._feedback = ""
        except (ConfigurationError, IdentityError) as exc:
            self._report_error(exc)
        self.recipes.clear()
        self.recipes.addItems([e.recipe.display_name for e in self._entries])
        if self._labels_error:
            self._labels_model = None  # Explicit reload retries a failed metadata read.
        self.show_draft()

    def show_draft(self) -> None:
        if self._loading or self.session is None:
            self.refresh_state()
            return
        self._loading = True
        session, draft = self.session, self._displayed_recipe()
        if not self.browsing:
            self._browse_entry = session.entry
        entry = self._browse_entry if self.browsing else session.entry
        previous_catalog = self.process_parameters.catalog
        self.process_parameters.show_values(
            entry.process_catalog
            if self.browsing and entry is not None
            else session.process_catalog,
            {key: str(value) for key, value in draft.process_parameters}
            if isinstance(draft, RecipeDefinition)
            else draft.process_parameters,
        )
        if self.process_parameters.catalog != previous_catalog:
            self._tab_order()
        self.metadata["display_name"].setText(draft.display_name)
        self.metadata["product_code"].setText(draft.product_code or "")
        self.archive_directory.setText(draft.image_archive.root_directory)
        for name, check in self.archive_checks.items():
            check.setChecked(getattr(draft.image_archive, f"save_{name}"))
        self.model.clear()
        self._model_ids = (None,) + tuple(m.model_id for m in session.registry.models)
        self.model.addItem(text("unavailable_model", self.language))
        self.model.addItems(
            [
                f"{m.display_name} · {m.expected_sha256[:8]}"
                for m in session.registry.models
            ]
        )
        effective = draft.model_id
        if effective not in self._model_ids:
            self._model_ids += (effective,)
            self.model.addItem(text("unavailable_model", self.language))
        self.model.setCurrentIndex(self._model_ids.index(effective))
        self.recipes.setCurrentIndex(
            next(
                (
                    i
                    for i, e in enumerate(self._entries)
                    if entry and e.path == entry.path
                ),
                -1,
            )
        )
        self._loading = False
        if self.preview is not None and not self.browsing:
            self.preview.show_settings()
        self.refresh_state()

    def _process_edited(self) -> None:
        if self._loading or not self.editable() or self.session is None:
            return
        self.session.draft.process_parameters = self.process_parameters.values()
        self.changed()

    def bind_process_reader(self, reader: ProcessPreviewReader) -> None:
        self._process_reader = reader
        self.refresh_state()

    def _read_process(self) -> None:
        card, session, reader = (
            self.process_parameters,
            self.session,
            self._process_reader,
        )
        if reader is None or session is None or self.closing:
            card.show_plc_values(None)
            return
        self._cancel_process_read()
        stop = self._process_stop = Event()
        revision = self.bridge.application.session_revision
        catalog = card.catalog

        def completed(
            value: ProcessObservation | None, error: BaseException | None
        ) -> None:
            if (
                self._process_stop is not stop
                or stop.is_set()
                or not card.plc_previewing
            ):
                return
            if (
                self.closing
                or not self.isVisible()
                or card.catalog != catalog
                or revision != self.bridge.application.session_revision
            ):
                card.end_plc_preview()
                return
            if error is not None or value is None:
                card.show_plc_values(
                    None,
                    detail=str(error or "Missing PLC observation"),
                    error_key=failure_key(error, "process_plc_unavailable")
                    if error
                    else "process_plc_unavailable",
                )
            else:
                card.show_plc_values(
                    dict(value.values),
                    detail=(
                        f"FINS/TCP {value.endpoint.host}:{value.endpoint.port} · "
                        f"{value.completed_at.astimezone():%H:%M:%S}"
                    ),
                )

        reader(catalog, session.catalog.process_catalog_path, stop, completed)

    def _cancel_process_read(self) -> None:
        if self._process_stop is not None:
            self._process_stop.set()
            self._process_stop = None

    def _metadata_changed(self) -> None:
        if self._loading or not self.editable() or self.session is None:
            return
        self.session.draft.display_name = self.metadata["display_name"].text()
        self.session.draft.product_code = self.metadata["product_code"].text() or None
        self.changed()

    def _archive_edited(self) -> None:
        if self._loading or not self.editable() or self.session is None:
            return
        draft = self.session.draft.image_archive
        draft.root_directory = self.archive_directory.text()
        draft.save_ok = self.archive_checks["ok"].isChecked()
        draft.save_ng = self.archive_checks["ng"].isChecked()
        draft.save_error = self.archive_checks["error"].isChecked()
        draft.save_empty = self.archive_checks["empty"].isChecked()
        self.changed()

    def _browse_archive(self) -> None:
        if not self.editable():
            return
        chosen = QFileDialog.getExistingDirectory(
            self, text("archive_browse", self.language), self.archive_directory.text()
        )
        if chosen and self.editable():
            self.archive_directory.setText(chosen)
            self._archive_edited()

    def _open_archive(self) -> None:
        path = Path(self.archive_directory.text())
        if (
            not path.is_absolute()
            or not path.is_dir()
            or not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        ):
            self._report_error(text("archive_issue", self.language))
            self.refresh_state()

    def _model_changed(self, index: int) -> None:
        if (
            not self._loading
            and self.editable()
            and self.session is not None
            and index >= 0
        ):
            self.session.draft.model_id = self._model_ids[index]
            self.changed()

    def changed(self) -> None:
        self.setup_changed.emit()
        self._error = self._feedback = ""
        self.refresh_state()

    def refresh_state(self) -> None:
        session, language = self.session, self.language
        allowed = self.editable()
        self.process_parameters.set_plc_available(
            self._process_reader is not None
            and session is not None
            and not self.closing
            and permits(self.bridge.application, Role.ENGINEER)
        )
        self.process_parameters.set_editable(allowed)
        self.archive_directory.setEnabled(allowed or self.browsing)
        self.archive_directory.setReadOnly(not allowed)
        self.archive_directory.setToolTip(
            self.archive_directory.text() or text("archive_directory", language)
        )
        for check in self.archive_checks.values():
            check.setEnabled(allowed)
        if self.preview is not None:
            self.preview.refresh_state()
        for mode in ("size", "color"):
            check = self.capsule_enabled[mode]
            caption = text(mode, language)
            check.setText(caption)
            check.setAccessibleName(text(mode + "_check", language))
            check.setToolTip(check.accessibleName())
            check.setEnabled(allowed)
            if session is not None:
                settings = self._displayed_recipe().capsule
                check.blockSignals(True)
                check.setChecked(
                    settings.size.enabled if mode == "size" else settings.color.enabled
                )
                check.blockSignals(False)
                summary = (
                    f"L {settings.size.target_length} ± {settings.size.length_tolerance} px"
                    if mode == "size"
                    else f"H {settings.color.target_hue} ± {settings.color.hue_tolerance}"
                )
                self.expand_tuning[mode].setText(summary)
                self.expand_tuning[mode].setAccessibleDescription(
                    caption + " · " + summary
                )
        for mode, button in self.expand_tuning.items():
            button.setEnabled(
                session is not None
                and not self.browsing
                and not self.busy
                and not self.closing
                and not session.machine_editor.dirty
                and session.machine_editor.persisted
            )
            button.setChecked(self.preview is not None and self.preview.mode == mode)
        self.refresh_theme()
        self._present_model()
        for line in self.metadata.values():
            line.setEnabled(allowed or self.browsing)
            line.setReadOnly(not allowed)
        self.model.setEnabled(allowed and self.model.count() > 1)
        self.recipes.setEnabled(
            session is not None and not self.busy and not self.closing
        )
        for key, button in self.buttons.items():
            enabled = allowed
            if key == "archive_open" and self.browsing:
                enabled = bool(self.archive_directory.text())
            if key == "reload":
                enabled = session is not None and not self.busy and not self.closing
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
            elif key == "cache_prepare":
                enabled = (
                    allowed
                    and self._selected_model() is not None
                    and not self.bridge.closing
                    and not self.bridge.application.preparation_snapshot().owns_resources
                )
            button.setEnabled(enabled)
            if key not in ("reload", "archive_open"):
                button.setToolTip(
                    text("permission_engineer" if self.browsing else key, language)
                )
        self.model_controls.refresh()
        state = (
            "preparing"
            if self.busy
            else "no_catalog"
            if session is None
            else "saved"
            if self.browsing and self._browse_entry is not None
            else "select_saved_recipe"
            if self.browsing
            else "unsaved"
            if session.dirty
            else "saved"
            if session.entry
            else "new_draft"
        )
        self.identity.setText("" if state == "saved" else text(state, language))
        self.identity.setToolTip("")
        self.identity.setAccessibleName(text(state, language))
        startup_message = " · ".join(
            text(key, language) for key in self.configuration_messages
        )
        feedback = text(self._feedback, language) if self._feedback else ""
        refresh = self.editing.recipe_refresh
        refresh_details = ""
        if (
            refresh is not None
            and session is not None
            and session.entry is not None
            and refresh[0] == session.entry.recipe.recipe_id
            and self._feedback == "saved"
        ):
            feedback = f"{feedback} · {text(refresh[1], language)}"
            refresh_details = str(refresh[2]) if refresh[2] else ""
            if state == "preparing":
                self.identity.setText("")  # The combined Save feedback already says it.
        self.status.setText(
            (text(self._error_key, language) if self._error else "")
            or (
                " · ".join(part for part in (feedback, startup_message) if part)
                if startup_message
                else ""
            )
            or (text("recipe_error", language) if self._catalog_error else "")
            or feedback
            or (text("recipe_labels_error", language) if self._labels_error else "")
        )
        self.status.setToolTip(
            "\n".join(
                part
                for part in (
                    self._error if self._error_key != "cache_failed" else "",
                    self.configuration_details,
                    self._catalog_error,
                    self._labels_error,
                    refresh_details,
                )
                if part
            )
        )
        self.status.setAccessibleDescription(self.status.toolTip())

    def _report_error(
        self, error: BaseException | str, key: str = "recipe_error"
    ) -> None:
        # Keep the original details for diagnostics and existing completion callbacks.
        self._error, self._error_key = str(error), failure_key(error, key)

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
        revision = self.bridge.application.session_revision

        def finished() -> None:
            chosen = dialog.clickedButton() is accept
            self._dialog = None
            dialog.deleteLater()
            if chosen and revision != self.bridge.application.session_revision:
                self._report_error(StaleSessionError("Session decision was superseded"))
                self.refresh_state()
            elif chosen and self.editable():
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

        revision = self.bridge.application.session_revision

        def delete() -> None:
            if not self.editable() or session.entry != entry:
                return

            def finished(_: object, error: BaseException | None) -> None:
                if error is None:
                    self._entries = tuple(
                        e for e in self._entries if e.path != entry.path
                    )
                    self.recipes.clear()
                    self.recipes.addItems(
                        [e.recipe.display_name for e in self._entries]
                    )
                    self._error = self._feedback = ""
                else:
                    self._report_error(error, "recipe_delete_error")
                self.show_draft()

            if not self.editing.run_model_io(
                lambda: session.delete(
                    protected_ids=self._protected_recipes(), revision=revision
                ),
                finished,
                revision=revision,
            ):
                finished(None, ConfigurationError("Recipe deletion is unavailable"))

        self._confirm_recipe_action("delete_recipe", lambda: self.leave_preview(delete))

    def _selected_model(self) -> ModelDefinition | None:
        model_id = self._displayed_recipe().model_id
        if self.session is None or model_id is None:
            return None
        try:
            return self.session.registry.get(model_id)
        except ConfigurationError:
            return None

    @property
    def labels_loading(self) -> bool:
        return self._labels_thread is not None

    def _present_model(self) -> None:
        model = self._selected_model()
        preparation = self.bridge.application.preparation_snapshot()
        requested = self._cache_request()
        matches = (
            requested is not None
            and preparation.request is not None
            and replace(
                preparation.request,
                model=replace(
                    preparation.request.model, display_name=requested.model.display_name
                ),
            )
            == requested
        )
        state = preparation.state if matches else ModelPreparationState.UNKNOWN
        key = {
            ModelPreparationState.UNKNOWN: "unknown",
            ModelPreparationState.PREPARING: "preparing",
            ModelPreparationState.SUCCEEDED: "technical_success",
            ModelPreparationState.FAILED: "cache_failed",
        }[state]
        self.cache_status.setToolTip("")
        self.cache_status.setAccessibleName(
            text("cache_prepare", self.language) + " · " + text(key, self.language)
        )
        colors = theme_colors(self)
        icon = production_icon(
            {
                ModelPreparationState.UNKNOWN: "unknown",
                ModelPreparationState.PREPARING: "STARTING",
                ModelPreparationState.SUCCEEDED: "READY",
                ModelPreparationState.FAILED: "FAULT",
            }[state],
            colors.secondary if state is ModelPreparationState.UNKNOWN else None,
        )
        self.cache_status.setPixmap(icon.pixmap(Style.icon, Style.icon))
        tooltip = (
            f"{model.display_name}\nID: {model.model_id}\n{text('configured_hash', self.language)}: {model.expected_sha256}"
            if model
            else text("unavailable_model", self.language)
        )
        self.model.setToolTip(tooltip)
        self.model.setAccessibleDescription(tooltip)
        if model != self._labels_model:
            self._clear_label_choices()
            self._labels_error = ""
            if self.isVisible() and not self.closing:
                QTimer.singleShot(0, self._ensure_model_labels)
        disabled = self._displayed_recipe().defaults.vimo.disabled_labels
        retained = tuple(label for label in disabled if label not in self.label_checks)
        self.label_choices.setToolTip(
            text("recipe_labels_retained", self.language) + " " + ", ".join(retained)
            if retained
            else text("recipe_labels", self.language)
        )
        editable = self.editable() and model == self._labels_model
        for label, check in self.label_checks.items():
            check.blockSignals(True)
            check.setChecked(label not in disabled)
            check.blockSignals(False)
            check.setEnabled(editable)
            check.setToolTip(text("recipe_default_label", self.language) + " " + label)

    def _cache_request(self) -> ModelPreparationRequest | None:
        model, session = self._selected_model(), self.session
        if model is None or session is None:
            return None
        path = Path(model.model_path)
        if not path.is_absolute():
            path = session.base_directory / path
        if session.managed_models:
            path = session.managed_models.model_path(model)
        # Initialization needs no camera/image/dimensions. No guessed sample shape.
        return ModelPreparationRequest(
            model,
            path.absolute(),
            managed_model=session.managed_models is not None,
        )

    def prepare_model_cache(self) -> None:
        request = self._cache_request()
        if request is None or not self.editable() or self.bridge.closing:
            return
        try:
            future = self.bridge.application.prepare_model_cache(request)
        except Exception as exc:
            self._report_error(exc, "cache_failed")
        else:
            self._delivery.watch("model_cache", future)
        self.refresh_state()
        self.bridge.refresh()

    def _clear_label_choices(self) -> None:
        self._labels_model = None
        while (item := self._label_flow.takeAt(0)) is not None:
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.deleteLater()
        self.label_checks.clear()
        self.label_choices.hide()

    def _ensure_model_labels(self) -> None:
        model, session = self._selected_model(), self.session
        if (
            model is None
            or session is None
            or self.closing
            or not self.isVisible()
            or self.labels_loading
            or self.busy
            or self._labels_model == model
        ):
            return
        self._labels_error = ""
        self._clear_label_choices()
        self._labels_model = model
        # The request identity survives display invalidation until its owner exits.
        self._labels_read_model = model
        path = Path(model.model_path)
        if not path.is_absolute():
            path = session.base_directory / path
        future: Future[tuple[str, ...]] = Future()

        def read() -> None:
            try:
                future.set_result(
                    (self._label_reader or read_model_labels)(
                        path,
                        module_id=model.module_id,
                        expected_sha256=model.expected_sha256,
                    )
                )
            except BaseException as exc:
                future.set_exception(exc)

        # One bounded metadata job and one current catalog. This does not occupy
        # the setup/preparation slot or gate Production readiness/Start.
        self._delivery.watch("model_labels", future)
        self._labels_thread = Thread(
            target=read, name="RecipeModelLabels", daemon=False
        )
        self._labels_thread.start()
        self._monitor.start()

    def _finish_label_read(self) -> None:
        thread = self._labels_thread
        if thread is None or thread.is_alive() or self._labels_completion is None:
            return
        thread.join(0)
        self._labels_thread = None
        model, self._labels_read_model = self._labels_read_model, None
        value, error = self._labels_completion
        self._labels_completion = None
        self._monitor.stop()
        if self.closing:
            self._clear_label_choices()
        elif model is not None and model == self._selected_model():
            self._labels_model = model
            rows = cast(tuple[object, ...], value) if isinstance(value, tuple) else ()
            labels = tuple(label for label in rows if isinstance(label, str))
            if isinstance(error, BaseException):
                self._labels_error = str(error)
            elif (
                not labels
                or len(labels) != len(rows)
                or len(set(labels)) != len(labels)
            ):
                self._labels_error = "Missing complete model label catalog"
            else:
                for label in labels:
                    check = QPushButton(label.replace("&", "&&"), self.label_choices)
                    check.setObjectName("recipeChoice")
                    check.setCheckable(True)
                    check.setAccessibleName(label)
                    check.setMinimumHeight(Style.touch)
                    self._label_flow.addWidget(check)
                    self.label_checks[label] = check

                    def toggled(checked: bool, name: str = label) -> None:
                        self._label_toggled(name, checked)

                    check.toggled.connect(toggled)
                self.label_choices.show()
                self._tab_order()
        self.refresh_state()
        self._continue_after_labels()

    def _label_toggled(self, label: str, included: bool) -> None:
        session = self.session
        if (
            session is not None
            and self.editable()
            and self._labels_model == self._selected_model()
            and label in self.label_checks
        ):
            try:
                self.bridge.application.authorize_engineering()
            except IdentityError as exc:
                self._report_error(exc)
                self.refresh_state()
                return
            defaults = session.draft.defaults
            disabled = tuple(
                item for item in defaults.vimo.disabled_labels if item != label
            )
            if not included:
                disabled += (label,)
            session.draft.defaults = replace(
                defaults, vimo=replace(defaults.vimo, disabled_labels=disabled)
            )
            self.changed()
        self.refresh_state()

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
        if self.browsing:
            return
        if (
            self.session is None
            or self.busy
            or self.closing
            or self.session.machine_editor.dirty
            or not self.session.machine_editor.persisted
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
        self.preview = RecipeTuning(
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
        self.inspection_section.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self._empty_space.hide()
        self._tab_order()
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
            self.inspection_section.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum
            )
            self._empty_space.show()
            workspace.deleteLater()
            self._tab_order()
        self.preview_changed.emit()
        self.show_draft()

    def save(
        self, after: Callable[[], None] | None = None, *, revision: int | None = None
    ) -> None:
        if not self.editable() or self.session is None:
            return
        session = self.session
        changed = session.dirty or session.entry is None

        def finished(_: object, error: BaseException | None) -> None:
            if error is not None:
                self._report_error(
                    error,
                    "recipe_validation_error"
                    if isinstance(error, ValueError)
                    and not isinstance(error, ConfigurationIOError)
                    else "recipe_save_error",
                )
            else:
                if changed:
                    self.reload()
                self._error = ""
                self._feedback = "saved"
            self.refresh_state()
            if error is None and after is not None:
                after()

        try:
            revision = self.bridge.application.authorize_engineering(revision)
        except IdentityError as exc:
            finished(None, exc)
            return
        if changed:
            if not self.editing.save_recipe(finished, revision=revision):
                finished(None, RuntimeError("Recipe save is unavailable"))
        else:
            finished(None, None)

    def _select(self, index: int) -> None:
        if not 0 <= index < len(self._entries) or self.busy:
            return
        entry = self._entries[index]
        if self.browsing:
            try:
                assert self.session is not None
                loaded = self.session.catalog.read(entry.path)
                recipe = loaded.recipe
                if recipe.recipe_id != entry.recipe.recipe_id:
                    raise ConfigurationError(
                        "Recipe identity changed; reload the catalog"
                    )
                self._browse_entry = loaded
                self._error = self._feedback = ""
            except ConfigurationError as exc:
                self._browse_entry = None
                self._report_error(exc)
            self.show_draft()
            return

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

        revision = self.bridge.application.session_revision

        def decided(decision: DirtyDecision) -> None:
            if (
                decision != "cancel"
                and revision != self.bridge.application.session_revision
            ):
                self._report_error(StaleSessionError("Session decision was superseded"))
                self.refresh_state()
                return
            if decision == "save":
                self.save(action, revision=revision)
            elif decision == "discard":
                try:
                    session.discard()
                except ConfigurationError as exc:
                    self._report_error(exc)
                    self.refresh_state()
                    return
                self.show_draft()
                action()
            else:
                self.refresh_state()

        self.confirm_dirty(decided)

    def confirm_dirty(self, callback: Callable[[DirtyDecision], None]) -> None:
        if self._dialog is not None:
            return
        dialog = QMessageBox(self)
        dialog.setWindowTitle(text("page_recipe", self.language))
        dialog.setText(text("recipe_dirty_question", self.language))
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

        revision = self.bridge.application.session_revision

        def create() -> None:
            if revision != self.bridge.application.session_revision:
                dialog.reject()
                self._report_error(
                    StaleSessionError("Session changed during Recipe creation")
                )
                self.refresh_state()
                return
            if not self.editable():
                return
            try:
                if (
                    duplicate
                    and session.catalog.parameters() != session.process_catalog
                ):
                    raise ConfigurationError(
                        "Process catalog changed; reload before Duplicate"
                    )
                identity = session.catalog.new_identity()
                recipe = (
                    replace(
                        session.draft.to_recipe(session.process_catalog),
                        recipe_id=identity,
                        display_name=name.text(),
                    )
                    if duplicate
                    else RecipeDefinition(identity, name.text(), CheckConfiguration())
                )
                session.new()  # Guard resolved previous unsaved changes.
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

    @Slot(str, object, object)
    def _model_observed(self, name: str, value: object, error: object) -> None:
        if name == "model_cache":
            # Application owns cleanup and exact-identity evidence. This callback
            # only refreshes presentation, including after model/page changes.
            self.bridge.refresh()
            self.refresh_state()
            return
        if name == "model_labels":
            self._labels_completion = (value, error)
            self._finish_label_read()
            return

    def _continue_after_labels(self) -> None:
        if self._after_labels is not None and not self.labels_loading:
            callback, self._after_labels = self._after_labels, None
            self._closing = False
            callback()

    def settle(self, final: Callable[[], None]) -> None:
        """Settle only Recipe-owned metadata, Model dialogs/imports and Preview."""
        self.process_parameters.end_plc_preview()
        if self.model_controls.close_pending(lambda: self.settle(final)):
            return
        if self.preview is not None:
            self.leave_preview(lambda: self.settle(final))
            return
        if self.labels_loading:
            self._closing = True
            self._after_labels = lambda: self.settle(final)
            self.refresh_state()
        else:
            self._closing = False
            final()

    def dispose(self) -> None:
        self.process_parameters.end_plc_preview()
        if (
            self.labels_loading
            or self.preview is not None
            or self.model_controls.pending is not None
        ):
            raise RuntimeError("Recipe owners must settle before disposal")
        self._closing = True
        self._delivery.detach()
