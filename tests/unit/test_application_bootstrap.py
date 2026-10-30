"""Persistent discovery/preparation with fake artifacts, never GPU inference."""

import json
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from tests.support.application import WAIT
from tests.support.startup import close, saved_data

from capsule_vision.application import (
    ApplicationState,
    ApplicationStateError,
    ProductionCounters,
)
from capsule_vision.application.startup import ApplicationData, load_startup
from capsule_vision.configuration import (
    ConfigurationError,
    ConfigurationIOError,
    RecipeDefinition,
    load_model_definition,
    load_recipe,
    save_camera_sources,
    save_model_definition,
    save_recipe,
)
from capsule_vision.configuration.catalog import discover_models
from capsule_vision.configuration.preferences import (
    ApplicationPreferences,
    load_preferences,
    read_preferences,
    save_preferences,
)
from capsule_vision.inspection import CameraId


@pytest.mark.parametrize(
    "environment_rules,file_rules,font_info,other_info",
    [
        (None, None, False, True),
        ("qt.text.font.db.info=true", None, True, True),
        (None, "qt.text.font.db.info=true", True, True),
        ("qt.text.font.db.info=false", "qt.text.font.db.info=true", False, True),
        ("font.f1.other.info=false", None, False, False),
        (None, "font.f1.other.info=false", False, False),
    ],
)
def test_gui_startup_logging_rule_and_external_precedence(
    tmp_path: Path,
    environment_rules: str | None,
    file_rules: str | None,
    font_info: bool,
    other_info: bool,
) -> None:
    # A fresh registry per case avoids changing the test process's logging owner.
    env = dict(os.environ)
    env.pop("QT_LOGGING_RULES", None)
    env.pop("QT_LOGGING_CONF", None)
    if environment_rules is not None:
        env["QT_LOGGING_RULES"] = environment_rules
    if file_rules is not None:
        config = tmp_path / "qtlogging.ini"
        config.write_text("[Rules]\n" + file_rules + "\n", encoding="utf-8")
        env["QT_LOGGING_CONF"] = str(config)
    probe = """
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from PySide6 import QtCore
from capsule_vision.ui import bootstrap

font = QtCore.QLoggingCategory("qt.text.font.db")
other = QtCore.QLoggingCategory("font.f1.other")
neighbor = QtCore.QLoggingCategory("qt.text.font.db.other")
levels = (QtCore.QtMsgType.QtDebugMsg, QtCore.QtMsgType.QtWarningMsg,
          QtCore.QtMsgType.QtCriticalMsg, QtCore.QtMsgType.QtFatalMsg)
before = [font.isEnabled(level) for level in levels]
neighbor_before = neighbor.isInfoEnabled()

def at_qapplication(_arguments):
    assert font.isInfoEnabled() is FONT_INFO
    assert other.isInfoEnabled() is OTHER_INFO
    assert [font.isEnabled(level) for level in levels] == before
    assert font.isWarningEnabled() and font.isCriticalEnabled()
    assert font.isEnabled(QtCore.QtMsgType.QtFatalMsg)
    assert neighbor.isInfoEnabled() == neighbor_before
    assert QtCore.QLoggingCategory("default").isInfoEnabled()
    raise SystemExit(0)

from capsule_vision.application.composition import unconfigured_application
owner = unconfigured_application()
with patch.object(bootstrap, "load_startup", return_value=SimpleNamespace(issues=(), application=owner)):
    with patch.object(bootstrap, "QApplication", side_effect=at_qapplication):
        with patch.object(QtCore, "qInstallMessageHandler", side_effect=AssertionError):
            bootstrap.main(Path.cwd(), [])
""".replace("FONT_INFO", repr(font_info)).replace("OTHER_INFO", repr(other_info))
    result = subprocess.run(
        [sys.executable, "-c", probe],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_explicit_identity_override_anchor_and_no_startup_inference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = saved_data(tmp_path)
    model = load_model_definition(data.models / "general" / "model.json")
    (data.models / "other").mkdir()
    shutil.copyfile(
        data.models / "general" / "model.vimosln",
        data.models / "other" / "model.vimosln",
    )
    save_model_definition(
        data.models / "other" / "model.json",
        replace(
            model, model_id="other", display_name="Other", model_path="model.vimosln"
        ),
    )
    save_recipe(data.recipes / "aaa.json", RecipeDefinition("aaa", "First sorted"))
    save_recipe(
        data.recipes / "selected.json",
        RecipeDefinition("selected", "Selected", model_id="other"),
    )
    save_preferences(data.preferences, ApplicationPreferences("selected", "en"))
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
        assert startup.prepared is not None and startup.editor is not None
        assert startup.prepared.model_path == data.models / "other" / "model.vimosln"
        assert startup.prepared.configuration.recipe_id == "selected"
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
        "model_unreadable_duplicate",
        "recipe_missing",
        "recipe_duplicate",
        "recipe_invalid",
        "machine_missing",
        "machine_invalid",
        "sources_missing",
        "sources_invalid",
        "artifact_missing",
        "manifest_missing",
        "recipe_model_unknown",
        "recipe_unknown",
        "sources_wrong_cameras",
        "hash_mismatch",
        "manifest_invalid",
        "recipe_model_missing",
        "recipe_unselected",
    ],
)
def test_unavailable_never_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    data = saved_data(tmp_path)
    if kind == "model_missing":
        (data.models / "general" / "model.json").unlink()
    elif kind in ("model_duplicate", "model_unreadable_duplicate", "recipe_duplicate"):
        original_iterdir = Path.iterdir
        directory = data.recipes if kind == "recipe_duplicate" else data.models
        alias = directory / ("LOCAL.json" if kind == "recipe_duplicate" else "GENERAL")

        def ambiguous(path: Path):
            entries = list(original_iterdir(path))
            return iter([*entries, alias] if path == directory else entries)

        monkeypatch.setattr(Path, "iterdir", ambiguous)
        if kind == "model_unreadable_duplicate":
            original_stat = Path.stat

            def unreadable(path: Path, *, follow_symlinks: bool = True):
                if path.name == "GENERAL":
                    raise PermissionError("case-colliding Model directory denied")
                return original_stat(path, follow_symlinks=follow_symlinks)

            monkeypatch.setattr(Path, "stat", unreadable)
    elif kind == "recipe_missing":
        save_recipe(
            data.recipes / "other.json",
            RecipeDefinition("other", "Available but not selected"),
        )
        (data.recipes / "local.json").unlink()
    elif kind == "hash_mismatch":
        (data.models / "general" / "model.vimosln").write_bytes(b"modified artifact")
    elif kind == "sources_wrong_cameras":
        data.sources.write_text('{"OTHER1":{"folder":"../frames"}}', encoding="utf-8")
    elif kind == "artifact_missing":
        (data.models / "general" / "model.vimosln").unlink()
    elif kind == "recipe_model_unknown":
        recipe_path = data.recipes / "local.json"
        save_recipe(recipe_path, replace(load_recipe(recipe_path), model_id="unknown"))
    elif kind == "recipe_unknown":
        save_preferences(data.preferences, ApplicationPreferences("unknown"))
    elif kind == "recipe_model_missing":
        recipe_path = data.recipes / "local.json"
        save_recipe(recipe_path, replace(load_recipe(recipe_path), model_id=None))
    elif kind == "recipe_unselected":
        save_preferences(data.preferences, ApplicationPreferences(None))
    else:
        path = {
            "model_invalid": data.models / "general" / "model.json",
            "recipe_invalid": data.recipes / "local.json",
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
            path.write_text(
                '{"cameras":{}}' if kind == "sources_invalid" else "{}",
                encoding="utf-8",
            )
    before = {p: p.read_bytes() for p in data.root.rglob("*.json")}
    startup = load_startup(data)
    try:
        assert startup.issues and startup.prepared is None
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
        assert startup.machine is startup.prepared is None
        assert (
            startup.editor is not None and not startup.editor.machine_editor.persisted
        )
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


def test_startup_does_not_fall_back_to_superseded_configuration_paths(
    tmp_path: Path,
) -> None:
    data = saved_data(tmp_path)
    originals: list[tuple[Path, bytes]] = []
    for current, obsolete in (
        (data.machine, data.root / "machine" / "machine.json"),
        (data.sources, data.root / "sources" / "camera_sources.json"),
    ):
        obsolete.parent.mkdir(parents=True, exist_ok=True)
        payload = current.read_bytes()
        obsolete.write_bytes(payload)
        current.unlink()
        originals.append((obsolete, payload))
    startup = load_startup(data)
    try:
        assert startup.application.snapshot().state is ApplicationState.UNCONFIGURED
        assert startup.machine is None and startup.prepared is None
        assert not data.machine.exists() and not data.sources.exists()
        setup = [i for i in startup.issues if i.key == "camera_setup"]
        assert len(setup) == 2
        assert any(str(data.machine) in i.detail for i in setup)
        assert any(str(data.sources) in i.detail for i in setup)
        assert all(path.read_bytes() == payload for path, payload in originals)
    finally:
        close(startup)


@pytest.mark.parametrize("mode", ["folder", "basler"])
def test_missing_camera_entries_can_be_edited_but_cannot_start_sources(
    tmp_path: Path, mode: str
) -> None:
    from capsule_vision.application import ApplicationError
    from capsule_vision.application.basler_connection import SourceConnectionCommands
    from capsule_vision.configuration import load_camera_sources

    data = saved_data(tmp_path)
    assert data.machine == data.root / "machine.tsv"
    assert data.sources == data.root / "camera_sources.json"
    sources = load_camera_sources(data.sources)
    sources = replace(
        sources,
        mode="basler" if mode == "basler" else "folder",
        cameras=tuple(
            replace(c, basler_serial=f"100{i}")
            for i, c in enumerate(sources.cameras, 1)
            if c.camera_id != "CAM3"
        ),
    )
    save_camera_sources(data.sources, sources)
    original = data.sources.read_bytes()
    calls: list[object] = []

    def forbidden(value: object):
        calls.append(value)
        raise AssertionError("Incomplete source must fail before engine creation")

    startup = load_startup(data, engine_factory=forbidden)
    try:
        assert startup.editor is not None and startup.editor.camera_sources is not None
        draft = startup.editor.camera_sources
        assert draft.saved == sources and not draft.dirty
        assert draft.definition.camera(CameraId("CAM3")).folder == ""
        assert len(draft.definition.cameras) == 5
        assert isinstance(startup.application, SourceConnectionCommands)
        if mode == "folder":
            assert startup.application.snapshot().state is ApplicationState.READY
            assert any(
                i.key == "startup_source_unavailable" and not i.blocking
                for i in startup.issues
            )
            assert startup.editor.entry is not None
            startup.editor.prepare(startup.editor.entry.recipe)
            with pytest.raises(ApplicationError):
                startup.application.start().result(WAIT)
        else:
            assert startup.application.snapshot().state is ApplicationState.UNCONFIGURED
            with pytest.raises(ConfigurationError):
                startup.application.connect_source()
            assert startup.editor.entry is not None
            with pytest.raises(ConfigurationError):
                startup.editor.prepare(startup.editor.entry.recipe)
        assert data.sources.read_bytes() == original and not calls
        assert startup.application.snapshot().active is None
        assert startup.application.snapshot().counters == ProductionCounters()
        draft.set_folder(CameraId("CAM3"), "../frames")
        draft.set_serial(CameraId("CAM3"), "1003")
        draft.save()
        assert len(load_camera_sources(data.sources).cameras) == 6
    finally:
        close(startup)


@pytest.mark.parametrize("missing", [False, True])
def test_optional_preferences_leave_selection_empty_and_allow_recipe_save(
    tmp_path: Path, missing: bool
) -> None:
    data = saved_data(tmp_path)
    if missing:
        data.preferences.unlink()
    else:
        data.preferences.write_text("{}", encoding="utf-8")
    assert read_preferences(data.preferences).preferences == ApplicationPreferences()
    startup = load_startup(data)
    try:
        assert startup.preferences == ApplicationPreferences()
        assert startup.preferences_writable
        assert not any(i.key == "startup_preferences" for i in startup.issues)
        assert startup.prepared is None and startup.editor is not None
        assert startup.editor.entry is None
        startup.editor.draft.recipe_id = startup.editor.catalog.new_identity()
        startup.editor.draft.display_name = "Incomplete"
        startup.editor.save()
        assert startup.editor.entry is not None
        assert startup.editor.entry.recipe.model_id is None
        assert startup.application.snapshot().candidate is None
        assert startup.application.snapshot().active is None
        assert startup.application.snapshot().counters == ProductionCounters()
        assert data.preferences.exists() != missing
        if not missing:
            assert data.preferences.read_text(encoding="utf-8") == "{}"
    finally:
        close(startup)


def test_saved_incomplete_basler_sources_cannot_prepare_connect_or_start(
    tmp_path: Path,
) -> None:
    from capsule_vision.application.basler_connection import SourceConnectionCommands
    from capsule_vision.configuration import load_camera_sources

    data = saved_data(tmp_path)
    sources = replace(load_camera_sources(data.sources), mode="basler")
    save_camera_sources(data.sources, sources)
    calls: list[object] = []

    def forbidden(value: object):
        calls.append(value)
        raise AssertionError("No engine for incomplete mappings")

    startup = load_startup(data, engine_factory=forbidden)
    try:
        assert startup.editor is not None and startup.editor.camera_sources is not None
        assert startup.editor.camera_sources.saved == sources
        assert startup.prepared is None
        assert isinstance(startup.application, SourceConnectionCommands)
        with pytest.raises(ConfigurationError, match="serials"):
            startup.application.connect_source()
        assert startup.editor.entry is not None
        with pytest.raises(ConfigurationError, match="serials"):
            startup.editor.prepare(startup.editor.entry.recipe)
        assert startup.application.snapshot().state is ApplicationState.UNCONFIGURED
        with pytest.raises(ApplicationStateError):
            startup.application.start()
        assert not calls and not startup.application.source_snapshot().locked
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
    model_catalog = discover_models(data.models)
    assert len(model_catalog.registry.models) == 1
    assert (
        model_catalog.issues and model_catalog.issues[0].path == nested / "model.json"
    )
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
    original = ApplicationPreferences("local", "zh")
    save_preferences(path, original)

    def fail(_source: object, _target: object) -> None:
        raise OSError("replace denied")

    monkeypatch.setattr("capsule_vision.configuration._io.os.replace", fail)
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
    save_preferences(path, ApplicationPreferences("local"))
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
    assert loaded.preferences == ApplicationPreferences("local", "zh")
    assert loaded.invalid_language == (language != "omitted")
    startup = load_startup(data)
    try:
        assert startup.application.snapshot().state is ApplicationState.READY
        assert startup.prepared is not None
        assert startup.prepared.configuration.recipe_id == "local"
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
        '{"language":"en","language":"ja"}',
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
        assert startup.prepared is None and startup.editor is not None
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
        "model": data.models / "general" / "model.json",
        "recipe": data.recipes / "local.json",
        "artifact": data.models / "general" / "model.vimosln",
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
                data.sources.parent / "../frames" if kind == "folder" else target
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
        "model_id": "general",
        "display_name": "Local test",
        "defaults": {},
    }
    if invalid:
        value["capsule"] = {"size": {"enabled": "yes"}}
    path = data.recipes / "local.json"
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


