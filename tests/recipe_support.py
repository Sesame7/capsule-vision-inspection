"""Qt-independent Recipe editor setup shared by unit and UI tests."""

from dataclasses import replace
from pathlib import Path

from tests.application_support import FIXTURES, prepared
from vimo_demo.configuration import (
    MachineLayoutDefinition,
    ModelRegistry,
    load_recipe,
    save_machine_layout,
    save_recipe,
)
from vimo_demo.ui.recipe_catalog import RecipeCatalog
from vimo_demo.ui.recipe_editor import RecipeEditorSession


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
