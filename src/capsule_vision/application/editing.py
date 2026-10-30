"""Stopped Recipe editing and preparation; persistence and activation stay distinct."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from capsule_vision.configuration import (
    CameraSources,
    ConfigurationError,
    ConfigurationIOError,
    MachineLayoutDefinition,
    ModelRegistry,
    RecipeDefinition,
    ResolvedInspectionConfiguration,
    load_camera_sources,
    load_machine_layout,
    load_model_definition,
    load_recipe,
    resolve_configuration,
)
from capsule_vision.configuration.catalog import RecipeCatalog, RecipeEntry
from capsule_vision.configuration.drafts import (
    CameraSourceDraft,
    MachineLayoutDraft,
    RecipeDraft,
)
from capsule_vision.configuration.managed_models import ManagedModels
from capsule_vision.configuration.preferences import load_preferences

from .basler_connection import ConnectionState, SourceConnectionSnapshot
from .models import PreparedConfiguration, prepare_configuration


@dataclass(frozen=True, slots=True)
class SavedVision:
    machine: MachineLayoutDefinition | None
    sources: CameraSources | None


@dataclass(frozen=True, slots=True)
class VisionSaveResult:
    machine_saved: bool
    sources_saved: bool
    errors: tuple[str, ...]


class RecipeEditorSession:
    """Single editing owner. Methods never create an engine or mutate an application."""

    def __init__(
        self,
        catalog: RecipeCatalog,
        machine: MachineLayoutDefinition | None,
        registry: ModelRegistry,
        base_directory: Path,
        machine_path: Path | None = None,
        camera_sources: CameraSourceDraft | None = None,
        cli_sources: bool = False,
        auto_select_first: bool = True,
        require_saved_sources: bool = False,
        preferences_path: Path | None = None,
        model_catalog: Callable[[], tuple[ModelRegistry, tuple[str, ...]]]
        | None = None,
        managed_models: ManagedModels | None = None,
    ) -> None:
        self.camera_sources = camera_sources
        self.cli_sources = cli_sources
        self.auto_select_first = auto_select_first
        self.require_saved_sources = require_saved_sources
        self.preferences_path = preferences_path
        self.model_catalog = model_catalog
        self.managed_models = managed_models
        self.model_issues: tuple[str, ...] = ()
        self.machine_path = machine_path
        self.machine_editor = MachineLayoutDraft(machine, machine_path)
        self.catalog, self.registry = catalog, registry
        self.base_directory = base_directory
        self.entry: RecipeEntry | None = None
        self.draft = RecipeDraft("", "")

    @property
    def machine(self) -> MachineLayoutDefinition:
        return self.machine_editor.saved

    def reload_models(self) -> None:
        """Metadata only, on explicit stopped-editor refresh; never select a Model."""
        if self.model_catalog is not None:
            self.registry, self.model_issues = self.model_catalog()

    @property
    def dirty(self) -> bool:
        baseline = (
            RecipeDraft("", "")
            if self.entry is None
            else RecipeDraft.from_recipe(self.entry.recipe)
        )
        return self.draft != baseline

    def load(self, entry: RecipeEntry) -> None:
        if self.dirty:
            raise ConfigurationError("Resolve unsaved changes before selection")
        self.entry = RecipeEntry(entry.path, load_recipe(entry.path))
        self.draft = RecipeDraft.from_recipe(self.entry.recipe)

    def new(self) -> None:
        if self.dirty:
            raise ConfigurationError("Resolve unsaved changes before New")
        self.entry = None
        self.draft = RecipeDraft("", "")

    def discard(self) -> None:
        if self.entry is not None:
            recipe = load_recipe(self.entry.path)
            if recipe.recipe_id != self.entry.recipe.recipe_id:
                raise ConfigurationError(
                    "Recipe identity changed externally; reload explicitly"
                )
            self.entry = RecipeEntry(self.entry.path, recipe)
            self.draft = RecipeDraft.from_recipe(recipe)
        else:
            self.draft = RecipeDraft("", "")

    def delete(self, *, protected_ids: frozenset[str]) -> None:
        if self.entry is None:
            raise ConfigurationError("Select a saved Recipe before Delete")
        if self.preferences_path is not None:
            # Read the actual manifest, including an earlier applied identity
            # retained after a preference-write failure. Unknown state fails closed.
            persisted = load_preferences(self.preferences_path).last_applied_recipe_id
            if persisted is not None:
                protected_ids = protected_ids | {persisted}
        self.catalog.delete(self.entry, protected_ids=protected_ids)
        self.entry = None
        self.draft = RecipeDraft("", "")

    def resolve(
        self, recipe: RecipeDefinition | None = None
    ) -> ResolvedInspectionConfiguration:
        return resolve_configuration(
            self.machine,
            recipe or self.draft.to_recipe(),
            self.registry,
        )

    def save(self) -> None:
        recipe = self.draft.to_recipe()
        entry = (
            self.catalog.save_generated(recipe)
            if self.entry is None
            else self.catalog.save(recipe, original=self.entry)
        )
        self.entry = entry
        self.draft = RecipeDraft.from_recipe(entry.recipe)

    def read_vision(self) -> SavedVision:
        """Read and validate both definitions without replacing either draft."""
        draft = self.machine_editor
        if draft.path is None:
            raise ConfigurationError("Saved Vision paths unavailable")
        cameras = (
            draft.saved_definition.layout.camera_ids
            if draft.saved_definition
            else draft.camera_ids
        )
        try:
            machine = load_machine_layout(draft.path)
        except ConfigurationIOError as exc:
            if not isinstance(exc.__cause__, FileNotFoundError):
                raise
            machine = None
        if (
            machine is not None
            and machine.layout.camera_ids != cameras
            and len(machine.layout.cameras) == len(cameras)
        ):
            raise ConfigurationError(
                "Saved camera order/identities changed; reopen the application before reloading"
            )
        try:
            sources = (
                load_camera_sources(self.camera_sources.path)
                if self.camera_sources is not None
                else None
            )
        except ConfigurationIOError as exc:
            if not isinstance(exc.__cause__, FileNotFoundError):
                raise
            sources = None
        if sources is not None and machine is not None:
            sources.validate_cameras(machine.layout.camera_ids, complete=False)
        return SavedVision(machine, sources)

    def accept_vision(
        self, saved: SavedVision, connection: SourceConnectionSnapshot | None
    ) -> None:
        if (
            self.camera_sources is not None
            and connection is not None
            and connection.locked
            and saved.sources != self.camera_sources.saved
        ):
            raise ConfigurationError(
                "Disconnect before reloading externally changed camera sources"
            )
        if self.camera_sources is not None:
            self.camera_sources.load_saved(saved.sources)
        self.machine_editor.load_saved(saved.machine)

    def validate_vision_save(self, connection: SourceConnectionSnapshot | None) -> None:
        draft = self.machine_editor
        if not draft.complete:
            return
        machine, previous = draft.definition, draft.saved_definition
        if previous is None or previous.layout.camera_ids != machine.layout.camera_ids:
            if (
                connection is not None
                and connection.locked
                and tuple(c.camera_id for c in connection.cameras)
                != machine.layout.camera_ids
            ):
                raise ConfigurationError(
                    "Disconnect before changing configured cameras"
                )
        if connection is not None and connection.state is ConnectionState.CONNECTED:
            for camera, actual in zip(
                machine.layout.cameras, connection.cameras, strict=True
            ):
                camera.validate_shape((actual.height, actual.width, 3))

    def save_vision(
        self,
        connection: SourceConnectionSnapshot | None,
        validation_error: BaseException | None,
    ) -> VisionSaveResult:
        """Independent file outcomes; failed validation never discards unsaved drafts."""
        errors: list[str] = []
        machine_saved = sources_saved = False
        if self.camera_sources is not None:
            try:
                if (
                    connection is not None
                    and connection.locked
                    and self.camera_sources.dirty
                ):
                    raise ConfigurationError(
                        "Disconnect before changing camera sources"
                    )
                self.camera_sources.save()
                sources_saved = True
            except (ValueError, OSError) as exc:
                errors.append(f"CameraSources: {type(exc).__name__}: {exc}")
        if validation_error is not None:
            errors.append(
                f"MachineLayout: {type(validation_error).__name__}: {validation_error}"
            )
        elif self.machine_editor.complete:
            try:
                self.machine_editor.save()
                machine_saved = True
            except (ValueError, OSError) as exc:
                errors.append(f"MachineLayout: {type(exc).__name__}: {exc}")
        return VisionSaveResult(machine_saved, sources_saved, tuple(errors))

    def apply_recipe(self) -> RecipeDefinition:
        """Capture immutable saved data before asynchronous hashing."""
        if self.entry is None or self.dirty or self.machine_editor.dirty:
            raise ConfigurationError("Save changes before Apply")
        saved = load_recipe(self.entry.path)
        if saved != self.entry.recipe:
            raise ConfigurationError("Recipe changed externally; reload before Apply")
        if self.entry not in self.catalog.discover().entries:
            raise ConfigurationError(
                "Selected Recipe is invalid or ambiguous; reload before Apply"
            )
        self.resolve(saved)
        return saved

    def prepare(self, recipe: RecipeDefinition) -> PreparedConfiguration:
        """Background preparation only; immutable inputs, no draft/application access."""
        configuration = self.resolve(recipe)
        if self.require_saved_sources:
            metadata = Path(configuration.model.model_path).parent / "model.json"
            if load_model_definition(metadata) != configuration.model:
                raise ConfigurationError(
                    "Selected Model definition changed externally; reload before Apply"
                )
        prepared = prepare_configuration(
            configuration, base_directory=self.base_directory
        )
        if self.managed_models is not None:
            prepared = replace(
                prepared,
                model_path=self.managed_models.model_path(configuration.model),
                managed_model=True,
            )
        if self.require_saved_sources:
            current = self.catalog.discover()
            if not any(e.recipe == recipe for e in current.entries):
                raise ConfigurationError(
                    "Selected Recipe is invalid or ambiguous; reload before Apply"
                )
            if self.camera_sources is None:
                raise ConfigurationError("Saved camera sources are unavailable")
        if self.camera_sources is not None:
            sources = (
                load_camera_sources(self.camera_sources.path)
                if self.require_saved_sources
                else self.camera_sources.saved
            )
            sources.validate_cameras(
                prepared.configuration.layout.camera_ids,
                complete=sources.mode == "basler",
            )
            sources.validate_mapping()
            # Folder availability is acquisition preflight, not candidate validity.
            # The unchanged local session factory checks it before engine creation.
        return prepared
