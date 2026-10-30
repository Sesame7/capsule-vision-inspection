from dataclasses import replace
from pathlib import Path

import pytest
from tests.support.recipe import editor

from capsule_vision.configuration import (
    CheckConfiguration,
    ConfigurationError,
    ConfigurationIOError,
    ModelArtifactError,
    RecipeDefinition,
    ViMoCheckConfig,
    load_recipe,
    save_recipe,
)
from capsule_vision.configuration.catalog import RecipeCatalog
from capsule_vision.configuration.drafts import RecipeDraft
from capsule_vision.configuration.preferences import (
    ApplicationPreferences,
    save_preferences,
)


def test_catalog_discovery_errors_and_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = RecipeCatalog(tmp_path / "missing")
    assert catalog.discover().entries == catalog.discover().issues == ()
    assert not catalog.directory.exists()
    catalog = RecipeCatalog(tmp_path)
    save_recipe(tmp_path / "b.json", RecipeDefinition("b", "乙"))
    save_recipe(tmp_path / "a.json", RecipeDefinition("a", "甲"))
    (tmp_path / "unrelated.json").write_text("invalid", encoding="utf-8")
    before = {p: p.read_bytes() for p in tmp_path.iterdir()}
    snapshot = catalog.discover()
    assert len(snapshot.entries) == 2 and len(snapshot.issues) == 1
    assert snapshot.issues[0].path == tmp_path / "unrelated.json"
    assert snapshot == catalog.discover()
    assert {p: p.read_bytes() for p in tmp_path.iterdir()} == before
    original_iterdir = Path.iterdir

    def ambiguous(path: Path):
        return iter([*original_iterdir(path), tmp_path / "A.json"])

    monkeypatch.setattr(Path, "iterdir", ambiguous)
    (tmp_path / "broken.json").write_text("{", encoding="utf-8")
    snapshot = catalog.discover()
    assert len(snapshot.issues) == 4 and len(snapshot.entries) == 1
    assert snapshot.entries[0].recipe.recipe_id == "b"
    created = catalog.save(RecipeDefinition("c", "C"), original=None)
    assert created.path == tmp_path / "c.json" and created.recipe.model_id is None
    with pytest.raises(ConfigurationError, match="ambiguous"):
        catalog.save(RecipeDefinition("a", "A"), original=None)


