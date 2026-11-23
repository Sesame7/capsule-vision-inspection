"""Cycle-owned Mask Area exclusions, joined to the reporting owner's stored slots."""

import json
import sqlite3

from capsule_vision.inspection.production import ProductionInspectionResult

CREATE_TABLE = """CREATE TABLE IF NOT EXISTS cycle_area_filtered (
    run TEXT NOT NULL, cycle TEXT NOT NULL, camera TEXT NOT NULL, channel INTEGER NOT NULL,
    evidence TEXT NOT NULL, PRIMARY KEY(run,cycle,channel),
    FOREIGN KEY(run,cycle) REFERENCES cycles(run_id,cycle_id) ON DELETE CASCADE
) WITHOUT ROWID"""


def available(db: sqlite3.Connection) -> bool:
    return (
        db.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='cycle_area_filtered'"
        ).fetchone()
        is not None
    )


def record(
    db: sqlite3.Connection, run: str, result: ProductionInspectionResult
) -> None:
    rows: list[tuple[str, str, str, int, str]] = []
    for camera in result.cameras:
        if (
            camera.frame_id is None
            or camera.image_shape is None
            or camera.collection_issue
        ):
            continue
        height, width, _ = camera.image_shape
        for channel in camera.channels:
            if not channel.area_filtered:
                continue
            findings: list[object] = []
            for item in channel.area_filtered:
                f = item.finding
                rect = f.bounding_rect
                findings.append(
                    (
                        f.label,
                        (rect.x, rect.y, rect.width, rect.height),
                        [channel.channel_id],
                        f.area,
                    )
                )
            encoded = json.dumps(
                ((width, height), findings), ensure_ascii=False, separators=(",", ":")
            )
            rows.append(
                (
                    run,
                    str(result.cycle_id),
                    camera.camera_id,
                    channel.channel_id,
                    encoded,
                )
            )
    if rows:
        db.execute(CREATE_TABLE)
        db.executemany("INSERT INTO cycle_area_filtered VALUES(?,?,?,?,?)", rows)


GROUP_CHANNELS = """(SELECT group_concat(DISTINCT f.channel)
    FROM group_members fm JOIN cycle_area_filtered f USING(run,cycle)
    WHERE fm.run=g.run AND fm.first_cycle=g.first_cycle AND r.reporting_mode='four_cycle'
    AND typeof(f.channel)='integer')"""
