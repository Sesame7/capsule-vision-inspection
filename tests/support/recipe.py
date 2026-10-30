"""Qt-independent Recipe editor setup shared by unit and UI tests."""

from dataclasses import replace
from pathlib import Path

from capsule_vision.application.editing import RecipeEditorSession
from capsule_vision.configuration import (
    MachineLayoutDefinition,
    ModelRegistry,
    load_recipe,
    save_machine_layout,
    save_recipe,
)
from capsule_vision.configuration.catalog import RecipeCatalog
from tests.support.application import FIXTURES, prepared


def editor(tmp_path: Path) -> RecipeEditorSession:
    config = prepared(tmp_path).configuration
    folder = tmp_path / "recipes"
    folder.mkdir()
    for name in ("product-a", "product-b"):
        recipe = load_recipe(FIXTURES / f"{name}.json")
        save_recipe(folder / f"{name}.json", recipe)
    registry = ModelRegistry((config.model, replace(config.model, model_id="special")))
    machine_path = tmp_path / "machine.tsv"
    save_machine_layout(machine_path, MachineLayoutDefinition(config.layout))
    session = RecipeEditorSession(
        RecipeCatalog(folder),
        MachineLayoutDefinition(config.layout),
        registry,
        tmp_path,
        machine_path=machine_path,
    )
    session.load(
        next(
            e
            for e in session.catalog.discover().entries
            if e.recipe.recipe_id == "product-a"
        )
    )
    return session
