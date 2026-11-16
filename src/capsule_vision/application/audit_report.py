"""Private Audit report snapshots using the existing Audit/History owners."""

import re
import sqlite3
from collections.abc import Iterator
from concurrent.futures import CancelledError
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from time import time
from typing import TYPE_CHECKING

from capsule_vision.localization.catalog import AUDIT_ACTIONS, text
from capsule_vision.localization.languages import Language
from capsule_vision.localization.record_text import readable_value, record_reason

from .audit import AuditAccount, AuditOperation, AuditQuery, AuditRecord
from .audit_presentation import (
    ACTION_CAPTIONS,
    BATCH_FIELDS,
    OUTCOME_CAPTIONS,
    AuditChange,
    ChangeComparison,
    change_caption,
    change_comparison,
    configuration_field_caption,
    incomplete_changes,
    login_failure_reason,
    plc_requested_values,
    plc_write_range,
    recorded_change_pairs,
    target_caption,
    unsuccessful_login,
)
from .identity_models import Role
from .records_query import RecordsScope, associated_audit

if TYPE_CHECKING:
    from .history import ProductionHistory
    from .identity import LocalIdentity
    from .report_content import AuditReportRow, Table, Value


class NoAuditRecords(ValueError):
    """An otherwise valid export scope contains no matching recorded operation."""


@dataclass(frozen=True, slots=True)
class AuditReportRequest:
    filters: RecordsScope = field(default_factory=RecordsScope)
    account: AuditAccount | None = None

    def query(self) -> AuditQuery:
        def utc(stamp: int | None) -> str:
            return (
                datetime.fromtimestamp(stamp, UTC).isoformat(timespec="seconds")
                if stamp is not None
                else ""
            )

        return AuditQuery(
            since=utc(self.filters.since),
            until=utc(self.filters.until),
            account=self.account,
        )


def capture_links(db: sqlite3.Connection, path: Path, cancel: Event) -> None:
    if cancel.is_set():
        raise CancelledError()
    db.execute("ATTACH DATABASE ? AS report", (str(path),))
    try:
        with db:
            db.execute("BEGIN")
            db.set_progress_handler(lambda: int(cancel.is_set()), 4096)
            db.execute(
                "CREATE TABLE report.batches AS SELECT uuid,label FROM main.batches"
            )
            db.execute("CREATE UNIQUE INDEX report.batch_uuid ON batches(uuid)")
            db.execute("CREATE TABLE report.runs AS SELECT uuid,batch FROM main.runs")
            db.execute("CREATE UNIQUE INDEX report.run_uuid ON runs(uuid)")
            db.execute(
                "CREATE TABLE report.cycles AS SELECT DISTINCT run_id,batch_label FROM main.cycles WHERE batch_label IS NULL"
            )
            db.execute("CREATE INDEX report.unbatched_run ON cycles(run_id)")
    except sqlite3.OperationalError:
        if cancel.is_set():
            raise CancelledError() from None
        raise
    finally:
        db.set_progress_handler(None, 0)
        db.execute("DETACH DATABASE report")


def record(row: sqlite3.Row) -> AuditRecord:
    return AuditRecord(
        row["cursor"],
        AuditOperation(
            *(
                row[k]
                for k in (
                    "actor_id",
                    "username",
                    "action",
                    "target",
                    "old_value",
                    "new_value",
                    "id",
                    "utc",
                )
            )
        ),
        row["outcome"],
        row["completed"],
        row["detail"],
    )


def capture(
    identity: LocalIdentity,
    history: ProductionHistory | None,
    request: AuditReportRequest,
    path: Path,
    cancel: Event,
    revision: int,
) -> None:
    """Export-worker coordinator; no owner waits for a different owner or identity lock."""

    def check() -> None:
        if cancel.is_set():
            raise CancelledError()
        with identity.admission(Role.ENGINEER, revision, action=""):
            pass

    check()
    observed = time()
    identity.audit.capture_report(request.query(), path, cancel, revision).result()
    scope = request.filters
    if scope.batch is not None or scope.unbatched:
        check()
        if history is None:
            raise ValueError("History unavailable for Audit Batch associations")
        history.capture_audit_links(path, cancel).result()
    with closing(sqlite3.connect(path)) as db, db:
        db.row_factory = sqlite3.Row
        label = None
        if scope.batch is not None or scope.unbatched:
            selected = db.execute(
                "SELECT label FROM batches WHERE uuid=?", (scope.batch,)
            ).fetchone()
            label = selected[0] if selected else None
            if scope.batch is not None and label is None:
                raise ValueError("Selected Batch is unavailable")
            cursor: int | None = None
            while True:
                check()
                rows = db.execute(
                    "SELECT * FROM audit_records WHERE (? IS NULL OR cursor<?) ORDER BY cursor DESC LIMIT 51",
                    (cursor, cursor),
                ).fetchall()
                if not rows:
                    break
                records = tuple(record(row) for row in rows)
                kept = {r.cursor for r in associated_audit(db, scope, records)}
                db.executemany(
                    "DELETE FROM audit_records WHERE cursor=?",
                    ((r.cursor,) for r in records if r.cursor not in kept),
                )
                cursor = records[-1].cursor
        check()
        if not db.execute("SELECT 1 FROM audit_records LIMIT 1").fetchone():
            raise NoAuditRecords()
        db.execute(
            """CREATE TABLE meta AS SELECT 'audit' AS scope,'' AS basis,? AS observed,
                ? AS since,? AS until,? AS filter_label,? AS unbatched,? AS account,? AS scoped_segment""",
            (
                observed,
                scope.since,
                scope.until,
                label,
                scope.unbatched,
                request.account.username if request.account else None,
                scope.batch is not None,
            ),
        )


