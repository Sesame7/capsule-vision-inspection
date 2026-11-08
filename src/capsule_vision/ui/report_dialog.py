"""Compact, context-driven customer export; file publication stays on its worker."""

from concurrent.futures import CancelledError
from datetime import datetime, timedelta
from pathlib import Path
from threading import Event
from typing import cast

from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from capsule_vision.application import ProductionApplication
from capsule_vision.application.alarm_report import AlarmReportRequest, NoAlarmRecords
from capsule_vision.application.audit_report import AuditReportRequest, NoAuditRecords
from capsule_vision.application.report_identification import suggested_report_filename
from capsule_vision.application.report_scope import (
    ALL_RESULTS,
    DefectScope,
    HistoryQuery,
    ReportBasis,
    ReportRequest,
    result_code,
)
from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import Language

from .future_delivery import FutureDelivery
from .permissions import failure_key
from .style import ChevronComboBox, Style


class ReportDialog(QDialog):
    def __init__(
        self,
        app: ProductionApplication,
        language: Language,
        parent: QWidget,
        filters: HistoryQuery,
        batch: tuple[str, str] | None = None,
        *,
        audit: AuditReportRequest | None = None,
        audit_label: str = "",
        alarm: AlarmReportRequest | None = None,
        alarm_label: str = "",
    ) -> None:
        super().__init__(parent)
        self.app, self.filters, self.batch = app, filters, batch
        self.audit, self.audit_label = audit, audit_label
        self.alarm, self.alarm_label = alarm, alarm_label
        self._classification_batch: tuple[str, str] | None = None
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
        self._dismissed = self._attributable = False
        self._reading = batch is None and audit is None and alarm is None
        self._scope_ready = not self._reading
        self._filter_label: str | None = None
        self._feedback_key = self._detail = ""
        layout = QVBoxLayout(self)
        layout.setSpacing(Style.space)
        self.scope_label, self.hint, self.feedback = QLabel(), QLabel(), QLabel()
        for label in (self.scope_label, self.hint, self.feedback):
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
        layout.addWidget(self.scope_label)
        form = QFormLayout()
        layout.addLayout(form)
        self.basis, self.format, self.output_language = (
            ChevronComboBox() for _ in range(3)
        )
        self.labels: dict[str, QLabel] = {}
        for key, combo in (
            ("export_basis", self.basis),
            ("export_format", self.format),
            ("export_language", self.output_language),
        ):
            label = QLabel()
            label.setBuddy(combo)
            self.labels[key] = label
            combo.setObjectName("reportChoice")
            combo.setMinimumHeight(Style.touch)
            form.addRow(label, combo)
        self.basis.setVisible(batch is None)
        self.labels["export_basis"].setVisible(batch is None)
        self.format.addItem("PDF (.pdf)", "pdf")
        self.format.addItem("Excel (.xlsx)", "xlsx")
        self.output_language.addItem("简体中文", "zh")
        self.output_language.addItem("English", "en")
        self.output_language.setCurrentIndex(int(language != "zh"))
        layout.addWidget(self.hint)
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
        self.set_language(language)
        self.resize(500, self.sizeHint().height())
        if self._reading:
            self._message("history_loading")
            try:
                future = app.defect_report_scope(filters, revision=self.revision)
            except Exception as exc:
                self._completed("scope", None, exc)
            else:
                self._delivery.watch("scope", future)

    def set_language(self, language: Language) -> None:
        self.language = language
        self.setWindowTitle(
            text(
                "customer_alarm"
                if self.alarm is not None
                else "customer_audit"
                if self.audit is not None
                else "customer_batch"
                if self.batch
                else "customer_defects",
                language,
            )
        )
        for key, label in self.labels.items():
            label.setText(text(key, language))
            label.buddy().setAccessibleName(label.text())
        for i in range(self.basis.count()):
            self.basis.setItemText(
                i,
                text(
                    "customer_positions"
                    if self.basis.itemData(i) == "capsule"
                    else "export_detections",
                    language,
                ),
            )
        entire_batch = self.batch or self._classification_batch
        self.basis.setVisible(
            entire_batch is None and self.audit is None and self.alarm is None
        )
        self.labels["export_basis"].setVisible(
            entire_batch is None and self.audit is None and self.alarm is None
        )
        if entire_batch:
            scope = text("customer_entire_batch", language).format(
                batch=entire_batch[1]
            )
        else:
            start, end = (
                (self.alarm.filters.since, self.alarm.filters.until)
                if self.alarm
                else (self.audit.filters.since, self.audit.filters.until)
                if self.audit
                else (self.filters.since, self.filters.until)
            )
            dates = (
                (
                    datetime.fromtimestamp(start).strftime("%Y-%m-%d")
                    if start is not None
                    else "…"
                )
                + " — "
                + (
                    (datetime.fromtimestamp(end) - timedelta(microseconds=1)).strftime(
                        "%Y-%m-%d"
                    )
                    if end is not None
                    else "…"
                )
            )
            scope = " · ".join(
                (
                    dates
                    if start is not None or end is not None
                    else text("history_all_dates", language),
                    text("records_no_batch", language)
                    if self.filters.unbatched
                    else self._filter_label or text("records_all_batches", language),
                    result_code(self.filters.result)
                    if self.filters.result != ALL_RESULTS
                    else text("history_all", language),
                )
            )
            if self.audit is not None:
                batch_scope = (
                    text("customer_audit_batch", language) + ": " + self.audit_label
                    if self.audit.filters.batch
                    else text(
                        "records_no_batch"
                        if self.audit.filters.unbatched
                        else "records_all_batches",
                        language,
                    )
                )
                scope = " · ".join(
                    (
                        dates
                        if start is not None or end is not None
                        else text("history_all_dates", language),
                        batch_scope,
                        self.audit.account.username
                        if self.audit.account
                        else text("records_all_accounts", language),
                    )
                )
            if self.alarm is not None:
                scope = " · ".join(
                    (
                        dates
                        if start is not None or end is not None
                        else text("history_all_dates", language),
                        self.alarm_label or "—"
                        if self.alarm.filters.batch
                        else text(
                            "records_no_batch"
                            if self.alarm.filters.unbatched
                            else "records_all_batches",
                            language,
                        ),
                        text(
                            "alarm_" + self.alarm.category
                            if self.alarm.category
                            else "alarm_all_types",
                            language,
                        ),
                    )
                )
        self.scope_label.setText(scope)
        self.scope_label.setToolTip(scope)
        self.scope_label.setAccessibleName(scope)
        self.hint.setText(
            text("customer_detection_only", language)
            if self.audit is None
            and self.alarm is None
            and not entire_batch
            and self._scope_ready
            and not self._attributable
            else ""
        )
        self.hint.setVisible(bool(self.hint.text()))
        self.cancel.setText(text("cancel", language))
        self.export.setText(text("report_export", language))
        self._message(self._feedback_key, self._detail)
        self._controls()

    def _message(self, key: str, detail: str = "") -> None:
        self._feedback_key, self._detail = key, detail
        value = text(key, self.language) if key else ""
        self.feedback.setText(f"{value} · {detail}" if detail else value)
        self.feedback.setVisible(bool(value))

    def _controls(self) -> None:
        idle = self._cancel is None and self._chooser is None
        self.export.setEnabled(idle and self._scope_ready and not self._reading)
        for combo in (self.basis, self.format, self.output_language):
            combo.setEnabled(idle and not self._reading)
        self.cancel.setVisible(self._cancel is not None)

    @Slot()
    def _choose_file(self) -> None:
        if not self.export.isEnabled():
            return
        chooser = self._chooser = QFileDialog(self, self.windowTitle())
        chooser.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
        ext = str(self.format.currentData())
        chooser.setNameFilter("Excel (*.xlsx)" if ext == "xlsx" else "PDF (*.pdf)")
        chooser.setDefaultSuffix(ext)
        entire_batch = self.batch or self._classification_batch
        batch_label = (
            entire_batch[1]
            if entire_batch
            else self.audit_label
            if self.audit and self.audit.filters.batch
            else self.alarm_label
            if self.alarm and self.alarm.filters.batch
            else None
        )
        chooser.selectFile(
            suggested_report_filename(self.windowTitle(), batch_label, ext)
        )
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
        destination = Path(files[0])
        if destination.suffix.lower() != "." + str(self.format.currentData()):
            self._message("report_failed", self.format.currentText())
            return
        self._cancel = Event()
        self._message("report_writing")
        self._controls()
        try:
            entire_batch = self.batch or self._classification_batch
            future = self.app.export_report(
                self.audit
                or self.alarm
                or ReportRequest(
                    "batch" if self.batch else "defects",
                    "capsule"
                    if entire_batch
                    else cast(ReportBasis, self.basis.currentData()),
                    entire_batch[0] if entire_batch else None,
                    self.filters,
                ),
                destination,
                cast(Language, self.output_language.currentData()),
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
        if kind == "scope":
            self._reading = False
            self._scope_ready = isinstance(value, DefectScope) and error is None
            if isinstance(value, DefectScope) and error is None:
                self._attributable = value.attributable
                self._filter_label = value.batch_label
                if (
                    value.batch_label is not None
                    and self.filters.batch_segment is not None
                ):
                    self._classification_batch = (
                        self.filters.batch_segment,
                        value.batch_label,
                    )
                self.basis.clear()
                if value.attributable:
                    self.basis.addItem("", "capsule")
                self.basis.addItem("", "detections")
                self._message("")
                self.set_language(self.language)
            else:
                self._message(
                    failure_key(error, "history_unavailable", "permission_operator"),
                    str(error),
                )
        else:
            self._cancel = None
            if isinstance(error, CancelledError):
                self._message("report_cancelled")
            elif isinstance(error, NoAuditRecords):
                self._message("audit_no_matches")
            elif isinstance(error, NoAlarmRecords):
                self._message("alarm_no_matches")
            elif error is not None:
                self._message(
                    failure_key(
                        error,
                        "report_failed",
                        "permission_engineer" if self.audit else "permission_operator",
                    ),
                    str(error),
                )
            else:
                self._message("report_saved", str(value))
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
