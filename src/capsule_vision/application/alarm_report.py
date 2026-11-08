"""Customer Alarm event snapshots and text, without inferring occurrences."""

import re
import sqlite3
from collections.abc import Iterator
from concurrent.futures import CancelledError
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING

from capsule_vision.configuration.alarm_definitions import CATEGORIES
from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import Language
from capsule_vision.localization.record_text import readable_value, record_reason

from .alarm_records import AlarmQuery
from .records_query import RecordsScope

if TYPE_CHECKING:
    from .report_content import Row, Table, Value


class NoAlarmRecords(ValueError):
    """The retained, authorized event scope is empty."""


@dataclass(frozen=True, slots=True)
class AlarmReportRequest:
    filters: RecordsScope = field(default_factory=RecordsScope)
    category: str = ""

    def __post_init__(self) -> None:
        if self.category and self.category not in CATEGORIES:
            raise ValueError("Unknown alarm category")

    def query(self) -> AlarmQuery:
        return AlarmQuery(
            since=self.filters.since,
            until=self.filters.until,
            category=self.category,
            batch_id=self.filters.batch,
            unbatched=self.filters.unbatched,
        )


def capture(
    db: sqlite3.Connection,
    request: AlarmReportRequest,
    path: Path,
    now: float,
    cancel: Event,
    *,
    available: bool,
) -> None:
    """Runs only on History's existing owner, under the Alarm read fence."""
    if cancel.is_set():
        raise CancelledError()
    if not available:
        raise NoAlarmRecords()
    clauses, args = request.query().conditions(now - 30 * 86400)
    db.execute("ATTACH DATABASE ? AS report", (str(path),))
    try:
        with db:
            db.execute("BEGIN")
            db.set_progress_handler(lambda: int(cancel.is_set()), 4096)
            db.execute(
                "CREATE TABLE report.alarm_records AS SELECT "
                "id,observed,state,zh,en,source,batch_id,batch_label,detail "
                "FROM main.alarm_events WHERE " + " AND ".join(clauses),
                args,
            )
            if not db.execute("SELECT 1 FROM report.alarm_records LIMIT 1").fetchone():
                raise NoAlarmRecords()
            scope = request.filters
            selected = db.execute(
                "SELECT label FROM main.batches WHERE uuid=?", (scope.batch,)
            ).fetchone()
            # A historical event retains its own label even if Batch review is unavailable.
            label = selected[0] if selected else None
            if scope.batch is not None and label is None:
                row = db.execute(
                    "SELECT batch_label FROM report.alarm_records "
                    "WHERE batch_label IS NOT NULL ORDER BY observed DESC,id DESC LIMIT 1"
                ).fetchone()
                label = row[0] if row else None
            db.execute(
                """CREATE TABLE report.meta AS SELECT 'alarm' AS scope,'' AS basis,
                ? AS observed,? AS since,? AS until,? AS filter_label,? AS unbatched,
                ? AS scoped_segment,? AS category""",
                (
                    now,
                    scope.since,
                    scope.until,
                    label,
                    scope.unbatched,
                    scope.batch is not None,
                    request.category,
                ),
            )
    except sqlite3.OperationalError:
        if cancel.is_set():
            raise CancelledError() from None
        raise
    finally:
        db.set_progress_handler(None, 0)
        db.execute("DETACH DATABASE report")


def field_groups(
    db: sqlite3.Connection, language: Language
) -> tuple[list[tuple[str, Value]], list[tuple[str, Value]]]:
    m = db.execute("SELECT * FROM meta").fetchone()
    left: list[tuple[str, Value]] = [
        (
            text("batch_input", language),
            m["filter_label"]
            or (
                "—"
                if m["scoped_segment"]
                else text(
                    "records_no_batch" if m["unbatched"] else "records_all_batches",
                    language,
                )
            ),
        ),
        (
            text("alarm_type", language),
            text(
                "alarm_" + m["category"] if m["category"] else "alarm_all_types",
                language,
            ),
        ),
    ]
    right: list[tuple[str, Value]] = [
        (text(key, language), datetime.fromtimestamp(m[name]))
        for name, key in (
            ("since", "customer_audit_from"),
            ("until", "customer_audit_before"),
        )
        if m[name] is not None
    ]
    return left, right or [
        (text("customer_alarm_period", language), text("history_all_dates", language))
    ]


def includes_batch(db: sqlite3.Connection) -> bool:
    m = db.execute("SELECT * FROM meta").fetchone()
    return (
        not (m["scoped_segment"] or m["unbatched"])
        and db.execute(
            "SELECT 1 FROM alarm_records WHERE batch_id IS NOT NULL LIMIT 1"
        ).fetchone()
        is not None
    )


def widths(db: sqlite3.Connection) -> tuple[int, ...]:
    return (24, 43, 25, 14, 26) if includes_batch(db) else (24, 47, 28, 15)


def _business_text(value: str) -> str:
    # These fields are not customer report content, even when embedded in free text.
    value = re.sub(r"[a-z][a-z0-9+.-]*://\S+", "", value, flags=re.I)
    value = re.sub(
        r"\b(?:W|D|DM|CIO|HR)\d+(?:\.\d+)?\b|\bbit\s*[:=]?\s*\d+\b",
        "",
        value,
        flags=re.I,
    )
    return readable_value(value).strip(" ·,;:—\n")


def details(value: str, name: str, language: Language) -> str:
    if value in (
        "First observation after a communication gap",
        "First valid observation after a communication gap",
    ):
        return text("records_after_gap", language)
    reason = record_reason(value, language)
    if reason == value:
        # Unknown exception class names add no customer meaning; retain the recorded stage.
        stage, separator, exception = value.partition(" · ")
        if (
            separator
            and stage in ("startup", "runtime", "shutdown")
            and re.fullmatch(r"\w+(?:Error|Exception)", exception)
        ):
            reason = text(stage + "_error", language)
    reason = _business_text(reason)
    return reason if reason != name else ""


def table(db: sqlite3.Connection, language: Language) -> Table:
    from .report_content import Table

    batch = includes_batch(db)

    def rows() -> Iterator[Row]:
        for row in db.execute(
            "SELECT * FROM alarm_records ORDER BY observed DESC,id DESC"
        ):
            name = _business_text(row["zh"] if language == "zh" else row["en"]) or text(
                "alarm_name", language
            )
            detail = details(row["detail"], name, language)
            state = row["state"]
            key = {
                "activated": "alarm_activated",
                "recovered": "alarm_recovered",
                "observed_active": "records_seen_active",
                "observed_recovered": "records_seen_clear",
            }.get(state, "unknown")
            source = row["source"]
            values: Row = (
                datetime.fromtimestamp(row["observed"]),
                name + ("\n" + detail if detail else ""),
                text(key, language),
                text("records_software", language)
                if source == "application"
                else "PLC"
                if source == "PLC" or source.startswith("fins_tcp://")
                else "—",
            )
            yield (
                (
                    *values,
                    row["batch_label"] or "—" if row["batch_id"] is not None else "—",
                )
                if batch
                else values
            )

    keys = ("audit_time", "alarm_name", "alarm_state", "comm_source")
    return Table(
        text("alarm_history", language),
        tuple(
            text(k, language)
            for k in (*keys, "batch_input")
            if batch or k != "batch_input"
        ),
        widths(db),
        rows(),
    )
