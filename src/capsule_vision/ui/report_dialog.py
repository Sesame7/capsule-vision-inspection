"""Choose a durable named Batch independently of the Cycle browser's filters."""

from concurrent.futures import CancelledError
from pathlib import Path
from threading import Event

from PySide6.QtCore import QSize, Qt, Slot
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from capsule_vision.application import ProductionApplication
from capsule_vision.application.batch_records import ReportBatches
from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import Language

from .future_delivery import FutureDelivery
from .image_tools import image_icon
from .permissions import failure_key
from .style import Style, theme_colors


class ReportDialog(QDialog):
    def __init__(
        self, app: ProductionApplication, language: Language, parent: QWidget
    ) -> None:
        super().__init__(parent)
        self.app = app
        self.language: Language = language
        self.revision = app.session_revision
        self._delivery = FutureDelivery()
        self._delivery.emitter.completed.connect(
            self._completed, Qt.ConnectionType.QueuedConnection
        )
        self.finished.connect(self._dismiss)
        self.destroyed.connect(
            self._delivery.detach, Qt.ConnectionType.DirectConnection
        )
        self._cancel: Event | None = None
        self._chooser: QFileDialog | None = None
        self._request = 0
        self._reading = False
        self._dismissed = False
        self._cursor: str | None = None
        self._back = False
        self._more = False
        self._feedback_key = self._detail = ""

        layout = QVBoxLayout(self)
        filters = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setClearButtonEnabled(True)
        filters.addWidget(self.search, 1)
        self.previous, self.next = QPushButton(), QPushButton()
        for button in (self.previous, self.next):
            button.setFixedSize(Style.touch, Style.touch)
            button.setIconSize(QSize(Style.icon, Style.icon))
            button.setAutoDefault(False)
            filters.addWidget(button)
        layout.addLayout(filters)
        self.batches = QListWidget()
        self.batches.setObjectName("reportBatches")
        layout.addWidget(self.batches)
        self.feedback = QLabel()
        self.feedback.setTextFormat(Qt.TextFormat.PlainText)
        self.feedback.setWordWrap(True)
        self.feedback.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.feedback)
        actions = QHBoxLayout()
        actions.addStretch()
        self.cancel, self.export = QPushButton(), QPushButton()
        for button in (self.cancel, self.export):
            button.setMinimumHeight(Style.touch)
            actions.addWidget(button)
        layout.addLayout(actions)
        self.cancel.setAutoDefault(False)
        self.export.setDefault(True)
        self.cancel.clicked.connect(self._cancel_export)
        self.export.clicked.connect(self._choose_file)
        self.search.textChanged.connect(self._search_changed)
        self.previous.clicked.connect(lambda: self._turn(True))
        self.next.clicked.connect(lambda: self._turn(False))
        self.batches.currentRowChanged.connect(self._controls)
        self.resize(470, 350)
        self.set_language(language)
        self._read()

    def set_language(self, language: Language) -> None:
        self.language = language
        self.setWindowTitle(text("report_export", language))
        self.batches.setAccessibleName(text("batch_input", language))
        label = text("report_search", language)
        self.search.setPlaceholderText(label)
        self.search.setAccessibleName(label)
        for button, icon, key in (
            (self.previous, "previous", "previous_page"),
            (self.next, "next", "next_page"),
        ):
            button.setIcon(image_icon(icon, theme_colors(self).primary))
            button.setAccessibleName(text(key, language))
            button.setToolTip(button.accessibleName())
        self.cancel.setText(text("cancel", language))
        self.export.setText(text("report_export", language))
        self._message(self._feedback_key, self._detail)
        self._controls()

    def _message(self, key: str, detail: str = "") -> None:
        self._feedback_key, self._detail = key, detail
        value = text(key, self.language) if key else ""
        self.feedback.setText(f"{value} · {detail}" if detail else value)
        self.feedback.setVisible(bool(value))

    @Slot()
    def _controls(self) -> None:
        idle = self._cancel is None and self._chooser is None
        ready = idle and not self._reading and self.batches.currentRow() >= 0
        self.previous.setEnabled(
            ready and (self._more if self._back else self._cursor is not None)
        )
        self.next.setEnabled(
            ready and (self._cursor is not None if self._back else self._more)
        )
        self.export.setEnabled(ready)
        self.search.setEnabled(idle)
        self.batches.setEnabled(idle and not self._reading)
        self.cancel.setVisible(self._cancel is not None)

    @Slot()
    def _search_changed(self) -> None:
        self._cursor, self._back = None, False
        self._request += 1
        if not self._reading:
            self._read()

    def _turn(self, back: bool) -> None:
        if self.batches.count():
            item = self.batches.item(0 if back else self.batches.count() - 1)
            self._cursor, self._back = item.text(), back
            self._request += 1
            self._read()

    def _read(self) -> None:
        self._reading = True
        self._controls()
        self._message("history_loading")
        try:
            future = self.app.report_batches(
                self.search.text(), self._cursor, self._back, revision=self.revision
            )
        except Exception as exc:
            self._completed(str(self._request), None, exc)
        else:
            self._delivery.watch(str(self._request), future)

    @Slot()
    def _choose_file(self) -> None:
        if not self.export.isEnabled():
            return
        chooser = self._chooser = QFileDialog(self, self.windowTitle())
        chooser.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
        chooser.setNameFilter("Excel (*.xlsx)")
        chooser.setDefaultSuffix("xlsx")
        chooser.selectFile(self.windowTitle() + ".xlsx")
        chooser.finished.connect(self._file_chosen)
        self._controls()
        chooser.open()

    @Slot(int)
    def _file_chosen(self, result: int) -> None:
        chooser, self._chooser = self._chooser, None
        if chooser is None:
            return
        files = chooser.selectedFiles()
        chooser.deleteLater()
        self._controls()
        if self._dismissed or result != QDialog.DialogCode.Accepted or not files:
            return
        if self.batches.currentRow() < 0:
            return
        item = self.batches.currentItem()
        self._cancel = Event()
        self._message("report_writing")
        self._controls()
        try:
            future = self.app.export_report(
                item.data(Qt.ItemDataRole.UserRole),
                Path(files[0]),
                self.language,
                revision=self.revision,
                cancel=self._cancel,
            )
        except Exception as exc:
            self._completed("export", None, exc)
        else:
            self._delivery.watch("export", future)

    @Slot(str, object, object)
    def _completed(self, kind: str, value: object, error: object) -> None:
        if self._dismissed:
            return
        if kind == "export":
            self._cancel = None
            if isinstance(error, CancelledError):
                self._message("report_cancelled")
            elif error is not None:
                self._message(
                    failure_key(error, "report_failed", "permission_operator"),
                    str(error),
                )
            else:
                self._message("report_saved", str(value))
        else:
            self._reading = False
            if kind != str(self._request):
                self._read()
                return
            self.batches.clear()
            self._more = False
            if isinstance(value, ReportBatches):
                self._more = value.more
                for uuid, label in value.choices:
                    item = QListWidgetItem(label, self.batches)
                    item.setData(Qt.ItemDataRole.UserRole, uuid)
                    item.setToolTip(f"{label}\n{uuid}")
                self.batches.setCurrentRow(0)
                self._message("" if value.choices else "report_no_batches")
            else:
                self._message(
                    failure_key(error, "history_unavailable", "permission_operator"),
                    str(error),
                )
        self._controls()

    def _cancel_export(self) -> None:
        if self._cancel is not None:
            self._cancel.set()

    def _dismiss(self) -> None:
        self._dismissed = True
        self._cancel_export()
        if self._chooser is not None:
            self._chooser.reject()
        self._delivery.detach()
