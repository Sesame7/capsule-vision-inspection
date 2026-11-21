"""Read-only capsule identities and geometry from attributed four-exposure History."""

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from capsule_vision.inference import BoundingRect
from capsule_vision.inspection.production.layout import valid_rect

from .records_query import RecordsScope

if TYPE_CHECKING:
    from .history import HistoryChannel, HistoryFinding


@dataclass(frozen=True, slots=True)
class CapsuleKey:
    run: str
    first_cycle: str
    channel: int


@dataclass(frozen=True, slots=True)
class CapsuleRow:
    observed: float
    key: CapsuleKey
    batch_label: str | None = None
    recipe_name: str | None = None
    camera: str | None = None
    batch_uuid: str | None = None


@dataclass(frozen=True, slots=True)
class CapsulePage:
    rows: tuple[CapsuleRow, ...] = ()
    more: bool = False


@dataclass(frozen=True, slots=True)
class FrozenROI:
    camera: str
    slot: int
    rect: BoundingRect


@dataclass(frozen=True, slots=True)
class CapsuleExposure:
    cycle: str
    channel: HistoryChannel | None
    path: Path | None
    size: tuple[int, int] | None
    findings: tuple[HistoryFinding, ...]


@dataclass(frozen=True, slots=True)
class CapsuleDetail:
    key: CapsuleKey
    roi: FrozenROI | None
    exposures: tuple[CapsuleExposure, ...]


# Older trusted group facts can belong to a Run admitted without a reporting selection.
_GROUPS = """FROM plc_groups g JOIN runs r ON r.uuid=g.run
    LEFT JOIN batches b ON b.uuid=r.batch
    WHERE r.reporting_mode IN ('four_cycle','disabled') AND EXISTS (
        SELECT 1 FROM group_members m WHERE (m.run,m.first_cycle)=(g.run,g.first_cycle)
        GROUP BY m.run,m.first_cycle HAVING count(*)=4 AND count(DISTINCT m.cycle)=4
        AND count(DISTINCT m.slot)=4 AND sum(m.slot IN (1,2,3,4))=4
        AND sum(m.slot=1 AND m.cycle=g.first_cycle)=1)"""


def _rejected(encoded: str) -> tuple[int, ...]:
    # A damaged group projection cannot establish the final outcome of a position.
    try:
        pairs: list[tuple[int, str]] = json.loads(encoded)
        if len(pairs) not in (30, 36):
            return ()
        finals = dict(pairs)
        if (
            set(finals) != set(range(1, len(pairs) + 1))
            or any(type(channel) is not int for channel in finals)
            or not set(finals.values()) <= {"OK", "NG", "EMPTY", "UNKNOWN"}
        ):
            return ()
        return tuple(
            sorted(channel for channel, state in finals.items() if state == "NG")
        )
    except TypeError, ValueError:
        return ()


def page(
    db: sqlite3.Connection, scope: RecordsScope, cursor: CapsuleRow | None, newer: bool
) -> CapsulePage:
    """Page stored NG identities before History attaches labels to the visible rows."""
    where = _GROUPS
    args: list[object] = []
    for field, op, value in (
        ("g.observed", ">=", scope.since),
        ("g.observed", "<", scope.until),
        ("r.batch", "=", scope.batch),
    ):
        if value is not None:
            where += f" AND {field}{op}?"
            args.append(value)
    if scope.unbatched:
        where += " AND r.batch IS NULL"
    if cursor is not None:
        op = ">=" if newer else "<="
        where += f" AND (g.observed,g.run,g.first_cycle) {op} (?,?,?)"
        args.extend((cursor.observed, cursor.key.run, cursor.key.first_cycle))
    order = "ASC" if newer else "DESC"
    sql = (
        "SELECT g.observed,g.run,g.first_cycle,g.final_channels,b.label,r.batch "
        + where
    )
    rows: list[CapsuleRow] = []
    # Stream groups without decoding Cycle blobs or materializing the entire date range.
    # The cursor can fall in the middle of a group with several rejected channels.
    for observed, run, first, encoded, batch_label, batch_uuid in db.execute(
        sql + f" ORDER BY g.observed {order},g.run {order},g.first_cycle {order}", args
    ):
        channels = _rejected(encoded)
        for channel in reversed(channels) if newer else channels:
            if (
                cursor
                and (observed, run, first)
                == (cursor.observed, cursor.key.run, cursor.key.first_cycle)
                and (
                    channel >= cursor.key.channel
                    if newer
                    else channel <= cursor.key.channel
                )
            ):
                continue
            if len(rows) == 50:
                return CapsulePage(
                    tuple(reversed(rows)) if newer else tuple(rows), True
                )
            rows.append(
                CapsuleRow(
                    observed,
                    CapsuleKey(run, first, channel),
                    batch_label,
                    batch_uuid=batch_uuid,
                )
            )
    return CapsulePage(tuple(reversed(rows)) if newer else tuple(rows))


