"""Original pixels, identity, decisions and bounded asynchronous disk ownership."""

import os
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from threading import Event, get_ident
from threading import enumerate as threads

import cv2
import numpy as np
import pytest
from tests.support.application import production
from tests.support.business import channel

from capsule_vision.application.image_archive import (
    ArchiveImage,
    ImageArchive,
    camera_archive_status,
    write_png,
)
from capsule_vision.configuration import ImageArchiveSettings
from capsule_vision.inspection import CycleId, Frame
from capsule_vision.inspection.production import (
    CameraInspectionResult,
    FindingSource,
    OccupancyState,
    ProductJudgment,
    Reason,
    TechnicalStatus,
)

OK, NG, UNKNOWN = ProductJudgment
SUCCESS, ERROR, DISABLED = TechnicalStatus
PRESENT, EMPTY, UNCERTAIN = (
    OccupancyState.PRESENT,
    OccupancyState.EMPTY,
    OccupancyState.UNKNOWN,
)
STAMP = datetime(2026, 10, 17, 23, 59, 59, 123456).astimezone()


def inspected() -> CameraInspectionResult:
    camera = production().cameras[0]
    return replace(
        camera,
        channels=tuple(channel(c, PRESENT, SUCCESS, OK) for c in camera.channels),
    )


def pixels(camera: CameraInspectionResult) -> Frame:
    assert camera.frame_id is not None
    return Frame(
        camera.frame_id,
        3.0,
        np.arange(20 * 60 * 3, dtype=np.uint8).reshape(20, 60, 3).copy(),
    )


@pytest.mark.parametrize(
    "occupancy,technical,judgment,expected",
    [
        (PRESENT, SUCCESS, OK, 0),
        (PRESENT, SUCCESS, NG, 1),
        (PRESENT, ERROR, UNKNOWN, 2),
        (PRESENT, ERROR, NG, 1),
        (EMPTY, SUCCESS, OK, 3),
        (UNCERTAIN, SUCCESS, OK, None),
        (None, SUCCESS, OK, None),
        (PRESENT, DISABLED, UNKNOWN, None),
    ],
)
def test_camera_decision_uses_all_six_explicit_channels(
    occupancy: OccupancyState | None,
    technical: TechnicalStatus,
    judgment: ProductJudgment,
    expected: int | None,
) -> None:
    camera = inspected()
    changed = replace(
        camera,
        channels=(
            channel(camera.channels[0], occupancy, technical, judgment),
            *camera.channels[1:],
        ),
    )
    assert camera_archive_status(changed) == expected
    unfinished = replace(
        changed,
        collection_issue=Reason(FindingSource.COLLECTION, "basler.inspection_deadline"),
    )
    assert camera_archive_status(unfinished) is None


@pytest.mark.parametrize(
    "enabled",
    [
        (True, False, False, False),
        (False, True, False, False),
        (False, False, True, False),
        (False, False, False, True),
        (True, True, True, False),
        (True, True, True, True),
    ],
)
def test_mixed_cameras_switches_pixels_identity_timestamp_and_once(
    tmp_path: Path, enabled: tuple[bool, bool, bool, bool]
) -> None:
    result = production()
    states = (
        (PRESENT, SUCCESS, OK),
        (PRESENT, SUCCESS, NG),
        (PRESENT, ERROR, UNKNOWN),
        (EMPTY, SUCCESS, OK),
        (UNCERTAIN, SUCCESS, OK),
        (PRESENT, ERROR, NG),
    )
    result = replace(
        result,
        cameras=tuple(
            replace(
                camera, channels=tuple(channel(row, *state) for row in camera.channels)
            )
            for camera, state in zip(result.cameras, states, strict=True)
        ),
    )
    count = [0]

    def now() -> datetime:
        count[0] += 1
        return STAMP

    archive = ImageArchive(
        ImageArchiveSettings(str(tmp_path), *enabled),
        9,
        result.layout.camera_ids,
        now=now,
    )
    frames = tuple(pixels(camera) for camera in result.cameras)
    archive.start()
    assert archive.begin(result.cycle_id)
    for frame in reversed(frames):
        archive.retain(frame)
    archive.complete(result.cycle_id, result)
    archive.complete(result.cycle_id, result)
    archive.retain(frames[0])  # Late pixels cannot reopen the terminal cycle.
    archive.request_stop()
    assert archive.join(3)
    expected = {"CAM1": 0, "CAM2": 1, "CAM3": 2, "CAM4": 3, "CAM6": 1}
    expected = {camera: code for camera, code in expected.items() if enabled[code]}
    files = sorted(tmp_path.rglob("*.png"))
    assert len(files) == len(expected) and count == [6]
    for file in files:
        camera = file.parent.name
        assert file.name == f"{expected[camera]}_235959123_1.png"
        assert file.parent.parent.name == "2026-10-17"
        original = frames[result.layout.camera_ids.index(camera)].image
        loaded = cv2.imdecode(np.fromfile(file, dtype=np.uint8), cv2.IMREAD_COLOR)
        np.testing.assert_array_equal(loaded, original)
    snapshot = archive.snapshot()
    assert (
        snapshot.written == len(expected)
        and snapshot.retained_bytes == snapshot.pending == snapshot.collecting == 0
    )
    assert not snapshot.alive


