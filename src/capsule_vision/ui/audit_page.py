"""Read-only, revision-fenced audit browser within History."""

from dataclasses import replace
from html import escape
from threading import Event
from typing import cast

from PySide6.QtCore import Qt, Slot

from capsule_vision.application.audit import AuditQuery, AuditRecord
from capsule_vision.application.audit_presentation import (
    CONFIGURATION_ACTIONS,
    action_caption,
    after_caption_key,
    before_caption_key,
    change_caption,
    configuration_details,
    failure_caption,
    incomplete_changes,
    local_change_comparison,
    outcome_caption,
    outcome_evidence_caption,
    plc_requested_values,
    target_caption,
    unsuccessful_login,
)
from capsule_vision.application.history import ProductionHistory
from capsule_vision.application.identity import LocalIdentity
from capsule_vision.application.identity_models import IdentityError, Role
from capsule_vision.application.records_query import RecordsScope, scoped_audit_page
from capsule_vision.localization.audit_wording import audit_text, has_approved_wording
from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import LANGUAGES, Language

from .audit_accounts import AuditAccounts
from .future_delivery import FutureDelivery
from .history_event_table import (
    HistoryEventTable,
    audit_utc_bound,
    readable_value,
    record_time,
)

_OUTCOMES = (
    "success",
    "failed",
    "stale",
    "denied",
    "uncertain",
    "cancelled",
    "unresolved",
)


