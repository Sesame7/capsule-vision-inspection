"""Synthetic saved application data for Qt-aware bootstrap tests."""

import shutil
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from threading import enumerate as threads

from capsule_vision.application import PreparedConfiguration
from capsule_vision.application.source_control import (
    SourceControlCommands,
    SourceControlState,
)
from capsule_vision.application.startup import ApplicationData, Startup, load_startup
from capsule_vision.configuration import (
    CameraFolder,
    CameraSources,
    load_camera_sources,
    load_model_definition,
    save_camera_sources,
    save_model_definition,
)
from capsule_vision.configuration.preferences import (
    ApplicationPreferences,
    save_preferences,
)
from capsule_vision.inspection import CameraId
from capsule_vision.ui.bootstrap import create_window
from capsule_vision.ui.future_delivery import FutureDelivery
from capsule_vision.ui.main_window import MainWindow
from tests.support.application import WAIT, ReplayEngine
from tests.support.qt import close_window
from tests.support.source_inputs import input_files, replay_files


def saved_data(root: Path, *, camera_count: int = 6) -> ApplicationData:
    data = ApplicationData.at_entry(root)
    for folder in (data.models, data.recipes, data.machine.parent, data.sources.parent):
        folder.mkdir(parents=True, exist_ok=True)
    frames = root / "frames"
    frames.mkdir()
    model_file, machine_file, recipe_file, _ = input_files(
        frames, camera_count=camera_count
    )
    model = load_model_definition(model_file)
    (data.models / "general").mkdir()
    shutil.copyfile(model.model_path, data.models / "general" / "model.vimosln")
    save_model_definition(
        data.models / "general" / "model.json",
        replace(model, model_path="model.vimosln"),
    )
    shutil.copyfile(machine_file, data.machine)
    shutil.copyfile(recipe_file, data.recipes / "local.json")
    save_camera_sources(
        data.sources,
        CameraSources(
            tuple(
                CameraFolder(CameraId(f"CAM{i}"), "../frames")
                for i in range(1, camera_count + 1)
            ),
            loop=False,
            target_fps=3.5,
        ),
    )
    save_preferences(data.preferences, ApplicationPreferences("local", "en"))
    return data


def close(startup: Startup) -> None:
    startup.application.close().result(WAIT)
    assert startup.application.join(WAIT)


@contextmanager
def normal_replay(
    root: Path,
    *,
    loop: bool = True,
    first_camera_images: int = 2,
    hold_call: int | None = None,
    fail: str | None = None,
) -> Generator[tuple[Startup, MainWindow, list[ReplayEngine]]]:
    original_threads = {thread.ident for thread in threads()}
    data = saved_data(root)
    replay_files(root / "frames", loop=loop, first_camera_images=first_camera_images)
    sources = load_camera_sources(root / "frames" / "camera_sources.json")
    save_camera_sources(
        data.sources,
        replace(
            sources,
            cameras=tuple(
                CameraFolder(camera.camera_id, f"../frames/{camera.folder}")
                for camera in sources.cameras
            ),
        ),
    )
    engines: list[ReplayEngine] = []

    def factory(_prepared: PreparedConfiguration) -> ReplayEngine:
        engine = ReplayEngine(fail=fail)
        engine.hold_call = hold_call if not engines else None
        engines.append(engine)
        return engine

    delivery = FutureDelivery()
    startup = load_startup(
        data,
        engine_factory=factory,
        observe_source=lambda future: delivery.watch("folder_source", future),
    )
    window = create_window(startup, delivery)
    window.show()
    try:
        yield startup, window, engines
    finally:
        for engine in engines:
            engine.release.set()
        close_window(window)
        delivery.detach()
        assert all(engine.closed for engine in engines)
        assert not {thread.ident for thread in threads()} - original_threads


def replay_state(startup: Startup) -> SourceControlState | None:
    assert isinstance(startup.application, SourceControlCommands)
    snapshot = startup.application.control_snapshot()
    return snapshot.state if snapshot else None
