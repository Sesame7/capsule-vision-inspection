import json
import sqlite3
import zlib
from collections.abc import Iterator
from contextlib import ExitStack, closing
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest
from scripts.area_explorer.evidence import Frame, Region
from scripts.area_explorer.replay import CameraImage
from scripts.area_explorer.study import Study
from scripts.history_area_matching import encode, reconcile
from scripts.history_area_plan import Replay, finding, legacy_plan, source_path
from scripts.rebuild_history_areas import (
    compare,
    pending_journal,
    publish,
    stage_identity,
)

from capsule_vision.application.history import HistoryFinding
from capsule_vision.application.ng_capsules import FrozenROI
from capsule_vision.inference import BoundingRect
from capsule_vision.inspection import CameraId
from capsule_vision.inspection.production import CameraLayout, ChannelId, ChannelROI


def camera() -> CameraLayout:
    return CameraLayout(
        CameraId("CAM1"),
        tuple(
            ChannelROI(i, ChannelId(i + 1), BoundingRect(20 * i, 0, 20, 100))
            for i in range(6)
        ),
    )


def box(x: int, y: int, width: int = 6, height: int = 6) -> BoundingRect:
    return BoundingRect(x, y, width, height)


def region(number: int, rect: BoundingRect, area: int, label: str = "Dent") -> Region:
    return Region(number, label, area, rect, 6, 4, (), (), (), (), True, "replayed")


def replay(*regions: Region) -> Frame:
    return Frame(
        "photo.png",
        "sha",
        "receipt",
        120,
        100,
        regions,
        {n: "PRESENT" for n in range(1, 7)},
        {n: "NG" for n in range(1, 7)},
    )


def recorded(rect: BoundingRect, *channels: int, label: str = "Dent") -> HistoryFinding:
    return HistoryFinding(label, rect, channels or (1,))


def test_matches_current_maxima_per_view_and_channel_with_shared_region_and_tie() -> (
    None
):
    a, b, c, d = box(1, 5), box(18, 8), box(4, 47), box(7, 60)
    f = replay(region(1, a, 18), region(2, b, 22), region(3, c, 17), region(4, d, 17))
    old = [recorded(a), recorded(b, 1, 2), recorded(c), recorded(d)]
    result = reconcile(old, f, 99, camera(), "2", frozenset(), {"Dent": 17})
    assert result.findings == [
        HistoryFinding("Dent", b, (1, 2), 22),
        HistoryFinding("Dent", c, (1,), 17),
    ]
    assert result.counts["measured_assignments"] == 3
    assert result.counts["subordinate_assignments"] == 2


def test_replay_uses_measured_geometry_and_preserves_other_unresolved_view() -> None:
    a, b, lost = box(1, 4, 10, 8), box(2, 4, 10, 8), box(4, 82)
    result = reconcile(
        [recorded(a), recorded(lost)],
        replay(region(1, b, 46)),
        8,
        camera(),
        "2",
        frozenset(),
        {},
    )
    assert result.findings == [recorded(lost), HistoryFinding("Dent", b, (1,), 46)]
    assert (
        result.counts["measured_assignments"] == result.counts["historical_only"] == 1
    )


def test_replacement_needs_agreement_on_channel_view_and_label_only() -> None:
    # The winning region has no geometric overlap with any recorded rectangle.
    old = [
        recorded(box(1, 4)),
        recorded(box(2, 8)),
        recorded(box(3, 9)),
        recorded(box(7, 80), label="Hair"),
    ]
    measured = replay(
        region(1, box(1, 40), 16),
        region(2, box(7, 37), 31),
        region(3, box(8, 80), 22, "Warp"),
    )
    result = reconcile(old, measured, 8, camera(), "2", frozenset(), {})
    assert result.findings == [old[3], HistoryFinding("Dent", box(7, 37), (1,), 31)]
    assert {i["reason"] for i in result.issues} == {"historical_only", "replay_only"}
    assert (
        reconcile(result.findings, measured, 8, camera(), "2", frozenset(), {}).findings
        == result.findings
    )


def test_shared_box_is_split_only_by_supported_channel_association() -> None:
    shared = recorded(box(18, 4), 1, 2)
    replayed = region(1, box(1, 8), 16)
    result = reconcile([shared], replay(replayed), 9, camera(), "2", frozenset(), {})
    assert result.findings == [
        replace(shared, channels=(2,)),
        HistoryFinding("Dent", replayed.box, (1,), 16),
    ]
    # An overlapping replay on channel 2 cannot create its first historical Dent.
    result = reconcile(
        [replace(shared, channels=(1,))],
        replay(region(1, shared.bounding_rect, 26)),
        9,
        camera(),
        "2",
        frozenset(),
        {},
    )
    assert result.findings == [replace(shared, channels=(1,), area=26)]
    assert result.issues[0]["reason"] == "replay_only"


