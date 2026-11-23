"""Stored positions, membership and narrowly projected historical evidence."""

import json
import sqlite3
import zlib
from contextlib import closing
from hashlib import sha256
from pathlib import Path

import pytest
from tests.support.application import WAIT
from tests.support.ng_capsules import (
    MEMBERS,
    RECT,
    RUN,
    add_group,
    add_measured_groups,
    sample,
)

from capsule_vision.application.history_review import open_history_review
from capsule_vision.application.ng_capsules import CapsuleFilter, CapsuleKey, FrozenROI
from capsule_vision.application.records_query import RecordsScope


@pytest.mark.parametrize("cameras", (5, 6))
def test_area_sort_measures_all_members_and_both_views_in_the_selected_scope(
    tmp_path: Path, cameras: int
) -> None:
    saved = sample(tmp_path, cameras, with_filtered=True, edge_area=41)
    ch = saved.channel
    database = tmp_path / "history.sqlite3"
    with closing(sqlite3.connect(database)) as db, db:
        for i, cy in enumerate(MEMBERS):
            (blob,) = db.execute(
                "SELECT detail FROM cycles WHERE run_id=? AND cycle_id=?",
                (RUN, str(cy)),
            ).fetchone()
            payload = json.loads(zlib.decompress(blob))
            for item in payload[0]:
                if item[2] in (ch, ch + 1):
                    item[3:6] = ["NG", "SUCCESS", "NG"]
            camera = payload[1][0]
            camera[1:3] = ["SUCCESS", "NG"]
            rows = [
                ["Dent", [1144, 41, 19, 17], [ch], (11, 23, 18, 13)[i]],
                ["Dent", [1144, 124, 19, 17], [ch], (8, 7, 16, 42)[i]],
                [
                    "Dent",
                    [1100, 20, 170, 120],
                    [ch],
                ],  # Larger rectangle without a measurement.
                ["Legacy", [1140, 30, 64, 40], [ch]],
                ["Time", [1130, 36, 9, 8], [ch, ch + 1], 0],
            ]
            if i == 0:
                rows.append(["Dent", [1270, 41, 19, 17], [ch + 1], 19])
            camera[5:] = [[[1520, 196], rows]]
            db.execute(
                "UPDATE cycles SET detail=? WHERE run_id=? AND cycle_id=?",
                (zlib.compress(json.dumps(payload).encode()), RUN, str(cy)),
            )
        db.execute("DELETE FROM archive_images WHERE cycle_id=?", (str(MEMBERS[3]),))
        db.execute(
            "UPDATE cycle_area_filtered SET evidence=? WHERE cycle=? AND channel=?",
            (
                json.dumps(
                    [
                        [1520, 196],
                        [
                            ["Hair", [775, 120, 12, 6], [ch - 1], 53],
                            ["Dent", [788, 32, 12, 6], [ch - 1], 33],
                        ],
                    ]
                ),
                str(MEMBERS[2]),
                ch - 1,
            ),
        )
    digest = sha256(database.read_bytes()).digest()
    scope = RecordsScope(saved.observed, saved.observed + 1, saved.batch)
    with open_history_review(tmp_path) as reader:

        def areas(
            label: str, mode: CapsuleFilter = CapsuleFilter.NG
        ) -> list[tuple[int, int | None]]:
            return [
                (r.key.channel, r.sort_area)
                for r in reader.ng_capsule_page(scope, mode=mode, area_label=label)
                .result(WAIT)
                .rows
            ]

        assert reader.ng_capsule_page(scope).result(WAIT).area_labels is None
        timed = reader.ng_capsule_page(scope, include_area_labels=True).result(WAIT)
        assert [r.key.channel for r in timed.rows] == [
            ch,
            ch + 1,
        ] and timed.area_labels == ("Dent", "Time")
        assert areas("Dent") == [(ch + 1, 19), (ch, 42)]
        assert areas("Legacy") == areas("dent") == areas("Hair") == []
        assert areas("Time") == [(ch, 0), (ch + 1, 0)]
        assert areas("Dent", CapsuleFilter.FILTERED) == [(ch - 1, 33)]
        assert areas("Dent", CapsuleFilter.ALL) == [
            (ch + 1, 19),
            (ch - 1, 33),
            (ch, 42),
        ]
        assert areas("Time", CapsuleFilter.FILTERED) == []
        assert (
            areas("Hair", CapsuleFilter.FILTERED)
            == areas("Hair", CapsuleFilter.ALL)
            == [(ch - 1, 53)]
        )
        all_rows = reader.ng_capsule_page(
            scope, mode=CapsuleFilter.ALL, area_label="Hair"
        ).result(WAIT)
        assert (
            all_rows.area_labels == ("Dent", "Hair", "Time")
            and all_rows.rows[0].filtered
        )
        for other in (
            RecordsScope(until=saved.observed),
            RecordsScope(batch="Training lot"),
            RecordsScope(unbatched=True),
        ):
            empty = reader.ng_capsule_page(other, area_label="Dent").result(WAIT)
            assert not empty.rows and empty.area_labels == ()
    assert sha256(database.read_bytes()).digest() == digest


