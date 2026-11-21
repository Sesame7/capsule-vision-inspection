"""Stored positions, membership and narrowly projected historical evidence."""

import json
import sqlite3
import zlib
from contextlib import closing
from hashlib import sha256
from pathlib import Path

import pytest
from tests.support.application import WAIT
from tests.support.ng_capsules import MEMBERS, RECT, RUN, add_group, sample

from capsule_vision.application.history_review import open_history_review
from capsule_vision.application.ng_capsules import CapsuleKey, FrozenROI
from capsule_vision.application.records_query import RecordsScope


@pytest.mark.parametrize("cameras", (5, 6))
def test_four_persisted_slots_and_only_selected_evidence(
    tmp_path: Path, cameras: int
) -> None:
    saved = sample(tmp_path, cameras, recipe_name="Amber capsule at Start")
    db_path = tmp_path / "history.sqlite3"
    before = sha256(db_path.read_bytes()).digest()
    with open_history_review(tmp_path) as reader:
        page = reader.ng_capsule_page(
            RecordsScope(saved.observed, saved.observed + 1, saved.batch)
        ).result(WAIT)
        assert [r.key.channel for r in page.rows] == [saved.channel, saved.channel + 1]
        assert not page.more
        first = page.rows[0]
        assert first.observed == saved.observed
        assert all(row.batch_label == "Training lot" for row in page.rows)
        assert all(
            row.recipe_name == "Amber capsule at Start" and row.camera == saved.camera
            for row in page.rows
        )
        assert first.key == CapsuleKey(RUN, str(MEMBERS[0]), saved.channel)
        detail = reader.ng_capsule_detail(first.key).result(WAIT)
        assert detail and detail.roi == FrozenROI(saved.camera, 4, RECT)
        assert [x.cycle for x in detail.exposures] == list(map(str, MEMBERS))
        assert [x.channel.business for x in detail.exposures if x.channel] == [
            "NG",
            "OK",
            "EMPTY",
            "UNKNOWN",
        ]
        assert [x.path is not None for x in detail.exposures] == [
            True,
            True,
            False,
            True,
        ]
        spot, ok, empty, unknown = detail.exposures
        assert [f.label for f in spot.findings] == ["Spot", "Spot", "Edge"]
        assert all(f.channels == (saved.channel,) for f in spot.findings)
        assert not ok.findings and not empty.findings and not unknown.findings
        assert (
            unknown.channel
            and unknown.channel.technical == "ERROR"
            and unknown.channel.reasons == ("execution_error",)
        )
        assert (
            reader.ng_capsule_detail(CapsuleKey(RUN, first.key.first_cycle, 1)).result(
                WAIT
            )
            is None
        )
        assert (
            not reader.ng_capsule_page(RecordsScope(until=saved.observed))
            .result(WAIT)
            .rows
        )
        assert (
            not reader.ng_capsule_page(RecordsScope(unbatched=True)).result(WAIT).rows
        )
    assert sha256(db_path.read_bytes()).digest() == before


