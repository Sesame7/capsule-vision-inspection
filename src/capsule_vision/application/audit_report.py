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
    OUTCOME_CAPTIONS,
    change_caption,
    target_caption,
)
from .identity_models import Role
from .records_query import RecordsScope, associated_audit

if TYPE_CHECKING:
    from .history import ProductionHistory
    from .identity import LocalIdentity
    from .report_content import Row, Table, Value


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
    left: list[tuple[str, Value]] = [
        (
            text(
                "customer_audit_batch" if m["scoped_segment"] else "batch_input",
                language,
            ),
            m["filter_label"]
            or text(
                "records_no_batch" if m["unbatched"] else "records_all_batches",
                language,
            ),
        ),
        (
            text("customer_audit_account", language),
            m["account"]
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


def _target(entry: AuditRecord, language: Language) -> str:
    op = entry.operation
    if op.action == "production.start" and entry.outcome == "success":
        return text("records_run", language)
    if op.action == "plc.write":
        # manual_target records area/start/count after its transport description.
        match = re.search(r" (130|177|holding) (\d+) × (\d+)$", op.target)
        if match:
            area, address, count = match.groups()
            prefix = {"130": "D", "177": "W", "holding": "HR"}[area]
            return f"PLC · {prefix}{address} × {count}"
    if (
        op.action in ("account.role", "account.enabled", "account.reset_password")
        and entry.outcome == "success"
        and entry.detail
    ):
        # The account owner records the committed target username on completion.
        return entry.detail
    return target_caption(entry, language)


def _changes(action: str, value: str, language: Language) -> str:
    if action == "model.cache":
        return ""  # Native-device selection is not a customer business change.
    if action == "communication.web_power" and value in ("True", "False"):
        return text(
            "identity_enabled" if value == "True" else "identity_disabled", language
        )
    if action not in (
        "recipe.save",
        "vision.layout",
        "vision.sources",
        "communication.save",
    ):
        return change_caption(action, value, language)
    # These are the existing typed definitions' recorded leaf differences. Never
    # turn a saved technical ID into a business name, or expose raw field paths.
    fields = {
        "display_name": "display_name",
        "product_code": "product_code",
        "vimo": "ViMo",
        "disabled_labels": "customer_audit_excluded",
        "enabled": "identity_status",
        "required": "customer_audit_required",
        "size": "size",
        "color": "color",
        "target_length": "target_length",
        "length_tolerance": "length_tolerance",
        "target_hue": "target_color",
        "hue_tolerance": "color_tolerance",
        "image_archive": "image_archive",
        "root_directory": "archive_directory",
        "save_ok": "archive_ok",
        "save_ng": "archive_ng",
        "save_error": "archive_error",
        "save_empty": "archive_empty",
        "cameras": "nav_cameras",
        "camera_id": "history_camera",
        "slots": "customer_audit_slot",
        "channel_id": "channel",
        "slot": "customer_audit_slot",
        "rect": "roi_workspace",
        "x": "roi_x",
        "y": "roi_y",
        "width": "roi_w",
        "height": "roi_h",
        "folder": "source_folder",
        "basler_serial": "source_serial",
        "loop": "source_loop",
        "target_fps": "vision_fps",
        "mode": "comm_source",
        "reporting": "short_reporting",
        "diagnostic": "comm_diagnostics",
        "web": "Web",
        "alarm": "alarm_monitor",
        "status": "status_service",
        "word_address": "status_alarm_address",
        "heartbeat_address": "status_heartbeat",
        "fault_address": "status_fault",
        "station": "alarm_station",
        "host": "comm_host",
        "port": "comm_port",
        "timeout": "comm_timeout",
        "destination": "comm_destination",
        "source": "comm_source",
        "network": "comm_network",
        "node": "comm_node",
        "unit": "comm_unit",
        "unit_id": "comm_unit",
        "base_address": "comm_address",
        "type": "comm_protocol",
        "interval_ms": "web_refresh",
        "max_pending": "customer_audit_capacity",
    }
    lines: list[str] = []
    for line in value.splitlines():
        path, sep, recorded = line.partition(" = ")
        if not sep or not readable_value(recorded):
            continue
        labels: list[str] = []
        for part in path.split("."):
            match = re.fullmatch(r"([a-z_]+)(?:\[(\d+)\])?", part)
            if not match:
                break
            key, index = match.groups()
            if key in ("defaults", "capsule", "layout", "settings", "endpoint"):
                continue
            if key not in fields:
                break
            caption = (
                fields[key] if key in ("vimo", "web") else text(fields[key], language)
            )
            if key == "mode" and action == "communication.save":
                caption = text("plc_mode", language)
            # Tuple ordinals are recorded indexes, not inferred channel identities.
            labels.append(caption + (f" [{index}]" if index is not None else ""))
        else:
            if labels:
                names = {
                    "True": "identity_enabled",
                    "False": "identity_disabled",
                    "folder": "source_folder_status",
                    "basler": "vision_basler_mode",
                    "per_cycle": "plc_mode_per_cycle",
                    "four_cycle": "plc_mode_four_cycle",
                    "single_cycle": "plc_mode_single_cycle",
                }
                protocols = {
                    "FinsUdpEndpoint": "FINS/UDP",
                    "FinsTcpEndpoint": "FINS/TCP",
                    "ModbusSettings": "Modbus/TCP",
                }
                if path.endswith((".enabled", ".required")) or path.split(".")[-1] in (
                    "loop",
                    "save_ok",
                    "save_ng",
                    "save_error",
                    "save_empty",
                ):
                    recorded = (
                        text(names[recorded], language)
                        if recorded in names
                        else recorded
                    )
                elif path.endswith("mode"):
                    recorded = (
                        text(names[recorded], language)
                        if recorded in names
                        else recorded
                    )
                elif path.endswith(".type"):
                    recorded = protocols.get(recorded, text("unavailable", language))
                lines.append(" · ".join(labels) + " = " + recorded)
    return "\n".join(lines)


def details(entry: AuditRecord, language: Language) -> str:
    """Customer fields only; never include credential actions' stored value payloads."""
    op = entry.operation
    secret = "password" in op.action or op.action in (
        "session.login",
        "session.auto_login",
        "session.logout",
        "account.setup",
        "account.recovery",
    )
    before = "" if secret else _changes(op.action, op.before, language)
    after = "" if secret else _changes(op.action, op.after, language)
    if op.action == "batch.information" and all(
        line.partition(" = ")[2] in ("", "—") for line in op.before.splitlines()
    ):
        before = ""
    if op.action in ("production.recipe", "production.start"):
        before = after = ""  # Saved Recipe IDs are not customer changes or names.
    if before == after or (
        (not before or not after) and (before or after) == _target(entry, language)
    ):
        before = after = ""
    lines: list[str] = []
    for caption, value in (
        ("audit_before", before),
        ("audit_after" if entry.outcome == "success" else "audit_requested", after),
    ):
        safe = "\n".join(line for line in value.splitlines() if readable_value(line))
        if safe:
            lines.append(f"{text(caption, language)}: {safe}")
    if entry.outcome != "success" and entry.detail.strip() not in (
        "",
        entry.outcome,
        op.target,
        before,
        after,
    ):
        value = record_reason(entry.detail.strip(), language)
        errors = {
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
        if value in errors:
            value = text(errors[value], language)
        elif re.fullmatch(r"[A-Za-z_][\w.]*?(Error|Exception|Unavailable)", value):
            value = text("operation_error", language)
        elif (
            (parts := value.split(" · "))
            and len(parts) == 2
            and re.fullmatch(r"[A-Za-z_][\w.]*?(Error|Exception|Unavailable)", parts[1])
        ):
            value = text(
                {
                    "startup": "startup_error",
                    "runtime": "runtime_error",
                    "shutdown": "shutdown_error",
                }.get(parts[0], "operation_error"),
                language,
            )
        if (
            readable_value(value)
            and not re.search(
                r"(?:password|credential|verifier|pbkdf|salt|digest)\s*[:=]",
                value,
                re.I,
            )
            and not re.match(r"Requested revision \d+; current revision \d+", value)
        ):
            lines.append(f"{text('reasons', language)}: {value}")
    return "\n".join(lines)


def table(db: sqlite3.Connection, language: Language) -> Table:
    from .report_content import Table

    def rows() -> Iterator[Row]:
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
            yield (
                datetime.fromisoformat(op.utc).astimezone().replace(tzinfo=None),
                op.username or "—",
                action,
                _target(entry, language),
                outcome,
                details(entry, language),
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
        detail_rows=True,
    )
