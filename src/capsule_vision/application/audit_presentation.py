"""Stored Audit business names shared by Records and customer reports."""

import re
from dataclasses import dataclass

from capsule_vision.localization.audit_wording import audit_text, has_approved_wording
from capsule_vision.localization.catalog import AUDIT_ACTIONS, text
from capsule_vision.localization.languages import Language
from capsule_vision.localization.record_text import readable_value, record_reason

from .audit import AUDIT_CHANGE_LIMIT, AuditOperation, AuditRecord
from .identity_models import Role

ACTION_CAPTIONS = {
    "session.login": "identity_login",
    "session.auto_login": "identity_auto_login",
    "session.logout": "identity_logout",
    "account.create": "identity_create",
    "account.role": "identity_change_role",
    "account.change_password": "identity_change",
    "account.reset_password": "identity_reset",
    "account.set_auto_login": "identity_set_auto_login",
    "account.disable_auto_login": "identity_disable_auto_login",
    "production.start": "replay_start",
    "production.stop": "production_stop",
    "production.recipe": "recipe",
    "communication.save": "comm_save_settings",
    "alarm_definitions.create": "records_create_alarm_file",
}
OUTCOME_CAPTIONS = {
    "success": "records_success",
    "uncertain": "records_uncertain",
    "unresolved": "records_unresolved",
}


def action_caption(
    operation: AuditOperation,
    language: Language,
    *,
    tooltip: bool = False,
    customer: bool = False,
) -> str:
    action = operation.action
    short = ACTION_CAPTIONS.get(action)
    if action == "account.enabled" and operation.after in ("True", "False"):
        short = "identity_enable" if operation.after == "True" else "identity_disable"
    if short and (not tooltip or short.startswith("identity_")):
        return audit_text(short, language)
    if action in AUDIT_ACTIONS:
        return audit_text(action, language)
    return text("audit_action", language) if customer else readable_value(action) or "—"


def outcome_caption(outcome: str, language: Language, *, tooltip: bool = False) -> str:
    key = "audit_" + outcome
    return audit_text(key if tooltip else OUTCOME_CAPTIONS.get(outcome, key), language)


BATCH_FIELDS = {
    "product": "batch_product",
    "specification": "batch_specification",
    "notes": "batch_notes",
}
_CONFIGURATION_LABELS = {
    "display_name": "display_name",
    "product_code": "product_code",
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
    "loop": "source_loop",
    "target_fps": "vision_fps",
    "reporting": "short_reporting",
    "diagnostic": "comm_diagnostics",
    "status": "status_service",
    "timeout": "comm_timeout",
    "type": "comm_protocol",
    "interval_ms": "web_refresh",
}
_FIELD_CHANGES = {
    "recipe.save",
    "recipe.delete",
    "vision.layout",
    "vision.sources",
    "communication.save",
    "batch.information",
}
_SCALAR_CHANGES = {
    "account.role": "identity_role",
    "account.enabled": "identity_status",
    "communication.web_power": "identity_status",
    "model.rename": "model_name",
    "batch.select": "batch_id",
}
_PATH_COMPONENT = r"[a-z_][a-z0-9_]*(?:\[[0-9]+\])*"
_FIELD_PATH = re.compile(rf"{_PATH_COMPONENT}(?:\.{_PATH_COMPONENT})*")

type AuditChange = tuple[str, str, str]


@dataclass(frozen=True, slots=True)
class ChangeComparison:
    headers: tuple[str, str, str]
    rows: tuple[AuditChange, ...]


def incomplete_changes(op: AuditOperation) -> bool:
    if op.action not in _FIELD_CHANGES and op.action not in _SCALAR_CHANGES:
        return False
    return any(
        len(value) >= AUDIT_CHANGE_LIMIT
        or (op.action in _FIELD_CHANGES and "…" in value.splitlines())
        for value in (op.before, op.after)
    )


def _parse_fields(action: str, value: str) -> dict[str, str] | None:
    fields: dict[str, str] = {}
    for line in value.split("\n"):
        field, separator, recorded = line.partition(" = ")
        if (
            not separator
            or not _FIELD_PATH.fullmatch(field)
            or (action == "batch.information" and field not in BATCH_FIELDS)
            or field in fields
            or (recorded and not recorded.isprintable())
        ):
            return None
        fields[field] = recorded
    return fields