def test_area_keysets_include_the_complete_scope_and_reverse_at_a_tie(
    tmp_path: Path,
) -> None:
    saved = sample(tmp_path)
    expected = add_measured_groups(saved)
    with open_history_review(tmp_path) as reader:
        scope = RecordsScope(batch=saved.batch)
        timed = reader.ng_capsule_page(scope).result(WAIT)
        first = reader.ng_capsule_page(scope, area_label="Dent").result(WAIT)
        second = reader.ng_capsule_page(
            scope, first.rows[-1], area_label="Dent"
        ).result(WAIT)
        assert [
            (r.key.first_cycle, r.sort_area) for r in (*first.rows, *second.rows)
        ] == expected
        assert (
            first.more
            and not second.more
            and (len(first.rows), len(second.rows)) == (50, 13)
        )
        assert first.rows[0].key not in {r.key for r in timed.rows}
        assert first.rows[-1].sort_area == second.rows[0].sort_area == 27
        previous = reader.ng_capsule_page(
            scope, second.rows[0], True, area_label="Dent"
        ).result(WAIT)
        assert previous.rows == first.rows and not previous.more
        assert reader.ng_capsule_page(scope).result(WAIT) == timed


def test_equal_area_and_group_time_keep_repeated_cycle_ids_in_their_own_run(
    tmp_path: Path,
) -> None:
    saved = sample(tmp_path, edge_area=41)
    other = "unbatched-run"
    with closing(sqlite3.connect(tmp_path / "history.sqlite3")) as db, db:
        db.execute(
            """INSERT INTO runs(uuid,started,outcome,recipe,reporting_mode)
            VALUES(?,?,'stopped','{}','four_cycle')""",
            (other, saved.observed),
        )
        add_group(db, other, MEMBERS, saved.observed, (saved.channel,), 30)
        db.execute(
            """INSERT INTO cycles(completed_at,batch_segment,batch_label,run_id,cycle_id,recipe_id,
                       model_id,model_sha,status,technical,judgment,empty_count,unknown_count,detail)
            SELECT completed_at,?,NULL,?,cycle_id,recipe_id,model_id,model_sha,status,technical,
                   judgment,empty_count,unknown_count,detail FROM cycles WHERE run_id=? AND cycle_id='101'""",
            (other, other, RUN),
        )
    with open_history_review(tmp_path) as reader:

        def keys(scope: RecordsScope) -> list[CapsuleKey]:
            return [
                r.key
                for r in reader.ng_capsule_page(scope, area_label="Edge")
                .result(WAIT)
                .rows
            ]

        batched = [
            CapsuleKey(RUN, "101", c) for c in (saved.channel, saved.channel + 1)
        ]
        unbatched = [CapsuleKey(other, "101", saved.channel)]
        assert keys(RecordsScope()) == [*unbatched, *batched]
        assert keys(RecordsScope(batch=saved.batch)) == batched
        assert keys(RecordsScope(unbatched=True)) == unbatched


@pytest.mark.parametrize("frozen", (True, False))
def test_cycles_and_ng_reduce_legacy_rows_on_read_with_shared_associations(
    tmp_path: Path, frozen: bool
) -> None:
    saved = sample(tmp_path)
    ch, camera = saved.channel, saved.camera
    path = tmp_path / "history.sqlite3"
    with closing(sqlite3.connect(path)) as db, db:
        if not frozen:
            db.execute("DELETE FROM run_configuration_resources")
            db.execute("DELETE FROM run_configurations")
        sql = "SELECT detail FROM cycles WHERE cycle_id='101'"
        payload = json.loads(zlib.decompress(db.execute(sql).fetchone()[0]))
        # The shared upper box wins for the neighbour. Channel ch has a measured winner.
        payload[1][0][5][1] = [
            ["Warp", [1100, 32, 30, 20], [ch, ch + 1]],
            ["Warp", [1110, 30, 8, 6], [ch], 17],
            ["Warp", [1120, 32, 3, 4], [ch], 17],
            ["Warp", [1120, 90, 12, 8], [ch]],
            ["Warp", [1140, 120, 8, 12], [ch]],
            ["Dent", [1110, 120, 3, 4], [ch]],
        ]
        db.execute(
            "UPDATE cycles SET detail=? WHERE cycle_id='101'",
            (zlib.compress(json.dumps(payload).encode()),),
        )
    digest = sha256(path.read_bytes()).digest()
    with open_history_review(tmp_path) as reader:
        detail = reader.ng_capsule_detail(CapsuleKey(RUN, "101", ch)).result(WAIT)
        assert detail is not None
        cycle = next(
            r for r in reader.query().result(WAIT).records if r.cycle_id == 101
        )
        evidence = next(c.image_evidence for c in cycle.cameras if c.camera == camera)
        assert evidence is not None
        camera_rows = [
            (f.label, f.bounding_rect.x, f.area, f.channels) for f in evidence.findings
        ]
        assert camera_rows == [
            ("Warp", 1100, None, (ch + 1,)),
            ("Warp", 1110, 17, (ch,)),
            *([("Warp", 1120, None, (ch,))] if frozen else []),
            ("Dent", 1110, None, (ch,)),
        ]
        assert [
            (f.label, f.bounding_rect.x, f.area, f.channels)
            for f in detail.exposures[0].findings
        ] == [row for row in camera_rows if row[3] == (ch,)]
        assert bool(detail.exposures[0].path) == frozen
    assert sha256(path.read_bytes()).digest() == digest