@pytest.mark.parametrize(
    ("disabled", "minimum", "reason"),
    [(True, 0, "historical_only"), (False, 17, "below_frozen_minimum")],
)
def test_ineligible_replay_keeps_one_historical_box(
    disabled: bool, minimum: int, reason: str
) -> None:
    old = [recorded(box(2, 5, 10, 8)), recorded(box(4, 7))]
    result = reconcile(
        old,
        replay(region(1, old[0].bounding_rect, 16)),
        9,
        camera(),
        "2",
        frozenset({"Dent"}) if disabled else frozenset(),
        {"Dent": minimum},
    )
    assert result.findings == [old[0]]
    assert result.issues[0]["reason"] == reason


def test_legacy_fallback_uses_unclipped_center_stored_order_and_never_measures() -> (
    None
):
    roi = FrozenROI("CAM1", 0, BoundingRect(0, 10, 20, 81))  # divider is y=50
    upper = recorded(box(1, 42, 7, 8))
    tied = recorded(box(2, 41, 8, 7))
    lower = recorded(box(1, 46, 7, 8))  # center exactly at divider
    measured = replace(recorded(box(4, 59)), area=6)
    old = [upper, tied, lower, measured, recorded(box(1, 62, 18, 22))]
    row: list[object] = [
        "CAM1",
        "SUCCESS",
        "NG",
        [],
        [],
        [[20, 100], [encode(f) for f in old]],
    ]
    audit: dict[str, object] = {}
    assert legacy_plan({1: roi}, row, [["CAM1", 0, 1, "NG"]], audit)
    assert audit["after"] == [encode(upper), encode(measured)]
    assert not legacy_plan({1: roi}, row, [["CAM1", 0, 1, "NG"]], {})
    with pytest.raises(ValueError, match="geometry"):
        legacy_plan({}, row, [["CAM1", 0, 1, "NG"]], {})


def test_empty_gating_and_incompatible_geometry() -> None:
    f = replay(region(1, box(18, 9), 20))
    f = replace(
        f, occupancy={**f.occupancy, 2: "EMPTY"}, business={**f.business, 2: "EMPTY"}
    )
    result = reconcile([recorded(box(18, 9))], f, 7, camera(), "2", frozenset(), {})
    assert result.findings == [HistoryFinding("Dent", box(18, 9), (1,), 20)]
    partial = reconcile(
        [recorded(box(18, 9), 1, 2)],
        replace(f, business={**f.business, 2: "OK"}),
        7,
        camera(),
        "2",
        frozenset(),
        {},
    )
    assert partial.findings == [
        recorded(box(18, 9), 2),
        HistoryFinding("Dent", box(18, 9), (1,), 20),
    ]
    assert partial.issues[0]["reason"] == "occupancy_unavailable"
    with pytest.raises(ValueError, match="ROI"):
        reconcile([], replace(f, width=119), 7, camera(), "2", frozenset(), {})


def test_invalid_original_measurement_association_or_link(tmp_path: Path) -> None:
    for row in (
        ["Dent", [2, 2, 8, 7], [2]],
        ["Dent", [2, 2, 8, 7], [1], False],
        ["Dent", [2, 2, 8, 7], [1, 1]],
    ):
        with pytest.raises(ValueError):
            finding(row, {1}, 20, 20)
    with pytest.raises(ValueError, match="Invalid archived"):
        source_path(tmp_path, "C:/archive/2026-11-17/CAM2/photo.png", "CAM1")
    with pytest.raises(FileNotFoundError):
        source_path(tmp_path, "C:/archive/2026-11-17/CAM1/photo.png", "CAM1")


