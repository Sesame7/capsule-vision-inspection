"""Persistent discovery/preparation with fake artifacts, never GPU inference."""

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from tests.application_support import WAIT
from tests.bootstrap_support import saved_data

from vimo_demo.application import ApplicationState, ProductionCounters
from vimo_demo.application.preferences import (
    ApplicationPreferences,
    load_preferences,
    read_preferences,
    save_preferences,
)
from vimo_demo.configuration import (
    CameraFolder,
    CameraSources,
    ConfigurationError,
    ConfigurationIOError,
    RecipeDefinition,
    load_model_definition,
    save_camera_sources,
    save_model_definition,
    save_recipe,
)
from vimo_demo.inspection import CameraId
from vimo_demo.ui.bootstrap import (
    ApplicationData,
    Startup,
    discover_models,
    load_startup,
)


def close(startup: Startup) -> None:
    startup.application.close().result(WAIT)
    assert startup.application.join(WAIT)


def test_explicit_identity_override_anchor_and_no_startup_inference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = saved_data(tmp_path)
    model = load_model_definition(data.models / "general.json")
    shutil.copyfile(data.models / "general.vimosln", data.models / "other.vimosln")
    save_model_definition(
        data.models / "other.json",
        replace(
            model, model_id="other", display_name="Other", model_path="other.vimosln"
        ),
    )
    save_recipe(
        data.recipes / "aaa.recipe.json", RecipeDefinition("aaa", "First sorted")
    )
    save_recipe(
        data.recipes / "selected.recipe.json",
        RecipeDefinition("selected", "Selected", model_override_id="other"),
    )
    save_preferences(
        data.preferences, ApplicationPreferences("general", "selected", "en")
    )
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.chdir(other)
    calls: list[object] = []

    def forbidden(config: object):
        calls.append(config)
        raise AssertionError("Engine creation at startup")

    startup = load_startup(data, engine_factory=forbidden)
    try:
        assert not startup.issues
        assert startup.choice is not None and startup.editor is not None
        assert startup.choice.prepared.model_path == data.models / "other.vimosln"
        assert startup.choice.prepared.configuration.recipe_id == "selected"
        assert (
            startup.editor.entry is not None
            and startup.editor.entry.recipe.recipe_id == "selected"
        )
        assert len(startup.editor.registry.models) == 2
        snapshot = startup.application.snapshot()
        assert snapshot.state is ApplicationState.READY
        assert snapshot.active is None and snapshot.generation == 0
        assert snapshot.batch_id is None and snapshot.counters == ProductionCounters()
        assert snapshot.latest_production_result is None and not snapshot.runtime_owned
        assert not calls and not startup.frames.snapshot().frames
        assert ApplicationData.at_entry(tmp_path) == data
    finally:
        close(startup)


