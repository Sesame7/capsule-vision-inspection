"""Compact saved Recipe selection; the candidate remains Application-owned."""

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QPaintEvent, QPalette, QStandardItemModel
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QPushButton,
    QSizePolicy,
    QStyle,
    QStyleOptionComboBox,
    QStylePainter,
    QWidget,
)

from capsule_vision.application import ProductionApplicationSnapshot
from capsule_vision.configuration.catalog import RecipeEntry
from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import Language

from .editing import EditingCoordinator
from .image_tools import image_icon
from .style import Style, theme_colors


class _RecipeSelector(QComboBox):
    def paintEvent(self, event: QPaintEvent) -> None:
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        painter = QStylePainter(self)
        painter.drawComplexControl(QStyle.ComplexControl.CC_ComboBox, option)
        if self.currentIndex() < 0:
            option.currentText = self.placeholderText()
            option.palette.setBrush(
                QPalette.ColorRole.ButtonText,
                option.palette.brush(QPalette.ColorRole.PlaceholderText),
            )
        field = self.style().subControlRect(
            QStyle.ComplexControl.CC_ComboBox,
            option,
            QStyle.SubControl.SC_ComboBoxEditField,
            self,
        )
        # Elide only the painted label; model text, identity and accessibility stay full.
        option.currentText = self.fontMetrics().elidedText(
            option.currentText, Qt.TextElideMode.ElideRight, field.width()
        )
        painter.drawControl(QStyle.ControlElement.CE_ComboBoxLabel, option)
        painter.end()