def field_groups(
    db: sqlite3.Connection, language: Language
) -> tuple[list[tuple[str, Value]], list[tuple[str, Value]]]:
    m = db.execute("SELECT * FROM meta").fetchone()
    label = m["filter_label"]
    left: list[tuple[str, Value]] = [
        (
            text(
                "customer_audit_batch" if m["scoped_segment"] else "batch_input",
                language,
            ),
            (_business_text(label) or "—")
            if label is not None
            else text(
                "records_no_batch" if m["unbatched"] else "records_all_batches",
                language,
            ),
        ),
        (
            text("customer_audit_account", language),
            _business_text(m["account"]) or "—"
            if m["account"] is not None
            else text("records_all_accounts", language),
        ),
    ]
    right: list[tuple[str, Value]] = []
    for name, key in (
        ("since", "customer_audit_from"),
        ("until", "customer_audit_before"),
    ):
        if m[name] is not None:
            right.append((text(key, language), datetime.fromtimestamp(m[name])))
    if not right:
        right.append(
            (text("export_period", language), text("history_all_dates", language))
        )
    return left, right


_PRIVATE_TEXT = re.compile(
    r"""\\|(?:^|[\s("'=])(?:[a-z]:/|/\S|\.{1,2}/)|"""
    r"\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b|"
    r"\b(?=[a-z0-9._-]*[a-z])[a-z0-9._-]+:[0-9]{1,5}\b|"
    r"\b(?:password|passwd|credential|verifier|secret|salt|digest|token)\s*[:=]",
    re.IGNORECASE | re.ASCII,
)


def _business_text(value: str) -> str:
    # Apply this only to fields explicitly recorded as business names or text.
    return (
        readable_value(value)
        if value.isprintable() and not _PRIVATE_TEXT.search(value)
        else ""
    )


def _target(entry: AuditRecord, language: Language) -> str:
    op = entry.operation
    if op.action == "production.start" and entry.outcome == "success":
        return text("records_run", language)
    if op.action == "plc.write":
        if register_range := plc_write_range(op.target):
            address, count = register_range
            return f"PLC · {address} × {count}"
        return "PLC"
    if op.action in ("batch.select", "batch.end", "batch.information"):
        label = {
            "batch.select": op.after,
            "batch.end": op.before,
            "batch.information": op.target
            if entry.outcome not in ("denied", "stale")
            else "",
        }[op.action]
        return _business_text(label) or text("batch_id", language)
    if (
        op.action in ("account.role", "account.enabled", "account.reset_password")
        and entry.outcome == "success"
    ):
        # The account owner supplies the committed target username.
        return _business_text(entry.detail) or text("records_user", language)
    if op.action == "model.stage":
        return text("model", language)
    return _business_text(target_caption(entry, language)) or "—"


