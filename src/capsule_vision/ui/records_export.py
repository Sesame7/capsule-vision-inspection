"""The shared Records action captures its report before opening the native PDF chooser."""

from concurrent.futures import CancelledError
from pathlib import Path
from threading import Event

from PySide6.QtCore import QDateTime, QSize, Qt, Signal, Slot
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
from .image_tools import image_icon
from .permissions import failure_key, permits
from .style import Style, button_width, theme_colors

type CustomerReport = ReportRequest | AuditReportRequest | AlarmReportRequest


def _report_key(request: CustomerReport) -> str:
    if isinstance(request, ReportRequest):
        return "customer_" + request.scope
    return (
        "customer_audit"
        if isinstance(request, AuditReportRequest)
        else "customer_alarm"
    )


def _scope_caption(
    request: CustomerReport, batch_label: str, language: Language
) -> str:
    if isinstance(request, ReportRequest):
        return (
            f"{text('records_entire_batch', language)} · {batch_label or request.batch}"
        )

    filters = request.filters
    parts: list[str] = []
    if filters.since is not None:
        start = QDateTime.fromSecsSinceEpoch(filters.since).toString("yyyy-MM-dd")
        parts.append(f"{text('records_from', language)} {start}")
    if filters.until is not None:
        # The query stores the exclusive start of the day after the selected end.
        end = QDateTime.fromSecsSinceEpoch(filters.until - 1).toString("yyyy-MM-dd")
        parts.append(f"{text('records_to', language)} {end}")
    if not parts:
        parts.append(text("history_all_dates", language))
    if filters.batch:
        parts.append(f"{text('batch_id', language)} {batch_label or filters.batch}")
    else:
        parts.append(
            text(
                "records_no_batch" if filters.unbatched else "records_all_batches",
                language,
            )
        )
    if isinstance(request, AuditReportRequest):
        parts.append(
            f"{text('customer_audit_account', language)} {request.account.username}"
            if request.account
            else text("records_all_accounts", language)
        )
    else:
        parts.append(
            text(
                "alarm_" + request.category if request.category else "alarm_all_types",
                language,
            )
        )
    return " · ".join(parts)


class RecordsExport(QPushButton):
    feedback_changed = Signal()

    def __init__(self, app: ProductionApplication) -> None:
        super().__init__()
        self.app = app
        self.language: Language = "zh"
        self.request: CustomerReport | None = None
        self.batch_label = ""
        self.missing: tuple[str, str] = ("customer_batch", "customer_select_batch")
        self.chooser: QFileDialog | None = None
        self.feedback: tuple[str, str] | None = None
        self._session = app.session_revision
        self._export_revision: int | None = None
        self._cancel: Event | None = None
        self._admitted: tuple[CustomerReport, str] | None = None
        self._icon_state: tuple[str, str] | None = None
        self._delivery = FutureDelivery()
        self._delivery.emitter.completed.connect(
            self._completed, Qt.ConnectionType.QueuedConnection
        )
        self.destroyed.connect(
            self._delivery.detach, Qt.ConnectionType.DirectConnection
        )
        self.setProperty("compactAction", True)
        self.setIconSize(QSize(Style.icon, Style.icon))
        self.setMinimumHeight(Style.touch)
        self.clicked.connect(self._choose)
        self.present()

    def present(self) -> None:
        if self._session != self.app.session_revision:
            self._session = self.app.session_revision
            self._admitted = None
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
        active = self._cancel is not None
        icon = ("cancel" if active else "export", theme_colors(self).primary)
        if icon != self._icon_state:
            self.setIcon(image_icon(*icon))
            self._icon_state = icon
        caption = text("cancel", self.language) if active else "PDF"
        self.setText(caption)
        self.setFixedWidth(button_width(self, ("cancel",)))
        report_key, missing_reason = self.missing
        reason = ""
        if not allowed:
            reason = "permission_engineer" if audit else "permission_operator"
        elif not available:
            reason = "history_unavailable"
        elif self.request is None:
            reason = missing_reason
        enabled = allowed and available and self.request is not None
        if self._cancel is not None:
            enabled = not self._cancel.is_set()
        self.setEnabled(enabled and self.chooser is None)

        request, batch = self._admitted or (self.request, self.batch_label)
        title = caption
        scope = ""
        if not active or self._admitted is not None:
            key = _report_key(request) if request is not None else report_key
            title = f"{caption} · {text(key, self.language)}"
            if request is not None:
                scope = _scope_caption(request, batch, self.language)
        why = (
            text(reason, self.language)
            if reason and not (active or self.chooser)
            else ""
        )
        self.setAccessibleName(" · ".join(filter(None, (title, scope))))
        self.setAccessibleDescription(why)
        self.setToolTip("\n".join(filter(None, (title, scope, why))))

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
        key = _report_key(request)
        self._admitted = (request, self.batch_label)
        report_language = pdf_language(language)
        chooser = QFileDialog(self.window(), text(key, language))
        self.chooser = chooser
        chooser.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
        chooser.setNameFilter("PDF (*.pdf)")
        chooser.setDefaultSuffix("pdf")
        chooser.selectFile(
            suggested_report_filename(text(key, report_language), self.batch_label)
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
                self._admitted = None
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
        self._admitted = None
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