class ProductionRecipes(QWidget):
    feedback_changed = Signal()
    selector_width = 170

    def __init__(self, editing: EditingCoordinator) -> None:
        super().__init__()
        self.editing = editing
        self.language: Language = "zh"
        self.feedback: tuple[str, str] | None = None
        self._catalog_feedback: tuple[str, str] | None = None
        self._presentation: tuple[str, str] | None = None
        self._entries: tuple[RecipeEntry, ...] = ()
        self._requested: tuple[str, str] | None = None
        self._catalog_revision = 0
        self._selection_presentation: tuple[int, str | None, str | None] | None = None
        self.setMaximumWidth(self.selector_width + Style.touch + Style.small)
        self.setFixedHeight(Style.touch)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(Style.small)
        self.recipes = _RecipeSelector()
        self.recipes.setMinimumWidth(2 * Style.touch)
        self.recipes.setMaximumWidth(self.selector_width)
        self.recipes.setFixedHeight(Style.touch)
        self.recipes.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        row.addWidget(self.recipes, 1)
        self.refresh_button = QPushButton()
        self.refresh_button.setFixedSize(Style.touch, Style.touch)
        self.refresh_button.setIconSize(QSize(Style.icon, Style.icon))
        row.addWidget(self.refresh_button)
        self.recipes.activated.connect(self.select)
        self.refresh_button.clicked.connect(self.reload)
        editing.changed.connect(self.refresh_state)
        editing.bridge.snapshot_changed.connect(self.refresh_state)
        editing.bridge.model_preparation_changed.connect(self.refresh_state)

    def sizeHint(self) -> QSize:
        return QSize(self.maximumWidth(), Style.touch)

    def reload(self) -> None:
        if self.editing.busy or self.editing.closing:
            return
        session = self.editing.session
        self.feedback = None
        self._entries = ()
        if session is not None:
            catalog = session.catalog.discover()
            self._entries = catalog.entries
            if catalog.issues:
                self.feedback = (
                    "recipe_error",
                    "\n".join(f"{e.path.name}: {e.message}" for e in catalog.issues),
                )
        self._catalog_feedback = self.feedback
        self._catalog_revision += 1
        self.present(self.editing.bridge.application.snapshot())
        self.refresh_state()
        self.feedback_changed.emit()

    def select(self, index: int) -> None:
        if not self.recipes.isEnabled() or index < 0:
            return
        identity = self.recipes.itemData(index)
        if not isinstance(identity, str) or not any(
            e.recipe.recipe_id == identity for e in self._entries
        ):
            return
        requested_name = next(
            e.recipe.display_name
            for e in self._entries
            if e.recipe.recipe_id == identity
        )
        self._requested = (identity, requested_name)
        self.feedback = ("preparing", "")
        self.present(self.editing.bridge.application.snapshot())
        self.refresh_state()
        self.feedback_changed.emit()

        def completed(error: BaseException | None) -> None:
            self._requested = None
            if error is not None:
                self.feedback = (
                    "recipe_prepare_error",
                    f"{requested_name} [{identity}]\n{error}",
                )
            else:
                self.feedback = self._catalog_feedback
            self.editing.bridge.refresh()
            self.present(self.editing.bridge.application.snapshot())
            self.refresh_state()
            self.feedback_changed.emit()

        self.editing.select_saved(identity, completed)

    def present(self, snapshot: ProductionApplicationSnapshot) -> tuple[str, str]:
        """Reflect the installed candidate, never invoke preparation or retag saved data."""
        candidate = snapshot.candidate_prepared
        identity = candidate.configuration.recipe_id if candidate else None
        name = candidate.configuration.display_name if candidate else None
        presentation = self._catalog_revision, identity, name
        if presentation != self._selection_presentation:
            self._selection_presentation = presentation
            self.recipes.clear()
            for entry in self._entries:
                self.recipes.addItem(entry.recipe.display_name, entry.recipe.recipe_id)
                self.recipes.setItemData(
                    self.recipes.count() - 1,
                    f"{entry.recipe.display_name} [{entry.recipe.recipe_id}]",
                    Qt.ItemDataRole.ToolTipRole,
                )
            if candidate is not None:
                index = self.recipes.findData(identity)
                if index < 0:
                    self.recipes.addItem(candidate.configuration.display_name, identity)
                    index = self.recipes.count() - 1
                    model = self.recipes.model()
                    if isinstance(model, QStandardItemModel):
                        item = model.item(index)
                        item.setEnabled(False)
                # Catalog refresh cannot relabel an already prepared definition.
                self.recipes.setItemText(index, name or "")
        self.recipes.setCurrentIndex(
            self.recipes.findData(identity) if candidate else -1
        )
        details = (
            f"{text('page_recipe', self.language)}: {name}\nID: {identity}"
            if candidate
            else text("select_saved_recipe", self.language)
        )
        inline = ""
        active = snapshot.active_prepared
        if active is not None and (
            candidate is None or snapshot.has_pending_configuration
        ):
            active_text = f"{text('active_recipe', self.language)}: {active.configuration.display_name}"
            inline = active_text
            details += f"\n{active_text} [{active.configuration.recipe_id}]"
            if candidate is not None:
                details += f"\n{text('pending_recipe', self.language)}: {name}"
        if self._requested is not None:
            requested_id, requested_name = self._requested
            inline = (
                f"{text('recipe_preparing_short', self.language)}: {requested_name}"
            )
            details += f"\n{text('preparing', self.language)}: {requested_name} [{requested_id}]"
            self.feedback = ("preparing", details)
        self.recipes.setToolTip(details)
        self.recipes.setAccessibleName(
            f"{text('select_saved_recipe', self.language)} · {details}"
        )
        return inline, details

    def refresh_state(self, _snapshot: object = None) -> None:
        allowed = (
            self.editing.editable()
            and self._requested is None
            and not self.editing.bridge.application.preparation_snapshot().owns_resources
        )
        self.recipes.setEnabled(allowed and bool(self._entries))
        self.refresh_button.setEnabled(
            self.editing.session is not None
            and not self.editing.busy
            and not self.editing.closing
            and self._requested is None
        )
        color = theme_colors(self).primary
        presentation = (self.language, color)
        if presentation == self._presentation:
            return
        self._presentation = presentation
        self.recipes.setPlaceholderText(text("select_saved_recipe", self.language))
        self.refresh_button.setToolTip(text("short_refresh", self.language))
        self.refresh_button.setAccessibleName(self.refresh_button.toolTip())
        self.refresh_button.setIcon(image_icon("refresh", color))
