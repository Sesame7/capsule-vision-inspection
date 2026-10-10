"""Qt-free editable drafts; validation and precedence stay in Phase 2B."""

from dataclasses import dataclass, field
from pathlib import Path

from vimo_demo.application import PreparedConfiguration, prepare_configuration
from vimo_demo.configuration import (
    CameraOverride,
    ChannelOverride,
    CheckConfiguration,
    CheckOverrides,
    ConfigurationError,
    MachineLayoutDefinition,
    ModelRegistry,
    RecipeDefinition,
    ResolvedInspectionConfiguration,
    load_camera_sources,
    load_recipe,
    resolve_configuration,
)
from vimo_demo.inspection import CameraId
from vimo_demo.inspection.production import ChannelId
from vimo_demo.inspection.production.capsule import CapsuleSettings

from .camera_source_editor import CameraSourceDraft
from .machine_editor import MachineLayoutDraft
from .recipe_catalog import RecipeCatalog, RecipeEntry


@dataclass
class RecipeDraft:
    recipe_id: str
    display_name: str
    product_code: str | None = None
    model_override_id: str | None = None
    defaults: CheckConfiguration = field(default_factory=CheckConfiguration)
    cameras: dict[CameraId, CheckOverrides] = field(default_factory=lambda: {})
    channels: dict[ChannelId, CheckOverrides] = field(default_factory=lambda: {})

    capsule: CapsuleSettings = field(default_factory=CapsuleSettings)

    @classmethod
    def from_recipe(cls, recipe: RecipeDefinition) -> RecipeDraft:
        return cls(
            recipe.recipe_id,
            recipe.display_name,
            recipe.product_code,
            recipe.model_override_id,
            recipe.defaults,
            {c.camera_id: c.checks for c in recipe.camera_overrides},
            {c.channel_id: c.checks for c in recipe.channel_overrides},
            recipe.capsule,
        )

    def to_recipe(self) -> RecipeDefinition:
        return RecipeDefinition(
            self.recipe_id,
            self.display_name,
            self.defaults,
            self.product_code,
            self.model_override_id,
            tuple(CameraOverride(c, rows) for c, rows in self.cameras.items()),
            tuple(ChannelOverride(c, rows) for c, rows in self.channels.items()),
            capsule=self.capsule,
        )


class RecipeEditorSession:
    """Single GUI owner. Methods never create an engine or mutate an application."""

    def __init__(
        self,
        catalog: RecipeCatalog,
        machine: MachineLayoutDefinition,
        registry: ModelRegistry,
        default_model_id: str,
        base_directory: Path,
        machine_path: Path | None = None,
        camera_sources: CameraSourceDraft | None = None,
        cli_sources: bool = False,
        auto_select_first: bool = True,
        require_saved_sources: bool = False,
    ) -> None:
        self.camera_sources = camera_sources
        self.cli_sources = cli_sources
        self.auto_select_first = auto_select_first
        self.require_saved_sources = require_saved_sources
        self.machine_path = machine_path
        self.machine_editor = MachineLayoutDraft(machine, machine_path)
        self.catalog, self.registry = catalog, registry
        self.default_model_id, self.base_directory = default_model_id, base_directory
        self.entry: RecipeEntry | None = None
        self.draft = RecipeDraft("", "")

    @property
    def machine(self) -> MachineLayoutDefinition:
        return self.machine_editor.saved

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
            self.entry = RecipeEntry(self.entry.path, recipe)
            self.draft = RecipeDraft.from_recipe(recipe)
        else:
            self.draft = RecipeDraft("", "")

    def resolve(
        self, recipe: RecipeDefinition | None = None
    ) -> ResolvedInspectionConfiguration:
        return resolve_configuration(
            self.machine,
            recipe or self.draft.to_recipe(),
            self.registry,
            self.default_model_id,
        )

    def save(self, *, new_identity: tuple[str, str] | None = None) -> None:
        draft = self.draft
        if new_identity is not None:
            from dataclasses import replace

            draft = replace(
                draft, recipe_id=new_identity[0], display_name=new_identity[1]
            )
            if (
                self.entry is not None
                and draft.recipe_id == self.entry.recipe.recipe_id
            ):
                raise ConfigurationError("Save As requires a new Recipe ID")
        recipe = draft.to_recipe()
        self.resolve(recipe)
        entry = self.catalog.save(
            recipe, original=None if new_identity is not None else self.entry
        )
        self.entry = entry
        self.draft = RecipeDraft.from_recipe(recipe)

    def apply_recipe(self) -> RecipeDefinition:
        """Capture immutable saved data on GUI thread, before asynchronous hashing."""
        if self.entry is None or self.dirty or self.machine_editor.dirty:
            raise ConfigurationError("Save changes before Apply")
        saved = load_recipe(self.entry.path)
        if saved != self.entry.recipe:
            raise ConfigurationError("Recipe changed externally; reload before Apply")
        self.resolve(saved)
        return saved

    def prepare(self, recipe: RecipeDefinition) -> PreparedConfiguration:
        """Background preparation only; immutable inputs, no draft/application access."""
        prepared = prepare_configuration(
            self.resolve(recipe), base_directory=self.base_directory
        )
        if self.require_saved_sources:
            if self.catalog.discover().issues:
                raise ConfigurationError(
                    "Recipe catalog has invalid or duplicate files; repair before Apply"
                )
            if self.camera_sources is None:
                raise ConfigurationError("Saved camera sources are unavailable")
            path = self.camera_sources.path
            load_camera_sources(path).validate_cameras(
                prepared.configuration.layout.camera_ids
            )
            # Folder availability is acquisition preflight, not candidate validity.
            # The unchanged local session factory checks it before engine creation.
        return prepared
