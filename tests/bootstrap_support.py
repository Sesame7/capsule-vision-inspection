"""Synthetic saved application data for Qt-aware bootstrap tests."""

import shutil
from dataclasses import replace
from pathlib import Path

from tests.local_input_support import input_files
from vimo_demo.application.preferences import ApplicationPreferences, save_preferences
from vimo_demo.configuration import (
    CameraFolder,
    CameraSources,
    load_model_definition,
    save_camera_sources,
    save_model_definition,
)
from vimo_demo.inspection import CameraId
from vimo_demo.ui.bootstrap import ApplicationData


def saved_data(root: Path) -> ApplicationData:
    data = ApplicationData.at_entry(root)
    for folder in (data.models, data.recipes, data.machine.parent, data.sources.parent):
        folder.mkdir(parents=True)
    frames = root / "frames"
    frames.mkdir()
    model_file, machine_file, recipe_file, _ = input_files(frames)
    model = load_model_definition(model_file)
    shutil.copyfile(model.model_path, data.models / "general.vimosln")
    save_model_definition(
        data.models / "general.json", replace(model, model_path="general.vimosln")
    )
    shutil.copyfile(machine_file, data.machine)
    shutil.copyfile(recipe_file, data.recipes / "local.recipe.json")
    save_camera_sources(
        data.sources,
        CameraSources(
            tuple(
                CameraFolder(CameraId(f"CAM{i}"), "../../frames") for i in range(1, 7)
            ),
            loop=False,
            target_fps=3.5,
        ),
    )
    save_preferences(data.preferences, ApplicationPreferences("general", "local", "en"))
    return data