@pytest.mark.parametrize(
    "kind",
    [
        "model_missing",
        "model_invalid",
        "model_duplicate",
        "recipe_missing",
        "recipe_duplicate",
        "recipe_invalid",
        "machine_missing",
        "machine_invalid",
        "sources_missing",
        "sources_invalid",
        "artifact_missing",
        "manifest_missing",
        "default_unknown",
        "recipe_unknown",
        "sources_wrong_cameras",
        "hash_mismatch",
        "manifest_invalid",
        "default_missing",
        "recipe_unselected",
    ],
)
def test_unavailable_never_falls_back(tmp_path: Path, kind: str) -> None:
    data = saved_data(tmp_path)
    if kind == "model_missing":
        (data.models / "general.json").unlink()
    elif kind == "model_duplicate":
        shutil.copyfile(data.models / "general.json", data.models / "duplicate.json")
    elif kind == "recipe_duplicate":
        shutil.copyfile(
            data.recipes / "local.recipe.json", data.recipes / "duplicate.recipe.json"
        )
    elif kind == "recipe_missing":
        save_recipe(
            data.recipes / "other.recipe.json",
            RecipeDefinition("other", "Available but not selected"),
        )
        (data.recipes / "local.recipe.json").unlink()
    elif kind == "hash_mismatch":
        (data.models / "general.vimosln").write_bytes(b"modified artifact")
    elif kind == "sources_wrong_cameras":
        save_camera_sources(
            data.sources,
            CameraSources(
                tuple(
                    CameraFolder(CameraId(f"OTHER{i}"), "../../frames")
                    for i in range(1, 7)
                )
            ),
        )
    elif kind == "artifact_missing":
        (data.models / "general.vimosln").unlink()
    elif kind == "default_unknown":
        save_preferences(data.preferences, ApplicationPreferences("unknown", "local"))
    elif kind == "recipe_unknown":
        save_preferences(data.preferences, ApplicationPreferences("general", "unknown"))
    elif kind == "default_missing":
        save_preferences(data.preferences, ApplicationPreferences(None, "local"))
    elif kind == "recipe_unselected":
        save_preferences(data.preferences, ApplicationPreferences("general", None))
    else:
        path = {
            "model_invalid": data.models / "general.json",
            "recipe_invalid": data.recipes / "local.recipe.json",
            "machine_missing": data.machine,
            "machine_invalid": data.machine,
            "sources_missing": data.sources,
            "sources_invalid": data.sources,
            "manifest_invalid": data.preferences,
            "manifest_missing": data.preferences,
        }[kind]
        if kind.endswith("missing"):
            path.unlink()
        else:
            path.write_text("{}", encoding="utf-8")
    before = {p: p.read_bytes() for p in data.root.rglob("*.json")}
    startup = load_startup(data)
    try:
        assert startup.issues and startup.choice is None
        assert startup.application.snapshot().state is ApplicationState.UNCONFIGURED
        assert {p: p.read_bytes() for p in before} == before
        if kind in ("recipe_unselected", "recipe_missing", "recipe_duplicate"):
            assert startup.editor is not None and startup.editor.entry is None
    finally:
        close(startup)


def test_missing_data_does_not_generate_configurations(tmp_path: Path) -> None:
    data = ApplicationData.at_entry(tmp_path)
    startup = load_startup(data)
    try:
        assert startup.application.snapshot().state is ApplicationState.UNCONFIGURED
        assert startup.machine is startup.editor is startup.choice is None
        assert startup.application.history is not None
        assert not startup.application.history.query().result(WAIT).records
        assert set(tmp_path.rglob("*")) <= {
            data.root,
            data.history,
            Path(f"{data.history}-wal"),
            Path(f"{data.history}-shm"),
        }
    finally:
        close(startup)


def test_discovery_is_nonrecursive_and_preserves_machine_sources(
    tmp_path: Path,
) -> None:
    data = saved_data(tmp_path)
    nested = data.models / "archive"
    nested.mkdir()
    (nested / "invalid.json").write_text("{}")
    (data.recipes / "unrelated.json").write_text("{}")
    assert len(discover_models(data.models).models) == 1
    first, second = load_startup(data), load_startup(data)
    try:
        assert first.machine == second.machine
        assert first.editor is not None and second.editor is not None
        assert (
            first.editor.camera_sources is not None
            and second.editor.camera_sources is not None
        )
        assert first.editor.camera_sources.saved == second.editor.camera_sources.saved
        assert first.editor.camera_sources.saved.target_fps == 3.5
        assert not first.editor.camera_sources.saved.loop
        assert first.frames.cameras == tuple(f"CAM{i}" for i in range(1, 7))
    finally:
        close(first)
        close(second)


