"""Pure resolution and separately requested artifact verification."""

import hashlib
from pathlib import Path

from vimo_demo.inspection.production import CameraLayout, ChannelROI, InspectionLayout

from .errors import ConfigurationError, ModelArtifactError
from .models import (
    BlobCheckConfig,
    CheckConfig,
    CheckConfiguration,
    CheckOverride,
    CheckOverrides,
    MachineLayoutDefinition,
    ModelDefinition,
    ModelRegistry,
    RecipeDefinition,
    ResolvedChannelConfiguration,
    ResolvedInspectionConfiguration,
    ViMoCheckConfig,
)


def _inherit[T](base: T, override: T | None) -> T:
    return base if override is None else override


def _policy(base: CheckConfig, override: CheckOverride) -> CheckConfig:
    return CheckConfig(
        _inherit(base.enabled, override.enabled),
        _inherit(base.required, override.required),
    )


def _merge(base: CheckConfiguration, override: CheckOverrides) -> CheckConfiguration:
    blob = _policy(base.blob, override.blob)
    vimo = _policy(base.vimo, override.vimo)
    return CheckConfiguration(
        presence=_policy(base.presence, override.presence),
        blob=BlobCheckConfig(
            blob.enabled,
            blob.required,
            _inherit(base.blob.min_area, override.blob.min_area),
            _inherit(base.blob.max_area, override.blob.max_area),
        ),
        color=_policy(base.color, override.color),
        vimo=ViMoCheckConfig(
            vimo.enabled,
            vimo.required,
            _inherit(base.vimo.disabled_labels, override.vimo.disabled_labels),
        ),
    )


def resolve_configuration(
    base_layout: MachineLayoutDefinition,
    recipe: RecipeDefinition,
    model_registry: ModelRegistry,
    default_model_id: str,
) -> ResolvedInspectionConfiguration:
    """Validate all references, then resolve once; never load or activate a model.

    Even an overridden default ID must identify a registered model. Camera order
    is the explicit machine order; the channel view is always ascending by ID.
    """
    model = model_registry.get(default_model_id)
    if recipe.model_override_id is not None:
        model = model_registry.get(recipe.model_override_id)
    camera_overrides = {row.camera_id: row.checks for row in recipe.camera_overrides}
    channel_overrides = {row.channel_id: row for row in recipe.channel_overrides}
    layout = base_layout.layout
    unknown_cameras = set(camera_overrides) - set(layout.camera_ids)
    known_channels = {
        slot.channel_id for camera in layout.cameras for slot in camera.slots
    }
    unknown_channels = set(channel_overrides) - known_channels
    if unknown_cameras or unknown_channels:
        raise ConfigurationError(
            f"Recipe {recipe.recipe_id!r}: unknown cameras {sorted(unknown_cameras)}, "
            f"unknown channels {sorted(unknown_channels)}"
        )
    cameras: list[CameraLayout] = []
    channels: list[ResolvedChannelConfiguration] = []
    for camera in layout.cameras:
        try:
            defaults = _merge(
                recipe.defaults,
                camera_overrides.get(camera.camera_id, CheckOverrides()),
            )
        except ConfigurationError as exc:
            raise ConfigurationError(
                f"Recipe {recipe.recipe_id!r}, camera {camera.camera_id!r}: {exc}"
            ) from exc
        slots: list[ChannelROI] = []
        for slot in camera.slots:
            override = channel_overrides.get(slot.channel_id)
            checks = defaults
            if override is not None:
                try:
                    checks = _merge(defaults, override.checks)
                except ConfigurationError as exc:
                    raise ConfigurationError(
                        f"Recipe {recipe.recipe_id!r}, CH{slot.channel_id}: {exc}"
                    ) from exc
            slots.append(slot)
            channels.append(
                ResolvedChannelConfiguration(
                    slot.channel_id, camera.camera_id, slot.slot, slot.rect, checks
                )
            )
        cameras.append(CameraLayout(camera.camera_id, tuple(slots)))
    return ResolvedInspectionConfiguration(
        recipe.recipe_id,
        recipe.display_name,
        recipe.product_code,
        model,
        InspectionLayout(tuple(cameras)),
        tuple(sorted(channels, key=lambda row: row.channel_id)),
    )


def validate_model_artifact(model: ModelDefinition, *, base_directory: Path) -> Path:
    """Resolve relative paths against the explicit base and stream SHA-256.

    Validation is a point-in-time check, not a file lease or model activation.
    """
    path = base_directory / model.model_path
    try:
        path = path.resolve()
        with path.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
    except (OSError, ValueError) as exc:
        raise ModelArtifactError(f"Model {model.model_id!r}, {path}: {exc}") from exc
    if actual != model.expected_sha256:
        raise ModelArtifactError(
            f"Model {model.model_id!r}, {path}: SHA-256 mismatch "
            f"(expected {model.expected_sha256}, observed {actual})"
        )
    return path