@pytest.mark.parametrize(
    "other,expected",
    [
        ((EMPTY, SUCCESS, OK), 3),
        ((PRESENT, SUCCESS, OK), 3),
        ((PRESENT, SUCCESS, NG), 1),
        ((PRESENT, ERROR, UNKNOWN), 2),
        ((EMPTY, ERROR, UNKNOWN), 2),
        ((UNCERTAIN, SUCCESS, OK), None),
        ((None, SUCCESS, OK), None),
        ((PRESENT, DISABLED, UNKNOWN), None),
    ],
)
def test_empty_requires_confirmed_evidence_and_all_remaining_ok_or_empty(
    other: tuple[OccupancyState | None, TechnicalStatus, ProductJudgment],
    expected: int | None,
) -> None:
    base = inspected()
    empty = replace(
        base, channels=tuple(channel(row, EMPTY, SUCCESS, OK) for row in base.channels)
    )
    mixed = replace(
        empty, channels=(channel(base.channels[0], *other), *empty.channels[1:])
    )
    assert camera_archive_status(mixed) == expected
    assert (
        camera_archive_status(replace(mixed, frame_id=None, image_shape=None)) is None
    )
    for reason in (
        "basler.missing_camera",
        "basler.inspection_deadline",
        "basler.shutdown_unfinished",
    ):
        assert (
            camera_archive_status(
                replace(
                    mixed, collection_issue=Reason(FindingSource.COLLECTION, reason)
                )
            )
            is None
        )


@pytest.mark.parametrize(
    "mismatch", ["sequence", "cycle", "shape", "missing", "unfinished"]
)
def test_mismatched_or_unfinished_evidence_never_archives(
    tmp_path: Path, mismatch: str
) -> None:
    result = production()
    camera = inspected()
    frame = pixels(camera)
    if mismatch == "sequence":
        camera = replace(camera, frame_id=replace(frame.id, sequence=99))
    elif mismatch == "cycle":
        result = replace(
            result,
            cycle_id=CycleId(9),
            cameras=tuple(
                replace(c, frame_id=replace(c.frame_id, cycle_id=CycleId(9)))
                if c.frame_id
                else c
                for c in result.cameras
            ),
        )
    elif mismatch == "shape":
        camera = replace(camera, image_shape=(21, 60, 3))
    elif mismatch == "missing":
        camera = replace(camera, frame_id=None, image_shape=None)
    else:
        camera = replace(
            camera,
            collection_issue=Reason(
                FindingSource.COLLECTION, "basler.shutdown_unfinished"
            ),
        )
    if mismatch != "cycle":
        result = replace(result, cameras=(camera, *result.cameras[1:]))
    archive = ImageArchive(
        ImageArchiveSettings(str(tmp_path), True, True, True),
        1,
        result.layout.camera_ids,
    )
    archive.start()
    archive.begin(frame.id.cycle_id)
    archive.retain(frame)
    archive.complete(frame.id.cycle_id, result)
    archive.request_stop()
    assert archive.join(3) and archive.snapshot().written == 0
    assert not list(tmp_path.iterdir())


def test_atomic_collision_publication_and_encoding_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frame = pixels(inspected())
    item = ArchiveImage(2, frame, STAMP, 0)
    write_png(tmp_path, item)
    original = next(tmp_path.rglob("*.png"))
    before = original.read_bytes()
    write_png(tmp_path, item)
    assert sorted(p.name for p in tmp_path.rglob("*.png")) == [
        "0_235959123_1.png",
        "0_235959123_1_1.png",
    ]
    assert original.read_bytes() == before and not list(tmp_path.rglob("*.part"))

    def failed_encode(*_args: object) -> tuple[bool, np.typing.NDArray[np.uint8]]:
        return False, np.zeros(0, dtype=np.uint8)

    monkeypatch.setattr(cv2, "imencode", failed_encode)
    with pytest.raises(OSError, match="encoding"):
        write_png(tmp_path, item)
    assert len(list(tmp_path.rglob("*.png"))) == 2