def recorded_change_pairs(op: AuditOperation) -> tuple[AuditChange, ...]:
    """Return only unambiguous pairs, using recorded keys and the prior order."""
    if not op.before or not op.after or op.before == op.after or incomplete_changes(op):
        return ()
    if op.action in _SCALAR_CHANGES:
        if not (op.before.isprintable() and op.after.isprintable()):
            return ()
        if op.action == "account.role" and any(
            value.upper() not in Role.__members__ for value in (op.before, op.after)
        ):
            return ()
        if op.action in ("account.enabled", "communication.web_power") and any(
            value not in ("True", "False") for value in (op.before, op.after)
        ):
            return ()
        return ((_SCALAR_CHANGES[op.action], op.before, op.after),)
    if op.action not in _FIELD_CHANGES:
        return ()
    before = _parse_fields(op.action, op.before)
    after = _parse_fields(op.action, op.after)
    if before is None or after is None or before.keys() != after.keys():
        return ()
    return tuple((field, value, after[field]) for field, value in before.items())


def change_comparison(
    rows: tuple[AuditChange, ...], outcome: str, language: Language
) -> ChangeComparison:
    return ChangeComparison(
        (
            text("audit_field", language),
            text("audit_before", language),
            text(
                "audit_after" if outcome == "success" else "audit_requested", language
            ),
        ),
        rows,
    )


def configuration_field_caption(action: str, path: str, language: Language) -> str:
    leaf = path.rsplit(".", 1)[-1].partition("[")[0]
    if leaf != "mode" and leaf not in _CONFIGURATION_LABELS:
        return ""
    labels: list[str] = []
    for part in path.split("."):
        field, indexed, index = part.partition("[")
        if field in ("vimo", "web"):
            caption = "ViMo" if field == "vimo" else "Web"
        elif field == "mode":
            caption = text(
                "plc_mode" if action == "communication.save" else "comm_source",
                language,
            )
        else:
            key = _CONFIGURATION_LABELS.get(field)
            if key is None:  # Definition wrappers have no field caption.
                continue
            caption = audit_text(key, language)
        labels.append(f"{caption} [{index[:-1]}]" if indexed else caption)
    return " · ".join(labels)


def scalar_change_caption(action: str, field: str, language: Language) -> str:
    if action == "communication.web_power" and has_approved_wording(language):
        return audit_text("audit_web_enabled", language)
    return audit_text(field, language)


def local_change_comparison(
    record: AuditRecord, language: Language
) -> ChangeComparison | None:
    action = record.operation.action
    rows: list[AuditChange] = []
    for field, before, after in recorded_change_pairs(record.operation):
        if action in _SCALAR_CHANGES:
            caption = scalar_change_caption(action, field, language)
            value_action = (
                "account.enabled" if action == "communication.web_power" else action
            )
            before = change_caption(value_action, before, language)
            after = change_caption(value_action, after, language)
            if not before or not after:
                return None
        elif action == "batch.information":
            caption = text(BATCH_FIELDS[field], language)
        else:
            if any(
                not readable_value(f"{field} = {value}") for value in (before, after)
            ):
                return None
            caption = configuration_field_caption(action, field, language) or field
        rows.append(
            (
                caption,
                before or text("audit_empty_value", language),
                after or text("audit_empty_value", language),
            )
        )
    return change_comparison(tuple(rows), record.outcome, language) if rows else None


def unsuccessful_login(record: AuditRecord) -> bool:
    return record.operation.action == "session.login" and record.outcome != "success"


