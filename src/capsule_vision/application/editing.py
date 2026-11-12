"""Stopped Recipe editing and preparation; persistence and activation stay distinct."""

from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, replace
from pathlib import Path

from capsule_vision.configuration import (
    CameraSources,
    ConfigurationError,
    ConfigurationIOError,
    ConfigurationPublication,
    MachineLayoutDefinition,
    ModelRegistry,
    RecipeDefinition,
    ResolvedInspectionConfiguration,
    load_camera_sources,
    load_machine_layout,
    load_model_definition,
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
from capsule_vision.configuration.process_parameters import BUILTIN_PROCESS_CATALOG

from .audit import (
    AuditOperation,
    AuditUnavailable,
    RevisionConfirmationError,
    RevisionEvidence,
    differences,
    error_outcome,
)
from .basler_connection import ConnectionState, SourceConnectionSnapshot
from .identity import LocalIdentity, admit_engineering
from .identity_models import IdentityError, Role
from .models import PreparedConfiguration, prepare_configuration
from .recipe_dependencies import RecipeDependencies
from .run_configuration import capture_prepared
from .vision_dependencies import VisionDependencies, read_sources


@dataclass(frozen=True, slots=True)
class SavedVision:
    machine: MachineLayoutDefinition | None
    sources: CameraSources | None


@dataclass(frozen=True, slots=True)
class VisionSaveResult:
    machine_saved: bool
    sources_saved: bool
    errors: tuple[str, ...]
    permission_error: IdentityError | None = None
    unconfirmed: frozenset[str] = frozenset()


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
        fixed_sources: CameraSources | None = None,
        identity: LocalIdentity | None = None,
    ) -> None:
        self.identity = identity
        self.camera_sources = camera_sources
        self.cli_sources = cli_sources
        self.fixed_sources = fixed_sources
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
        self.process_catalog = BUILTIN_PROCESS_CATALOG
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
        self.entry = self.catalog.read(entry.path)
        self.process_catalog = self.entry.process_catalog
        self.draft = RecipeDraft.from_recipe(self.entry.recipe)

    def new(self) -> None:
        if self.dirty:
            raise ConfigurationError("Resolve unsaved changes before New")
        self.process_catalog = self.catalog.parameters()
        self.entry = None
        self.draft = RecipeDraft("", "")

    def discard(self) -> None:
        if self.entry is not None:
            loaded = self.catalog.read(self.entry.path)
            recipe = loaded.recipe
            if recipe.recipe_id != self.entry.recipe.recipe_id:
                raise ConfigurationError(
                    "Recipe identity changed externally; reload explicitly"
                )
            self.entry = loaded
            self.process_catalog = loaded.process_catalog
            self.draft = RecipeDraft.from_recipe(recipe)
        else:
            self.process_catalog = self.catalog.parameters()
            self.draft = RecipeDraft("", "")

    def delete(
        self, *, protected_ids: frozenset[str], revision: int | None = None
    ) -> None:
        revision = admit_engineering(self.identity, revision)
        if self.entry is None:
            raise ConfigurationError("Select a saved Recipe before Delete")
        if self.preferences_path is not None:
            # Read the actual manifest, including an earlier applied identity
            # retained after a preference-write failure. Unknown state fails closed.
            persisted = load_preferences(self.preferences_path).last_applied_recipe_id
            if persisted is not None:
                protected_ids = protected_ids | {persisted}
        with self._change(
            "recipe.delete",
            self.entry.recipe.recipe_id,
            revision,
            self.entry.recipe,
            None,
        ):
            self.catalog.delete(self.entry, protected_ids=protected_ids)
        self.entry = None
        self.draft = RecipeDraft("", "")

    def resolve(
        self, recipe: RecipeDefinition | None = None
    ) -> ResolvedInspectionConfiguration:
        return resolve_configuration(
            self.machine,
            recipe or self.draft.to_recipe(self.process_catalog),
            self.registry,
            process_catalog=self.process_catalog,
        )

    def save(self, revision: int | None = None) -> RecipeEntry | None:
        """Persist independently; return evidence only for a changed definition."""
        admission = (
            self.identity.admission(Role.ENGINEER, revision)
            if self.identity
            else nullcontext(None)
        )
        with admission as actor:
            revision = actor.revision if actor else None
            try:
                if self.catalog.parameters() != self.process_catalog:
                    raise ConfigurationError(
                        "Process catalog changed; reload before Save"
                    )
                recipe = self.draft.to_recipe(self.process_catalog)
            except ValueError as exc:
                if actor and self.identity:
                    self.identity.audit.record(
                        AuditOperation.by(actor, "recipe.save", self.draft.recipe_id),
                        "failed",
                        type(exc).__name__,
                    )
                raise
        original = self.entry

        def publish(value: RecipeDefinition) -> RecipeEntry:
            return self._save_file(
                "recipe.save",
                "recipe",
                value.recipe_id,
                revision,
                original.recipe if original else None,
                value,
                lambda write: self.catalog.save(
                    value,
                    original=original,
                    process_catalog=self.process_catalog,
                    publish=write,
                ),
            )

        entry = (
            self.catalog.save_generated(recipe, publish=publish)
            if original is None
            else publish(recipe)
        )
        self.entry = entry
        self.draft = RecipeDraft.from_recipe(entry.recipe)
        return entry if original is None or original.recipe != entry.recipe else None

    def _save_file[T](
        self,
        action: str,
        artifact: str,
        resource: str,
        revision: int | None,
        old: object,
        new: object,
        save: Callable[[Callable[[ConfigurationPublication], None]], T],
    ) -> T:
        """Prepare/validate through the codec, then audit the very bytes it publishes."""
        identity = self.identity
        if identity is None:
            return save(lambda publication: publication.publish())
        before, after = differences(old, new)
        with identity.admission(
            Role.ENGINEER, revision, action=action, target=resource
        ) as actor:
            pass
        admitted = False

        def publish(publication: ConfigurationPublication) -> None:
            nonlocal admitted
            admitted = True
            evidence = (
                RevisionEvidence(artifact, resource, publication.content)
                if old != new
                else None
            )
            with identity.change(
                action, resource, actor.revision, before, after, evidence=evidence
            ):
                publication.publish()

        try:
            return save(publish)
        except BaseException as exc:
            if not admitted:
                identity.audit.record(
                    AuditOperation.by(actor, action, resource, before, after),
                    error_outcome(exc),
                    type(exc).__name__,
                )
            raise

    def _change(
        self, action: str, target: str, revision: int | None, old: object, new: object
    ) -> AbstractContextManager[None]:
        if self.identity is None:
            return nullcontext()
        return self.identity.change(action, target, revision, *differences(old, new))

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
        revision: int | None = None,
    ) -> VisionSaveResult:
        """Independent file outcomes; failed validation never discards unsaved drafts."""
        if self.identity:
            with self.identity.admission(Role.ENGINEER, revision) as actor:
                revision = actor.revision
                if validation_error is not None:
                    path = self.machine_editor.path
                    self.identity.audit.record(
                        AuditOperation.by(
                            actor,
                            "vision.layout",
                            path.name if path else "MachineLayout",
                        ),
                        error_outcome(validation_error),
                        type(validation_error).__name__,
                    )
        errors: list[str] = []
        permission_error = None
        unconfirmed: set[str] = set()
        machine_saved = sources_saved = False
        if self.camera_sources is not None:
            try:
                draft = self.camera_sources

                def sources_save(
                    write: Callable[[ConfigurationPublication], None],
                ) -> None:
                    if connection is not None and connection.locked and draft.dirty:
                        raise ConfigurationError(
                            "Disconnect before changing camera sources"
                        )
                    draft.save(publish=write)

                self._save_file(
                    "vision.sources",
                    "sources",
                    "camera_sources.json",
                    revision,
                    draft.saved if draft.persisted else None,
                    draft.definition,
                    sources_save,
                )
                sources_saved = True
            except (IdentityError, AuditUnavailable, ValueError, OSError) as exc:
                if isinstance(exc, RevisionConfirmationError):
                    unconfirmed.add("CameraSources")
                if isinstance(exc, IdentityError):
                    permission_error = exc
                errors.append(f"CameraSources: {type(exc).__name__}: {exc}")
        if validation_error is not None:
            errors.append(
                f"MachineLayout: {type(validation_error).__name__}: {validation_error}"
            )
        elif self.machine_editor.complete:
            try:
                admit_engineering(self.identity, revision)
                machine = self.machine_editor
                self._save_file(
                    "vision.layout",
                    "machine",
                    "machine.tsv",
                    revision,
                    machine.saved_definition,
                    machine.definition,
                    lambda write: machine.save(publish=write),
                )
                machine_saved = True
            except (IdentityError, AuditUnavailable, ValueError, OSError) as exc:
                if isinstance(exc, RevisionConfirmationError):
                    unconfirmed.add("MachineLayout")
                if isinstance(exc, IdentityError):
                    permission_error = exc
                errors.append(f"MachineLayout: {type(exc).__name__}: {exc}")
        return VisionSaveResult(
            machine_saved,
            sources_saved,
            tuple(errors),
            permission_error,
            frozenset(unconfirmed),
        )

    def production_preparation(self) -> RecipeEditorSession:
        """Capture saved dependencies without sharing the editor's working copies."""
        return RecipeEditorSession(
            self.catalog,
            self.machine_editor.saved_definition,
            self.registry,
            self.base_directory,
            machine_path=self.machine_path,
            camera_sources=(
                CameraSourceDraft(self.camera_sources.saved, self.camera_sources.path)
                if self.camera_sources is not None
                else None
            ),
            cli_sources=self.cli_sources,
            fixed_sources=self.fixed_sources,
            identity=self.identity,
            require_saved_sources=self.require_saved_sources,
            model_catalog=self.model_catalog,
            managed_models=self.managed_models,
        )

    def prepare_saved(self, recipe_id: str) -> PreparedConfiguration:
        """Resolve the current saved identity off dispatch; never read an editor draft."""
        entry = next(
            (
                e
                for e in self.catalog.discover().entries
                if e.recipe.recipe_id == recipe_id
            ),
            None,
        )
        if entry is None:
            raise ConfigurationError(
                "Saved Recipe is unavailable or ambiguous; refresh"
            )
        self.process_catalog = entry.process_catalog
        if self.machine_path is not None:
            self.machine_editor.load_saved(load_machine_layout(self.machine_path))
        self.reload_models()
        prepared = self.prepare(entry.recipe)
        if entry not in self.catalog.discover().entries:
            raise ConfigurationError(
                "Saved Recipe changed during preparation; select again"
            )
        return capture_prepared(
            replace(
                prepared,
                recipe_dependencies=RecipeDependencies(
                    entry, self.catalog.process_catalog_path
                ),
            )
        )

    def prepare(self, recipe: RecipeDefinition) -> PreparedConfiguration:
        """Background preparation only; immutable inputs, no draft/application access."""
        if self.catalog.parameters() != self.process_catalog:
            raise ConfigurationError(
                "Process catalog changed; reload before preparation"
            )
        configuration = self.resolve(recipe)
        if self.require_saved_sources:
            metadata = Path(configuration.model.model_path).parent / "model.json"
            if load_model_definition(metadata) != configuration.model:
                raise ConfigurationError(
                    "Selected Model definition changed externally; select the Recipe again"
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
                    "Selected Recipe is invalid or ambiguous; select the Recipe again"
                )
            if self.camera_sources is None:
                raise ConfigurationError("Saved camera sources are unavailable")
        sources = None
        sources_path = None
        if self.camera_sources is not None:
            sources_path = None if self.cli_sources else self.camera_sources.path
            sources = (
                self.fixed_sources
                if self.cli_sources and self.fixed_sources is not None
                else read_sources(sources_path)
                if sources_path is not None
                else self.camera_sources.saved
            )
            if self.require_saved_sources and sources is None:
                raise ConfigurationError("Saved camera sources are unavailable")
            if sources is not None:
                sources.validate_cameras(
                    prepared.configuration.layout.camera_ids,
                    complete=sources.mode == "basler",
                )
                sources.validate_mapping()
            # Folder availability is acquisition preflight, not candidate validity.
            # The unchanged local session factory checks it before engine creation.
        if self.machine_path is not None:
            dependencies = VisionDependencies(
                self.machine_path,
                self.machine,
                sources_path,
                sources,
            )
            dependencies.validate_current()
            prepared = replace(prepared, vision_dependencies=dependencies)
        return prepared