def test_changed_original_cannot_fall_through_to_new_inference(tmp_path: Path) -> None:
    study = Study(
        tmp_path,
        tmp_path,
        "model",
        "model-sha",
        "db-sha",
        "study-sha",
        "sdk",
        "2",
        {},
        (),
        (),
    )
    image = CameraImage(
        "r", "9", 3, "11", "CAM1", "2026-11-17/CAM1/a.png", "changed", 120, 100, {}
    )
    with closing(sqlite3.connect(tmp_path / "plan.sqlite3")) as prior, prior:
        stage_identity(
            prior,
            dict(
                database_sha=study.database_sha,
                model_sha=study.model_sha,
                receipts_sha=study.analysis_sha,
            ),
        )
        prior.execute(
            "INSERT INTO plans VALUES(1,'r','11','',NULL,?,0,'9')",
            (
                json.dumps(
                    [dict(camera=image.camera, path=image.path, sha256="original")]
                ),
            ),
        )
    with closing(sqlite3.connect(":memory:")) as ref:
        ref.executescript(
            "CREATE TABLE runs(uuid,recipe); CREATE TABLE archive_images(camera_id,path);"
        )
        loader = Replay(
            ref,
            study,
            tmp_path / "model",
            tmp_path / "explorer",
            tmp_path / "work",
            tmp_path / "replay-cache",
        )
        with pytest.raises(ValueError, match="previously verified original"):
            loader.read(image)
        pilot = replace(replay(), path=image.path, sha256="original")
        loader.pilot[(image.run, image.group, 3, "11", "CAM1")] = pilot
        loader.known_photos.clear()
        with pytest.raises(ValueError, match="pilot original"):
            loader.read(image)
        assert loader.client.process is None


def test_empty_wal_is_checkpointed_and_nonempty_journal_is_protected(
    tmp_path: Path,
) -> None:
    path = tmp_path / "history.sqlite3"
    wal = path.with_name(path.name + "-wal")
    wal.touch()
    assert not pending_journal(path)
    wal.write_bytes(b"pending frames")
    assert pending_journal(path)


type Stores = tuple[sqlite3.Connection, sqlite3.Connection, sqlite3.Connection, Path]


@pytest.fixture
def stores(tmp_path: Path) -> Iterator[Stores]:
    with ExitStack() as stack:
        ref, target, stage = [
            stack.enter_context(closing(sqlite3.connect(":memory:"))) for _ in range(3)
        ]
        ref.executescript("""CREATE TABLE cycles(id INTEGER PRIMARY KEY,run_id,cycle_id,detail);
            CREATE TABLE archive_images(run_id,cycle_id,camera_id,path);
            CREATE TABLE totals(ng INTEGER); INSERT INTO totals VALUES(1);""")
        stage_identity(stage, {"version": 1})
        photo = tmp_path / "2026-11-17" / "CAM1" / "a.png"
        photo.parent.mkdir(parents=True)
        photo.write_bytes(b"verified bytes")
        for ident in (1, 10, 65):
            item = recorded(box(2, 3))

            def blob(f: HistoryFinding) -> bytes:
                data = [
                    [["CAM1", 0, 1, "NG", "SUCCESS", "NG", ["defect_region"]]],
                    [["CAM1", "SUCCESS", "NG", [], [], [[20, 100], [encode(f)]]]],
                    "Test",
                ]
                return zlib.compress(json.dumps(data).encode())

            old, after = blob(item), blob(replace(item, area=16))
            meta = [
                dict(
                    camera="CAM1",
                    before=[encode(item)],
                    after=[encode(replace(item, area=16))],
                    sha256=sha256(photo.read_bytes()).hexdigest(),
                )
            ]
            ref.execute(
                "INSERT INTO cycles VALUES(?,?,?,?)", (ident, "r", str(ident), old)
            )
            ref.execute(
                "INSERT INTO archive_images VALUES(?,?,?,?)",
                ("r", str(ident), "CAM1", str(photo)),
            )
            stage.execute(
                "INSERT INTO plans VALUES(?,?,?,?,?,?,0,'g')",
                (
                    ident,
                    "r",
                    str(ident),
                    sha256(old).hexdigest(),
                    after,
                    json.dumps(meta),
                ),
            )
        ref.commit()
        ref.backup(target)
        yield ref, target, stage, tmp_path


def test_bounded_resume_and_idempotence(stores: Stores) -> None:
    ref, target, stage, root = stores
    assert publish(ref, target, stage, root, 1, 1) == 1
    assert compare(ref, target, stage) == {"pending": 2, "applied": 1}
    assert publish(ref, target, stage, root, 2, None) == 2
    assert publish(ref, target, stage, root, 2, None) == 0
    assert compare(ref, target, stage) == {"applied": 3}


def test_changed_png_rolls_back_batch_and_preserves_source(stores: Stores) -> None:
    ref, target, stage, root = stores
    (root / "2026-11-17" / "CAM1" / "a.png").write_bytes(b"different")
    with pytest.raises(ValueError, match="PNG changed"):
        publish(ref, target, stage, root, 2, None)
    assert compare(ref, target, stage) == {"pending": 3}


