"""Bounded keyset selection over measured capsule evidence on the existing History owner."""

import sqlite3
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import replace
from typing import Protocol

from . import ng_capsules
from .ng_capsules import CapsuleFilter, CapsulePage, CapsuleRow, FrozenROI
from .records_query import RecordsScope


class SubmitRead(Protocol):
    def __call__[T](self, read: Callable[[sqlite3.Connection], T], /) -> Future[T]: ...


type GroupAreas = Callable[
    [sqlite3.Connection, tuple[CapsuleRow, ...], dict[int, FrozenROI]],
    dict[int, dict[str, int]],
]

_GROUPS_PER_READ = 12


def _time_key(row: CapsuleRow) -> tuple[float, str, str, int]:
    return row.observed, row.key.run, row.key.first_cycle, -row.key.channel


class AreaScan:
    def __init__(
        self,
        scope: RecordsScope,
        mode: CapsuleFilter,
        label: str | None,
        anchor: CapsuleRow | None,
        newer: bool,
    ) -> None:
        if label is not None and anchor is not None and anchor.sort_area is None:
            raise ValueError("Area pagination requires a measured cursor")
        self.scope = scope
        self.mode = mode
        self.label = label
        self.anchor = anchor
        self.newer = newer
        self.labels: set[str] = set()
        self._best: list[CapsuleRow] = []
        self._cursor: ng_capsules.GroupPosition | None = None

    def advance(self, db: sqlite3.Connection, measures: GroupAreas) -> bool:
        layouts: dict[str, dict[int, FrozenROI]] = {}
        count = 0
        for group, members in ng_capsules.position_groups(
            db,
            self.scope,
            self._cursor,
            False,
            self.mode,
            include_cursor=False,
            limit=_GROUPS_PER_READ,
        ):
            self._cursor = group
            count += 1
            if not members:
                continue
            run = group[1]
            if run not in layouts:
                layouts[run] = ng_capsules.frozen_rois(db, run)
            areas = measures(db, members, layouts[run])
            for row in members:
                labels = areas.get(row.key.channel, {})
                self.labels.update(labels)
                area = labels.get(self.label) if self.label is not None else None
                if area is None:
                    continue
                if self.anchor is not None and self.anchor.sort_area is not None:
                    earlier = (
                        area < self.anchor.sort_area
                        if area != self.anchor.sort_area
                        else _time_key(row) > _time_key(self.anchor)
                    )
                    if row.key == self.anchor.key or earlier != self.newer:
                        continue
                self._best.append(replace(row, sort_area=area))
        # Stable sorting preserves incoming historical order within each tied area.
        # Keep one lookahead, including for backward pages nearest the cursor.
        self._best.sort(key=lambda r: r.sort_area or 0)
        self._best = self._best[-51:] if self.newer else self._best[:51]
        return count < _GROUPS_PER_READ

    def page(self) -> CapsulePage:
        rows = self._best[-50:] if self.newer else self._best[:50]
        return CapsulePage(
            tuple(rows), len(self._best) > 50, tuple(sorted(self.labels))
        )


def scheduled_page(
    submit: SubmitRead, step: Callable[[sqlite3.Connection], CapsulePage | None]
) -> Future[CapsulePage]:
    """Return one page, yielding the SQLite owner between slices. Cancellation ends admission."""
    result: Future[CapsulePage] = Future()

    def enqueue() -> None:
        if result.cancelled():
            return
        try:
            pending = submit(lambda db: None if result.cancelled() else step(db))
        except Exception as exc:
            if result.set_running_or_notify_cancel():
                result.set_exception(exc)
            return
        pending.add_done_callback(completed)

    def completed(future: Future[CapsulePage | None]) -> None:
        if result.cancelled():
            return
        try:
            page = future.result()  # Invoked only by the completed owner Future.
        except Exception as exc:
            if result.set_running_or_notify_cancel():
                result.set_exception(exc)
            return
        if page is None:
            enqueue()
        elif result.set_running_or_notify_cancel():
            result.set_result(page)

    enqueue()
    return result
