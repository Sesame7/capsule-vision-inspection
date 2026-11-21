"""The shared Records action captures its report before opening the native PDF chooser."""

from concurrent.futures import CancelledError
from pathlib import Path
from threading import Event

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import QDialog, QFileDialog, QPushButton

from capsule_vision.application import ProductionApplication
from capsule_vision.application.alarm_report import AlarmReportRequest, NoAlarmRecords
from capsule_vision.application.audit_report import AuditReportRequest, NoAuditRecords
from capsule_vision.application.identity_models import Role
from capsule_vision.application.report_identification import (
    pdf_language,
    suggested_report_filename,
)
from capsule_vision.application.report_scope import ReportRequest
from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import Language

from .future_delivery import FutureDelivery
from .permissions import failure_key, permits
from .style import Style, button_width

type CustomerReport = ReportRequest | AuditReportRequest | AlarmReportRequest


class RecordsExport(QPushButton):
    feedback_changed = Signal()

    def __init__(self, app: ProductionApplication) -> None:
        super().__init__()
        self.app = app
        self.language: Language = "zh"
        self.request: CustomerReport | None = None
        self.batch_label = ""
        self.missing = "customer_select_batch"
        self.chooser: QFileDialog | None = None
        self.feedback: tuple[str, str] | None = None
        self._session = app.session_revision
        self._export_revision: int | None = None
        self._cancel: Event | None = None
        self._delivery = FutureDelivery()
        self._delivery.emitter.completed.connect(
            self._completed, Qt.ConnectionType.QueuedConnection
        )
        self.destroyed.connect(
            self._delivery.detach, Qt.ConnectionType.DirectConnection
        )
        self.setMinimumHeight(Style.touch)
        self.clicked.connect(self._choose)
        self.present()

    def present(self) -> None:
        if self._session != self.app.session_revision:
            self._session = self.app.session_revision
            if self.chooser is not None:
                self.chooser.reject()
            if self._cancel is not None:
                self._cancel.set()
            self.feedback = None
        audit = isinstance(self.request, AuditReportRequest)
        allowed = permits(self.app, Role.ENGINEER if audit else Role.OPERATOR)
        available = (
            self.app.identity is not None if audit else self.app.history is not None
        )
        key = "cancel" if self._cancel is not None else "report_export"
        self.setText(text(key, self.language))
        self.setAccessibleName(self.text())
        self.setFixedWidth(button_width(self, ("report_export", "cancel")))
        tip = key
        if not allowed:
            tip = "permission_engineer" if audit else "permission_operator"
        elif not available:
            tip = "history_unavailable"
        elif self.request is None:
            tip = self.missing
        enabled = allowed and available and self.request is not None
        if self._cancel is not None:
            enabled = not self._cancel.is_set()
            tip = key
        self.setEnabled(enabled and self.chooser is None)
        self.setToolTip(text(tip, self.language))

    @Slot()
    def _choose(self) -> None:
        if self._cancel is not None:
            self._cancel.set()
            self.present()
            return
        request, revision, language = (
            self.request,
            self.app.session_revision,
            self.language,
        )
        if request is None or self.chooser is not None or not self.isEnabled():
            return
        scope = (
            request.scope
            if isinstance(request, ReportRequest)
            else "audit"
            if isinstance(request, AuditReportRequest)
            else "alarm"
        )
        report_language = pdf_language(language)
        chooser = QFileDialog(self.window(), text("customer_" + scope, language))
        self.chooser = chooser
        chooser.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
        chooser.setNameFilter("PDF (*.pdf)")
        chooser.setDefaultSuffix("pdf")
        chooser.selectFile(
            suggested_report_filename(
                text("customer_" + scope, report_language), self.batch_label
            )
        )

        def chosen(result: int) -> None:
            files = chooser.selectedFiles()
            self.chooser = None
            chooser.deleteLater()
            if (
                result != QDialog.DialogCode.Accepted
                or not files
                or revision != self.app.session_revision
            ):
                self.present()
                return
            self._export_revision = revision
            self._cancel = Event()
            permission = (
                "permission_engineer"
                if isinstance(request, AuditReportRequest)
                else "permission_operator"
            )
            try:
                future = self.app.export_report(
                    request,
                    Path(files[0]),
                    report_language,
                    revision=revision,
                    cancel=self._cancel,
                )
            except Exception as exc:
                self._completed(permission, None, exc)
            else:
                self.feedback = ("report_writing", "")
                self._delivery.watch(permission, future)
                self.present()
                self.feedback_changed.emit()

        chooser.finished.connect(chosen)
        self.present()
        chooser.open()

    @Slot(str, object, object)
    def _completed(self, permission: str, result: object, error: object) -> None:
        self._cancel = None
        if self._export_revision == self.app.session_revision:
            if isinstance(error, CancelledError):
                self.feedback = ("report_cancelled", "")
            elif isinstance(error, NoAuditRecords):
                self.feedback = ("audit_no_matches", "")
            elif isinstance(error, NoAlarmRecords):
                self.feedback = ("alarm_no_matches", "")
            elif error is not None:
                self.feedback = (
                    failure_key(error, "report_failed", permission),
                    str(error),
                )
            else:
                self.feedback = ("report_saved", str(result))
        self.present()
        self.feedback_changed.emit()

    def dispose(self) -> None:
        if self.chooser is not None:
            self.chooser.reject()
        if self._cancel is not None:
            self._cancel.set()
        self._delivery.detach()