def test_capsule_pages_cross_group_and_run_boundaries_and_filter_invalid_groups(
    tmp_path: Path,
) -> None:
    saved = sample(tmp_path)
    count = 30
    now = saved.observed
    with closing(sqlite3.connect(tmp_path / "history.sqlite3")) as db, db:
        for run, mode in (
            ("second", "four_cycle"),
            ("legacy", "disabled"),
            ("single", "single_cycle"),
            ("per", "per_cycle"),
        ):
            db.execute(
                """INSERT INTO runs(uuid,batch,started,terminated,outcome,recipe,reporting_mode)
                VALUES(?,NULL,?,?,'stopped','{}',?)""",
                (run, now - 1, now + 10, mode),
            )
        for member in (201, 301):
            add_group(
                db,
                RUN,
                (member, member + 11, member + 5, member + 20),
                now + 2,
                tuple(range(1, count + 1)),
                count,
            )
        add_group(db, "second", MEMBERS, now + 2, (1, 9, 27), count)
        add_group(db, "legacy", MEMBERS, now - 2, (11,), count)
        add_group(db, "single", (101,), now + 9, (3,), count)
        add_group(db, "single", (201, 202, 203, 204), now + 9, (3,), count)
        add_group(db, "per", MEMBERS, now + 9, (3,), count)
        for member, length in (
            (401, 3),
            (501, 5),
            (601, 4),
            (701, 4),
            (801, 4),
            (901, 4),
        ):
            add_group(
                db, RUN, tuple(range(member, member + length)), now + 8, (1,), count
            )
        db.execute(
            "UPDATE group_members SET slot=3 WHERE run=? AND cycle='604'", (RUN,)
        )
        db.execute(
            "UPDATE group_members SET slot=5-slot WHERE run=? AND first_cycle='701'",
            (RUN,),
        )
        db.execute(
            "UPDATE group_members SET slot=2.5 WHERE run=? AND cycle='802'", (RUN,)
        )
        db.execute(
            "UPDATE plc_groups SET final_channels=? WHERE run=? AND first_cycle='901'",
            (json.dumps([[1, "NG"]] * 30), RUN),
        )
    with open_history_review(tmp_path) as reader:
        scope = RecordsScope()
        first = reader.ng_capsule_page(scope).result(WAIT)
        second = reader.ng_capsule_page(scope, first.rows[-1]).result(WAIT)
        assert (
            len(first.rows) == 50
            and first.more
            and len(second.rows) == 16
            and not second.more
        )
        assert (
            reader.ng_capsule_page(scope, second.rows[0], True).result(WAIT).rows
            == first.rows
        )
        rows = (*first.rows, *second.rows)
        assert len({r.key for r in rows}) == len(rows)
        assert [(r.key.run, r.key.first_cycle, r.key.channel) for r in rows] == [
            *((RUN, group, ch) for group in ("301", "201") for ch in range(1, 31)),
            *(("second", "101", ch) for ch in (1, 9, 27)),
            (RUN, "101", 29),
            (RUN, "101", 30),
            ("legacy", "101", 11),
        ]
        unbatched = reader.ng_capsule_page(
            RecordsScope(now + 2, now + 3, unbatched=True)
        ).result(WAIT)
        assert [r.key.run for r in unbatched.rows] == ["second"] * 3
        assert all(
            r.batch_label is None and r.recipe_name is None and r.camera is None
            for r in unbatched.rows
        )
        assert (
            not reader.ng_capsule_page(RecordsScope(batch="Training lot"))
            .result(WAIT)
            .rows
        )
        detail = reader.ng_capsule_detail(unbatched.rows[0].key).result(WAIT)
        assert detail and detail.roi is None and len(detail.exposures) == 4
        assert all(
            x.channel is None and x.path is None and not x.findings
            for x in detail.exposures
        )


@pytest.mark.parametrize("conflict", (False, True))
def test_labels_from_retained_members_stay_with_the_group(
    tmp_path: Path, conflict: bool
) -> None:
    saved = sample(tmp_path)
    with closing(sqlite3.connect(tmp_path / "history.sqlite3")) as db, db:
        db.execute("DROP TABLE run_configuration_resources")
        db.execute("DROP TABLE run_configurations")
        # Slot 2 is Cycle 131. The adjacent Cycle 102 belongs to a different group.
        add_group(
            db, RUN, (102, 103, 104, 105), saved.observed + 1, (saved.channel,), 30
        )
        for cycle, name, camera in (
            (131, "Retained member name", "Archive-Camera-B"),
            (
                190,
                "Different member name" if conflict else None,
                "Archive-Camera-A" if conflict else "Archive-Camera-B",
            ),
        ):
            channel: list[object] = [
                camera,
                4,
                saved.channel,
                "NG",
                "SUCCESS",
                "NG",
                [],
            ]
            db.execute(
                "UPDATE cycles SET detail=? WHERE run_id=? AND cycle_id=?",
                (
                    zlib.compress(json.dumps([[channel], [], name]).encode()),
                    RUN,
                    str(cycle),
                ),
            )
        db.execute(
            "UPDATE cycles SET detail=x'00' WHERE run_id=? AND cycle_id='101'", (RUN,)
        )
        db.execute("DELETE FROM cycles WHERE run_id=? AND cycle_id='117'", (RUN,))
    with open_history_review(tmp_path) as reader:
        adjacent, retained, other_channel = (
            reader.ng_capsule_page(RecordsScope()).result(WAIT).rows
        )
    assert adjacent.key.first_cycle == "102" and (
        adjacent.recipe_name,
        adjacent.camera,
    ) == (None, None)
    assert retained.key.channel == saved.channel and other_channel.camera is None
    assert (
        retained.recipe_name
        == other_channel.recipe_name
        == (None if conflict else "Retained member name")
    )
    assert retained.camera == (None if conflict else "Archive-Camera-B")


