from dataclasses import replace
from pathlib import Path

import pytest
from tests.unit.test_production_application import FIXTURES, prepared

from vimo_demo.configuration import (
    ConfigurationError,
    ConfigurationIOError,
    MachineLayoutDefinition,
    ModelArtifactError,
    ModelRegistry,
    RecipeDefinition,
    load_recipe,
    save_machine_layout,
    save_recipe,
)
from vimo_demo.inspection import CameraId
from vimo_demo.inspection.production import ChannelId
from vimo_demo.ui.recipe_catalog import RecipeCatalog
from vimo_demo.ui.recipe_editor import RecipeDraft, RecipeEditorSession


def editor(tmp_path: Path) -> RecipeEditorSession:
    config = prepared(tmp_path).configuration
    folder = tmp_path / "recipes"
    folder.mkdir()
    for name in ("defaults", "overrides"):
        recipe = load_recipe(FIXTURES / f"{name}.json")
        save_recipe(folder / f"{name}.recipe.json", recipe)
    registry = ModelRegistry((config.model, replace(config.model, model_id="special")))
    machine_path = tmp_path / "machine.json"
    save_machine_layout(machine_path, MachineLayoutDefinition(config.layout))
    session = RecipeEditorSession(
        RecipeCatalog(folder),
        MachineLayoutDefinition(config.layout),
        registry,
        config.model.model_id,
        tmp_path,
        machine_path=machine_path,
    )
    session.load(
        next(
            e
            for e in session.catalog.discover().entries
            if not e.recipe.channel_overrides
        )
    )
    return session


def test_catalog_discovery_errors_and_read_only(tmp_path: Path) -> None:
    catalog = RecipeCatalog(tmp_path / "missing")
    with pytest.raises(ConfigurationIOError):
        catalog.discover()
    assert not catalog.directory.exists()
    catalog = RecipeCatalog(tmp_path)
    save_recipe(tmp_path / "b.recipe.json", RecipeDefinition("b", "乙"))
    save_recipe(tmp_path / "a.recipe.json", RecipeDefinition("a", "甲"))
    (tmp_path / "unrelated.json").write_text("invalid", encoding="utf-8")
    before = {p: p.read_bytes() for p in tmp_path.iterdir()}
    snapshot = catalog.discover()
    assert len(snapshot.entries) == 2 and not snapshot.issues
    assert snapshot == catalog.discover()
    assert {p: p.read_bytes() for p in tmp_path.iterdir()} == before
    save_recipe(tmp_path / "duplicate.recipe.json", RecipeDefinition("a", "重复"))
    (tmp_path / "broken.recipe.json").write_text("{", encoding="utf-8")
    snapshot = catalog.discover()
    assert len(snapshot.issues) == 3 and len(snapshot.entries) == 1
    with pytest.raises(ConfigurationError, match="catalog"):
        catalog.save(RecipeDefinition("c", "C"), original=None)


def test_load_dirty_save_as_new_discard_and_identity(tmp_path: Path) -> None:
    session = editor(tmp_path)
    assert not session.dirty and session.entry is not None
    original = session.entry
    session.draft.display_name = "修改"
    assert session.dirty
    with pytest.raises(ConfigurationError, match="unsaved"):
        session.load(original)
    with pytest.raises(ConfigurationError, match="unsaved"):
        session.new()
    with pytest.raises(ConfigurationError, match="Save changes"):
        session.apply_recipe()
    session.draft.display_name = original.recipe.display_name
    assert not session.dirty
    session.draft.display_name = "修改"
    session.save()
    assert not session.dirty and load_recipe(original.path).display_name == "修改"
    with pytest.raises(ConfigurationError, match="new Recipe ID"):
        session.save(new_identity=(original.recipe.recipe_id, "dup"))
    session.save(new_identity=("新/配方", "变体"))
    assert not session.dirty and session.entry.path != original.path
    assert session.entry.path.parent == original.path.parent
    assert load_recipe(original.path).display_name == "修改"
    session.draft.display_name = "临时"
    session.discard()
    assert not session.dirty and session.draft.display_name == "变体"
    session.new()
    assert (
        session.entry is None
        and not session.draft.cameras
        and not session.draft.channels
    )
    with pytest.raises(ConfigurationError):
        session.save()


def test_inheritance_and_explicit_values_round_trip(tmp_path: Path) -> None:
    session = editor(tmp_path)
    entry = next(
        e for e in session.catalog.discover().entries if e.recipe.channel_overrides
    )
    session.load(entry)
    resolved = session.resolve()
    assert [resolved.channels[i].checks.blob.max_area for i in (0, 12, 16)] == [
        1800,
        1850,
        1900,
    ]
    session.draft.defaults["blob"].max_area = "1801"
    assert session.resolve().channels[0].checks.blob.max_area == 1801
    session.draft.cameras[CameraId("CAM3")]["blob"].max_area = "1851"
    channel = session.draft.channels[ChannelId(17)]
    channel["blob"].max_area = None
    assert session.resolve().channels[16].checks.blob.max_area == 1851
    channel["blob"].enabled = False
    channel["blob"].required = False
    channel["blob"].min_area = "0"
    channel["blob"].max_area = "0"
    channel["vimo"].disabled_labels = ()
    session.save()
    assert session.entry is not None
    saved = load_recipe(session.entry.path)
    check = saved.channel_overrides[0].checks
    assert check.blob.enabled is False and check.blob.required is False
    assert check.blob.min_area == check.blob.max_area == 0
    assert check.vimo.disabled_labels == ()
    session.draft.channels[ChannelId(17)]["vimo"].disabled_labels = None
    session.save()
    assert (
        load_recipe(session.entry.path).channel_overrides[0].checks.vimo.disabled_labels
        is None
    )
    assert RecipeDraft.from_recipe(saved).to_recipe() == saved


def test_invalid_and_io_failure_keep_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = editor(tmp_path)
    assert session.entry is not None
    before = session.entry.path.read_bytes()
    session.draft.defaults["blob"].min_area = "99"
    session.draft.defaults["blob"].max_area = "2"
    with pytest.raises(ConfigurationError):
        session.save()
    assert session.dirty and session.entry.path.read_bytes() == before
    session.draft.defaults["blob"].max_area = "100"

    def fail(*_args: object, **_kwargs: object) -> None:
        raise ConfigurationIOError("simulated disk error")

    monkeypatch.setattr("vimo_demo.ui.recipe_catalog.save_recipe", fail)
    with pytest.raises(ConfigurationIOError):
        session.save()
    assert session.dirty and session.entry.path.read_bytes() == before


def test_model_prepare_and_external_changes(tmp_path: Path) -> None:
    session = editor(tmp_path)
    original = session.resolve()
    session.draft.model_override_id = "special"
    session.save()
    resolved = session.resolve()
    assert resolved.model.model_id == "special"
    assert all(
        a.roi == b.roi
        for a, b in zip(original.channels, resolved.channels, strict=True)
    )
    session.draft.model_override_id = None
    session.save()
    assert session.resolve().channels[16].roi == original.channels[16].roi
    assert session.entry is not None
    before = session.entry.path.read_bytes()
    saved = session.apply_recipe()
    ready = session.prepare(saved)
    assert ready.configuration.model.model_id == "general"
    assert session.entry.path.read_bytes() == before
    ready.model_path.write_bytes(b"wrong")
    with pytest.raises(ModelArtifactError):
        session.prepare(saved)
    save_recipe(session.entry.path, replace(saved, display_name="external"))
    with pytest.raises(ConfigurationError, match="externally"):
        session.apply_recipe()
