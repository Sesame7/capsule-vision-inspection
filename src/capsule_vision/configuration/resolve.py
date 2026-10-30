"""Pure resolution and separately requested artifact verification."""

import hashlib
from pathlib import Path

from .errors import ConfigurationError, ModelArtifactError
from .models import (
    MachineLayoutDefinition,
    ModelDefinition,
    ModelRegistry,
    RecipeDefinition,
    ResolvedInspectionConfiguration,
)


def resolve_configuration(
    base_layout: MachineLayoutDefinition,
    recipe: RecipeDefinition,
    model_registry: ModelRegistry,
) -> ResolvedInspectionConfiguration:
    """Validate all references, then resolve once; never load or activate a model.

    A Recipe must select its exact Model. The immutable machine layout remains
    the sole authority for camera order, slots, channel identities and ROIs.
    """
    if recipe.model_id is None:
        raise ConfigurationError(
            f"Recipe {recipe.recipe_id!r}: model_id is required for preparation"
        )
    model = model_registry.get(recipe.model_id)
    return ResolvedInspectionConfiguration(
        recipe.recipe_id,
        recipe.display_name,
        recipe.product_code,
        model,
        recipe.defaults,
        base_layout.layout,
        capsule=recipe.capsule,
        image_archive=recipe.image_archive,
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