_RECIPE_FIELDS = {
    "display_name",
    "product_code",
    "defaults.vimo.enabled",
    "defaults.vimo.required",
    "defaults.vimo.disabled_labels[]",
    "capsule.size.enabled",
    "capsule.size.target_length",
    "capsule.size.length_tolerance",
    "capsule.color.enabled",
    "capsule.color.target_hue",
    "capsule.color.hue_tolerance",
    "image_archive.save_ok",
    "image_archive.save_ng",
    "image_archive.save_error",
    "image_archive.save_empty",
}
_CONFIGURATION_FIELDS = {
    "recipe.save": _RECIPE_FIELDS,
    "recipe.delete": _RECIPE_FIELDS,
    "vision.layout": {
        "layout.cameras[].camera_id",
        "layout.cameras[].slots[].slot",
        "layout.cameras[].slots[].channel_id",
        *(
            f"layout.cameras[].slots[].rect.{key}"
            for key in ("x", "y", "width", "height")
        ),
    },
    "vision.sources": {
        "cameras[].camera_id",
        "loop",
        "target_fps",
        "mode",
    },
    "communication.save": {
        "diagnostic.timeout",
        "diagnostic.type",
        "reporting.enabled",
        "reporting.settings.mode",
        "reporting.settings.endpoint.type",
        "web.enabled",
        "web.interval_ms",
        "status.enabled",
    },
}
_SWITCH = {"True": "identity_enabled", "False": "identity_disabled"}
_SOURCE_MODES = {"folder": "source_folder_status", "basler": "vision_basler_mode"}
_REPORTING_MODES = {
    "per_cycle": "plc_mode_per_cycle",
    "four_cycle": "plc_mode_four_cycle",
    "single_cycle": "plc_mode_single_cycle",
}
_PROTOCOLS = {
    "FinsUdpEndpoint": "FINS/UDP",
    "FinsTcpEndpoint": "FINS/TCP",
    "ModbusSettings": "Modbus/TCP",
}


def _configuration_value(path: str, value: str, language: Language) -> str:
    if value == "—":
        return value
    field = path.rsplit(".", 1)[-1]
    if field in ("display_name", "product_code", "disabled_labels[]"):
        return _business_text(value) if value else text("audit_empty_value", language)
    if field == "camera_id":
        return value if re.fullmatch(r"CAM[1-6]", value) else ""
    if field == "type":
        return _PROTOCOLS.get(value, "")
    if field in ("enabled", "required", "loop") or field.startswith("save_"):
        key = _SWITCH.get(value)
        return text(key, language) if key else ""
    if field == "mode":
        options = (
            _REPORTING_MODES if path == "reporting.settings.mode" else _SOURCE_MODES
        )
        key = options.get(value)
        return text(key, language) if key else ""
    pattern = (
        r"[0-9]+(?:\.[0-9]+)?(?:e[+-]?[0-9]+)?"
        if field in ("target_fps", "timeout")
        else r"[0-9]+"
    )
    return value if re.fullmatch(pattern, value) else ""


def _approved_field(
    action: str, path: str, recorded: str, language: Language
) -> tuple[str, str] | None:
    if action == "batch.information":
        key = BATCH_FIELDS.get(path)
        value = (
            _business_text(recorded)
            if recorded
            else text("audit_empty_value", language)
        )
        return (text(key, language), value) if key and value else None
    # Match the whole recorded field path before rendering any part of it. The
    # normalized path is for disclosure only. Pairing keeps the recorded index.
    normalized = re.sub(r"\[[0-9]{1,3}\]", "[]", path)
    if "[]" in path or normalized not in _CONFIGURATION_FIELDS[action]:
        return None
    rendered = _configuration_value(normalized, recorded, language)
    caption = configuration_field_caption(action, path, language)
    return (caption, rendered) if caption and rendered else None


def _changes(action: str, value: str, language: Language) -> str:
    if action in _CONFIGURATION_FIELDS or action == "batch.information":
        lines: list[str] = []
        separator = " · " if action == "batch.information" else " = "
        for line in value.splitlines():
            name, sep, recorded = line.partition(" = ")
            if sep and (approved := _approved_field(action, name, recorded, language)):
                lines.append(separator.join(approved))
        return "\n".join(lines)
    if action in ("batch.select", "batch.end", "model.rename"):
        return _business_text(value)
    if action in ("account.enabled", "communication.web_power"):
        key = _SWITCH.get(value)
        return text(key, language) if key else ""
    if action == "account.role" and value.upper() in Role.__members__:
        return text("identity_" + value.lower(), language)
    if action == "account.create":
        parts = value.rsplit(" · ", 2)
        if (
            len(parts) == 3
            and parts[1] in Role.__members__
            and parts[2] in ("enabled", "first setup")
        ):
            return change_caption(action, value, language)
    if action == "recipe.process_value":
        match = re.fullmatch(
            r"[a-zA-Z0-9_.-]+: (D[0-9]{1,5}–D[0-9]{1,5} = -?[0-9]{1,10})", value
        )
        return match[1] if match else ""
    if action == "production.clear_statistics" and re.fullmatch(
        r"cycles=[0-9]+, groups=[0-9]+", value
    ):
        return value
    return ""