@pytest.mark.parametrize("mode", ["folder", "basler"])
def test_source_restore_keeps_file_bytes_and_does_not_load_optional_sdk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    import sys

    from capsule_vision.application.source_control import SourceControlCommands
    from capsule_vision.configuration import load_camera_sources

    data = saved_data(tmp_path)
    if mode == "basler":
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
        assert startup.editor.camera_sources.saved.mode == (mode)
        assert isinstance(startup.application, SourceControlCommands)
        assert startup.application.control_available
        assert startup.application.control_snapshot() is None
        assert data.sources.read_bytes() == original
        assert "pypylon.pylon" not in sys.modules
        assert startup.application.snapshot().counters == ProductionCounters()
    finally:
        close(startup)
    assert data.sources.read_bytes() == original


@pytest.mark.parametrize("unreadable", [False, True])
def test_unrelated_catalog_errors_are_visible_and_do_not_block_saved_dependencies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unreadable: bool
) -> None:
    data = saved_data(tmp_path)
    bad_model = data.models / "broken-model" / "model.json"
    bad_model.parent.mkdir()
    bad_model.write_text("{", encoding="utf-8")
    bad_recipe = data.recipes / "broken-recipe.json"
    bad_recipe.write_text("{", encoding="utf-8")
    (data.models / "unmanaged-root.json").write_text("{", encoding="utf-8")
    if unreadable:
        original_open = Path.open

        def denied(
            path: Path,
            mode: str = "r",
            buffering: int = -1,
            encoding: str | None = None,
            errors: str | None = None,
            newline: str | None = None,
        ):
            if path == bad_model:
                raise PermissionError("unrelated Model denied")
            return original_open(path, mode, buffering, encoding, errors, newline)

        monkeypatch.setattr(Path, "open", denied)
    startup = load_startup(data)
    try:
        assert startup.prepared is not None and startup.editor is not None
        assert startup.application.snapshot().state is ApplicationState.READY
        assert all(not issue.blocking for issue in startup.issues)
        assert any(str(bad_model) in issue.detail for issue in startup.issues)
        assert any(str(bad_recipe) in issue.detail for issue in startup.issues)
        assert not any("unmanaged-root" in issue.detail for issue in startup.issues)
        editor = startup.editor
        editor.draft.display_name = "Saved despite unrelated errors"
        editor.save()
        saved = editor.apply_recipe()
        ready = editor.prepare(saved)
        assert ready.configuration.display_name == saved.display_name
        assert startup.application.snapshot().active is None
        assert startup.application.snapshot().counters == ProductionCounters()
    finally:
        close(startup)


