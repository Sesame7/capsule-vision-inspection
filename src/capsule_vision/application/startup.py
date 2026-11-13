"""Qt-free persisted startup loading and service assembly; never starts inspection."""

from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, replace
from pathlib import Path

from capsule_vision.application.audit import AuditOperation
from capsule_vision.application.editing import RecipeEditorSession
from capsule_vision.application.folder_replay import FolderReplay, FolderSourceError
from capsule_vision.application.history import ProductionHistory
from capsule_vision.application.identity import LocalIdentity, admit_engineering
from capsule_vision.application.identity_models import Role
from capsule_vision.application.session import EngineFactory
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
from capsule_vision.diagnostics import RuntimeDiagnostics
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
from .recipe_dependencies import RecipeDependencies
from .run_configuration import capture_prepared
from .session import real_engine
from .vision_dependencies import VisionDependencies


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
    def audit(self) -> Path:
        return self.root / "audit.sqlite3"

    @property
    def security(self) -> Path:
        return self.root / "security.sqlite3"

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

    @property
    def process_catalog(self) -> Path:
        return self.root / "process_parameters.json"


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
    security_error: str = ""


def load_startup(
    data: ApplicationData,
    *,
    engine_factory: EngineFactory = real_engine,
    observe_source: Callable[[Future[None]], None] = lambda _: None,
    diagnostics: RuntimeDiagnostics | None = None,
) -> Startup:
    """Acquire local identity ownership before any application resources exist."""
    identity = LocalIdentity(data.security, diagnostics=diagnostics)
    try:
        identity.ready.result()  # Explicit blocking startup IO, before Qt exists.
        return _load_startup(
            data,
            identity,
            engine_factory=engine_factory,
            observe_source=observe_source,
            diagnostics=diagnostics,
        )
    except BaseException:
        identity.request_stop()
        identity.join()
        raise


def _load_startup(
    data: ApplicationData,
    identity: LocalIdentity,
    *,
    engine_factory: EngineFactory,
    observe_source: Callable[[Future[None]], None],
    diagnostics: RuntimeDiagnostics | None,
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
                catalog = RecipeCatalog(
                    path, process_catalog_path=data.process_catalog
                ).discover()
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

        def on_cancel(digest: str, revision: int | None) -> Callable[[], None]:
            with identity.admission(Role.ENGINEER, revision) as actor:
                operation = AuditOperation.by(actor, "model.import", digest)
            return lambda: identity.audit.record(operation, "cancelled")

        def model_catalog() -> tuple[ModelRegistry, tuple[str, ...]]:
            snapshot = discover_models(data.models)
            return snapshot.registry, tuple(
                f"{i.path}: {i.message}" for i in snapshot.issues
            )

        editor = RecipeEditorSession(
            RecipeCatalog(data.recipes, process_catalog_path=data.process_catalog),
            machine,
            registry,
            data.models,
            machine_path=data.machine,
            camera_sources=CameraSourceDraft(sources, data.sources),
            auto_select_first=False,
            require_saved_sources=True,
            preferences_path=data.preferences,
            model_catalog=model_catalog,
            managed_models=ManagedModels(
                data.models,
                authorize=lambda revision: admit_engineering(identity, revision),
                change=lambda action, target, revision, before, after: identity.change(
                    action,
                    target,
                    revision,
                    before,
                    after,
                    uncertain=lambda error: (
                        action == "model.delete" and isinstance(error, OSError)
                    ),
                ),
                on_cancel=on_cancel,
            ),
            identity=identity,
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
                    process_catalog=entry.process_catalog,
                ),
                base_directory=data.models,
            )
            prepared = replace(
                prepared,
                model_path=ManagedModels(data.models).model_path(
                    prepared.configuration.model
                ),
                managed_model=True,
                recipe_dependencies=RecipeDependencies(
                    entry, editor.catalog.process_catalog_path
                ),
                vision_dependencies=VisionDependencies(
                    data.machine, editor.machine, data.sources, sources
                ),
            )
            assert prepared.vision_dependencies is not None
            prepared.vision_dependencies.validate_current()
            assert prepared.recipe_dependencies is not None
            prepared.recipe_dependencies.validate_current()
            prepared = capture_prepared(prepared)
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

    communication = CommunicationPreferences()
    communication_error = ""
    communication_content = None
    try:
        status_errors: list[str] = []
        communication_content = data.communication.read_bytes()
        communication = load_communication_preferences(
            data.communication,
            status_error=status_errors.append,
            content=communication_content,
        )
        if status_errors:
            communication_content = None
            communication_error = status_errors[0]
            issues.append(
                StartupIssue(
                    "startup_communication", communication_error, blocking=False
                )
            )
    except (ConfigurationError, OSError) as exc:
        communication_content = None
        if not isinstance(exc, FileNotFoundError) and not isinstance(
            exc.__cause__, FileNotFoundError
        ):
            # Preserve the bad file; safe in-memory recovery is visible and only
            # an explicit settings Save can replace it. Never open a wider bind.
            communication_error = str(exc)
            issues.append(
                StartupIssue(
                    "startup_communication", communication_error, blocking=False
                )
            )

    def history_error(kind: str) -> None:
        identity.audit.record(
            AuditOperation("system", "System", "history.storage", data.history.name),
            "failed",
            kind,
        )

    def alarm_access(revision: int | None) -> int:
        with identity.admission(Role.OPERATOR, revision) as actor:
            return actor.revision

    def configuration_access(revision: int | None) -> int:
        with identity.admission(Role.ENGINEER, revision) as actor:
            return actor.revision

    history = ProductionHistory(
        data.history,
        on_failure=history_error,
        alarm_access=alarm_access,
        configuration_access=configuration_access,
        diagnostics=diagnostics,
    )
    if editor is None or editor.camera_sources is None:
        application = unconfigured_application(
            history,
            communication_path=data.communication,
            diagnostics=diagnostics,
            identity=identity,
            initial_reporting=communication.reporting,
            initial_communication=communication_content,
            initial_status=communication.status,
        )
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
            communication_path=data.communication,
            diagnostics=diagnostics,
            engine_factory=engine_factory,
            observe_source=observe_source,
            reporting_editable=True,
            history=history,
            identity=identity,
            replay_controls=True,
            initial_candidate=prepared,
            initial_reporting=communication.reporting,
            initial_communication=communication_content,
            initial_status=communication.status,
        )
    if diagnostics:
        for issue in issues:
            diagnostics.record("startup.issue", code=issue.key, blocking=issue.blocking)
        diagnostics.record(
            "startup.loaded",
            recipe_id=prepared.configuration.recipe_id if prepared else None,
        )
    startup = Startup(
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
        identity.storage_snapshot().issue,
    )
    identity.begin_startup_login()
    return startup


def create_web_runtime(startup: Startup, *, restore_saved: bool = False) -> WebRuntime:
    """Only startup composition can request restoration of saved enablement."""

    def factory(settings: WebPreferences) -> ProductionSnapshotService:
        return ProductionSnapshotService(
            startup.application,
            startup.frames,
            host=settings.host,
            port=settings.port,
            interval=settings.interval_ms / 1000,
        )

    return WebRuntime(
        factory,
        authorize=startup.application.authorize_engineering,
        initial_settings=startup.communication.web if restore_saved else None,
    )