def login_failure_reason(record: AuditRecord, language: Language) -> str:
    if record.outcome == "cancelled":
        return ""
    key = {
        "AuthenticationError": "identity_invalid",
        "StaleSessionError": "identity_stale",
        "SecurityStorageError": "identity_storage",
        "OperationalError": "identity_storage",
        "DatabaseError": "identity_storage",
        "OSError": "identity_storage",
    }.get(record.detail)
    return audit_text(key, language) if key else ""


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
_APPROVED_FAILURE_CAPTIONS = {
    "AuthorizationError": "audit_authorization_denied",
    "AuthenticationError": "audit_identity_check_failed",
    "ApplicationStateError": "audit_state_unavailable",
    "OSError": "audit_file_error",
    "PermissionError": "audit_access_denied",
}
_REDUNDANT_REASONS = {
    ("denied", "AuthorizationError"),
    ("stale", "StaleSessionError"),
    ("cancelled", "CancelledError"),
}
_PROTECTED_WRITE_DETAIL = re.compile(
    r"Manual write overlaps active (?:PLC status W[0-9]{1,5}, W[0-9]{1,5}"
    r"|reporting (?:W|HR )[0-9]{1,5}\.\.[0-9]{1,5})"
)
_PROCESS_READBACK_DETAIL = re.compile(
    r"[1-9][0-9]* register values verified; no ladder-consumption confirmation"
)
_MATCHED_READBACK_DETAIL = "Register readback matched"


def _plc_failure_key(detail: str, outcome: str) -> str:
    if outcome in ("failed", "denied") and (
        detail == "ReportingWriteProtectedError"
        or _PROTECTED_WRITE_DETAIL.fullmatch(detail)
    ):
        return "plc_reporting_write_protected"
    if outcome == "failed" and detail in ("FinsPlcError", "ModbusExceptionResponse"):
        return "audit_plc_rejected"
    if detail in ("FinsTimeoutError", "ModbusTimeoutError"):
        return "audit_plc_timeout"
    return ""


def failure_caption(
    record: AuditRecord, language: Language, *, customer: bool = False
) -> str:
    """Translate recorded classifications with a positive customer disclosure rule."""
    if unsuccessful_login(record):
        return login_failure_reason(record, language)
    detail = record.detail.strip()
    if record.outcome == "success" or detail in (
        "",
        record.outcome,
        record.operation.target,
    ):
        return ""
    approved = has_approved_wording(language)
    if approved:
        action = record.operation.action
        if (record.outcome, detail) in _REDUNDANT_REASONS:
            return ""
        if action == "plc.write" and (key := _plc_failure_key(detail, record.outcome)):
            return audit_text(key, language)
        if action == "production.start" and detail == "ApplicationStateError":
            return audit_text("audit_start_unavailable", language)
        if detail == "ValueError" and action in _FIELD_CHANGES:
            return audit_text("audit_validation_failed", language)
    translated = record_reason(detail, language)
    if translated != detail:
        return translated
    if approved:
        key = _APPROVED_FAILURE_CAPTIONS.get(detail, _FAILURE_CAPTIONS.get(detail))
        if key and detail not in ("ValueError", "ReportingWriteProtectedError"):
            return audit_text(key, language)
        return "" if customer else detail
    if customer:
        return text(_FAILURE_CAPTIONS.get(detail, "operation_error"), language)
    return detail


def outcome_evidence_caption(record: AuditRecord, language: Language) -> str:
    """Report the kind of PLC evidence established by the recorded outcome."""
    if not has_approved_wording(language):
        return ""
    action = record.operation.action
    if action == "plc.write" and record.outcome == "uncertain":
        return audit_text("audit_plc_unknown", language)
    if record.outcome != "success":
        return ""
    if action == "plc.write":
        return audit_text("audit_plc_acknowledged", language)
    if action == "recipe.process_value" and record.detail == _MATCHED_READBACK_DETAIL:
        return audit_text("audit_readback_matched", language)
    if action == "recipe.process_apply" and _PROCESS_READBACK_DETAIL.fullmatch(
        record.detail
    ):
        return audit_text("audit_registers_matched", language)
    return ""


def before_caption_key(action: str, language: Language) -> str:
    if has_approved_wording(language) and action == "batch.end":
        return "audit_previous_batch"
    return "audit_before"


def after_caption_key(
    record: AuditRecord, language: Language, *, local: bool = False
) -> str:
    op = record.operation
    if record.outcome != "success" or op.action == "plc.write":
        return "audit_requested"
    if has_approved_wording(language):
        if op.action == "recipe.process_value":
            return "audit_requested"
        if local and op.action == "production.start":
            return "audit_recipe_id"
        if local and op.action == "model.publish" and _published_module(op.after):
            return "audit_module_id"
    return "audit_after"


