"""Select a production Batch or explicitly end the current selection."""

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
        self.end_requested = False
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
        self.end = QPushButton(text("batch_end", language))
        self.end.setAutoDefault(False)
        self.end.setEnabled(snapshot.batch_id is not None)
        self.end.clicked.connect(self._confirm_end)
        actions.addWidget(self.end)
        actions.addStretch()
        self.select = QPushButton(text("batch_select", language))
        self.select.setDefault(True)
        self.select.clicked.connect(self.accept)
        self.select.setEnabled(bool(self.label.text().strip()))
        self.label.textChanged.connect(self._label_changed)
        actions.addWidget(self.select)
        for button in (self.select, self.end):
            button.setMinimumHeight(Style.touch)
        layout.addLayout(actions)
        self.label.selectAll()
        self.label.setFocus()
        self.resize(max(440, self.sizeHint().width()), self.sizeHint().height())

    def _label_changed(self, value: str) -> None:
        self.select.setEnabled(bool(value.strip()))

    def _confirm_end(self) -> None:
        prompt = QMessageBox(self)
        prompt.setWindowTitle(text("batch_end", self.language))
        prompt.setText(text("batch_end_confirm", self.language))
        end = prompt.addButton(
            text("batch_end", self.language), QMessageBox.ButtonRole.AcceptRole
        )
        keep_open = prompt.addButton(
            text("batch_keep_open", self.language), QMessageBox.ButtonRole.RejectRole
        )
        prompt.setDefaultButton(keep_open)
        prompt.setEscapeButton(keep_open)
        prompt.exec()
        if prompt.clickedButton() is end:
            self.end_requested = True
            self.accept()
