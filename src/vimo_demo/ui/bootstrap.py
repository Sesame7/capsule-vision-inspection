"""Persistent real-inference entry composition; disk preparation precedes Qt."""

import sys
import traceback
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, replace
from pathlib import Path

from PySide6.QtCore import QObject, Slot
from PySide6.QtWidgets import QApplication

from vimo_demo.application import (
    PreparedConfiguration,
    ProductionApplication,
    prepare_configuration,
)
from vimo_demo.application.folder_replay import FolderReplay
from vimo_demo.application.preferences import (
    ApplicationPreferences,
    load_preferences,
    save_preferences,
)
from vimo_demo.application.session import EngineFactory
from vimo_demo.configuration import (
    ConfigurationError,
    MachineLayoutDefinition,
    ModelDefinition,
    ModelRegistry,
    load_camera_sources,
    load_machine_layout,
    load_model_definition,
    resolve_configuration,
)
from vimo_demo.inspection import CameraId

from .camera_source_editor import CameraSourceDraft
from .display_frames import LatestDisplayFrameStore
from .inference_bridge import FutureDelivery
from .local_production import SourceNotifications, local_application, real_engine
from .production_bridge import QtProductionBridge, RecipeChoice
from .production_window import ProductionWindow
from .recipe_catalog import RecipeCatalog
from .recipe_editor import RecipeEditorSession


@dataclass(frozen=True, slots=True)
class ApplicationData:
    """Fixed paths under the entry directory; never inferred from process cwd."""

    root: Path

    @classmethod
    def at_entry(cls, directory: Path) -> ApplicationData:
        return cls(directory.resolve() / "data")

    @property
    def preferences(self) -> Path:
        return self.root / "app.json"

    @property
    def machine(self) -> Path:
        return self.root / "machine" / "machine.json"

    @property
    def sources(self) -> Path:
        return self.root / "sources" / "camera_sources.json"

    @property
    def models(self) -> Path:
        return self.root / "models"

    @property
    def recipes(self) -> Path:
        return self.root / "recipes"


def discover_models(directory: Path) -> ModelRegistry:
    """Strict, nonrecursive discovery. Anchor each artifact to its metadata file."""
    paths = sorted(
        (p for p in directory.iterdir() if p.is_file() and p.suffix.lower() == ".json"),
        key=lambda p: (p.name.casefold(), p.name),
    )
    models: list[ModelDefinition] = []
    for path in paths:
        try:
            model = load_model_definition(path)
            models.append(
                replace(
                    model, model_path=str((path.parent / model.model_path).resolve())
                )
            )
        except ConfigurationError as exc:
            raise ConfigurationError(f"Model definition {path}: {exc}") from exc
    return ModelRegistry(tuple(models))


@dataclass
class Startup:
    data: ApplicationData
    preferences: ApplicationPreferences | None
    application: ProductionApplication
    frames: LatestDisplayFrameStore
    machine: MachineLayoutDefinition | None
    editor: RecipeEditorSession | None
    choice: RecipeChoice | None
    issues: tuple[str, ...]