def test_preferences_failed_atomic_replace_retains_previous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "app.json"
    original = ApplicationPreferences("general", "local", "zh")
    save_preferences(path, original)

    def fail(_source: object, _target: object) -> None:
        raise OSError("replace denied")

    monkeypatch.setattr("vimo_demo.application.preferences.os.replace", fail)
    with pytest.raises(ConfigurationIOError, match="replace denied"):
        save_preferences(path, replace(original, language="en"))
    assert load_preferences(path) == original
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize(
    "updates",
    [
        {"default_model_id": 1},
        {"last_applied_recipe_id": ""},
        {"extra": True},
        {"schema_version": True},
    ],
)
def test_preferences_strict_fields(tmp_path: Path, updates: dict[str, object]) -> None:
    path = tmp_path / "app.json"
    save_preferences(path, ApplicationPreferences("general", "local"))
    value = json.loads(path.read_text(encoding="utf-8"))
    value.update(updates)
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_preferences(path)


@pytest.mark.parametrize(
    "language", ["omitted", "bad", None, 123, True, ["en"], {"en": True}]
)
def test_language_fallback_preserves_explicit_identities_and_files(
    tmp_path: Path, language: object
) -> None:
    data = saved_data(tmp_path)
    value = json.loads(data.preferences.read_text(encoding="utf-8"))
    if language == "omitted":
        value.pop("language")
    else:
        value["language"] = language
    data.preferences.write_text(json.dumps(value), encoding="utf-8")
    before = data.preferences.read_bytes()
    loaded = read_preferences(data.preferences)
    assert loaded.preferences == ApplicationPreferences("general", "local", "zh")
    assert loaded.invalid_language == (language != "omitted")
    startup = load_startup(data)
    try:
        assert startup.application.snapshot().state is ApplicationState.READY
        assert startup.choice is not None
        assert startup.choice.prepared.configuration.recipe_id == "local"
        assert startup.preferences == loaded.preferences
        assert startup.preferences_writable == (language == "omitted")
        assert all(not issue.blocking for issue in startup.issues)
        assert data.preferences.read_bytes() == before
    finally:
        close(startup)


@pytest.mark.parametrize(
    "contents",
    [
        "{",
        "[]",
        "{}",
        '{"schema_version":2,"default_model_id":"general","last_applied_recipe_id":"local"}',
        '{"schema_version":1,"default_model_id":"general","default_model_id":"other","last_applied_recipe_id":"local"}',
        '{"schema_version":1,"last_applied_recipe_id":"local"}',
    ],
)
def test_unreliable_manifest_never_guesses_and_external_repair_recovers(
    tmp_path: Path, contents: str
) -> None:
    data = saved_data(tmp_path)
    original = data.preferences.read_bytes()
    data.preferences.write_text(contents, encoding="utf-8")
    broken = data.preferences.read_bytes()
    startup = load_startup(data)
    try:
        assert startup.preferences is None and not startup.preferences_writable
        assert startup.application.snapshot().state is ApplicationState.UNCONFIGURED
        assert startup.choice is None and startup.editor is None
        assert any(issue.key == "startup_preferences" for issue in startup.issues)
        assert data.preferences.read_bytes() == broken
    finally:
        close(startup)
    data.preferences.write_bytes(original)  # Maintenance repair, not application IO.
    restored = load_startup(data)
    try:
        assert not restored.issues
        assert restored.application.snapshot().state is ApplicationState.READY
    finally:
        close(restored)


