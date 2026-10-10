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
from vimo_demo.application.folder_replay import FolderReplay, FolderSourceError
from vimo_demo.application.preferences import (
    ApplicationPreferences,
    read_preferences,
    save_preferences,
)
from vimo_demo.application.session import EngineFactory
from vimo_demo.configuration import (
    ConfigurationError,
    MachineLayoutDefinition,
    ModelArtifactError,
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


@dataclass(frozen=True, slots=True)
class StartupIssue:
    key: str
    detail: str
    blocking: bool = True


@dataclass
class Startup:
    data: ApplicationData
    preferences: ApplicationPreferences | None
    application: ProductionApplication
    frames: LatestDisplayFrameStore
    machine: MachineLayoutDefinition | None
    editor: RecipeEditorSession | None
    choice: RecipeChoice | None
    issues: tuple[StartupIssue, ...]
    preferences_writable: bool


def load_startup(
    data: ApplicationData,
    *,
    engine_factory: EngineFactory = real_engine,
    observe_source: Callable[[Future[None]], None] = lambda _: None,
) -> Startup:
    """Blocking startup IO only, called before QApplication is constructed.

    No engine, runtime session, cycle, warm-up or candidate activation occurs.
    Production-critical errors prevent READY. Valid candidates with unavailable
    Folder inputs remain READY and must pass the existing Start preflight.
    """
    issues: list[StartupIssue] = []
    preferences = None
    preferences_writable = False
    machine = None
    registry = None
    sources = None
    entries = ()
    try:
        loaded = read_preferences(data.preferences)
        preferences = loaded.preferences
        preferences_writable = not loaded.invalid_language
        if loaded.invalid_language:
            issues.append(
                StartupIssue(
                    "startup_language",
                    f"{data.preferences}: invalid language; using zh in memory; file kept unchanged",
                    blocking=False,
                )
            )
    except ConfigurationError as exc:
        # Only a definitely missing manifest can be created by an explicit
        # language change. Permission/malformed/unsupported files remain intact.
        preferences_writable = isinstance(exc.__cause__, FileNotFoundError)
        issues.append(StartupIssue("startup_preferences", f"{data.preferences}: {exc}"))

    for kind, path in (
        ("machine", data.machine),
        ("models", data.models),
        ("sources", data.sources),
        ("recipes", data.recipes),
    ):
        try:
            if kind == "machine":
                machine = load_machine_layout(path)
                if len(machine.layout.cameras) != 6:
                    machine = None
                    raise ConfigurationError(
                        "Production requires six configured cameras"
                    )
            elif kind == "models":
                registry = discover_models(path)
            elif kind == "sources":
                sources = load_camera_sources(path)
            else:
                catalog = RecipeCatalog(path).discover()
                entries = catalog.entries
                issues.extend(
                    StartupIssue("startup_recipes", f"{e.path}: {e.message}")
                    for e in catalog.issues
                )
        except (ConfigurationError, OSError) as exc:
            issues.append(StartupIssue(f"startup_{kind}", f"{path}: {exc}"))

    if sources is not None and machine is not None:
        try:
            sources.validate_cameras(machine.layout.camera_ids)
        except ConfigurationError as exc:
            sources = None
            issues.append(StartupIssue("startup_sources", f"{data.sources}: {exc}"))

    default_id = preferences.default_model_id if preferences else None
    if preferences is not None and registry is not None:
        try:
            if default_id is None:
                raise ConfigurationError("No explicit default Model ID in app.json")
            registry.get(default_id)
        except ConfigurationError as exc:
            default_id = None
            issues.append(
                StartupIssue("startup_model_identity", f"{data.preferences}: {exc}")
            )

    editor = None
    entry = None
    if default_id is not None and registry is not None and machine is not None:
        editor = RecipeEditorSession(
            RecipeCatalog(data.recipes),
            machine,
            registry,
            default_id,
            data.models,
            machine_path=data.machine,
            camera_sources=CameraSourceDraft(sources, data.sources)
            if sources
            else None,
            auto_select_first=False,
            require_saved_sources=True,
        )
        assert preferences is not None
        entry = next(
            (
                e
                for e in entries
                if e.recipe.recipe_id == preferences.last_applied_recipe_id
            ),
            None,
        )
        if entry is None:
            issues.append(
                StartupIssue(
                    "startup_recipe_identity",
                    f"{data.preferences}: last applied Recipe ID {preferences.last_applied_recipe_id!r} is absent or unselected; no fallback",
                )
            )
        else:
            try:
                editor.load(entry)
            except ConfigurationError as exc:
                entry = None
                issues.append(StartupIssue("startup_recipes", str(exc)))

    prepared: PreparedConfiguration | None = None
    if editor is not None and entry is not None and not any(i.blocking for i in issues):
        try:
            prepared = prepare_configuration(
                resolve_configuration(
                    editor.machine,
                    entry.recipe,
                    editor.registry,
                    editor.default_model_id,
                ),
                base_directory=data.models,
            )
        except ModelArtifactError as exc:
            issues.append(StartupIssue("startup_artifact", str(exc)))
        except ConfigurationError as exc:
            issues.append(StartupIssue("startup_configuration", str(exc)))

    if machine is not None and sources is not None:
        try:
            # Observation only; Start reopens/preflights replay before constructing
            # a session/engine. Source unavailability does not invalidate a valid
            # candidate or invent a new application lifecycle state.
            FolderReplay(machine.layout, sources, data.sources.parent).next_group()
        except (FolderSourceError, OSError, ValueError) as exc:
            issues.append(
                StartupIssue("startup_source_unavailable", str(exc), blocking=False)
            )

    if editor is None or editor.camera_sources is None:

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
        data,
        preferences,
        application,
        frames,
        machine,
        editor,
        choice,
        tuple(issues),
        preferences_writable,
    )


