"""Read-only, revision-fenced audit browser within History."""

from dataclasses import replace
from threading import Event
from typing import cast

from PySide6.QtCore import Qt, Slot

from capsule_vision.application.audit import AuditQuery, AuditRecord
from capsule_vision.application.audit_presentation import (
    ACTION_CAPTIONS as _ACTION_CAPTIONS,
)
from capsule_vision.application.audit_presentation import (
    OUTCOME_CAPTIONS as _OUTCOME_CAPTIONS,
)
from capsule_vision.application.audit_presentation import (
    change_caption as _change_caption,
)
from capsule_vision.application.audit_presentation import (
    target_caption as _target_caption,
)
from capsule_vision.application.history import ProductionHistory
from capsule_vision.application.identity import LocalIdentity
from capsule_vision.application.identity_models import IdentityError, Role
from capsule_vision.application.records_query import RecordsScope, scoped_audit_page
from capsule_vision.localization.catalog import AUDIT_ACTIONS, text
from capsule_vision.localization.languages import Language

from .audit_accounts import AuditAccounts
from .future_delivery import FutureDelivery
from .history_event_table import (
    HistoryEventTable,
    audit_utc_bound,
    readable_value,
    record_reason,
    record_time,
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
        self._read_error = False
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
        self._read_error = False
        self.rows = ()
        self.table.setRowCount(0)
        self.accounts.clear()
        self.clear_details()
        self.message.clear()
        self.message.hide()
        self.message.setToolTip("")
        self.older.setEnabled(False)
        self.newer.setEnabled(False)

    def set_language(self, language: Language) -> None:
        self.language = language
        self.navigation_language(language)
        self.accounts.set_language(language)
        self.columns(
            language,
            (
                ("short_time", "2026-12-31 23:59:59", "2026-12-31 23:59:59", 1),
                ("records_user", "DEV-Operator", "DEV-Example-Engineer", 3),
                ("audit_action", "Change role", "Save camera sources", 4),
                ("audit_target", "DEV-LOT-F", "DEV-Example-Trainee-1", 4),
                ("audit_outcome", "Success", "Superseded", 1),
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
        self._read_error = error is not None
        self.refresh_status()
        self._render()

    @Slot()
    def refresh_status(self) -> None:
        if self.identity is None or self._revision is None:
            return
        lost, issue = self.identity.audit.observation()
        self.message.setText(
            (text("audit_warning", self.language).format(count=lost) if issue else "")
            or (
                text("history_unavailable", self.language)
                if self._read_error
                else text("audit_no_matches", self.language)
                if not self.rows
                else ""
            )
        )
        self.message.setToolTip(issue)
        self.message.setVisible(bool(self.message.text()))

    def _render(self) -> None:
        self.table.setRowCount(len(self.rows))
        for row, record in enumerate(self.rows):
            op = record.operation
            clock, full_time = record_time(op.utc)
            full_action = (
                text(op.action, self.language)
                if op.action in AUDIT_ACTIONS
                else readable_value(op.action) or "—"
            )
            short_key = _ACTION_CAPTIONS.get(op.action)
            if op.action == "account.enabled" and op.after in ("True", "False"):
                short_key = (
                    "identity_enable" if op.after == "True" else "identity_disable"
                )
            action = text(short_key, self.language) if short_key else full_action
            if short_key and short_key.startswith("identity_"):
                full_action = action
            outcome_key = "audit_" + record.outcome
            self.set_values(
                row,
                (
                    clock,
                    readable_value(op.username) or "—",
                    action,
                    _target_caption(record, self.language),
                    text(
                        _OUTCOME_CAPTIONS.get(record.outcome, outcome_key),
                        self.language,
                    ),
                ),
                {0: full_time, 2: full_action, 4: text(outcome_key, self.language)},
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
        op = record.operation
        before = _change_caption(op.action, op.before, self.language)
        after = _change_caption(op.action, op.after, self.language)
        if op.action == "production.recipe" and op.after == op.target:
            after = ""
        if before == after:
            before = after = ""
        elif not before or not after:
            if (before or after) == _target_caption(record, self.language):
                before = after = ""
        success = record.outcome == "success"
        reason = ""
        if not success and record.detail.strip() not in (
            record.outcome,
            op.target,
            before,
            after,
        ):
            reason = record_reason(record.detail.strip(), self.language)
        self.show_details(
            self.language,
            (
                ("audit_before", before),
                ("audit_after" if success else "audit_requested", after),
                ("reasons", reason),
            ),
        )

    def dispose(self) -> None:
        self._cancel.set()
        self.accounts.dispose()
        self._delivery.detach()
