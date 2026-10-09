"""Pure Folder selection and schema contracts; no runtime/GPU/Qt event loop."""

import json
from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np
import pytest
from tests.unit.test_production_application import prepared

from vimo_demo.application.folder_replay import (
    FolderReplay,
    FolderSourceError,
    folder_images,
    start_interval_wait,
)
from vimo_demo.configuration import (
    CameraFolder,
    CameraSources,
    ConfigurationError,
    load_camera_sources,
    save_camera_sources,
)
from vimo_demo.inspection import CameraId


def write_image(path: Path, value: int, shape: tuple[int, int] = (20, 60)) -> None:
    ok, data = cv2.imencode(".png", np.full((*shape, 3), value, dtype=np.uint8))
    assert ok
    data.tofile(path)


def folders(base: Path, *, loop: bool = True) -> CameraSources:
    rows: list[CameraFolder] = []
    for index in range(6):
        directory = base / f"independent-{index}"
        directory.mkdir()
        for number in range(1, 3 + index % 2):
            write_image(directory / f"unrelated{number}.png", 10 * index + number)
        rows.append(CameraFolder(CameraId(f"CAM{index + 1}"), directory.name))
    return CameraSources(tuple(reversed(rows)), loop=loop)


def test_persistence_independent_paths_and_camera_order(tmp_path: Path) -> None:
    sources = folders(tmp_path)
    path = tmp_path / "sources.json"
    save_camera_sources(path, sources)
    restored = load_camera_sources(path)
    assert restored == sources
    assert len({r.folder for r in restored.cameras}) == 6
    replay = FolderReplay(prepared(tmp_path).configuration.layout, restored, tmp_path)
    group = replay.next_group()
    assert group is not None
    assert [int(i[0, 0, 0]) for i in group] == [1, 11, 21, 31, 41, 51]
    assert all(not i.flags.writeable for i in group)


def test_natural_order_nonrecursive_and_extensions(tmp_path: Path) -> None:
    for name in (
        "sample10.TIFF",
        "sample2.jpg",
        "sample1.png",
        "sample11.bmp",
        "sample3.jpeg",
        "sample4.tif",
        "ignored.txt",
    ):
        (tmp_path / name).touch()
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "first.png").touch()
    assert [p.name for p in folder_images(tmp_path)] == [
        "sample1.png",
        "sample2.jpg",
        "sample3.jpeg",
        "sample4.tif",
        "sample10.TIFF",
        "sample11.bmp",
    ]


def test_independent_wrap_and_group_exhaustion(tmp_path: Path) -> None:
    sources = folders(tmp_path)
    layout = prepared(tmp_path).configuration.layout
    replay = FolderReplay(layout, sources, tmp_path)
    groups = [replay.next_group() for _ in range(4)]
    assert all(group is not None for group in groups)
    assert [
        [int(i[0, 0, 0]) for i in group] for group in groups if group is not None
    ] == [
        [1, 11, 21, 31, 41, 51],
        [2, 12, 22, 32, 42, 52],
        [1, 13, 21, 33, 41, 53],
        [2, 11, 22, 31, 42, 51],
    ]
    finite = FolderReplay(layout, replace(sources, loop=False), tmp_path)
    assert finite.next_group() is not None and finite.next_group() is not None
    indices = finite.indices.copy()
    assert finite.next_group() is None and finite.indices == indices


@pytest.mark.parametrize("bad", ["empty", "unreadable", "bounds", "resolution"])
def test_source_error_does_not_advance_partial_group(tmp_path: Path, bad: str) -> None:
    sources = folders(tmp_path)
    directory = sources.camera(CameraId("CAM4")).path(tmp_path)
    if bad == "empty":
        for p in directory.iterdir():
            p.unlink()
        with pytest.raises(FolderSourceError, match="CAM4.*empty"):
            FolderReplay(prepared(tmp_path).configuration.layout, sources, tmp_path)
        return
    replay = FolderReplay(prepared(tmp_path).configuration.layout, sources, tmp_path)
    if bad == "unreadable":
        (directory / "unrelated1.png").write_bytes(b"broken")
    elif bad == "bounds":
        write_image(directory / "unrelated1.png", 0, (1, 1))
    else:
        assert replay.next_group() is not None
        write_image(directory / "unrelated2.png", 0, (21, 61))
    before = replay.indices.copy()
    with pytest.raises(FolderSourceError, match="CAM4.*unrelated"):
        replay.next_group()
    assert replay.indices == before


@pytest.mark.parametrize(
    "field,value",
    [("source_type", "basler"), ("folder", 42), ("camera_id", ""), ("obsolete", 1)],
)
def test_strict_source_json(tmp_path: Path, field: str, value: object) -> None:
    row: dict[str, object] = {
        "camera_id": "CAM1",
        "source_type": "folder",
        "folder": "",
    }
    row[field] = value
    path = tmp_path / "source.json"
    path.write_text(
        json.dumps(
            {"schema_version": 1, "cameras": [row], "loop": True, "target_fps": 8}
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError):
        load_camera_sources(path)


def test_camera_identity_and_start_interval_budget() -> None:
    sources = CameraSources((CameraFolder(CameraId("CAM1"), ""),))
    with pytest.raises(ConfigurationError, match="identities"):
        sources.validate_cameras((CameraId("CAM2"),))
    assert start_interval_wait(10, 10.05, 8) == pytest.approx(0.075)
    assert start_interval_wait(10, 10.3, 8) == 0