def load_startup(
    data: ApplicationData,
    *,
    engine_factory: EngineFactory = real_engine,
    observe_source: Callable[[Future[None]], None] = lambda _: None,
) -> Startup:
    """Blocking startup IO only, called before QApplication is constructed.

    No engine, runtime session, cycle, warm-up or candidate activation occurs.
    Partial valid data may support presentation/editing; it is never READY.
    """
    issues: list[str] = []
    preferences = None
    machine = None
    registry = None
    sources = None
    entries = ()
    for kind, path in (
        ("Preferences", data.preferences),
        ("Machine", data.machine),
        ("Models", data.models),
        ("Sources", data.sources),
        ("Recipes", data.recipes),
    ):
        try:
            if kind == "Preferences":
                preferences = load_preferences(path)
            elif kind == "Machine":
                machine = load_machine_layout(path)
                if len(machine.layout.cameras) != 6:
                    machine = None
                    raise ConfigurationError(
                        "Production requires six configured cameras"
                    )
            elif kind == "Models":
                registry = discover_models(path)
            elif kind == "Sources":
                sources = load_camera_sources(path)
            else:
                catalog = RecipeCatalog(path).discover()
                entries = catalog.entries
                issues.extend(f"Recipe {e.path}: {e.message}" for e in catalog.issues)
        except (ConfigurationError, OSError) as exc:
            issues.append(f"{kind} {path}: {exc}")

    editor = None
    prepared: PreparedConfiguration | None = None
    try:
        if preferences is not None and registry is not None:
            if preferences.default_model_id is None:
                raise ConfigurationError("No explicit default Model ID in app.json")
            registry.get(preferences.default_model_id)
            if machine is not None and sources is not None:
                sources.validate_cameras(machine.layout.camera_ids)
                editor = RecipeEditorSession(
                    RecipeCatalog(data.recipes),
                    machine,
                    registry,
                    preferences.default_model_id,
                    data.models,
                    machine_path=data.machine,
                    camera_sources=CameraSourceDraft(sources, data.sources),
                    auto_select_first=False,
                    require_saved_sources=True,
                )
                entry = next(
                    (
                        e
                        for e in entries
                        if e.recipe.recipe_id == preferences.last_applied_recipe_id
                    ),
                    None,
                )
                if entry is None:
                    raise ConfigurationError(
                        "Last applied Recipe is absent or unselected; no fallback"
                    )
                editor.load(entry)
                # One source preflight group, discarded before Qt. Start reopens
                # the replay and worker normally; nothing is inspection data here.
                FolderReplay(machine.layout, sources, data.sources.parent).next_group()
                if not issues:
                    prepared = prepare_configuration(
                        resolve_configuration(
                            machine,
                            entry.recipe,
                            registry,
                            preferences.default_model_id,
                        ),
                        base_directory=data.models,
                    )
    except Exception as exc:
        issues.append(f"Configuration preparation: {type(exc).__name__}: {exc}")

    if editor is None:

        def unavailable(_config: PreparedConfiguration, _generation: int):
            raise ConfigurationError("Persistent configuration is unavailable")

        application = ProductionApplication(unavailable)
        # Shell identity slots only, never a layout/ROI/source/runtime fallback.
        cameras = (
            machine.layout.camera_ids
            if machine
            else tuple(CameraId(f"CAM{i}") for i in range(1, 7))
        )
        frames = LatestDisplayFrameStore(cameras)
    else:
        application, frames = local_application(
            editor, engine_factory=engine_factory, observe_source=observe_source
        )
    choice = None
    if prepared is not None:
        application.configure(prepared)
        choice = RecipeChoice(prepared, prepared.configuration.display_name)
    return Startup(
        data, preferences, application, frames, machine, editor, choice, tuple(issues)
    )


class PreferenceNotifications(QObject):
    """Reuse existing successful Apply/language signals, not a selection manager."""

    def __init__(
        self, window: ProductionWindow, path: Path, preferences: ApplicationPreferences
    ) -> None:
        super().__init__(window)
        self.window, self.path, self.preferences = window, path, preferences
        window.parameters.applied.connect(self.applied)
        window.language_changed.connect(self.language)

    def _save(self, value: ApplicationPreferences) -> None:
        try:
            save_preferences(self.path, value)
        except ConfigurationError as exc:
            traceback.print_exception(exc)
            self.window.bridge.command_failed.emit("preferences", exc)
        else:
            self.preferences = value

    @Slot(object)
    def applied(self, choice: object) -> None:
        if isinstance(choice, RecipeChoice):
            self._save(
                replace(
                    self.preferences,
                    last_applied_recipe_id=choice.prepared.configuration.recipe_id,
                )
            )

    @Slot(str)
    def language(self, value: str) -> None:
        if value in ("zh", "en"):
            self._save(
                replace(self.preferences, language="zh" if value == "zh" else "en")
            )


def create_window(startup: Startup, delivery: FutureDelivery) -> ProductionWindow:
    bridge = QtProductionBridge(startup.application, startup.frames)
    SourceNotifications(bridge, delivery)
    window = ProductionWindow(
        bridge,
        (startup.choice,) if startup.choice else (),
        language=startup.preferences.language if startup.preferences else "zh",
        recipe_editor=startup.editor,
        layout=startup.machine.layout if startup.machine else None,
        configuration_issue="; ".join(startup.issues),
    )
    if startup.preferences is not None:
        PreferenceNotifications(window, startup.data.preferences, startup.preferences)
    elif not startup.data.preferences.exists():
        # An explicit language change may create preferences with unselected IDs;
        # never replace a malformed existing manifest or fabricate domain data.
        PreferenceNotifications(
            window, startup.data.preferences, ApplicationPreferences()
        )
    return window


def main(entry_directory: Path) -> int:
    # Preparation before Qt creation, as in the explicit-path developer launcher.
    startup = load_startup(
        ApplicationData.at_entry(entry_directory),
        observe_source=lambda future: delivery.watch("folder_source", future),
    )
    for issue in startup.issues:
        print(issue, file=sys.stderr)
    qt = QApplication(sys.argv[:1])
    delivery = FutureDelivery()
    try:
        window = create_window(startup, delivery)
        window.resize(1080, 1920)
        window.show()
        return qt.exec()
    finally:
        # Outside Qt dispatch, retaining ownership until native/source work exits.
        startup.application.close().result()
        startup.application.join()
        delivery.detach()
