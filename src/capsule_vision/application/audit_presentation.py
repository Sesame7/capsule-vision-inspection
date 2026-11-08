"""Stored Audit business names shared by Records and customer reports."""

import re

from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import Language
from capsule_vision.localization.record_text import readable_value

from .audit import AuditRecord
from .identity_models import Role

ACTION_CAPTIONS = {
    "session.login": "identity_login",
    "session.logout": "identity_logout",
    "account.create": "identity_create",
    "account.role": "identity_change_role",
    "account.change_password": "identity_change",
    "account.reset_password": "identity_reset",
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


def target_caption(record: AuditRecord, language: Language) -> str:
    op = record.operation
    action, target = op.action, readable_value(op.target)
    categories = {
        "security.storage": "security.storage",
        "history.storage": "history.storage",
        "production.clear_statistics": "records_inspection",
        "vision.layout": "records_layout",
        "vision.sources": "records_sources",
        "communication.save": "page_communication",
        "alarm_definitions.create": "alarm_file",
    }
    if action in categories:
        return text(categories[action], language)
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
        # Only the recorded actor/target match establishes a self operation.
        if (
            op.actor_id
            and op.target == op.actor_id
            and (action != "session.login" or record.outcome == "success")
        ):
            return readable_value(op.username) or text("records_user", language)
        if action == "session.login" and record.outcome != "success" and target:
            return target  # The authentication owner recorded the attempted username.
        if action == "account.create":
            username, sep, role = op.after.rpartition(" · ")
            if sep and role in ("enabled", "first setup"):
                username, sep, role = username.rpartition(" · ")
            if sep and role in Role.__members__:
                return readable_value(username) or text("records_user", language)
            if op.actor_id == "offline-maintenance":
                return readable_value(op.after) or text("records_user", language)
        return text("records_user", language)
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


def change_caption(action: str, value: str, language: Language) -> str:
    if action == "batch.information":
        fields = {
            "product": "batch_product",
            "specification": "batch_specification",
            "notes": "batch_notes",
        }
        lines: list[str] = []
        for line in value.splitlines():
            name, sep, recorded = line.partition(" = ")
            lines.append(
                f"{text(fields[name], language)} · {recorded}"
                if sep and name in fields
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
                else text("identity_setup", language)
            )
    if action == "model.publish":
        parts = value.rsplit(" · ", 2)
        if len(parts) == 3 and re.fullmatch(r"[0-9a-fA-F]{64}", parts[2]):
            return readable_value(parts[1])
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