def plc_write_range(target: str) -> tuple[str, int] | None:
    """Read the register range without disclosing the endpoint or route."""
    match = re.search(r" (130|177|holding) ([0-9]{1,5}) × ([0-9]{1,4})$", target)
    if not match:
        return None
    area, address, count = match.groups()
    return {"130": "D", "177": "W", "holding": "HR"}[area] + address, int(count)


def plc_requested_values(
    operation: AuditOperation, language: Language, *, max_displayed: int | None = None
) -> str:
    recorded = operation.after
    if not recorded:
        return ""
    framed = recorded.startswith("[") and recorded.endswith("]")
    clipped = not framed and len(recorded) == AUDIT_CHANGE_LIMIT
    if framed:
        recorded = recorded[1:-1]
    elif clipped:
        # The old character cap can leave an apparently valid partial last word.
        recorded = recorded.rpartition(",")[0]

    incomplete = audit_text("identity_audit_incomplete", language)
    words = recorded.split(", ")
    if any(
        not re.fullmatch(r"[0-9]{1,5}", word) or int(word) > 65535 for word in words
    ):
        return incomplete
    register_range = plc_write_range(operation.target)
    if register_range and len(words) > register_range[1]:
        return incomplete
    lines = [", ".join(words[:max_displayed])]
    if max_displayed is not None and len(words) > max_displayed:
        lines.append(
            text("audit_plc_values_omitted", language).format(
                count=len(words) - max_displayed
            )
        )
    if clipped or (register_range and len(words) < register_range[1]):
        lines.append(incomplete)
    return "\n".join(lines)


_PRIVATE_TEXT = re.compile(
    r"""\\|(?:^|[\s("'=])(?:[a-z]:/|/\S|\.{1,2}/)|"""
    r"\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b|"
    r"\b(?=[a-z0-9._-]*[a-z])[a-z0-9._-]+:[0-9]{1,5}\b|"
    r"\b(?:password|passwd|credential|verifier|secret|salt|digest|token)\s*[:=]",
    re.IGNORECASE | re.ASCII,
)


def disclosed_business_text(value: str) -> str:
    """Apply only to fields explicitly recorded as business names or text."""
    return (
        readable_value(value)
        if value.isprintable() and not _PRIVATE_TEXT.search(value)
        else ""
    )


_TARGET_CATEGORIES = {
    "security.storage": "security.storage",
    "history.storage": "history.storage",
    "production.clear_statistics": "records_inspection",
    "vision.layout": "records_layout",
    "vision.sources": "records_sources",
    "communication.save": "page_communication",
    "alarm_definitions.create": "alarm_file",
}
_APPROVED_TARGET_CATEGORIES = {
    "security.storage": "audit_account_storage",
    "history.storage": "audit_production_history",
    "recipe.process_apply": "audit_process_parameters",
    "recipe.process_value": "audit_process_parameter",
    "preferences.save": "audit_preferences",
}


