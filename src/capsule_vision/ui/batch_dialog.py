"""Select or end a Production Batch, or clear the current unbatched statistics."""

from typing import Literal

from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from capsule_vision.application.models import ProductionApplicationSnapshot
from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import Language

from .style import Style


class BatchDialog(QDialog):
    def __init__(
        self,
        snapshot: ProductionApplicationSnapshot,
        language: Language,
        parent: QWidget,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("productionBatchDialog")
        self.setWindowTitle(text("batch", language))
        self.language: Language = language
        self.action: Literal["select", "end", "clear"] = "select"
        self._named_batch = snapshot.batch_id is not None
        layout = QVBoxLayout(self)
        form = QFormLayout()
        current = QLineEdit()
        current.setMaxLength(2_147_483_647)
        current.setText(snapshot.batch_id or "—")
        current.setReadOnly(True)
        current.setAccessibleName(text("batch_current", language))
        current.setToolTip(snapshot.batch_id or "—")
        form.addRow(text("batch_current", language), current)
        self.label = QLineEdit()
        self.label.setMaxLength(2_147_483_647)
        self.label.setText(snapshot.batch_id or "")
        self.label.setMinimumHeight(Style.touch)
        # Use the existing native Batch caption where a separate input label isn't provided.
        input_label = text(
            "batch_input" if language in ("zh", "en") else "batch_id", language
        )
        self.label.setAccessibleName(input_label)
        form.addRow(input_label, self.label)
        layout.addLayout(form)
        for visible, key in (
            (snapshot.batch_uncertain, "batch_uncertain"),
            (snapshot.detail_expired, "batch_expired"),
        ):
            if visible:
                warning = QLabel(text(key, language))
                warning.setWordWrap(True)
                layout.addWidget(warning)
        actions = QHBoxLayout()
        self.secondary = QPushButton(
            text(
                "batch_end" if self._named_batch else "production.clear_statistics",
                language,
            )
        )
        self.secondary.setAutoDefault(False)
        self.secondary.clicked.connect(self._confirm_secondary)
        actions.addWidget(self.secondary)
        actions.addStretch()
        self.select = QPushButton(text("batch_select", language))
        self.select.setDefault(True)
        self.select.clicked.connect(self.accept)
        self.select.setEnabled(bool(self.label.text().strip()))
        self.label.textChanged.connect(self._label_changed)
        actions.addWidget(self.select)
        for button in (self.select, self.secondary):
            button.setMinimumHeight(Style.touch)
        layout.addLayout(actions)
        self.label.selectAll()
        self.label.setFocus()
        self.resize(max(440, self.sizeHint().width()), self.sizeHint().height())

    def _label_changed(self, value: str) -> None:
        self.select.setEnabled(bool(value.strip()))

    def _confirm_secondary(self) -> None:
        prompt = QMessageBox(self)
        prompt.setWindowTitle(self.secondary.text())
        prompt.setText(
            text(
                "batch_end_confirm"
                if self._named_batch
                else "clear_statistics_confirm",
                self.language,
            )
        )
        confirm = prompt.addButton(
            self.secondary.text(), QMessageBox.ButtonRole.AcceptRole
        )
        cancel = prompt.addButton(
            text("batch_keep_open" if self._named_batch else "cancel", self.language),
            QMessageBox.ButtonRole.RejectRole,
        )
        prompt.setDefaultButton(cancel)
        prompt.setEscapeButton(cancel)
        prompt.exec()
        if prompt.clickedButton() is confirm:
            self.action = "end" if self._named_batch else "clear"
            self.accept()