_FAILURE_CAPTIONS = {
    "AuthorizationError": "audit_denied",
    "StaleSessionError": "identity_stale",
    "ValueError": "records_config_error",
    "OSError": "records_config_io",
    "AuditUnavailable": "audit_required",
    "AuthenticationError": "identity_invalid",
    "SecurityStorageError": "identity_storage",
    "AccountConflictError": "identity_conflict",
    "PasswordReuseError": "identity_rotate",
    "CancelledError": "audit_cancelled",
    "ApplicationStateError": "read_only",
    "PermissionError": "audit_denied",
    "ReportingWriteProtectedError": "plc_reporting_write_protected",
}


def _disclosed_change(
    action: str, field: str, value: str, language: Language
) -> tuple[str, str] | None:
    if action in _CONFIGURATION_FIELDS or action == "batch.information":
        return _approved_field(action, field, value, language)
    rendered = _changes(action, value, language)
    return (text(field, language), rendered) if rendered else None


def details(
    entry: AuditRecord, language: Language
) -> tuple[ChangeComparison | None, str]:
    """Return only approved paired fields and independently displayable detail."""
    if unsuccessful_login(entry):
        reason = login_failure_reason(entry, language)
        return None, f"{text('reasons', language)}: {reason}" if reason else ""
    op = entry.operation
    plc_write = op.action == "plc.write"
    pairs = recorded_change_pairs(op)
    rows: list[AuditChange] = []
    omitted = False
    incomplete = incomplete_changes(op)
    if pairs:
        prior: list[str] = []
        requested: list[str] = []
        separator = " · " if op.action == "batch.information" else " = "
        for field, old, new in pairs:
            left = _disclosed_change(op.action, field, old, language)
            right = _disclosed_change(op.action, field, new, language)
            if left is not None and right is not None:
                rows.append((left[0], left[1], right[1]))
            elif left is not None:
                prior.append(separator.join(left))
                omitted = True
            elif right is not None:
                requested.append(separator.join(right))
                omitted = True
        before, after = "\n".join(prior), "\n".join(requested)
    else:
        before = _changes(op.action, op.before, language)
        after = (
            plc_requested_values(op, language, max_displayed=16)
            if plc_write
            else _changes(op.action, op.after, language)
        )
        if not incomplete and (
            before == after
            or (
                (not before or not after)
                and (before or after) == _target(entry, language)
            )
        ):
            before = after = ""
    lines = [
        f"{text(caption, language)}: {value}"
        for caption, value in (
            ("audit_before", before),
            (
                "audit_after"
                if entry.outcome == "success" and not plc_write
                else "audit_requested",
                after,
            ),
        )
        if value
    ]
    detail = entry.detail.strip()
    if entry.outcome != "success" and detail not in ("", entry.outcome, op.target):
        # record_reason leaves unknown input unchanged. Only its known
        # translations and the explicit classifications above reach the report.
        reason = record_reason(detail, language)
        if reason == detail:
            reason = text(_FAILURE_CAPTIONS.get(detail, "operation_error"), language)
        lines.append(f"{text('reasons', language)}: {reason}")
    if incomplete:
        lines.append(text("identity_audit_incomplete", language))
    if omitted:
        lines.append(text("audit_approved_values", language))
    comparison = (
        change_comparison(tuple(rows), entry.outcome, language) if rows else None
    )
    return comparison, "\n".join(lines)


def table(db: sqlite3.Connection, language: Language) -> Table:
    from .report_content import AuditReportRow, Table

    def rows() -> Iterator[AuditReportRow]:
        for row in db.execute("SELECT * FROM audit_records ORDER BY cursor DESC"):
            entry = record(row)
            op = entry.operation
            short = ACTION_CAPTIONS.get(op.action)
            if op.action == "account.enabled" and op.after in ("True", "False"):
                short = "identity_enable" if op.after == "True" else "identity_disable"
            action = (
                text(short or op.action, language)
                if short or op.action in AUDIT_ACTIONS
                else text("audit_action", language)
            )
            outcome = text(
                OUTCOME_CAPTIONS.get(entry.outcome, "audit_" + entry.outcome), language
            )
            comparison, detail = details(entry, language)
            yield AuditReportRow(
                (
                    datetime.fromisoformat(op.utc).astimezone().replace(tzinfo=None),
                    _business_text(op.username) or "—",
                    action,
                    _target(entry, language),
                    outcome,
                ),
                detail,
                comparison,
            )

    return Table(
        text("audit_trail", language),
        tuple(
            text(k, language)
            for k in (
                "audit_time",
                "customer_audit_account",
                "audit_action",
                "audit_target",
                "audit_outcome",
            )
        ),
        (23, 18, 24, 23, 12),
        rows(),
    )
