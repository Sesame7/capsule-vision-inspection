"""Qt-free editable drafts; validation and precedence stay in Phase 2B."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from vimo_demo.application import PreparedConfiguration, prepare_configuration
from vimo_demo.configuration import (
    BlobCheckConfig,
    BlobOverride,
    CameraOverride,
    ChannelOverride,
    CheckConfig,
    CheckConfiguration,
    CheckOverride,
    CheckOverrides,
    ConfigurationError,
    MachineLayoutDefinition,
    ModelRegistry,
    RecipeDefinition,
    ResolvedInspectionConfiguration,
    ViMoCheckConfig,
    ViMoOverride,
    load_recipe,
    resolve_configuration,
)
from vimo_demo.inference import BoundingRect
from vimo_demo.inspection import CameraId
from vimo_demo.inspection.production import ChannelId

from .recipe_catalog import RecipeCatalog, RecipeEntry

type Group = Literal["presence", "blob", "color", "vimo"]
GROUPS: tuple[Group, ...] = ("presence", "blob", "color", "vimo")


def number(value: str | None, field_name: str) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigurationError(f"{field_name}: expected an integer") from exc


@dataclass
class CheckDraft:
    enabled: bool | None = None
    required: bool | None = None
    min_area: str | None = None
    max_area: str | None = None
    disabled_labels: tuple[str, ...] | None = None

    @classmethod
    def from_check(cls, check: CheckConfig | CheckOverride) -> CheckDraft:
        result = cls(check.enabled, check.required)
        if isinstance(check, BlobCheckConfig | BlobOverride):
            result.min_area = None if check.min_area is None else str(check.min_area)
            result.max_area = None if check.max_area is None else str(check.max_area)
        if isinstance(check, ViMoCheckConfig | ViMoOverride):
            result.disabled_labels = check.disabled_labels
        return result


def draft_checks(
    checks: CheckConfiguration | CheckOverrides,
) -> dict[Group, CheckDraft]:
    values: tuple[tuple[Group, CheckConfig | CheckOverride], ...] = (
        ("presence", checks.presence),
        ("blob", checks.blob),
        ("color", checks.color),
        ("vimo", checks.vimo),
    )
    return {name: CheckDraft.from_check(value) for name, value in values}


def overrides(rows: dict[Group, CheckDraft]) -> CheckOverrides:
    def policy(name: Group) -> CheckOverride:
        row = rows[name]
        return CheckOverride(row.enabled, row.required)

    blob, vimo = rows["blob"], rows["vimo"]
    return CheckOverrides(
        presence=policy("presence"),
        blob=BlobOverride(
            blob.enabled,
            blob.required,
            number(blob.min_area, "blob.min_area"),
            number(blob.max_area, "blob.max_area"),
        ),
        color=policy("color"),
        vimo=ViMoOverride(vimo.enabled, vimo.required, vimo.disabled_labels),
    )


@dataclass
class RecipeDraft:
    recipe_id: str
    display_name: str
    product_code: str | None = None
    model_override_id: str | None = None
    defaults: dict[Group, CheckDraft] = field(
        default_factory=lambda: draft_checks(CheckConfiguration())
    )
    cameras: dict[CameraId, dict[Group, CheckDraft]] = field(default_factory=lambda: {})
    channels: dict[ChannelId, dict[Group, CheckDraft]] = field(
        default_factory=lambda: {}
    )
    rois: dict[ChannelId, tuple[str, str, str, str]] = field(default_factory=lambda: {})

    @classmethod
    def from_recipe(cls, recipe: RecipeDefinition) -> RecipeDraft:
        return cls(
            recipe.recipe_id,
            recipe.display_name,
            recipe.product_code,
            recipe.model_override_id,
            draft_checks(recipe.defaults),
            {c.camera_id: draft_checks(c.checks) for c in recipe.camera_overrides},
            {c.channel_id: draft_checks(c.checks) for c in recipe.channel_overrides},
            {
                c.channel_id: (
                    str(c.roi.x),
                    str(c.roi.y),
                    str(c.roi.width),
                    str(c.roi.height),
                )
                for c in recipe.channel_overrides
                if c.roi is not None
            },
        )

    def to_recipe(self) -> RecipeDefinition:
        rows = self.defaults

        def policy(name: Group) -> CheckConfig:
            row = rows[name]
            if row.enabled is None or row.required is None:
                raise ConfigurationError(
                    f"defaults.{name}: enabled/required need explicit values"
                )
            return CheckConfig(row.enabled, row.required)

        blob, vimo = policy("blob"), policy("vimo")
        labels = rows["vimo"].disabled_labels
        if labels is None:
            raise ConfigurationError(
                "defaults.vimo.disabled_labels needs an explicit list"
            )
        defaults = CheckConfiguration(
            policy("presence"),
            BlobCheckConfig(
                blob.enabled,
                blob.required,
                number(rows["blob"].min_area, "blob.min_area"),
                number(rows["blob"].max_area, "blob.max_area"),
            ),
            policy("color"),
            ViMoCheckConfig(vimo.enabled, vimo.required, labels),
        )
        channels: list[ChannelOverride] = []
        for channel in sorted(self.channels.keys() | self.rois.keys()):
            checks = (
                overrides(self.channels[channel])
                if channel in self.channels
                else CheckOverrides()
            )
            roi = None
            if channel in self.rois:
                values = tuple(number(s, "ROI") for s in self.rois[channel])
                if any(v is None for v in values):
                    raise ConfigurationError("ROI requires four integers")
                # Every value originates from a required string and was parsed above.
                x, y, width, height = (int(s) for s in self.rois[channel])
                roi = BoundingRect(x, y, width, height)
            channels.append(ChannelOverride(channel, checks, roi))
        return RecipeDefinition(
            self.recipe_id,
            self.display_name,
            defaults,
            self.product_code,
            self.model_override_id,
            tuple(
                CameraOverride(c, overrides(rows)) for c, rows in self.cameras.items()
            ),
            tuple(channels),
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
    ) -> None:
        self.catalog, self.machine, self.registry = catalog, machine, registry
        self.default_model_id, self.base_directory = default_model_id, base_directory
        self.entry: RecipeEntry | None = None
        self.draft = RecipeDraft("", "")

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
        if self.entry is None or self.dirty:
            raise ConfigurationError("Save changes before Apply")
        saved = load_recipe(self.entry.path)
        if saved != self.entry.recipe:
            raise ConfigurationError("Recipe changed externally; reload before Apply")
        self.resolve(saved)
        return saved

    def prepare(self, recipe: RecipeDefinition) -> PreparedConfiguration:
        """Background preparation only; immutable inputs, no draft/application access."""
        return prepare_configuration(
            self.resolve(recipe), base_directory=self.base_directory
        )
