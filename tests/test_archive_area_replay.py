"""Verify the History backfill’s saved study receipts and serial inference worker."""

import hashlib
import json
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, replace
from multiprocessing import get_context
from pathlib import Path
from threading import get_ident
from typing import cast

import cv2
import numpy as np
import pytest
from numpy.typing import NDArray
from scripts.area_explorer import batch
from scripts.area_explorer.batch import Outcome
from scripts.area_explorer.evidence import Channel, Frame, Region
from scripts.area_explorer.replay import (
    CameraImage,
    ImageUnavailable,
    Profile,
    ReplaySetup,
    analysis_owner,
    cached_frame,
    capture,
    file_sha,
    image_identity,
    verified_png,
)
from scripts.area_explorer.study import Study, load_study

from capsule_vision.inference import DefectRegion, ImageResult, VimoSegmentationEngine
from capsule_vision.inference.errors import VimoInferenceError
from capsule_vision.inference.models import BoundingRect
from tests.support.cycles import image_result

CH = Channel(6, "CAM1", BoundingRect(40, 20, 30, 161))
MEMBERS = ("101", "38", "804", "17")


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def region(n: int, label: str, area: int, y: int) -> Region:
    return Region(
        n,
        label,
        area,
        BoundingRect(50, y, 10, 6),
        9.4,
        4.5,
        (),
        (6,),
        (6,),
        (),
        True,
        "exact",
    )


def _packet(root: Path, error: str = "") -> Study:
    archive = root / "images"
    directory = root / "study"
    (directory / "raw").mkdir(parents=True)
    day = archive / "2026-11-17/CAM1"
    day.mkdir(parents=True)
    path = day / "test.png"
    img = np.full((200, 90, 3), (19, 181, 73), dtype=np.uint8)
    assert cv2.imwrite(str(path), img)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    source = dict(
        run="saved-run",
        group_first_cycle="101",
        slot=1,
        cycle="101",
        camera="CAM1",
        path=path.relative_to(archive).as_posix(),
        sha256=sha,
    )
    receipt = "raw/saved-run_101_CAM1.json"
    engine = dict(
        model_sha256="model-sha",
        module_id="2",
        sdk_distribution="3.20",
        labels=["Hair"],
        area_thresholds={"Hair": 5},
    )
    box = [50, 30, 10, 6]
    sides = dict(
        region_id=1, label="Hair", sdk_area_px2=37, long_side_px=9.4, short_side_px=4.5
    )
    docs: dict[str, object] = {
        "identity-coverage.json": dict(
            database=str(archive / "history.sqlite3"),
            database_sha256="history-sha",
            model="demo.vimosln",
            model_sha256="model-sha",
            frozen_model=dict(expected_sha256="model-sha", module_id="2"),
            runs=[dict(run_id="saved-run")],
            vimo_policy=dict(enabled=True, disabled_labels=[]),
            layout=dict(
                cameras=[
                    dict(
                        camera_id="CAM1",
                        slots=[dict(channel_id=6, rect=asdict(CH.roi))],
                    )
                ]
            ),
        ),
        "engine-pilot.json": engine,
        "engine-remaining.json": engine,
        "selected-groups.json": [
            dict(
                run="saved-run",
                group_first_cycle="101",
                members=list(enumerate(MEMBERS, 1)),
                recorded_final_channels=[[6, "NG"]],
                camera_slots=[
                    dict(
                        slot=i,
                        cycle=v,
                        camera="CAM1",
                        image_state="available" if i == 1 else "no_stored_link",
                    )
                    for i, v in enumerate(MEMBERS, 1)
                ],
            )
        ],
        "history-evidence.json": {
            "saved-run_101_CAM1": dict(
                source=source,
                stored_archive_link=r"D:\History\2026-11-17\CAM1\test.png",
            )
        },
        "images.json": [
            dict(
                **source,
                width=90,
                height=200,
                raw_result=receipt,
                raw_regions=1,
                occupancy_state={"6": "PRESENT"},
                recorded_business={"6": "NG"},
            )
        ],
        "regions.json": [
            dict(
                **source,
                **sides,
                bbox_x=50,
                bbox_y=30,
                bbox_width=10,
                bbox_height=6,
                roi_channels=[6],
                effective_channels=[6],
                empty_gated_channels=[],
                recipe_label_enabled=True,
                comparison="exact",
            )
        ],
    }
    if error != "no receipt":
        docs[receipt] = dict(
            source=dict(source, cycle="wrong" if error == "wrong cycle" else "101"),
            model_sha256="other" if error == "wrong model" else "model-sha",
            module_id="2",
            image_shape_hwc=[200, 90, 3],
            regions=[
                dict(
                    sides,
                    sdk_area_px2=60 if error == "box as area" else 37,
                    bbox_xywh=box,
                    outer_contour=[[50, 30], [55, 31], [54, 34]],
                    inner_contours=[],
                )
            ],
        )
    for name, content in docs.items():
        (directory / name).write_text(json.dumps(content), "utf-8")
    return load_study(directory)