class PreferenceNotifications(QObject):
    """Reuse existing successful Apply/language signals, not a selection manager."""

    def __init__(
        self,
        window: ProductionWindow,
        path: Path,
        preferences: ApplicationPreferences,
        *,
        writable: bool = True,
    ) -> None:
        super().__init__(window)
        self.window, self.path, self.preferences = window, path, preferences
        self.writable = writable
        window.parameters.applied.connect(self.applied)
        window.language_changed.connect(self.language)

    def _save(self, value: ApplicationPreferences) -> None:
        try:
            if not self.writable:
                raise ConfigurationError(
                    "Repair app.json externally and restart before saving preferences"
                )
            try:
                current = read_preferences(self.path)
            except ConfigurationError as exc:
                if not isinstance(exc.__cause__, FileNotFoundError):
                    raise
            else:
                if current.invalid_language or current.preferences != self.preferences:
                    raise ConfigurationError(
                        "Application preferences changed/invalid; repair and restart"
                    )
            save_preferences(self.path, value)
        except ConfigurationError as exc:
            traceback.print_exception(exc)
            self.window.bridge.command_failed.emit("preferences", exc)
        else:
            self.preferences = value
            self.window.bridge.command_finished.emit("preferences")

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
        configuration_issue="\n".join(i.detail for i in startup.issues),
        configuration_messages=tuple(dict.fromkeys(i.key for i in startup.issues)),
        configuration_notices=tuple((i.key, i.detail) for i in startup.issues),
    )
    PreferenceNotifications(
        window,
        startup.data.preferences,
        startup.preferences or ApplicationPreferences(),
        writable=startup.preferences_writable,
    )
    return window


def main(entry_directory: Path) -> int:
    # Preparation before Qt creation, as in the explicit-path developer launcher.
    startup = load_startup(
        ApplicationData.at_entry(entry_directory),
        observe_source=lambda future: delivery.watch("folder_source", future),
    )
    for issue in startup.issues:
        print(issue.detail, file=sys.stderr)
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