def test_missing_photo_fallback_can_be_published_then_upgraded_on_retry(
    stores: Stores,
) -> None:
    ref, target, stage, root = stores
    stage.execute("DELETE FROM plans WHERE id<>1")
    original = ref.execute("SELECT detail FROM cycles WHERE id=1").fetchone()[0]
    payload = json.loads(zlib.decompress(original))
    rows = payload[1][0][5][1]
    rows.append(encode(recorded(box(4, 3, 2, 2))))
    start = zlib.compress(json.dumps(payload).encode())
    for db in (ref, target):
        db.execute("UPDATE cycles SET detail=? WHERE id=1", (start,))
        db.commit()
    measured, receipt = stage.execute("SELECT rebuilt,images FROM plans").fetchone()
    im = json.loads(receipt)[0]
    fallback = dict(
        camera="CAM1",
        source="legacy",
        error="missing PNG",
        before=rows,
        after=im["before"],
    )
    stage.execute(
        "UPDATE plans SET original_sha=?,rebuilt=?,images=?",
        (sha256(start).hexdigest(), original, json.dumps([fallback])),
    )
    photo = root / "2026-11-17" / "CAM1" / "a.png"
    photo.unlink()
    assert publish(ref, target, stage, root, 1, None) == 1
    assert publish(ref, target, stage, root, 1, None) == 0
    photo.write_bytes(b"verified bytes")
    stage.execute(
        "UPDATE plans SET rebuilt=?,images=?",
        (
            measured,
            json.dumps(
                [{**im, "before": rows, "previous_fallback": fallback["after"]}]
            ),
        ),
    )
    assert publish(ref, target, stage, root, 1, None) == 1
    assert publish(ref, target, stage, root, 1, None) == 0
    assert compare(ref, target, stage) == {"applied": 1, "unchanged": 2}


def test_refuses_changed_outcomes_and_manifests(stores: Stores) -> None:
    ref, target, stage, root = stores
    target.execute("UPDATE totals SET ng=0")
    with pytest.raises(ValueError, match="accounting"):
        publish(ref, target, stage, root, 2, None)
    with pytest.raises(ValueError, match="different History"):
        stage_identity(stage, {"version": 2})


def test_conflicting_record_or_plan_fails_before_any_write(stores: Stores) -> None:
    ref, target, stage, root = stores
    stage.execute("UPDATE plans SET images='[]' WHERE id=65")
    with pytest.raises(ValueError, match="camera comparison"):
        publish(ref, target, stage, root, 1, None)
    assert (
        target.execute("SELECT detail FROM cycles ORDER BY id").fetchall()
        == ref.execute("SELECT detail FROM cycles ORDER BY id").fetchall()
    )


def test_previously_published_camera_survives_extension_after_missing_image_returns(
    stores: Stores,
) -> None:
    ref, target, stage, root = stores
    original = ref.execute("SELECT detail FROM cycles WHERE id=1").fetchone()[0]
    after, audit = stage.execute(
        "SELECT rebuilt,images FROM plans WHERE id=1"
    ).fetchone()

    def combined(a: bytes, b: bytes) -> bytes:
        data = json.loads(zlib.decompress(a))
        added = json.loads(zlib.decompress(b))[1][0]
        added[0] = "CAM2"
        data[1].append(added)
        return zlib.compress(json.dumps(data).encode())

    baseline = combined(original, original)
    ref.execute("UPDATE cycles SET detail=? WHERE id=1", (baseline,))
    target.execute(
        "UPDATE cycles SET detail=? WHERE id=1", (combined(after, original),)
    )
    im = json.loads(audit)[0]
    first = Path(
        ref.execute("SELECT path FROM archive_images WHERE cycle_id='1'").fetchone()[0]
    )
    photo = first.parent.parent / "CAM2" / first.name
    photo.parent.mkdir()
    photo.write_bytes(first.read_bytes())
    for db in (ref, target):
        db.execute("INSERT INTO archive_images VALUES('r','1','CAM2',?)", (str(photo),))
        db.commit()
    stage.execute(
        "UPDATE plans SET original_sha=?,rebuilt=?,images=? WHERE id=1",
        (
            sha256(baseline).hexdigest(),
            combined(after, after),
            json.dumps([im, {**im, "camera": "CAM2"}]),
        ),
    )
    assert publish(ref, target, stage, root, 2, None) == 3
    assert compare(ref, target, stage) == {"applied": 3}