@pytest.mark.parametrize("error", ["wrong cycle", "wrong model", "box as area"])
def test_loader_rejects_mixed_receipts(tmp_path: Path, error: str) -> None:
    with pytest.raises(ValueError, match="identity|area/geometry"):
        _packet(tmp_path, error)


def test_photo_and_replay_availability_are_independent(tmp_path: Path) -> None:
    s = _packet(tmp_path, "no receipt")
    f = s.groups[0].frames[1, "CAM1"]
    g = s.groups[0]
    job = CameraImage(
        g.run,
        g.first_cycle,
        1,
        g.members[0],
        "CAM1",
        f.path,
        f.sha256,
        f.width,
        f.height,
        {},
    )
    setup = ReplaySetup(s, s.database_sha, tmp_path / "cache", {})
    source = s.package / f.path
    assert f.regions is None and verified_png(setup, job) == source.read_bytes()
    source.write_bytes(b"other photograph")
    with pytest.raises(ImageUnavailable, match="PNG changed"):
        verified_png(setup, job)
    source.unlink()
    with pytest.raises(ImageUnavailable, match="PNG unavailable"):
        verified_png(setup, job)


def _batch_sample(tmp_path: Path) -> tuple[ReplaySetup, CameraImage, Path]:
    study = _packet(tmp_path)
    model = tmp_path / "model.vimosln"
    model.write_bytes(b"test identity")
    db = study.package / "history.sqlite3"
    db.write_bytes(b"test history identity")
    group = study.groups[0]
    frame = group.frames[1, "CAM1"]
    job = CameraImage(
        group.run,
        group.first_cycle,
        1,
        group.members[0],
        "CAM1",
        frame.path,
        frame.sha256,
        frame.width,
        frame.height,
        {6: "NG"},
    )
    study = replace(study, model_sha=file_sha(model), database_sha=file_sha(db))
    return ReplaySetup(study, study.database_sha, tmp_path / "cache", {}), job, model


def test_cached_receipt_checks_source_checksum_and_native_area(tmp_path: Path) -> None:
    setup, job, _ = _batch_sample(tmp_path)
    ident = image_identity(setup, job)
    stored = setup.cache / "frames" / (digest(ident) + ".json")
    stored.parent.mkdir(parents=True)
    payload = dict(
        identity=ident,
        regions=[asdict(region(1, "Hair", 37, 30))],
        occupancy={"6": "PRESENT"},
    )
    stored.write_text(json.dumps(dict(data=payload, sha256=digest(payload))))
    found = cached_frame(setup, job)
    assert found and found.regions == (region(1, "Hair", 37, 30),)
    assert cached_frame(setup, replace(job, cycle="different")) is None
    invalid = dict(payload, regions=[asdict(region(1, "Hair", 4, 30))])
    stored.write_text(json.dumps(dict(data=invalid, sha256=digest(invalid))))
    with pytest.raises(ValueError, match="area"):
        cached_frame(setup, job)
    stored.write_text(json.dumps(dict(data=payload, sha256="corrupt")))
    with pytest.raises(ValueError, match="checksum"):
        cached_frame(setup, job)
    substituted = dict(payload, identity=dict(ident, cycle="another"))
    stored.write_text(json.dumps(dict(data=substituted, sha256=digest(substituted))))
    with pytest.raises(ValueError, match="identity"):
        cached_frame(setup, job)


