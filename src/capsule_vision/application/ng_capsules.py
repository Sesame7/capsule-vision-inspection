"""Read-only capsule identities and geometry from attributed four-exposure History."""

import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from capsule_vision.inference import BoundingRect
from capsule_vision.inspection.production.layout import valid_rect

from . import filtered_evidence
from .records_query import RecordsScope

if TYPE_CHECKING:
    from .history import HistoryChannel, HistoryFinding


class CapsuleFilter(StrEnum):
    NG = "ng"
    FILTERED = "filtered"
    ALL = "all"


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
    filtered: bool = False  # A final-OK channel with threshold-excluded evidence.
    sort_area: int | None = None


@dataclass(frozen=True, slots=True)
class CapsulePage:
    rows: tuple[CapsuleRow, ...] = ()
    more: bool = False
    area_labels: tuple[str, ...] | None = None


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


def _positions(
    encoded: str, filtered: str | None, mode: CapsuleFilter
) -> dict[int, bool]:
    # A damaged group projection cannot establish the final outcome of a position.
    try:
        pairs: list[tuple[int, str]] = json.loads(encoded)
        if len(pairs) not in (30, 36):
            return {}
        finals = dict(pairs)
        if (
            set(finals) != set(range(1, len(pairs) + 1))
            or any(type(channel) is not int for channel in finals)
            or not set(finals.values()) <= {"OK", "NG", "EMPTY", "UNKNOWN"}
        ):
            return {}
        excluded: set[int] = set(map(int, filtered.split(","))) if filtered else set()
        return {
            channel: state == "OK"
            for channel, state in sorted(finals.items())
            if (state == "NG" and mode != CapsuleFilter.FILTERED)
            or (state == "OK" and channel in excluded and mode != CapsuleFilter.NG)
        }
    except TypeError, ValueError:
        return {}


type GroupPosition = tuple[float, str, str]


def position_groups(
    db: sqlite3.Connection,
    scope: RecordsScope,
    cursor: GroupPosition | None,
    newer: bool,
    mode: CapsuleFilter,
    *,
    include_cursor: bool,
    limit: int | None = None,
) -> Iterator[tuple[GroupPosition, tuple[CapsuleRow, ...]]]:
    """Stream stored groups in historical order with their scoped channel identities."""
    where = _GROUPS
    has_filtered = mode != CapsuleFilter.NG and filtered_evidence.available(db)
    if mode == CapsuleFilter.FILTERED and not has_filtered:
        return
    excluded = filtered_evidence.GROUP_CHANNELS if has_filtered else "NULL"
    if mode == CapsuleFilter.FILTERED and limit is None:
        where += " AND " + excluded + " IS NOT NULL"
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
        op = (">" if newer else "<") + ("=" if include_cursor else "")
        where += f" AND (g.observed,g.run,g.first_cycle) {op} (?,?,?)"
        args.extend(cursor)
    order = "ASC" if newer else "DESC"
    sql = (
        f"SELECT g.observed,g.run,g.first_cycle,g.final_channels,b.label,r.batch,{excluded} "
        + where
        + f" ORDER BY g.observed {order},g.run {order},g.first_cycle {order}"
    )
    if limit is not None:
        sql += " LIMIT ?"
        args.append(limit)
    for observed, run, first, encoded, batch_label, batch_uuid, filtered in db.execute(
        sql, args
    ):
        channels = _positions(encoded, filtered, mode)
        yield (
            (observed, run, first),
            tuple(
                CapsuleRow(
                    observed,
                    CapsuleKey(run, first, channel),
                    batch_label,
                    batch_uuid=batch_uuid,
                    filtered=channels[channel],
                )
                for channel in (reversed(channels) if newer else channels)
            ),
        )


def page(
    db: sqlite3.Connection,
    scope: RecordsScope,
    cursor: CapsuleRow | None,
    newer: bool,
    mode: CapsuleFilter = CapsuleFilter.NG,
) -> CapsulePage:
    """Page qualified group positions before History attaches their recorded labels."""
    rows: list[CapsuleRow] = []
    anchor = (
        (cursor.observed, cursor.key.run, cursor.key.first_cycle) if cursor else None
    )
    for group, positions in position_groups(
        db, scope, anchor, newer, mode, include_cursor=True
    ):
        for row in positions:
            if (
                cursor
                and group == anchor
                and (
                    row.key.channel >= cursor.key.channel
                    if newer
                    else row.key.channel <= cursor.key.channel
                )
            ):
                continue
            if len(rows) == 50:
                return CapsulePage(
                    tuple(reversed(rows)) if newer else tuple(rows), True
                )
            rows.append(row)
    return CapsulePage(tuple(reversed(rows)) if newer else tuple(rows))


def selected_kind(db: sqlite3.Connection, key: CapsuleKey) -> CapsuleFilter | None:
    excluded = (
        filtered_evidence.GROUP_CHANNELS if filtered_evidence.available(db) else "NULL"
    )
    row = db.execute(
        f"SELECT g.final_channels,{excluded} "
        + _GROUPS
        + " AND g.run=? AND g.first_cycle=?",
        (key.run, key.first_cycle),
    ).fetchone()
    if row is not None and type(key.channel) is int:
        positions = _positions(row[0], row[1], CapsuleFilter.ALL)
        if key.channel in positions:
            return (
                CapsuleFilter.FILTERED if positions[key.channel] else CapsuleFilter.NG
            )
    return None


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


def frozen_rois(db: sqlite3.Connection, run: str) -> dict[int, FrozenROI]:
    """Read only unambiguous channel geometry from this Run's frozen layout."""
    if not _has_configurations(db):
        return {}
    rows = db.execute(
        _FROZEN_SLOTS
        + """SELECT slot ->> '$.channel_id', camera ->> '$.camera_id', slot ->> '$.slot',
        slot ->> '$.rect.x', slot ->> '$.rect.y', slot ->> '$.rect.width', slot ->> '$.rect.height'
        FROM slots WHERE json_type(slot,'$.channel_id')='integer'""",
        {"run": run},
    )
    result: dict[int, FrozenROI] = {}
    seen: set[int] = set()
    for channel, camera, slot, x, y, width, height in rows:
        if channel in seen:
            result.pop(channel, None)
            continue
        seen.add(channel)
        if (
            not isinstance(camera, str)
            or not camera.strip()
            or type(slot) is not int
            or not 0 <= slot < 6
        ):
            continue
        rect = BoundingRect(x, y, width, height)
        try:
            valid_rect(rect)
        except ValueError:
            continue
        if rect.height >= 2:
            result[channel] = FrozenROI(camera, slot, rect)
    return result