def test_missing_cycle_layout_and_archive_table_leave_independent_statuses(
    tmp_path: Path,
) -> None:
    saved = sample(tmp_path)
    key = CapsuleKey(RUN, str(MEMBERS[0]), saved.channel)
    with closing(sqlite3.connect(tmp_path / "history.sqlite3")) as db, db:
        db.execute(
            "DELETE FROM cycles WHERE run_id=? AND cycle_id=?", (RUN, str(MEMBERS[1]))
        )
        db.execute("DROP TABLE archive_images")
        db.execute("DELETE FROM run_configuration_resources")
        db.execute("DELETE FROM run_configurations")
    with open_history_review(tmp_path) as reader:
        detail = reader.ng_capsule_detail(key).result(WAIT)
        assert detail and detail.roi is None
        assert [
            x.channel.business if x.channel else None for x in detail.exposures
        ] == ["NG", None, "EMPTY", "UNKNOWN"]
        assert all(x.path is None for x in detail.exposures)


@pytest.mark.parametrize("duplicate_channel", (False, True))
def test_unreadable_or_ambiguous_frozen_roi_preserves_statuses(
    tmp_path: Path, duplicate_channel: bool
) -> None:
    saved = sample(tmp_path)
    with closing(sqlite3.connect(tmp_path / "history.sqlite3")) as db, db:
        frozen = b"invalid JSON"
        if duplicate_channel:
            (encoded,) = db.execute(
                """SELECT json_set(CAST(effective AS TEXT),
                '$.inspection.layout.cameras[0].slots[0].channel_id',?)
                FROM run_configurations WHERE run=?""",
                (saved.channel, RUN),
            ).fetchone()
            frozen = encoded.encode()
        db.execute("DELETE FROM run_configuration_resources WHERE run=?", (RUN,))
        db.execute("DELETE FROM run_configurations WHERE run=?", (RUN,))
        db.execute(
            "INSERT INTO run_configurations(run,effective,complete,revision_lookup) VALUES(?,?,0,'unavailable')",
            (RUN, frozen),
        )
    with open_history_review(tmp_path) as reader:
        detail = reader.ng_capsule_detail(CapsuleKey(RUN, "101", saved.channel)).result(
            WAIT
        )
        assert detail and detail.roi is None
        assert [ch.channel.business for ch in detail.exposures if ch.channel] == [
            "NG",
            "OK",
            "EMPTY",
            "UNKNOWN",
        ]
        assert all(ch.path is None for ch in detail.exposures)


def test_conservative_final_ng_and_png_link_without_cycle_detail(
    tmp_path: Path,
) -> None:
    saved = sample(tmp_path)
    key = CapsuleKey(RUN, "101", saved.channel)
    with closing(sqlite3.connect(tmp_path / "history.sqlite3")) as db, db:
        channels: list[list[object]] = [
            [saved.camera, 4, saved.channel, "OK", "SUCCESS", "OK", []],
            [
                saved.camera,
                5,
                saved.channel + 1,
                "NG",
                "SUCCESS",
                "NG",
                ["defect_region"],
            ],
        ]
        cameras: list[list[object]] = [
            [
                saved.camera,
                "SUCCESS",
                "NG",
                [],
                ["Unassigned"],
                [[1520, 196], [["Neighbour", [1270, 36, 8, 6], [saved.channel + 1]]]],
            ]
        ]
        db.execute(
            "UPDATE cycles SET detail=? WHERE run_id=? AND cycle_id='101'",
            (zlib.compress(json.dumps([channels, cameras, None]).encode()), RUN),
        )
        db.execute("DELETE FROM cycles WHERE run_id=? AND cycle_id='131'", (RUN,))
    with open_history_review(tmp_path) as reader:
        assert key in {
            r.key for r in reader.ng_capsule_page(RecordsScope()).result(WAIT).rows
        }
        detail = reader.ng_capsule_detail(key).result(WAIT)
        assert detail and [
            x.channel.business if x.channel else None for x in detail.exposures
        ] == ["OK", None, "EMPTY", "UNKNOWN"]
        assert all(not x.findings for x in detail.exposures)
        assert detail.exposures[0].channel and not detail.exposures[0].channel.reasons
        assert (
            detail.exposures[1].path
            == tmp_path / "2026-01-15" / saved.camera / "shot-131.png"
        )