@pytest.mark.parametrize("disabled", [False, True])
def test_capture_round_trips_native_area_geometry_and_recipe_evidence(
    tmp_path: Path, disabled: bool
) -> None:
    setup, job, _ = _batch_sample(tmp_path)
    study = setup.study
    profile = Profile(
        (CH,),
        study.model_sha,
        True,
        frozenset({"Hair"} if disabled else ()),
    )
    setup = replace(setup, profiles={job.run: profile})
    box = BoundingRect(50, 30, 10, 6)
    contour = ((50, 30), (55, 31), (54, 34))

    class Engine:
        def infer(self, image: NDArray[np.uint8]) -> ImageResult:
            assert image.shape == (200, 90, 3) and tuple(image[0, 0]) == (19, 181, 73)
            result = image_result(image)
            defect = DefectRegion(
                "Hair", 37, box, 9.4, 4.5, contour, (), result.rotated_roi
            )
            return replace(result, module_id=study.module, regions=(defect,))

    measured = capture(
        setup, job, cast(VimoSegmentationEngine, Engine()), verified_png(setup, job)
    )
    assert cached_frame(setup, job) == measured
    assert measured.gate(6) == "PRESENT" and measured.regions == (
        Region(
            1,
            "Hair",
            37,
            box,
            9.4,
            4.5,
            (contour,),
            (6,),
            () if disabled else (6,),
            (),
            not disabled,
            "replayed",
        ),
    )


def test_worker_reports_failures_reuses_receipts_and_checks_originals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup, job, model = _batch_sample(tmp_path)
    second = replace(job, slot=2, cycle=MEMBERS[1])
    threads: list[int] = []

    @contextmanager
    def engine(config: ReplaySetup, path: Path) -> Generator[VimoSegmentationEngine]:
        threads.append(get_ident())
        try:
            yield cast(VimoSegmentationEngine, object())
        finally:
            threads.append(get_ident())

    def fail(
        config: ReplaySetup, job: CameraImage, sdk: VimoSegmentationEngine, data: bytes
    ) -> Frame:
        threads.append(get_ident())
        raise VimoInferenceError("test native failure")

    def existing_binding(config: ReplaySetup) -> None:
        assert config.study.sdk == setup.study.sdk

    monkeypatch.setattr(batch, "_study_binding", existing_binding)
    monkeypatch.setattr(batch, "verified_engine", engine)
    monkeypatch.setattr(batch, "capture", fail)
    ident = image_identity(setup, job)

    payload = dict(identity=ident, regions=[], occupancy={"6": "PRESENT"})
    stored = setup.cache / "frames" / (digest(ident) + ".json")
    stored.parent.mkdir(parents=True)
    stored.write_text(json.dumps(dict(data=payload, sha256=digest(payload))))
    parent, child = get_context("spawn").Pipe()
    with parent, ThreadPoolExecutor(max_workers=1) as owner:
        task = owner.submit(batch.serve_replay, child, setup, model)
        parent.send(job)
        hit = parent.recv()
        assert isinstance(hit, Outcome) and hit.reused and hit.frame and not threads
        parent.send(second)
        bad = parent.recv()
        assert isinstance(bad, Outcome) and bad.problem == "test native failure"
        # Changing only the source bytes also invalidates a previously cached exposure.
        (setup.study.package / job.path).write_bytes(
            b"changed original in temporary fixture"
        )
        parent.send(job)
        missing = parent.recv()
        assert (
            isinstance(missing, Outcome)
            and missing.problem
            and "PNG changed" in missing.problem
        )
        parent.send(None)
        assert parent.recv() is None
        task.result(timeout=3)
    assert len(threads) == 3 and len(set(threads)) == 1 and threads[0] != get_ident()
    with (
        analysis_owner(setup.cache),
        pytest.raises(ValueError, match="already|Another"),
    ):
        with analysis_owner(setup.cache):
            pass


def test_spawned_replay_refuses_wrong_model_and_releases_cache(tmp_path: Path) -> None:
    setup, job, model = _batch_sample(tmp_path)
    client = batch.ReplayClient(setup, model)
    model.write_bytes(b"different model")
    try:
        with pytest.raises(RuntimeError, match="model differs"):
            client.request(job)
    finally:
        client.close()
    with analysis_owner(setup.cache):
        assert not list(setup.cache.glob("frames/*.json"))