@pytest.mark.parametrize(
    "kind",
    [
        "manifest",
        "machine",
        "sources",
        "model",
        "recipe",
        "artifact",
        "models_directory",
        "recipes_directory",
        "folder",
    ],
)
def test_permission_failures_keep_shell_and_original_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    data = saved_data(tmp_path)
    target = {
        "manifest": data.preferences,
        "machine": data.machine,
        "sources": data.sources,
        "model": data.models / "general.json",
        "recipe": data.recipes / "local.recipe.json",
        "artifact": data.models / "general.vimosln",
        "models_directory": data.models,
        "recipes_directory": data.recipes,
        "folder": tmp_path / "frames",
    }[kind]
    before = {p: p.read_bytes() for p in data.root.rglob("*") if p.is_file()}
    with monkeypatch.context() as patch:
        if kind.endswith("directory") or kind == "folder":
            original_iterdir = Path.iterdir

            def denied_iterdir(path: Path):
                if path.resolve() == target:
                    raise PermissionError("test directory denied")
                return original_iterdir(path)

            patch.setattr(Path, "iterdir", denied_iterdir)
        else:
            original_open = Path.open

            def denied_open(
                path: Path,
                mode: str = "r",
                buffering: int = -1,
                encoding: str | None = None,
                errors: str | None = None,
                newline: str | None = None,
            ):
                if path == target:
                    raise PermissionError("test file denied")
                return original_open(path, mode, buffering, encoding, errors, newline)

            patch.setattr(Path, "open", denied_open)
        startup = load_startup(data)
        try:
            expected = (
                ApplicationState.READY
                if kind == "folder"
                else ApplicationState.UNCONFIGURED
            )
            assert startup.application.snapshot().state is expected
            assert any("denied" in issue.detail for issue in startup.issues)
            context_path = (
                data.sources.parent / "../../frames" if kind == "folder" else target
            )
            assert any(str(context_path) in issue.detail for issue in startup.issues)
            if kind == "manifest":
                assert not startup.preferences_writable
        finally:
            close(startup)
    assert {p: p.read_bytes() for p in before} == before
    repaired = load_startup(data)
    try:
        assert repaired.application.snapshot().state is ApplicationState.READY
        assert not repaired.issues
    finally:
        close(repaired)


@pytest.mark.parametrize("invalid", [False, True])
def test_recipe_defaults_only_for_omitted_optional_fields(
    tmp_path: Path, invalid: bool
) -> None:
    data = saved_data(tmp_path)
    value: dict[str, object] = {
        "schema_version": 1,
        "recipe_id": "local",
        "display_name": "Local test",
        "defaults": {},
    }
    if invalid:
        value["capsule"] = {"size": {"enabled": "yes"}}
    path = data.recipes / "local.recipe.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    before = path.read_bytes()
    startup = load_startup(data)
    try:
        assert startup.application.snapshot().state is (
            ApplicationState.UNCONFIGURED if invalid else ApplicationState.READY
        )
        assert path.read_bytes() == before
    finally:
        close(startup)


@pytest.mark.parametrize("legacy", [False, True])
def test_source_restore_keeps_file_bytes_and_does_not_load_optional_sdk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, legacy: bool
) -> None:
    import sys

    from vimo_demo.application.local_replay import SourceControlCommands
    from vimo_demo.configuration import load_camera_sources

    data = saved_data(tmp_path)
    if legacy:
        value = json.loads(data.sources.read_text(encoding="utf-8"))
        value["schema_version"] = 1
        del value["mode"]
        for row in value["cameras"]:
            del row["basler_serial"]
            row["source_type"] = "folder"
        data.sources.write_text(json.dumps(value, indent=3), encoding="utf-8")
    else:
        source = load_camera_sources(data.sources)
        save_camera_sources(
            data.sources,
            replace(
                source,
                mode="basler",
                cameras=tuple(
                    replace(c, basler_serial=f"100{i}")
                    for i, c in enumerate(source.cameras, 1)
                ),
            ),
        )
    original = data.sources.read_bytes()
    monkeypatch.setitem(sys.modules, "pypylon", None)
    startup = load_startup(data)
    try:
        assert startup.application.snapshot().state is ApplicationState.READY
        assert not startup.issues and startup.editor is not None
        assert startup.editor.camera_sources is not None
        assert startup.editor.camera_sources.saved.mode == (
            "folder" if legacy else "basler"
        )
        assert isinstance(startup.application, SourceControlCommands)
        assert startup.application.control_available
        assert startup.application.control_snapshot() is None
        assert data.sources.read_bytes() == original
        assert "pypylon.pylon" not in sys.modules
        assert startup.application.snapshot().counters == ProductionCounters()
    finally:
        close(startup)
    assert data.sources.read_bytes() == original