class AuditPage(HistoryEventTable):
    def __init__(
        self, identity: LocalIdentity | None, history: ProductionHistory | None = None
    ) -> None:
        super().__init__("audit")
        self.identity = identity
        self.history = history
        self.scope = RecordsScope()
        self._cancel = Event()
        self.language: Language = "zh"
        self.rows: tuple[AuditRecord, ...] = ()
        self._token = 0
        self._revision: int | None = None
        self._query = AuditQuery()
        self._reading = False
        self._read_error = ""
        self._delivery = FutureDelivery()
        self._delivery.emitter.completed.connect(
            self._completed, Qt.ConnectionType.QueuedConnection
        )
        self.destroyed.connect(
            self._delivery.detach, Qt.ConnectionType.DirectConnection
        )
        self.search.hide()
        self.kind.hide()
        self.accounts = AuditAccounts(identity)
        self.filter_bar.insertWidget(0, self.accounts)
        self.table.itemSelectionChanged.connect(self._details)
        self.refresh_button.clicked.connect(self.refresh)
        self.accounts.changed.connect(self.refresh)
        self.newer.clicked.connect(lambda: self._turn(True))
        self.older.clicked.connect(lambda: self._turn(False))
        self.clear()

    def clear(self) -> None:
        self._cancel.set()
        self._token += 1
        self._revision = None
        self._reading = False
        self._read_error = ""
        self.rows = ()
        self.table.setRowCount(0)
        self.accounts.clear()
        self.clear_details()
        self.message.clear()
        self.message.hide()
        self.message.setToolTip("")
        self.message.setAccessibleDescription("")
        self.older.setEnabled(False)
        self.newer.setEnabled(False)

    def set_language(self, language: Language) -> None:
        self.language = language
        self.navigation_language(language)
        self.accounts.set_language(language)
        outcome = max(
            (outcome_caption(code, lang) for code in _OUTCOMES for lang in LANGUAGES),
            key=self.table.fontMetrics().horizontalAdvance,
        )
        self.columns(
            language,
            (
                ("short_time", "2026-12-31 23:59:59", "2026-12-31 23:59:59", 1),
                ("records_user", "DEV-Operator", "DEV-Example-Engineer", 3),
                (
                    "audit_action",
                    "Change role",
                    audit_text("recipe.process_value", "en"),
                    4,
                ),
                ("audit_target", "DEV-LOT-F", "DEV-Example-Trainee-1", 4),
                ("audit_outcome", outcome, outcome, 0),
            ),
        )
        self.table.setAccessibleName(text("audit_trail", language))
        self.table.setToolTip(text("records_audit_scope", language))
        self.details.setAccessibleName(text("audit_details", language))
        self.refresh_status()
        self._render()

    @Slot()
    def refresh(self) -> None:
        self._read(
            AuditQuery(
                audit_utc_bound(self.scope.since),
                audit_utc_bound(self.scope.until),
                account=self.accounts.selected,
            )
        )

    def _turn(self, newer: bool) -> None:
        if self.rows:
            self._read(
                replace(
                    self._query,
                    before=self.rows[0 if newer else -1].cursor,
                    newer=newer,
                )
            )

    def _read(self, query: AuditQuery) -> None:
        self._cancel.set()
        self._cancel = Event()
        self._query = query
        self._token += 1
        if self.identity is None:
            self.clear()
            return
        self._revision = self.identity.snapshot().revision
        self._reading = True
        self.newer.setEnabled(False)
        self.older.setEnabled(False)
        self.refresh_status()
        try:
            if self.scope.batch is not None or self.scope.unbatched:
                if self.history is None:
                    raise RuntimeError("History association unavailable")
                future = scoped_audit_page(
                    self.history,
                    self.identity,
                    query,
                    self.scope,
                    self._revision,
                    self._cancel,
                )
            else:
                future = self.identity.audit_page(query, self._revision)
        except IdentityError:
            self.clear()
            return
        except RuntimeError as exc:
            self._completed(str(self._token), None, exc)
            return
        self._delivery.watch(str(self._token), future)

    @Slot(str, object, object)
    def _completed(self, token: str, result: object, error: object) -> None:
        if token != str(self._token) or self.identity is None:
            return
        session = self.identity.snapshot()
        if session.revision != self._revision or session.role < Role.ENGINEER:
            self.clear()
            return
        page = (
            cast(tuple[AuditRecord, ...], result) if isinstance(result, tuple) else ()
        )
        ordered = page[:50]
        self.rows = ordered[::-1] if self._query.newer else ordered
        self.newer.setEnabled(
            len(page) > 50 if self._query.newer else self._query.before is not None
        )
        self.older.setEnabled(
            self._query.before is not None if self._query.newer else len(page) > 50
        )
        self._reading = False
        self._read_error = type(error).__name__ if error is not None else ""
        self.refresh_status()
        self._render()

    @Slot()
    def refresh_status(self) -> None:
        if self.identity is None or self._revision is None:
            return
        lost, issue = self.identity.audit.observation()
        storage = (
            text("audit_warning", self.language).format(count=lost) if issue else ""
        )
        query = (
            text("history_loading", self.language)
            if self._reading
            else text("history_unavailable", self.language)
            if self._read_error
            else text("audit_no_matches", self.language)
            if not self.rows and not issue
            else ""
        )
        self.message.setText(" · ".join(filter(None, (query, storage))))
        detail = "\n".join(filter(None, (self._read_error, issue)))
        self.message.setToolTip(detail)
        self.message.setAccessibleDescription(detail)
        self.message.setVisible(bool(self.message.text()))

    def _render(self) -> None:
        self.table.setRowCount(len(self.rows))
        for row, record in enumerate(self.rows):
            op = record.operation
            clock, full_time = record_time(op.utc)
            target = target_caption(record, self.language)
            self.set_values(
                row,
                (
                    clock,
                    readable_value(op.username) or "—",
                    action_caption(op, self.language),
                    target,
                    outcome_caption(record.outcome, self.language),
                ),
                {
                    0: full_time,
                    2: action_caption(op, self.language, tooltip=True),
                    4: outcome_caption(record.outcome, self.language, tooltip=True),
                },
            )
            if op.action == "production.recipe" and "<" in target:
                item = self.table.item(row, 3)
                if item is not None:
                    # Qt auto-detects rich text in tooltips. Keep the name literal.
                    item.setToolTip(
                        f"<span style='white-space: pre-wrap'>{escape(target)}</span>"
                    )
        if self.rows and self.table.currentRow() < 0:
            self.table.selectRow(0)
        self._details()

    def _details(self) -> None:
        row = self.table.currentRow()
        if not self.table.selectedItems() or not 0 <= row < len(self.rows):
            self.clear_details()
            return
        record = self.rows[row]
        reason = failure_caption(record, self.language)
        if unsuccessful_login(record):
            self.show_details(
                self.language,
                (("reasons", reason),),
                caption=audit_text,
            )
            return
        op = record.operation
        if op.action in CONFIGURATION_ACTIONS:
            config = configuration_details(record, self.language)
            self.show_details(
                self.language,
                (*config.text, ("reasons", reason)),
                tables=config.tables,
                notices=config.notices,
                caption=audit_text,
            )
            return
        plc_write = op.action == "plc.write"
        comparison = local_change_comparison(record, self.language)
        incomplete = incomplete_changes(op)
        before = change_caption(op.action, op.before, self.language)
        after = (
            plc_requested_values(op, self.language)
            if plc_write
            else change_caption(op.action, op.after, self.language)
        )
        if op.action == "production.recipe" and op.after == op.target:
            after = ""
        if not incomplete:
            if before == after:
                before = after = ""
            elif (not before or not after) and (before or after) == target_caption(
                record, self.language
            ):
                before = after = ""
        evidence = outcome_evidence_caption(record, self.language)
        if (
            has_approved_wording(self.language)
            and op.action == "alarm_definitions.create"
            and record.outcome == "success"
            and after == "Bundled template"
        ):
            after = ""
            evidence = audit_text("audit_from_template", self.language)
        self.show_details(
            self.language,
            (
                (
                    before_caption_key(op.action, self.language),
                    before if comparison is None else "",
                ),
                (
                    after_caption_key(record, self.language, local=True),
                    after if comparison is None else "",
                ),
                ("", evidence),
                (
                    "reasons",
                    reason if record.detail.strip() not in (before, after) else "",
                ),
                (
                    "",
                    audit_text("identity_audit_incomplete", self.language)
                    if incomplete
                    else "",
                ),
            ),
            tables=(comparison,) if comparison is not None else (),
            caption=audit_text,
        )

    def dispose(self) -> None:
        self._cancel.set()
        self.accounts.dispose()
        self._delivery.detach()
