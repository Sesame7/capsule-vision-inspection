"""Versionless source persistence and editable drafts; no SDK or Qt initialization."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from capsule_vision.configuration import (
    CameraFolder,
    CameraSources,
    ConfigurationError,
    load_camera_sources,
    save_camera_sources,
)
from capsule_vision.configuration.drafts import CameraSourceDraft
from capsule_vision.inspection import CameraId


def sources() -> CameraSources:
    return CameraSources(
        tuple(
            CameraFolder(
                CameraId(f"CAM{i}"), f"../images/cam{i}", basler_serial=f"100{i}"
            )
            for i in range(6, 0, -1)
        )
    )


def test_folder_load_never_rewrites_until_explicit_save(
    tmp_path: Path,
) -> None:
    path = tmp_path / "sources.json"
    value = {
        "CAM1": {"folder": "../reference"},
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
    assert "schema_version" not in saved and saved.get("mode", "folder") == "folder"
    assert "source_type" not in saved["CAM1"]
    assert load_camera_sources(path) == loaded


@pytest.mark.parametrize("mode", ["folder", "basler"])
def test_versionless_roundtrip_order_keys_and_deterministic_serialization(
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
    assert [c.camera_id for c in definition.cameras] == list(cameras)
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
        "unknown",
        "row_unknown",
        "serial_number",
        "serial_null",
        "serial_space",
        "serial_unicode",
        "mixed",
        "loop",
        "fps",
    ],
)
def test_versionless_rejects_malformed_sources_without_overwriting(
    tmp_path: Path, bad: str
) -> None:
    path = tmp_path / "sources.json"
    save_camera_sources(path, replace(sources(), mode="basler"))
    value = json.loads(path.read_text(encoding="utf-8"))
    row = value["CAM1"]
    if bad == "version":
        value["schema_version"] = 3
    elif bad == "bool_version":
        value["schema_version"] = True
    elif bad == "mode":
        value["mode"] = "mixed"
    elif bad == "unknown":
        value["extra"] = 1
    elif bad == "row_unknown":
        row["extra"] = 1
    elif bad == "serial_number":
        row["basler_serial"] = 1006
    elif bad == "serial_null":
        row["basler_serial"] = None
    elif bad == "serial_space":
        row["basler_serial"] = " 1006 "
    elif bad == "serial_unicode":
        row["basler_serial"] = "１２３"
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


def test_five_sources_roundtrip_and_exact_machine_mapping(tmp_path: Path) -> None:
    original = sources()
    five = replace(original, cameras=original.cameras[:-1], mode="basler")
    path = tmp_path / "sources.json"
    save_camera_sources(path, five)
    assert load_camera_sources(path) == five
    cameras = tuple(row.camera_id for row in five.cameras)
    five.validate_cameras(cameras)
    assert five.basler_serials(tuple(reversed(cameras))) == tuple(
        row.basler_serial for row in reversed(five.cameras)
    )
    with pytest.raises(ConfigurationError):
        five.validate_cameras((*cameras, CameraId("CAM6")))


def test_incomplete_basler_draft_can_be_saved_but_not_connected(tmp_path: Path) -> None:
    path = tmp_path / "sources.json"
    original = CameraSources(
        tuple(CameraFolder(CameraId(f"CAM{i}"), f"folder{i}") for i in range(1, 7))
    )
    save_camera_sources(path, original)
    draft = CameraSourceDraft(original, path)
    draft.set_mode("basler")
    draft.save()
    assert not draft.dirty and load_camera_sources(path) == draft.definition
    with pytest.raises(ConfigurationError, match="distinct"):
        draft.saved.basler_serials(tuple(c.camera_id for c in original.cameras))
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


def test_omitted_source_fields_use_defaults_and_present_invalid_values_fail(
    tmp_path: Path,
) -> None:
    path = tmp_path / "sources.json"
    path.write_text('{"CAM1":{}}', encoding="utf-8")
    definition = load_camera_sources(path)
    assert definition == CameraSources((CameraFolder(CameraId("CAM1")),))
    save_camera_sources(path, definition)
    assert json.loads(path.read_text(encoding="utf-8")) == {"CAM1": {}}
    for key, value in (
        ("mode", None),
        ("loop", None),
        ("target_fps", None),
        ("target_fps", 0),
        ("target_fps", float("inf")),
    ):
        path.write_text(
            json.dumps({"CAM1": {}, key: value}),
            encoding="utf-8",
        )
        with pytest.raises(ConfigurationError):
            load_camera_sources(path)


@pytest.mark.parametrize("mapping", ["missing", "empty", "duplicate", "four"])
def test_saved_incomplete_or_ambiguous_mapping_is_rejected_at_connection(
    tmp_path: Path, mapping: str
) -> None:
    original = replace(sources(), mode="basler")
    path = tmp_path / "sources.json"
    save_camera_sources(path, original)
    value = json.loads(path.read_text(encoding="utf-8"))
    if mapping == "missing":
        del value["CAM1"]["basler_serial"]
    elif mapping == "empty":
        value["CAM1"]["basler_serial"] = ""
    elif mapping == "duplicate":
        value["CAM1"]["basler_serial"] = value["CAM2"]["basler_serial"]
    else:
        value = {k: v for k, v in value.items() if k not in ("CAM5", "CAM6")}
    path.write_text(json.dumps(value), encoding="utf-8")
    definition = load_camera_sources(path)
    save_camera_sources(path, definition)
    assert load_camera_sources(path) == definition
    with pytest.raises(ConfigurationError):
        definition.basler_serials(tuple(row.camera_id for row in original.cameras))


def test_empty_and_sparse_camera_objects_remain_incomplete_until_explicit_edit(
    tmp_path: Path,
) -> None:
    path = tmp_path / "camera_sources.json"
    path.write_text('{"mode":"basler"}', encoding="utf-8")
    loaded = load_camera_sources(path)
    original = path.read_bytes()
    draft = CameraSourceDraft(loaded, path)
    assert loaded.cameras == () and not draft.dirty
    assert loaded.camera(CameraId("CAM3")) == CameraFolder(CameraId("CAM3"))
    assert loaded.cameras == () and path.read_bytes() == original
    draft.set_folder(CameraId("CAM3"), "../images/three")
    draft.set_serial(CameraId("CAM3"), "3003")
    draft.set_mode("folder")
    draft.save()
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "CAM3": {"folder": "../images/three", "basler_serial": "3003"}
    }
    draft.set_mode("basler")
    draft.save()
    assert (
        load_camera_sources(path).camera(CameraId("CAM3")).folder == "../images/three"
    )
    cameras = tuple(CameraId(f"CAM{i}") for i in range(1, 7))
    draft.saved.validate_cameras(cameras, complete=False)
    with pytest.raises(ConfigurationError):
        draft.saved.basler_serials(cameras)
    draft.set_folder(CameraId("CAM3"), "temporary")
    draft.discard()
    assert draft.definition.camera(CameraId("CAM3")).folder == "../images/three"


@pytest.mark.parametrize(
    "raw",
    [
        '{"cameras":[]}',
        '{"cameras":null}',
        '{"cameras":{}}',
        '{"cameras":{"CAM1":{}}}',
        '{"CAM0":{}}',
        '{"CAM7":{}}',
        '{"cam1":{}}',
        '{"CAM01":{}}',
        '{"CAM1":{"camera_id":"CAM2"}}',
        '{"CAM1":{},"CAM1":{}}',
        '{"CAM1":null}',
        '{"CAM1":{"folder":null}}',
    ],
)
def test_camera_keyed_json_rejects_arrays_unknown_identities_and_invalid_rows(
    tmp_path: Path, raw: str
) -> None:
    path = tmp_path / "camera_sources.json"
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_camera_sources(path)
    assert path.read_text(encoding="utf-8") == raw


@pytest.mark.parametrize("present", [False, True])
def test_first_source_save_does_not_replace_invalid_or_new_external_file(
    tmp_path: Path, present: bool
) -> None:
    path = tmp_path / "data" / "camera_sources.json"
    path.parent.mkdir()
    invalid = '{"cameras":{}}'
    if present:
        path.write_text(invalid, encoding="utf-8")
    draft = CameraSourceDraft(None, path)
    draft.set_folder(CameraId("CAM1"), "../images/cam1")
    if not present:
        save_camera_sources(path, CameraSources())
    original = path.read_bytes()
    with pytest.raises(ConfigurationError):
        draft.save()
    assert path.read_bytes() == original and draft.dirty and not draft.persisted