def test_write_failure_no_completed_partial_and_next_task_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failed(*_args: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "fsync", failed)
    with pytest.raises(OSError, match="disk full"):
        write_png(tmp_path, ArchiveImage(1, pixels(inspected()), STAMP, 1))
    assert not list(tmp_path.rglob("*.png")) and not list(tmp_path.rglob("*.part"))
    calls: list[int] = []

    def writer(_root: Path, item: ArchiveImage) -> None:
        calls.append(item.frame.id.cycle_id)
        if len(calls) == 1:
            raise PermissionError("not writable")

    archive = ImageArchive(
        ImageArchiveSettings(str(tmp_path), True),
        1,
        production().layout.camera_ids,
        writer=writer,
    )
    archive.start()
    for cycle in (1, 2):
        result = production(cycle)
        camera = replace(inspected(), frame_id=result.cameras[0].frame_id)
        result = replace(result, cameras=(camera, *result.cameras[1:]))
        archive.begin(result.cycle_id)
        archive.retain(pixels(camera))
        archive.complete(result.cycle_id, result)
    archive.request_stop()
    assert archive.join(3)
    snapshot = archive.snapshot()
    assert calls == [1, 2] and snapshot.failed == snapshot.written == 1
    assert "PermissionError" in snapshot.last_issue and snapshot.retained_bytes == 0


def test_bounded_memory_queue_and_actual_writer_termination(tmp_path: Path) -> None:
    entered, release = Event(), Event()
    owners: list[int] = []

    def blocked(_root: Path, _item: ArchiveImage) -> None:
        owners.append(get_ident())
        entered.set()
        assert release.wait(3)

    result, camera = production(), inspected()
    result = replace(result, cameras=(camera, *result.cameras[1:]))
    frame = pixels(camera)
    archive = ImageArchive(
        ImageArchiveSettings(str(tmp_path), True),
        1,
        result.layout.camera_ids,
        writer=blocked,
        max_bytes=frame.image.nbytes * 2,
        max_frame_bytes=frame.image.nbytes,
        max_pending=1,
        max_cycles=1,
    )
    archive.start()
    try:
        assert archive.begin(result.cycle_id)
        assert not archive.begin(CycleId(2))  # Bounded incomplete cycle state.
        archive.retain(frame)
        archive.retain(frame)  # Never replaces the original.
        archive.complete(result.cycle_id, result)
        assert entered.wait(3)
        second = replace(frame, id=replace(frame.id, cycle_id=CycleId(2)))
        archive.begin(second.id.cycle_id)
        archive.retain(second)
        archive.complete(
            second.id.cycle_id,
            replace(
                result,
                cycle_id=CycleId(2),
                cameras=tuple(
                    replace(c, frame_id=replace(c.frame_id, cycle_id=CycleId(2)))
                    if c.frame_id
                    else c
                    for c in result.cameras
                ),
            ),
        )
        archive.begin(CycleId(3))
        archive.retain(replace(frame, id=replace(frame.id, cycle_id=CycleId(3))))
        archive.retain(
            pixels(result.cameras[1])
        )  # Wrong-cycle frame cannot join cycle 3.
        snapshot = archive.snapshot()
        assert (
            snapshot.retained_bytes <= frame.image.nbytes * 2
            and snapshot.pending == 1
            and snapshot.collecting == 1
        )
        assert snapshot.dropped >= 3 and snapshot.last_issue
        archive.request_stop()
        assert not archive.join(0) and archive.snapshot().alive
    finally:
        release.set()
        archive.request_stop()
        assert archive.join(3)
    assert owners == [owners[0]] and owners[0] != get_ident()
    assert archive.snapshot().retained_bytes == 0
    assert not any(t.name == archive.thread_name for t in threads())


def test_raw_pixel_limits_include_collecting_frames(tmp_path: Path) -> None:
    result = production()
    first, second = (pixels(row) for row in result.cameras[:2])
    for budget, single in (
        (first.image.nbytes, first.image.nbytes),
        (first.image.nbytes * 3, first.image.nbytes - 1),
    ):
        archive = ImageArchive(
            ImageArchiveSettings(str(tmp_path), True),
            1,
            result.layout.camera_ids,
            max_bytes=budget,
            max_frame_bytes=single,
        )
        archive.start()
        try:
            archive.begin(result.cycle_id)
            archive.retain(first)
            archive.retain(second)
            snapshot = archive.snapshot()
            assert snapshot.retained_bytes <= budget
            assert snapshot.dropped == (1 if single == first.image.nbytes else 2)
            assert "capacity" in snapshot.last_issue
        finally:
            archive.request_stop()
            assert archive.join(3) and archive.snapshot().retained_bytes == 0
