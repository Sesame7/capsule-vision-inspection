"""Qt-free persisted startup loading and service assembly; never starts inspection."""

from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, replace
from pathlib import Path

from capsule_vision.application.editing import RecipeEditorSession
from capsule_vision.application.folder_replay import FolderReplay, FolderSourceError
from capsule_vision.application.history import ProductionHistory
from capsule_vision.application.session import EngineFactory
from capsule_vision.communication.reporting import ProductionReportingCommands
from capsule_vision.configuration import (
    ConfigurationError,
    MachineLayoutDefinition,
    ModelArtifactError,
    ModelRegistry,
    load_camera_sources,
    load_machine_layout,
    resolve_configuration,
)
from capsule_vision.configuration.catalog import RecipeCatalog, discover_models
from capsule_vision.configuration.communication_preferences import (
    CommunicationPreferences,
    WebPreferences,
    load_communication_preferences,
)
from capsule_vision.configuration.drafts import CameraSourceDraft
from capsule_vision.configuration.managed_models import ManagedModels
from capsule_vision.configuration.preferences import (
    ApplicationPreferences,
    read_preferences,
)
from capsule_vision.display import LatestDisplayFrameStore
from capsule_vision.inspection import CameraId
from capsule_vision.web import ProductionSnapshotService
from capsule_vision.web.runtime import WebRuntime

from .application import ProductionApplication
from .composition import create_production_application, unconfigured_application
from .models import (
    PreparedConfiguration,
    prepare_configuration,
)
from .session import real_engine


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
    def communication(self) -> Path:
        return self.root / "communication.json"

    @property
    def history(self) -> Path:
        return self.root / "history.sqlite3"

    @property
    def machine(self) -> Path:
        return self.root / "machine.tsv"

    @property
    def sources(self) -> Path:
        return self.root / "camera_sources.json"

    @property
    def models(self) -> Path:
        return self.root / "models"

    @property
    def recipes(self) -> Path:
        return self.root / "recipes"


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
    prepared: PreparedConfiguration | None
    issues: tuple[StartupIssue, ...]
    preferences_writable: bool
    communication: CommunicationPreferences = CommunicationPreferences()
    communication_error: str = ""


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
            elif kind == "models":
                models = discover_models(path)
                registry = models.registry
                issues.extend(
                    StartupIssue(
                        "startup_models", f"{e.path}: {e.message}", blocking=False
                    )
                    for e in models.issues
                )
            elif kind == "sources":
                sources = load_camera_sources(path)
            else:
                catalog = RecipeCatalog(path).discover()
                entries = catalog.entries
                issues.extend(
                    StartupIssue(
                        "startup_recipes", f"{e.path}: {e.message}", blocking=False
                    )
                    for e in catalog.issues
                )
        except (ConfigurationError, OSError) as exc:
            missing_setup = kind in ("machine", "sources") and isinstance(
                exc.__cause__, FileNotFoundError
            )
            issues.append(
                StartupIssue(
                    "camera_setup" if missing_setup else f"startup_{kind}",
                    f"{path}: {exc}",
                )
            )

    if sources is not None and machine is not None:
        try:
            sources.validate_cameras(machine.layout.camera_ids, complete=False)
        except ConfigurationError as exc:
            sources = None
            issues.append(StartupIssue("startup_sources", f"{data.sources}: {exc}"))

    editor = None
    entry = None
    if registry is not None:

        def model_catalog() -> tuple[ModelRegistry, tuple[str, ...]]:
            snapshot = discover_models(data.models)
            return snapshot.registry, tuple(
                f"{i.path}: {i.message}" for i in snapshot.issues
            )

        editor = RecipeEditorSession(
            RecipeCatalog(data.recipes),
            machine,
            registry,
            data.models,
            machine_path=data.machine,
            camera_sources=CameraSourceDraft(sources, data.sources),
            auto_select_first=False,
            require_saved_sources=True,
            preferences_path=data.preferences,
            model_catalog=model_catalog,
            managed_models=ManagedModels(data.models),
        )
        selected_id = preferences.last_applied_recipe_id if preferences else None
        entry = next(
            (e for e in entries if e.recipe.recipe_id == selected_id),
            None,
        )
        if entry is None:
            issues.append(
                StartupIssue(
                    "startup_recipe_identity",
                    f"{data.preferences}: last applied Recipe ID {selected_id!r} is absent or unselected; no fallback",
                )
            )
        else:
            try:
                editor.load(entry)
            except ConfigurationError as exc:
                entry = None
                issues.append(StartupIssue("startup_recipes", str(exc)))

    prepared: PreparedConfiguration | None = None
    if (
        machine is not None
        and editor is not None
        and entry is not None
        and not any(i.blocking for i in issues)
    ):
        try:
            if sources is not None:
                sources.validate_cameras(
                    editor.machine.layout.camera_ids, complete=sources.mode == "basler"
                )
                sources.validate_mapping()
            prepared = prepare_configuration(
                resolve_configuration(
                    editor.machine,
                    entry.recipe,
                    editor.registry,
                ),
                base_directory=data.models,
            )
            prepared = replace(
                prepared,
                model_path=ManagedModels(data.models).model_path(
                    prepared.configuration.model
                ),
                managed_model=True,
            )
        except ModelArtifactError as exc:
            issues.append(StartupIssue("startup_artifact", str(exc)))
        except ConfigurationError as exc:
            issues.append(StartupIssue("startup_configuration", str(exc)))

    if machine is not None and sources is not None and sources.mode == "folder":
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
        application = unconfigured_application(ProductionHistory(data.history))
        # Shell identity slots only, never a layout/ROI/source/runtime fallback.
        cameras = (
            machine.layout.camera_ids
            if machine
            else tuple(CameraId(f"CAM{i}") for i in range(1, 7))
        )
        frames = LatestDisplayFrameStore(cameras)
    else:
        application, frames = create_production_application(
            editor,
            engine_factory=engine_factory,
            observe_source=observe_source,
            reporting_editable=True,
            history=ProductionHistory(data.history),
            replay_controls=True,
        )
    communication = CommunicationPreferences()
    communication_error = ""
    try:
        communication = load_communication_preferences(data.communication)
    except ConfigurationError as exc:
        if not isinstance(exc.__cause__, FileNotFoundError):
            # Preserve the bad file; safe in-memory recovery is visible and only
            # an explicit settings Save can replace it. Never open a wider bind.
            communication_error = str(exc)
            issues.append(
                StartupIssue(
                    "startup_communication", communication_error, blocking=False
                )
            )
    if isinstance(application, ProductionReportingCommands):
        application.set_reporting_selection(communication.reporting)
    if prepared is not None:
        application.configure(prepared)
    return Startup(
        data,
        preferences,
        application,
        frames,
        machine,
        editor,
        prepared,
        tuple(issues),
        preferences_writable,
        communication,
        communication_error,
    )


def create_web_runtime(startup: Startup) -> WebRuntime:
    """Construct the existing observer owner; starting/binding stays explicit."""

    def factory(settings: WebPreferences) -> ProductionSnapshotService:
        return ProductionSnapshotService(
            startup.application,
            startup.frames,
            host=settings.host,
            port=settings.port,
            interval=settings.interval_ms / 1000,
        )

    return WebRuntime(factory)
