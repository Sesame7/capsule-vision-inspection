"""Exact PNG associations are optional evidence on the single History owner."""

import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from threading import Event

import pytest
from tests.support.application import WAIT, production
from tests.unit.test_history import context, finish

from capsule_vision.application import archive_links
from capsule_vision.application.archive_links import PublishedImage
from capsule_vision.application.history import ProductionHistory


def test_exact_links_both_orders_unlinked_rows_and_restart(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite3"
    old, first, second = context(), context(), context()
    initial = ProductionHistory(path)
    try:
        assert initial.offer(old, production(1))
        (original,) = initial.query().result(WAIT).records
    finally:
        finish(initial)
    with closing(sqlite3.connect(path)) as db, db:
        assert (
            db.execute(
                "SELECT name FROM sqlite_master WHERE name='archive_images'"
            ).fetchone()
            is None
        )

    committed = Event()

    class ObservedHistory(ProductionHistory):
        def _write_image(
            self, connection: sqlite3.Connection, image: PublishedImage
        ) -> None:
            super()._write_image(connection, image)
            committed.set()

    a = PublishedImage(
        first.run_id, 1, "CAM1", tmp_path / "first_1.png", first.completed_at
    )
    b = replace(a, camera_id="CAM2", path=tmp_path / "first_2.png")
    c = replace(a, run_id=second.run_id, path=tmp_path / "second_1.png")
    history = ObservedHistory(path)
    try:
        batch = history.select_batch("same batch").result(WAIT)
        assert batch
        first = replace(first, batch_segment=batch.uuid, batch_label=batch.label)
        second = replace(second, batch_segment=batch.uuid, batch_label=batch.label)
        history.start_run(batch.uuid, first.run_id, first.active, None).result(WAIT)
        history.offer_image(a)
        assert committed.wait(WAIT) and history.snapshot().images_linked == 1
        assert history.query().result(WAIT).records == (
            original,
        )  # No matching Cycle yet.
        assert history.offer(first, production(1))
        history.finish_run(first.run_id, "stopped", False).result(WAIT)
        history.start_run(batch.uuid, second.run_id, second.active, None).result(WAIT)
        assert history.offer(second, production(1))
        records = {r.context.run_id: r for r in history.query().result(WAIT).records}
        assert records[first.run_id].cameras[0].image_path == a.path
        assert records[second.run_id].cameras[0].image_path is None
        history.offer_image(b)
        history.offer_image(c)
        history.finish_run(second.run_id, "stopped", False).result(WAIT)
    finally:
        finish(history)
    assert (
        history.snapshot().images_linked == 3
        and not history.snapshot().image_link_error
    )
    reopened = ProductionHistory(path)
    try:
        records = {r.context.run_id: r for r in reopened.query().result(WAIT).records}
        assert records[old.run_id] == original
        assert [row.image_path for row in records[first.run_id].cameras] == [
            a.path,
            b.path,
            None,
            None,
            None,
            None,
        ]
        assert [row.image_path for row in records[second.run_id].cameras] == [
            c.path,
            None,
            None,
            None,
            None,
            None,
        ]
    finally:
        finish(reopened)
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (4,)


def test_image_overflow_and_sql_failure_do_not_displace_cycle_or_finish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = Event(), Event()
    original = archive_links.insert
    outcomes: list[str] = []

    def slow_insert(db: sqlite3.Connection, image: PublishedImage) -> None:
        if image.camera_id == "CAM1":
            entered.set()
            assert release.wait(WAIT)
            raise sqlite3.OperationalError("image link write failed")
        outcomes.append(
            db.execute(
                "SELECT outcome FROM runs WHERE uuid=?", (image.run_id,)
            ).fetchone()[0]
        )
        original(db, image)

    monkeypatch.setattr(archive_links, "insert", slow_insert)
    path = tmp_path / "history.sqlite3"
    history = ProductionHistory(path, max_pending=1)
    run = context()
    image = PublishedImage(
        run.run_id, 1, "CAM1", tmp_path / "written.png", run.completed_at
    )
    image.path.touch()
    try:
        history.start_run(None, run.run_id, run.active, None).result(WAIT)
        history.offer_image(image)
        assert entered.wait(WAIT)
        history.offer_image(
            replace(image, camera_id="CAM2", path=tmp_path / "second.png")
        )
        history.offer_image(replace(image, camera_id="CAM3"))
        assert history.snapshot().image_links_lost == 1
        assert history.offer(run, production(1))
        ending = history.finish_run(run.run_id, "stopped", False)
        assert not ending.done() and not history.join(0)
        release.set()
        ending.result(WAIT)
    finally:
        release.set()
        finish(history)
    state = history.snapshot()
    assert outcomes == ["stopped"] and image.path.exists()
    assert state.persisted == state.images_linked == 1
    assert (
        state.image_links_lost == 2
        and "image link write failed" in state.image_link_error
    )
    assert not state.error and state.failed == state.dropped == 0
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT uncertain FROM runs").fetchone() == (0,)
        assert db.execute("SELECT camera_id FROM archive_images").fetchall() == [
            ("CAM2",)
        ]


def test_retention_removes_only_expired_cycle_and_orphan_links(tmp_path: Path) -> None:
    now = [1_800_000_000.0]
    run = context(stamp=now[0])
    path = tmp_path / "history.sqlite3"
    image = PublishedImage(run.run_id, 1, "CAM1", tmp_path / "kept.png", now[0])
    image.path.touch()
    history = ProductionHistory(path, clock=lambda: now[0])
    try:
        history.start_run(None, run.run_id, run.active, None).result(WAIT)
        assert history.offer(run, production(1))
        assert history.offer(replace(run, completed_at=now[0] + 100), production(2))
        history.offer_image(image)
        history.offer_image(
            replace(image, cycle_id=2)
        )  # Earlier observation than terminal.
        history.offer_image(
            replace(image, cycle_id=3)
        )  # No terminal record ever arrived.
        history.finish_run(run.run_id, "stopped", False).result(WAIT)
    finally:
        finish(history)
    now[0] += 30 * 86400
    reopened = ProductionHistory(path, clock=lambda: now[0])
    try:
        assert (
            len(reopened.query().result(WAIT).records) == 2
        )  # Exactly 30 days retained.
        now[0] += 1
        (latest,) = reopened.query().result(WAIT).records
        assert latest.cycle_id == 2 and latest.cameras[0].image_path == image.path
    finally:
        finish(reopened)
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT cycle_id FROM archive_images").fetchall() == [("2",)]
    assert image.path.is_file()