def test_missing_catalog_directories_are_empty_without_side_effects(
    tmp_path: Path,
) -> None:
    from capsule_vision.configuration.catalog import RecipeCatalog

    models, recipes = tmp_path / "models", tmp_path / "recipes"
    snapshot = discover_models(models)
    assert snapshot.registry.models == snapshot.issues == ()
    assert (
        RecipeCatalog(recipes).discover().entries
        == RecipeCatalog(recipes).discover().issues
        == ()
    )
    assert not models.exists() and not recipes.exists()


@pytest.mark.parametrize("missing", ["models", "recipes"])
def test_missing_catalog_keeps_editors_without_implicit_selection(
    tmp_path: Path, missing: str
) -> None:
    data = saved_data(tmp_path)
    if missing == "models":
        owner = data.models / "general"
        (owner / "model.json").unlink()
        (owner / "model.vimosln").unlink()
        owner.rmdir()
        data.models.rmdir()
    else:
        (data.recipes / "local.json").unlink()
        data.recipes.rmdir()
    startup = load_startup(data)
    try:
        assert startup.editor is not None
        assert startup.prepared is None
        assert startup.application.snapshot().state is ApplicationState.UNCONFIGURED
        assert not any(issue.key == f"startup_{missing}" for issue in startup.issues)
        assert startup.application.snapshot().active is None
        assert startup.application.snapshot().counters == ProductionCounters()
    finally:
        close(startup)


@pytest.mark.parametrize("change", ["invalid", "changed", "missing"])
def test_prepare_rechecks_only_selected_model_metadata(
    tmp_path: Path, change: str
) -> None:
    data = saved_data(tmp_path)
    startup = load_startup(data)
    try:
        assert startup.editor is not None and startup.editor.entry is not None
        before = startup.application.snapshot()
        metadata = data.models / "general" / "model.json"
        if change == "invalid":
            metadata.write_text("{", encoding="utf-8")
        elif change == "changed":
            model = load_model_definition(metadata)
            save_model_definition(metadata, replace(model, module_id="changed-module"))
        else:
            metadata.unlink()
        with pytest.raises(ConfigurationError):
            startup.editor.prepare(startup.editor.entry.recipe)
        assert startup.application.snapshot() == before
    finally:
        close(startup)