def eligible(db: sqlite3.Connection, key: CapsuleKey) -> bool:
    row = db.execute(
        "SELECT g.final_channels " + _GROUPS + " AND g.run=? AND g.first_cycle=?",
        (key.run, key.first_cycle),
    ).fetchone()
    return (
        row is not None
        and type(key.channel) is int
        and key.channel in _rejected(row[0])
    )


_FROZEN_SLOTS = """
WITH frozen(doc) AS (
    SELECT CAST(effective AS TEXT) FROM run_configurations
    WHERE run=:run AND json_valid(CAST(effective AS TEXT))
), cameras AS (
    SELECT doc, CASE WHEN c.type='object' THEN c.value END AS camera
    FROM frozen LEFT JOIN json_each(doc,'$.inspection.layout.cameras') c
), slots AS (
    SELECT doc, camera, CASE WHEN s.type='object' THEN s.value END AS slot
    FROM cameras LEFT JOIN json_each(camera,'$.slots') s
) """


def _has_configurations(db: sqlite3.Connection) -> bool:
    return (
        db.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='run_configurations'"
        ).fetchone()
        is not None
    )


type GalleryLabels = tuple[str | None, dict[int, str]]


def unique_cameras(cameras: dict[int, set[str]]) -> dict[int, str]:
    return {ch: next(iter(ids)) for ch, ids in cameras.items() if len(ids) == 1}


def run_labels(db: sqlite3.Connection, run: str) -> GalleryLabels:
    """Read only the resolved name and channel/camera associations frozen at Start."""
    if not _has_configurations(db):
        return None, {}
    name: str | None = None
    cameras: dict[int, set[str]] = {}
    for recorded, channel, camera in db.execute(
        _FROZEN_SLOTS
        + """
        SELECT CASE WHEN json_type(doc,'$.inspection.display_name')='text'
                    THEN doc ->> '$.inspection.display_name' END,
               CASE WHEN json_type(slot,'$.channel_id')='integer' THEN slot ->> '$.channel_id' END,
               CASE WHEN json_type(camera,'$.camera_id')='text' THEN camera ->> '$.camera_id' END
        FROM slots""",
        {"run": run},
    ):
        if isinstance(recorded, str) and recorded.strip():
            name = recorded
        if type(channel) is int and isinstance(camera, str) and camera.strip():
            cameras.setdefault(channel, set()).add(camera)
    return name, unique_cameras(cameras)


def frozen_roi(db: sqlite3.Connection, key: CapsuleKey) -> FrozenROI | None:
    # Older/incomplete copies can still supply group outcomes and per-Cycle statuses.
    if not _has_configurations(db):
        return None
    rows = db.execute(
        _FROZEN_SLOTS
        + """SELECT camera ->> '$.camera_id', slot ->> '$.slot',
        slot ->> '$.rect.x', slot ->> '$.rect.y', slot ->> '$.rect.width', slot ->> '$.rect.height'
        FROM slots WHERE slot ->> '$.channel_id'=:channel LIMIT 2""",
        {"run": key.run, "channel": key.channel},
    ).fetchall()
    if len(rows) != 1:
        return None
    camera, slot, x, y, width, height = rows[0]
    if (
        not isinstance(camera, str)
        or not camera.strip()
        or type(slot) is not int
        or not 0 <= slot < 6
    ):
        return None
    rect = BoundingRect(x, y, width, height)
    try:
        valid_rect(rect)
    except ValueError:
        return None
    return FrozenROI(camera, slot, rect) if rect.height >= 2 else None