@pytest.mark.parametrize("cameras", (5, 6))
def test_four_persisted_slots_and_only_selected_evidence(
    tmp_path: Path, cameras: int
) -> None:
    saved = sample(
        tmp_path, cameras, recipe_name="Amber capsule at Start", edge_area=41
    )
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
        assert [f.area for f in spot.findings] == [None, None, 41]
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


@pytest.mark.parametrize("cameras", (5, 6))
def test_filtered_projection_uses_final_ok_actual_slots_batch_and_retained_detail(
    tmp_path: Path, cameras: int
) -> None:
    saved = sample(tmp_path, cameras, with_filtered=True)
    scope = RecordsScope(batch=saved.batch)
    with open_history_review(tmp_path) as reader:
        all_rows = (
            reader.ng_capsule_page(scope, mode=CapsuleFilter.ALL).result(WAIT).rows
        )
        filtered, *ng = all_rows
        assert [r.key.channel for r in all_rows] == [
            saved.channel - 1,
            saved.channel,
            saved.channel + 1,
        ]
        assert filtered.filtered and all(not row.filtered for row in ng)
        assert reader.ng_capsule_page(scope, mode=CapsuleFilter.FILTERED).result(
            WAIT
        ).rows == (filtered,)
        assert reader.ng_capsule_page(scope).result(WAIT).rows == tuple(ng)
        assert (
            not reader.ng_capsule_page(
                RecordsScope(unbatched=True), mode=CapsuleFilter.ALL
            )
            .result(WAIT)
            .rows
        )
        detail = reader.ng_capsule_detail(filtered.key).result(WAIT)
        assert detail and detail.roi and detail.roi.camera == saved.camera
        assert [e.cycle for e in detail.exposures] == list(map(str, MEMBERS))
        assert all(e.channel and e.channel.business == "OK" for e in detail.exposures)
        assert [
            [(f.label, f.area, f.filtered) for f in e.findings]
            for e in detail.exposures
        ] == [[("Hair", 17, True)], [], [("Hair", 17, True)], []]
        assert detail.exposures[2].path is None and detail.exposures[2].size == (
            1520,
            196,
        )
        rejected = reader.ng_capsule_detail(ng[0].key).result(WAIT)
        assert rejected and not any(
            f.filtered or f.label == "Hair"
            for e in rejected.exposures
            for f in e.findings
        )
    # A measured winner must not hide a malformed unmeasured Filtered companion.
    with closing(sqlite3.connect(tmp_path / "history.sqlite3")) as db, db:
        row = db.execute(
            "SELECT evidence FROM cycle_area_filtered WHERE cycle='101' AND channel=?",
            (filtered.key.channel,),
        ).fetchone()
        payload = json.loads(row[0])
        payload[1].append(payload[1][0][:3])
        db.execute(
            "UPDATE cycle_area_filtered SET evidence=? WHERE cycle='101' AND channel=?",
            (json.dumps(payload), filtered.key.channel),
        )
    with open_history_review(tmp_path) as reader:
        partial = reader.ng_capsule_detail(filtered.key).result(WAIT)
        assert (
            partial
            and partial.exposures[0].path is None
            and not partial.exposures[0].findings
        )
    # Cycle retention also expires its companion rows without changing group outcomes.
    with closing(sqlite3.connect(tmp_path / "history.sqlite3")) as db, db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("DELETE FROM cycles")
        assert db.execute("SELECT count(*) FROM cycle_area_filtered").fetchone() == (0,)
    with open_history_review(tmp_path) as reader:
        assert (
            not reader.ng_capsule_page(scope, mode=CapsuleFilter.FILTERED)
            .result(WAIT)
            .rows
        )
        assert len(reader.ng_capsule_page(scope).result(WAIT).rows) == 2


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