def target_caption(
    record: AuditRecord, language: Language, *, customer: bool = False
) -> str:
    op = record.operation
    if unsuccessful_login(record):
        return audit_text("records_user", language)
    action, target = op.action, readable_value(op.target)
    approved = has_approved_wording(language)
    if approved and action in _APPROVED_TARGET_CATEGORIES:
        return audit_text(_APPROVED_TARGET_CATEGORIES[action], language)
    if action in _TARGET_CATEGORIES:
        return audit_text(_TARGET_CATEGORIES[action], language)
    if action in ("plc.write", "communication.web_power"):
        return "PLC" if action == "plc.write" else "Web"
    if action in ("batch.select", "batch.end"):
        label = op.after if action == "batch.select" else op.before
        return readable_value(label) or target or text("batch_id", language)
    if action == "batch.information":
        return target or text("batch_id", language)
    if action == "production.stop":
        return text(
            "records_run"
            if op.target and not op.target.startswith("Start ")
            else "records_inspection",
            language,
        )
    if action.startswith(("session.", "account.")):
        fallback = audit_text("records_user", language)
        if (
            (approved or customer)
            and record.outcome == "success"
            and action in ("account.role", "account.enabled", "account.reset_password")
        ):
            # The account owner records the committed target name here.
            name = disclosed_business_text(record.detail)
            if name or customer:
                return name or fallback
        if (
            action in ("account.set_auto_login", "account.disable_auto_login")
            and record.outcome == "success"
        ):
            return readable_value(record.detail) or fallback
        # Only the recorded actor/target match establishes a self operation.
        if op.actor_id and op.target == op.actor_id:
            return readable_value(op.username) or fallback
        if action == "account.create":
            username, sep, role = op.after.rpartition(" · ")
            if sep and role in ("enabled", "first setup"):
                username, sep, role = username.rpartition(" · ")
            if sep and role in Role.__members__:
                return readable_value(username) or fallback
            if op.actor_id == "offline-maintenance":
                return readable_value(op.after) or fallback
        return fallback
    if (
        action == "production.start"
        and record.outcome == "success"
        and (approved or customer)
    ):
        return text("records_run", language)
    if action in ("production.recipe", "production.start"):
        # Start admission can fail before a Recipe has been identified.
        return text("page_recipe" if op.target else "records_inspection", language)
    if action in ("recipe.save", "recipe.delete"):
        # Recipe edits record explicit display-name differences when available.
        changes = (
            op.after
            if action == "recipe.save" and record.outcome == "success"
            else op.before
        )
        for line in changes.splitlines():
            key, sep, value = line.partition(" = ")
            if sep and key == "display_name" and readable_value(value) and value != "—":
                return value
        return text("page_recipe", language)
    if action.startswith("model."):
        if action == "model.rename":
            name = op.after if record.outcome == "success" else op.before
        elif action == "model.stage":
            name = op.after  # The import owner recorded the source filename.
        elif action in ("model.publish", "model.delete"):
            value, tail = (op.after, 2) if action == "model.publish" else (op.before, 1)
            fields = value.rsplit(" · ", tail)
            name = (
                fields[0]
                if len(fields) == tail + 1
                and re.fullmatch(r"[0-9a-fA-F]{64}", fields[-1])
                else ""
            )
        else:
            name = ""
        return readable_value(name) or text("model", language)
    return "—"


def _published_module(value: str) -> str:
    parts = value.rsplit(" · ", 2)
    if len(parts) == 3 and re.fullmatch(r"[0-9a-fA-F]{64}", parts[2]):
        return readable_value(parts[1])
    return ""


def change_caption(action: str, value: str, language: Language) -> str:
    if action == "batch.information":
        lines: list[str] = []
        for line in value.splitlines():
            name, sep, recorded = line.partition(" = ")
            lines.append(
                f"{text(BATCH_FIELDS[name], language)} · {recorded}"
                if sep and name in BATCH_FIELDS
                else line
            )
        return "\n".join(lines)
    if (
        action == "recipe.save"
        and value.startswith("display_name = ")
        and "\n" not in value
    ):
        return readable_value(value.removeprefix("display_name = "))
    if action == "account.create":
        parts = value.rsplit(" · ", 2)
        if (
            len(parts) == 3
            and parts[1] in Role.__members__
            and parts[2] in ("enabled", "first setup")
        ):
            role = text("identity_" + parts[1].lower(), language)
            return (
                f"{role} · {text('identity_enabled', language)}"
                if parts[2] == "enabled"
                else audit_text("identity_setup", language)
            )
    if action == "model.publish" and (module := _published_module(value)):
        return module
    if action == "account.enabled" and value in ("True", "False"):
        return text(
            "identity_enabled" if value == "True" else "identity_disabled", language
        )
    if action == "account.role" and value.upper() in Role.__members__:
        return text("identity_" + value.lower(), language)
    if action == "communication.save" and (
        match := re.fullmatch(r"diagnostic\.timeout = (\d+(?:\.\d+)?)", value)
    ):
        return f"{text('comm_diagnostics', language)} · {text('comm_timeout', language)} = {match[1]}"
    return "\n".join(
        line for line in value.splitlines() if not line or readable_value(line)
    )
