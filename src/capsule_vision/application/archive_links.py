"""Published PNG paths on the existing History SQLite owner; never pixels or file I/O."""

import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class PublishedImage:
    run_id: str
    cycle_id: int
    camera_id: str
    path: Path
    observed_at: float


def create(db: sqlite3.Connection) -> None:
    # A Cycle row can arrive later. The Run must already be durable.
    db.execute(
        "CREATE TABLE IF NOT EXISTS archive_images ("
        "run_id TEXT NOT NULL REFERENCES runs(uuid), cycle_id TEXT NOT NULL, "
        "camera_id TEXT NOT NULL, path TEXT NOT NULL, observed_at REAL NOT NULL, "
        "PRIMARY KEY (run_id,cycle_id,camera_id))"
    )
    db.execute("CREATE INDEX IF NOT EXISTS archive_age ON archive_images(observed_at)")


def insert(db: sqlite3.Connection, image: PublishedImage) -> None:
    db.execute(
        "INSERT INTO archive_images(run_id,cycle_id,camera_id,path,observed_at) "
        "VALUES(?,?,?,?,?)",
        (
            image.run_id,
            str(image.cycle_id),
            image.camera_id,
            str(image.path),
            image.observed_at,
        ),
    )


def for_page(
    db: sqlite3.Connection, record_ids: tuple[int, ...]
) -> dict[tuple[int, str], Path]:
    slots = ",".join("?" for _ in record_ids)
    return {
        (record, camera): Path(path)
        for record, camera, path in db.execute(
            "SELECT c.id,i.camera_id,i.path FROM cycles c JOIN archive_images i "
            "ON i.run_id=c.run_id AND i.cycle_id=c.cycle_id "
            f"WHERE c.id IN ({slots})",
            record_ids,
        )
    }


def expire(
    db: sqlite3.Connection, cycles: list[tuple[str, str]], cutoff: float, limit: int
) -> int:
    db.executemany("DELETE FROM archive_images WHERE run_id=? AND cycle_id=?", cycles)
    # A terminal History record may never have been admitted or committed.
    return db.execute(
        "DELETE FROM archive_images WHERE rowid IN (SELECT i.rowid FROM archive_images i "
        "WHERE i.observed_at < ? AND NOT EXISTS (SELECT 1 FROM cycles c "
        "WHERE c.run_id=i.run_id AND c.cycle_id=i.cycle_id) ORDER BY i.observed_at LIMIT ?)",
        (cutoff, limit),
    ).rowcount
