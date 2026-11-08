"""Select or end a Production Batch, or clear the current unbatched statistics."""

from typing import Literal

from PySide6.QtCore import Qt, QTimer, Slot
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

from capsule_vision.application import ProductionApplication
from capsule_vision.application.batch_review import BatchInformation, BatchSetup
from capsule_vision.application.models import ProductionApplicationSnapshot
from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import Language

from .future_delivery import FutureDelivery
from .style import Style


class BatchDialog(QDialog):
    def __init__(
        self,
        snapshot: ProductionApplicationSnapshot,
        language: Language,
        parent: QWidget,
        application: ProductionApplication | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("productionBatchDialog")
        self.setWindowTitle(text("batch", language))
        self.language: Language = language
        self.action: Literal["select", "end", "clear"] = "select"
        self._named_batch = snapshot.batch_id is not None
        self._history = application.history if application is not None else None
        self.expected: BatchSetup | None = None
        self._lookup: str | None = None
        self._loaded_label: str | None = None
        self._delivery = FutureDelivery()
        self._delivery.emitter.completed.connect(
            self._completed, Qt.ConnectionType.QueuedConnection
        )
        self.finished.connect(self._delivery.detach)
        self.destroyed.connect(
            self._delivery.detach, Qt.ConnectionType.DirectConnection
        )
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(180)
        self._timer.timeout.connect(self._load)
        self.finished.connect(self._timer.stop)
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
        self.inputs = tuple(QLineEdit() for _ in range(3))
        for field, key, limit in zip(
            self.inputs,
            ("batch_product", "batch_specification", "batch_notes"),
            (96, 96, 384),
            strict=True,
        ):
            field.setMaxLength(limit)
            field.setMinimumHeight(Style.touch)
            field.setAccessibleName(text(key, language))
            field.setEnabled(False)
            form.addRow(text(key, language), field)
        layout.addLayout(form)
        self.mode = QLabel()
        self.mode.setTextFormat(Qt.TextFormat.PlainText)
        self.mode.setWordWrap(True)
        layout.addWidget(self.mode)
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
        self.select.clicked.connect(self._submit)
        self.select.setEnabled(bool(self.label.text().strip()))
        self.label.textChanged.connect(self._label_changed)
        actions.addWidget(self.select)
        for button in (self.select, self.secondary):
            button.setMinimumHeight(Style.touch)
        layout.addLayout(actions)
        self.label.selectAll()
        self.label.setFocus()
        self.resize(max(440, self.sizeHint().width()), self.sizeHint().height())
        if self._history is not None:
            self._label_changed(self.label.text())

    @property
    def information(self) -> BatchInformation | None:
        return (
            BatchInformation(*(field.text().strip() for field in self.inputs))
            if self.expected is not None
            else None
        )

    def _submit(self) -> None:
        if not self.select.isEnabled():
            return
        try:
            _ = self.information
        except ValueError:
            self.mode.setText(text("batch_info_failed", self.language))
            return
        self.accept()

    def _label_changed(self, value: str) -> None:
        if self._history is None:
            self.select.setEnabled(bool(value.strip()))
            return
        if value.strip() == self._loaded_label:
            self.select.setEnabled(bool(value.strip()))
            return
        self.expected = None
        self._loaded_label = None
        self.select.setEnabled(False)
        self.mode.setText(
            text("history_loading", self.language) if value.strip() else ""
        )
        for field in self.inputs:
            field.setEnabled(False)
        self._timer.start()

    def _load(self) -> None:
        label = self.label.text().strip()
        if not label or self._lookup is not None or self._history is None:
            return
        self._lookup = label
        self._delivery.watch(label, self._history.batch_setup(label))

    @Slot(str, object, object)
    def _completed(self, label: str, value: object, error: object) -> None:
        self._lookup = None
        if label != self.label.text().strip():
            self._load()
            return
        if error is not None or not isinstance(value, BatchSetup):
            self.mode.setText(text("history_unavailable", self.language))
            return
        self.expected = value
        self._loaded_label = label
        info = value.information
        for field, content in zip(
            self.inputs, (info.product, info.specification, info.notes), strict=True
        ):
            field.setText(content)
            field.setEnabled(True)
        self.mode.setText(
            text("batch_continue" if value.uuid else "batch_create", self.language)
        )
        self.select.setEnabled(True)

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
