"""Qt-independent synthetic input files shared by local composition tests."""

from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np

from tests.application_support import prepared
from vimo_demo.configuration import (
    MachineLayoutDefinition,
    RecipeDefinition,
    save_machine_layout,
    save_model_definition,
    save_recipe,
)


def input_files(
    folder: Path, *, camera_count: int = 6
) -> tuple[Path, Path, Path, tuple[Path, ...]]:
    assert camera_count in (5, 6)
    config = prepared(folder).configuration
    model, machine, recipe = (
        folder / name for name in ("model.json", "machine.json", "local.recipe.json")
    )
    save_model_definition(model, config.model)
    save_machine_layout(
        machine,
        MachineLayoutDefinition(
            replace(config.layout, cameras=config.layout.cameras[:camera_count])
        ),
    )
    save_recipe(recipe, RecipeDefinition("local", "Local test"))
    paths: list[Path] = []
    for index in range(camera_count):
        path = folder / f"图像-{index}.png"
        ok, encoded = cv2.imencode(
            ".png", np.full((20, 60, 3), index + 1, dtype=np.uint8)
        )
        assert ok
        encoded.tofile(path)
        paths.append(path)
    return model, machine, recipe, tuple(paths)
