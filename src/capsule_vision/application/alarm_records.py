"""Scalar snapshots for alarm history, stored by the existing History owner."""

import json
import sqlite3
from dataclasses import dataclass, replace

from capsule_vision.configuration.alarm_definitions import (
    AlarmTranslations,
    alarm_label,
    parse_translations,
)
from capsule_vision.localization.languages import Language


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
    translations: AlarmTranslations = ()

    def label(self, language: Language) -> str:
        return alarm_label(language, self.zh, self.en, self.translations)


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
    batch_id: str | None = None
    unbatched: bool = False

    def conditions(self, cutoff: float) -> tuple[list[str], list[float | int | str]]:
        clauses = ["observed >= ?"]
        args: list[float | int | str] = [cutoff]
        for expression, value in (
            ("observed >= ?", self.since),
            ("observed < ?", self.until),
        ):
            if value is not None:
                clauses.append(expression)
                args.append(value)
        if self.category:
            clauses.append("category = ?")
            args.append(self.category)
        if self.batch:
            clauses.append("(batch_label = ? OR batch_id = ?)")
            args.extend((self.batch, self.batch))
        if self.batch_id is not None:
            clauses.append("batch_id = ?")
            args.append(self.batch_id)
        if self.unbatched:
            clauses.append("batch_id IS NULL")
        return clauses, args


_EVENT_COLUMNS = "observed,signal,state,category,zh,en,source,station,address,bit,definition,batch_id,batch_label,detail"


def create(db: sqlite3.Connection, *, legacy: bool) -> None:
    # Additive and lazy. Existing v2/v3 Run and Cycle schemas keep their identity.
    if legacy:
        # Check that this is the original alarm table before extending it.
        db.execute(f"SELECT id,{_EVENT_COLUMNS} FROM alarm_events LIMIT 0")
        db.execute("ALTER TABLE alarm_events ADD COLUMN translations TEXT")
        return
    db.execute("""CREATE TABLE IF NOT EXISTS alarm_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, observed REAL NOT NULL,
        signal TEXT NOT NULL, state TEXT NOT NULL, category TEXT NOT NULL,
        zh TEXT NOT NULL, en TEXT NOT NULL, source TEXT NOT NULL,
        station INTEGER, address INTEGER, bit INTEGER, definition TEXT NOT NULL,
        batch_id TEXT, batch_label TEXT, detail TEXT NOT NULL, translations TEXT)""")
    db.execute("CREATE INDEX IF NOT EXISTS alarm_time ON alarm_events(observed)")


def append(db: sqlite3.Connection, events: tuple[AlarmEvent, ...]) -> None:
    db.executemany(
        f"INSERT INTO alarm_events({_EVENT_COLUMNS},translations) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            (
                e.observed,
                e.signal,
                e.state,
                e.category,
                e.zh,
                e.en,
                e.source,
                e.station,
                e.address,
                e.bit,
                e.definition,
                e.batch_id,
                e.batch_label,
                e.detail,
                json.dumps(
                    dict(e.translations), ensure_ascii=False, separators=(",", ":")
                )
                if e.translations
                else None,
            )
            for e in events
        ),
    )


def page(
    db: sqlite3.Connection, query: AlarmQuery, cutoff: float, *, translated: bool
) -> tuple[AlarmRecord, ...]:
    clauses, args = query.conditions(cutoff)
    if query.cursor is not None:
        clauses.append("id > ?" if query.newer else "id < ?")
        args.append(query.cursor)
    order = "ASC" if query.newer else "DESC"
    column = "translations" if translated else "NULL"
    rows = db.execute(
        f"SELECT id,{_EVENT_COLUMNS},{column} FROM alarm_events WHERE "
        + " AND ".join(clauses)
        + f" ORDER BY id {order} LIMIT 51",
        args,
    )
    result: list[AlarmRecord] = []
    for row in rows:
        event = AlarmEvent(*row[1:-1])
        if row[-1] is not None:
            labels = parse_translations(json.loads(row[-1]), event.zh, event.en)
            event = replace(event, translations=labels)
        result.append(AlarmRecord(row[0], event))
    return tuple(result)