@pytest.mark.parametrize(
    "blocker",
    ["protected", "persisted", "external", "permission", "invalid_preferences"],
)
def test_delete_refuses_references_and_failures_without_changing_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, blocker: str
) -> None:
    session = editor(tmp_path)
    entry = session.entry
    assert entry is not None
    session.draft.display_name = "Unsaved"
    draft = replace(session.draft)
    protected: frozenset[str] = (
        frozenset({entry.recipe.recipe_id}) if blocker == "protected" else frozenset()
    )
    if blocker in ("persisted", "invalid_preferences"):
        session.preferences_path = tmp_path / "app.json"
        save_preferences(
            session.preferences_path,
            ApplicationPreferences(last_applied_recipe_id=entry.recipe.recipe_id),
        )
        if blocker == "invalid_preferences":
            session.preferences_path.write_text("{", encoding="utf8")
    if blocker == "external":
        save_recipe(entry.path, replace(entry.recipe, display_name="External"))
    if blocker == "permission":
        original = Path.unlink

        def fail(path: Path, missing_ok: bool = False) -> None:
            if path == entry.path:
                raise PermissionError("Delete denied")
            original(path, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", fail)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    with pytest.raises(ConfigurationError):
        session.delete(protected_ids=protected)
    assert session.entry == entry and session.draft == draft
    assert {p: p.read_bytes() for p in before} == before


def test_delete_only_selected_recipe_and_leave_no_implicit_selection(
    tmp_path: Path,
) -> None:
    session = editor(tmp_path)
    entry = session.entry
    assert entry is not None
    before = {
        p: p.read_bytes()
        for p in tmp_path.rglob("*")
        if p.is_file() and p != entry.path
    }
    session.delete(protected_ids=frozenset())
    assert not entry.path.exists() and session.entry is None
    assert not session.dirty and session.draft == RecipeDraft("", "")
    assert len(session.catalog.discover().entries) == 1
    assert {p: p.read_bytes() for p in before} == before


def test_load_dirty_save_new_discard_and_identity(tmp_path: Path) -> None:
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
    duplicate = replace(
        session.draft.to_recipe(),
        recipe_id=session.catalog.new_identity(),
        display_name="变体",
    )
    session.new()
    session.draft = RecipeDraft.from_recipe(duplicate)
    session.save()
    assert not session.dirty and session.entry.path != original.path
    assert session.entry.recipe.recipe_id == duplicate.recipe_id
    assert session.entry.path.parent == original.path.parent
    assert load_recipe(original.path).display_name == "修改"
    session.draft.display_name = "临时"
    session.discard()
    assert not session.dirty and session.draft.display_name == "变体"
    session.new()
    assert session.entry is None and session.draft.defaults == CheckConfiguration()
    with pytest.raises(ConfigurationError):
        session.save()


def test_shared_policy_explicit_values_round_trip(tmp_path: Path) -> None:
    session = editor(tmp_path)
    entry = next(
        e
        for e in session.catalog.discover().entries
        if e.recipe.recipe_id == "product-b"
    )
    session.load(entry)
    assert session.resolve().checks == session.draft.defaults
    for policy in (
        ViMoCheckConfig(False, False, ("Dent", "dent")),
        ViMoCheckConfig(True, False, ()),
    ):
        session.draft.defaults = CheckConfiguration(policy)
        assert session.dirty and session.resolve().checks.vimo == policy
        session.save()
        assert session.entry is not None
        saved = load_recipe(session.entry.path)
        assert saved.defaults.vimo == policy and not session.dirty
        assert RecipeDraft.from_recipe(saved).to_recipe() == saved


def test_invalid_and_io_failure_keep_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = editor(tmp_path)
    assert session.entry is not None
    before = session.entry.path.read_bytes()
    session.draft.display_name = ""
    with pytest.raises(ConfigurationError):
        session.save()
    assert session.dirty and session.entry.path.read_bytes() == before
    session.draft.display_name = "Valid again"

    def fail(*_args: object, **_kwargs: object) -> None:
        raise ConfigurationIOError("simulated disk error")

    monkeypatch.setattr("capsule_vision.configuration.catalog.save_recipe", fail)
    with pytest.raises(ConfigurationIOError):
        session.save()
    assert session.dirty and session.entry.path.read_bytes() == before


def test_model_prepare_and_external_changes(tmp_path: Path) -> None:
    session = editor(tmp_path)
    original = session.resolve()
    session.draft.model_id = "special"
    session.save()
    resolved = session.resolve()
    assert resolved.model.model_id == "special"
    assert resolved.layout == original.layout
    session.draft.model_id = "general"
    session.save()
    assert session.resolve().layout == original.layout
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


@pytest.mark.parametrize("model_id", [None, "unregistered"])
def test_incomplete_recipe_save_reload_and_strict_preparation(
    tmp_path: Path, model_id: str | None
) -> None:
    session = editor(tmp_path)
    assert session.entry is not None
    session.draft.model_id = model_id
    session.save()
    saved = session.entry.path.read_bytes()
    assert not session.dirty and load_recipe(session.entry.path).model_id == model_id
    session.draft.display_name = "Unsaved"
    session.discard()
    assert not session.dirty and session.draft.model_id == model_id
    with pytest.raises(ConfigurationError, match="model_id"):
        session.apply_recipe()
    with pytest.raises(ConfigurationError, match="model_id"):
        session.prepare(session.entry.recipe)
    assert session.entry.path.read_bytes() == saved


@pytest.mark.parametrize("change", ["external", "invalid", "removed", "collision"])
def test_recipe_save_protects_only_the_target(tmp_path: Path, change: str) -> None:
    catalog = RecipeCatalog(tmp_path)
    entry = catalog.save(RecipeDefinition("target", "Original"), original=None)
    (tmp_path / "unrelated.json").write_text("{", encoding="utf-8")
    if change == "external":
        save_recipe(entry.path, replace(entry.recipe, display_name="External"))
    elif change == "invalid":
        entry.path.write_text("{", encoding="utf-8")
    elif change == "removed":
        entry.path.unlink()
    expected = entry.path.read_bytes() if entry.path.exists() else None
    with pytest.raises(ConfigurationError):
        catalog.save(
            replace(entry.recipe, display_name="Draft"),
            original=None if change == "collision" else entry,
        )
    assert (entry.path.read_bytes() if entry.path.exists() else None) == expected
    other = catalog.save(RecipeDefinition("other", "Independent"), original=None)
    catalog.delete(other, protected_ids=frozenset())
    assert not other.path.exists()
    assert (tmp_path / "unrelated.json").read_text(encoding="utf-8") == "{"


def test_explicit_save_can_create_a_missing_recipe_catalog(tmp_path: Path) -> None:
    catalog = RecipeCatalog(tmp_path / "missing")
    assert not catalog.discover().entries and not catalog.directory.exists()
    saved = catalog.save(RecipeDefinition("new", "Incomplete"), original=None)
    assert saved.path == catalog.directory / "new.json"
    assert load_recipe(saved.path).model_id is None
