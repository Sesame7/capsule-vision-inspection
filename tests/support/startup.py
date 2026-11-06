"""Synthetic saved application data for Qt-aware bootstrap tests."""

import shutil
from collections.abc import Callable, Generator
from concurrent.futures import Future
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from threading import enumerate as threads

from capsule_vision.application import PreparedConfiguration
from capsule_vision.application.account_maintenance import initialize_administrator
from capsule_vision.application.identity import LocalIdentity
from capsule_vision.application.identity_models import Account, Role
from capsule_vision.application.production_application import (
    SourceProductionApplication,
)
from capsule_vision.application.session import EngineFactory, real_engine
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
from capsule_vision.diagnostics import RuntimeDiagnostics
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


TEST_PASSWORD = "isolated production test password"


def provision_account(
    owner: LocalIdentity, username: str, role: Role, password: str
) -> Account:
    """Create a usable test actor via the real temporary-password workflow."""
    administrator = owner.snapshot().account
    assert administrator and administrator.role is Role.ADMINISTRATOR
    temporary = "temporary fixture credential"
    owner.create_account(
        username, role, temporary, confirm_administrator=role is Role.ADMINISTRATOR
    ).result(WAIT)
    restricted = owner.login(username, temporary).result(WAIT)
    assert restricted.rotation_required
    account = owner.rotate_password(password, restricted.revision).result(WAIT).account
    assert account is not None
    owner.login(administrator.username, TEST_PASSWORD).result(WAIT)
    return account


def authenticated_startup(
    data: ApplicationData,
    *,
    engine_factory: EngineFactory = real_engine,
    observe_source: Callable[[Future[None]], None] = lambda _: None,
    select_basler_batch: bool = True,
    diagnostics: RuntimeDiagnostics | None = None,
) -> Startup:
    """Run existing privileged workflows against a real disposable account."""
    if not data.security.exists():
        initialize_administrator(data.security, "TestAdmin", TEST_PASSWORD)
    startup = load_startup(
        data,
        engine_factory=engine_factory,
        observe_source=observe_source,
        diagnostics=diagnostics,
    )
    try:
        owner = startup.application.identity
        assert owner is not None
        owner.login("TestAdmin", TEST_PASSWORD).result(WAIT)
        if (
            select_basler_batch
            and startup.prepared is not None
            and isinstance(startup.application, SourceProductionApplication)
            and startup.application.basler_available
            and startup.application.history is not None
            and startup.application.history.initial_selection.result(WAIT) is None
        ):
            startup.application.start_batch("Basler test Batch").result(WAIT)
    except BaseException:
        close(startup)
        raise
    return startup


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
    startup = authenticated_startup(
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
