"""Compact Model actions in Recipe; use the shared stopped-editing IO owner."""

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QWidget,
)

from capsule_vision.configuration import ModelDefinition
from capsule_vision.configuration.managed_models import PendingModelImport
from capsule_vision.localization.catalog import text

from .editing import EditingCoordinator
from .image_tools import image_icon
from .style import Style, theme_colors

if TYPE_CHECKING:
    from .recipe_page import RecipePage


class ModelControls(QWidget):
    def __init__(self, page: RecipePage, editing: EditingCoordinator) -> None:
        super().__init__(page.model_section)
        self.page, self.editing = page, editing
        self.pending: PendingModelImport | None = None
        self.dialog: QDialog | None = None
        self._closing = False
        self._icon_color: str | None = None
        self._after_close: Callable[[], None] | None = None
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(Style.small)
        for key, action in (
            ("model_import", self.import_model),
            ("model_rename", self.rename),
            ("model_delete", self.delete),
        ):
            button = QPushButton(self)
            button.setFixedSize(Style.touch, Style.touch)
            button.setIconSize(QSize(Style.icon, Style.icon))
            button.clicked.connect(action)
            page.buttons[key] = button
            row.addWidget(button)

    def refresh(self) -> None:
        page = self.page
        available = bool(self.editing.session and self.editing.session.managed_models)
        self.setVisible(available)
        self.refresh_theme()
        for key in (
            "model_import",
            "model_rename",
            "model_delete",
        ):
            button = page.buttons[key]
            button.setEnabled(
                available
                and page.editable()
                and not page.labels_loading
                and not self.editing.bridge.application.preparation_snapshot().owns_resources
                and self.dialog is None
                and self.pending is None
                and (key == "model_import" or page.selected_model() is not None)
            )

    def refresh_theme(self) -> None:
        page = self.page
        colors = theme_colors(page)
        if colors.primary == self._icon_color:
            return
        self._icon_color = colors.primary
        for key, icon in (
            ("model_import", "file_import"),
            ("model_rename", "write"),
            ("model_delete", "delete"),
        ):
            button = page.buttons[key]
            button.setIcon(image_icon(icon, colors.primary))

    def _run(
        self,
        work: Callable[[], object],
        completed: Callable[[object, BaseException | None], None],
        *,
        model: ModelDefinition | None = None,
        deleting: bool = False,
    ) -> bool:
        page = self.page
        if not self.editing.editable() or page.labels_loading:
            return False

        return self.editing.run_model_io(
            work,
            completed,
            model_id=model.model_id if model else None,
            deleting=deleting,
        )

    def _finished(self, value: object, error: BaseException | None) -> None:
        page = self.page
        if error is not None:
            page.model_operation_failed(error)
        elif self.editing.session is not None:
            # Only replace registry metadata. Never reload or select a Recipe/draft.
            self.editing.session.reload_models()
            page.show_draft()
        self.editing.bridge.refresh()
        if self.pending is None and self._after_close is not None:
            after, self._after_close = self._after_close, None
            self._closing = False
            after()

    def import_model(self) -> None:
        session = self.editing.session
        if (
            session is None
            or session.managed_models is None
            or not self.editing.editable()
            or self.page.labels_loading
        ):
            return
        path, _ = QFileDialog.getOpenFileName(
            self, text("model_import", self.page.language), "", "ViMo (*.vimosln)"
        )
        if not path:
            return
        catalog = session.managed_models

        def inspected(value: object, error: BaseException | None) -> None:
            if not isinstance(value, PendingModelImport):
                self._finished(value, error)
                return
            self.pending = value
            if error is not None or self._closing:
                self._cancel()
            elif len(value.modules) == 1:
                self._publish(value.modules[0].module_id, value.display_name)
            else:
                self._form(
                    "model_import",
                    value.display_name,
                    modules=tuple((m.module_id, m.name) for m in value.modules),
                    accept=self._publish,
                )

        self._run(lambda: catalog.inspect_import(Path(path)), inspected)

    def _publish(self, module: str, name: str) -> None:
        pending, session = self.pending, self.editing.session
        if pending is None or session is None or session.managed_models is None:
            return
        catalog = session.managed_models

        def finished(value: object, error: BaseException | None) -> None:
            if error is not None:
                self._cancel(error)
            else:
                self.pending = None
                self._finished(value, None)

        if not self._run(
            lambda: catalog.publish_import(pending, module, name), finished
        ):
            self._cancel(
                RuntimeError("Model publication unavailable; import cancelled")
            )

    def _cancel(self, original: BaseException | None = None) -> None:
        pending, session = self.pending, self.editing.session
        if pending is None or session is None or session.managed_models is None:
            return
        catalog = session.managed_models

        def completed(_value: object, error: BaseException | None) -> None:
            if error is None or self.editing.closing and not pending.directory.exists():
                self.pending = None
            if original is not None or error is not None and pending.directory.exists():
                self.page.model_operation_failed(
                    error or original or RuntimeError("Import cleanup failed")
                )
            after, self._after_close = self._after_close, None
            self._closing = False
            if after is not None and self.pending is None:
                after()

        self.editing.run_io(
            lambda: catalog.cancel_import(pending), completed, closing_cleanup=True
        )

    def close_pending(self, after: Callable[[], None]) -> bool:
        if self.pending is None:
            if self.dialog is not None:
                self.dialog.reject()
            return False
        self._closing = True
        self._after_close = after
        if self.dialog is not None:
            self.dialog.reject()
        elif not self.editing.busy:
            self._cancel()
        return True

    def _form(
        self,
        key: str,
        name: str,
        *,
        modules: tuple[tuple[str, str], ...] = (),
        accept: Callable[[str, str], None],
    ) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle(text(key, self.page.language))
        form = QFormLayout(dialog)
        field = QLineEdit(name, dialog)
        field.setMinimumHeight(Style.touch)
        form.addRow(text("model_name", self.page.language), field)
        choice: QComboBox | None = None
        if modules:
            choice = QComboBox(dialog)
            for identity, label in modules:
                choice.addItem(f"{label} · {identity}", identity)
            choice.setCurrentIndex(-1)
            choice.setMinimumHeight(Style.touch)
            form.addRow(text("model_module", self.page.language), choice)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=dialog,
        )
        ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setText(
            text(
                "model_import" if key == "model_import" else "save", self.page.language
            )
        )
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(
            text("cancel", self.page.language)
        )

        def validate() -> None:
            ok.setEnabled(
                bool(field.text().strip())
                and (choice is None or choice.currentIndex() >= 0)
            )

        field.textChanged.connect(validate)
        if choice is not None:
            choice.currentIndexChanged.connect(validate)
        validate()
        form.addRow(buttons)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        self.dialog = dialog

        def finished(result: int) -> None:
            self.dialog = None
            if result == QDialog.DialogCode.Accepted:
                accept(
                    str(choice.currentData()) if choice is not None else "",
                    field.text(),
                )
            elif self.pending is not None:
                self._cancel()
            dialog.deleteLater()
            self.page.refresh_state()

        dialog.finished.connect(finished)
        dialog.open()

    def rename(self) -> None:
        model = self.page.selected_model()
        if model is None:
            return

        def rename(_module: str, name: str) -> None:
            session = self.editing.session
            if session and session.managed_models:
                catalog = session.managed_models
                self._run(
                    lambda: catalog.rename(model, name),
                    self._finished,
                    model=model,
                )

        self._form("model_rename", model.display_name, accept=rename)

    def _confirm(
        self, key: str, model: ModelDefinition, action: Callable[[], object]
    ) -> None:
        dialog = QMessageBox(self)
        dialog.setTextFormat(Qt.TextFormat.PlainText)
        dialog.setWindowTitle(text(key, self.page.language))
        dialog.setText(
            text(key + "_question", self.page.language)
            + f"\n\n{model.display_name}\nID: {model.model_id}"
        )
        yes = dialog.addButton(
            text(key, self.page.language), QMessageBox.ButtonRole.DestructiveRole
        )
        no = dialog.addButton(
            text("cancel", self.page.language), QMessageBox.ButtonRole.RejectRole
        )
        dialog.setDefaultButton(no)
        self.dialog = dialog

        def finished() -> None:
            self.dialog = None
            if dialog.clickedButton() is yes:
                action()
            dialog.deleteLater()
            self.page.refresh_state()

        dialog.finished.connect(finished)
        dialog.open()

    def delete(self) -> None:
        model, session = self.page.selected_model(), self.editing.session
        if model is None or session is None or session.managed_models is None:
            return
        catalog = session.managed_models
        self._confirm(
            "model_delete",
            model,
            lambda: self._run(
                lambda: catalog.delete(model, session.catalog.directory),
                self._finished,
                model=model,
                deleting=True,
            ),
        )
