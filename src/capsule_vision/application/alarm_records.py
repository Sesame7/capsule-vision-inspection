"""Scalar snapshots for alarm history, stored by the existing History owner."""

import sqlite3
from dataclasses import astuple, dataclass


@dataclass(frozen=True, slots=True)
class AlarmEvent:
    observed: float
    signal: str
    state: str
    category: str
    zh: str
    en: str
    source: str
    station: int | None
    address: int | None
    bit: int | None
    definition: str
    batch_id: str | None
    batch_label: str | None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class AlarmRecord:
    cursor: int
    event: AlarmEvent


@dataclass(frozen=True, slots=True)
class AlarmQuery:
    since: float | None = None
    until: float | None = None
    category: str = ""
    batch: str = ""
    cursor: int | None = None
    newer: bool = False


def create(db: sqlite3.Connection) -> None:
    # Additive and lazy. Existing v2/v3 Run and Cycle schemas keep their identity.
    db.execute("""CREATE TABLE IF NOT EXISTS alarm_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, observed REAL NOT NULL,
        signal TEXT NOT NULL, state TEXT NOT NULL, category TEXT NOT NULL,
        zh TEXT NOT NULL, en TEXT NOT NULL, source TEXT NOT NULL,
        station INTEGER, address INTEGER, bit INTEGER, definition TEXT NOT NULL,
        batch_id TEXT, batch_label TEXT, detail TEXT NOT NULL)""")
    db.execute("CREATE INDEX IF NOT EXISTS alarm_time ON alarm_events(observed)")


def append(db: sqlite3.Connection, events: tuple[AlarmEvent, ...]) -> None:
    db.executemany(
        "INSERT INTO alarm_events(observed,signal,state,category,zh,en,source,station,address,bit,"
        "definition,batch_id,batch_label,detail) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (astuple(e) for e in events),
    )


def page(
    db: sqlite3.Connection, query: AlarmQuery, cutoff: float
) -> tuple[AlarmRecord, ...]:
    clauses = ["observed >= ?"]
    args: list[float | int | str] = [cutoff]
    for expression, value in (
        ("observed >= ?", query.since),
        ("observed < ?", query.until),
    ):
        if value is not None:
            clauses.append(expression)
            args.append(value)
    if query.category:
        clauses.append("category = ?")
        args.append(query.category)
    if query.batch:
        clauses.append("(batch_label = ? OR batch_id = ?)")
        args.extend((query.batch, query.batch))
    if query.cursor is not None:
        clauses.append("id > ?" if query.newer else "id < ?")
        args.append(query.cursor)
    order = "ASC" if query.newer else "DESC"
    rows = db.execute(
        "SELECT id,observed,signal,state,category,zh,en,source,station,address,bit,definition,"
        "batch_id,batch_label,detail FROM alarm_events WHERE "
        + " AND ".join(clauses)
        + f" ORDER BY id {order} LIMIT 51",
        args,
    )
    return tuple(AlarmRecord(row[0], AlarmEvent(*row[1:])) for row in rows)
