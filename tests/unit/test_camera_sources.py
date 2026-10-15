"""Strict v2 source persistence and editable drafts; no SDK or Qt initialization."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from vimo_demo.configuration import (
    CameraFolder,
    CameraSources,
    ConfigurationError,
    load_camera_sources,
    save_camera_sources,
)
from vimo_demo.inspection import CameraId
from vimo_demo.ui.camera_source_editor import CameraSourceDraft


def sources() -> CameraSources:
    return CameraSources(
        tuple(
            CameraFolder(
                CameraId(f"CAM{i}"), f"../images/cam{i}", basler_serial=f"100{i}"
            )
            for i in range(6, 0, -1)
        )
    )


def test_v1_read_is_folder_and_never_rewrites_until_explicit_save(
    tmp_path: Path,
) -> None:
    path = tmp_path / "sources.json"
    value = {
        "schema_version": 1,
        "cameras": [
            {"camera_id": "CAM1", "folder": "../reference", "source_type": "folder"}
        ],
        "loop": False,
        "target_fps": 3.5,
    }
    original = json.dumps(value, indent=3).encode()
    path.write_bytes(original)
    loaded = load_camera_sources(path)
    assert (
        loaded.mode == "folder" and loaded.camera(CameraId("CAM1")).basler_serial == ""
    )
    assert not loaded.loop and loaded.target_fps == 3.5
    draft = CameraSourceDraft(loaded, path)
    assert not draft.dirty and path.read_bytes() == original
    draft.save()
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["schema_version"] == 2 and saved["mode"] == "folder"
    assert "source_type" not in saved["cameras"][0]
    assert load_camera_sources(path) == loaded


@pytest.mark.parametrize("mode", ["folder", "basler"])
def test_v2_roundtrip_order_keys_and_deterministic_serialization(
    tmp_path: Path, mode: str
) -> None:
    definition = replace(sources(), mode="basler" if mode == "basler" else "folder")
    path = tmp_path / "sources.json"
    save_camera_sources(path, definition)
    original = path.read_bytes()
    assert load_camera_sources(path) == definition
    save_camera_sources(path, definition)
    assert path.read_bytes() == original
    cameras = tuple(CameraId(f"CAM{i}") for i in range(1, 7))
    definition.validate_cameras(cameras)
    assert [c.camera_id for c in definition.cameras] == list(reversed(cameras))
    row = definition.camera(cameras[0])
    assert row.path(tmp_path) == tmp_path / "../images/cam1"
    assert definition.source_key(cameras[0], tmp_path) == (
        "basler:1001" if mode == "basler" else row.key(tmp_path)
    )
    if mode == "basler":
        assert definition.basler_serials(cameras) == tuple(
            f"100{i}" for i in range(1, 7)
        )


@pytest.mark.parametrize(
    "bad",
    [
        "version",
        "bool_version",
        "mode",
        "missing_mode",
        "unknown",
        "row_unknown",
        "missing_serial",
        "serial_number",
        "serial_null",
        "serial_space",
        "serial_unicode",
        "duplicate",
        "empty",
        "five",
        "mixed",
        "loop",
        "fps",
    ],
)
def test_v2_rejects_malformed_sources_without_overwriting(
    tmp_path: Path, bad: str
) -> None:
    path = tmp_path / "sources.json"
    save_camera_sources(path, replace(sources(), mode="basler"))
    value = json.loads(path.read_text(encoding="utf-8"))
    row = value["cameras"][0]
    if bad == "version":
        value["schema_version"] = 3
    elif bad == "bool_version":
        value["schema_version"] = True
    elif bad == "mode":
        value["mode"] = "mixed"
    elif bad == "missing_mode":
        del value["mode"]
    elif bad == "unknown":
        value["extra"] = 1
    elif bad == "row_unknown":
        row["extra"] = 1
    elif bad == "missing_serial":
        del row["basler_serial"]
    elif bad == "serial_number":
        row["basler_serial"] = 1006
    elif bad == "serial_null":
        row["basler_serial"] = None
    elif bad == "serial_space":
        row["basler_serial"] = " 1006 "
    elif bad == "serial_unicode":
        row["basler_serial"] = "１２３"
    elif bad == "duplicate":
        row["basler_serial"] = value["cameras"][1]["basler_serial"]
    elif bad == "empty":
        row["basler_serial"] = ""
    elif bad == "five":
        value["cameras"].pop()
    elif bad == "mixed":
        row["source_type"] = "folder"
    elif bad == "loop":
        value["loop"] = 1
    elif bad == "fps":
        value["target_fps"] = True
    original = json.dumps(value).encode()
    path.write_bytes(original)
    with pytest.raises(ConfigurationError):
        load_camera_sources(path)
    assert path.read_bytes() == original


def test_incomplete_basler_draft_can_be_edited_but_not_saved(tmp_path: Path) -> None:
    path = tmp_path / "sources.json"
    original = CameraSources(
        tuple(CameraFolder(CameraId(f"CAM{i}"), f"folder{i}") for i in range(1, 7))
    )
    save_camera_sources(path, original)
    data = path.read_bytes()
    draft = CameraSourceDraft(original, path)
    draft.set_mode("basler")
    with pytest.raises(ConfigurationError, match="six distinct"):
        draft.save()
    assert path.read_bytes() == data and draft.saved == original
    for i in range(1, 7):
        draft.set_serial(CameraId(f"CAM{i}"), f"100{i}")
    draft.set_folder(CameraId("CAM2"), "new-reference")
    draft.set_loop(False)
    modified = draft.definition
    draft.set_mode("folder")
    draft.set_mode("basler")
    assert draft.definition == modified and draft.dirty
    draft.save()
    assert not draft.dirty and load_camera_sources(path) == modified
    draft.set_serial(CameraId("CAM1"), "999")
    draft.set_mode("folder")
    draft.discard()
    assert not draft.dirty and draft.definition == modified
