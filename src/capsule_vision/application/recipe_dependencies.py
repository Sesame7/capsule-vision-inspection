"""Immutable saved Recipe evidence, verified off dispatch before Start."""

from dataclasses import dataclass
from pathlib import Path

from capsule_vision.configuration import ConfigurationError
from capsule_vision.configuration.catalog import RecipeCatalog, RecipeEntry


class RecipePreparationRequired(ConfigurationError):
    """The saved Recipe can no longer justify the prepared candidate."""


@dataclass(frozen=True, slots=True)
class RecipeDependencies:
    entry: RecipeEntry
    process_catalog_path: Path | None = None

    def validate_current(self) -> None:
        catalog = RecipeCatalog(
            self.entry.path.parent, process_catalog_path=self.process_catalog_path
        )
        try:
            current = catalog.read(self.entry.path)
        except (ValueError, OSError) as exc:
            raise RecipePreparationRequired(
                "Saved Recipe is unreadable; reselect the saved Recipe"
            ) from exc
        if current != self.entry:
            raise RecipePreparationRequired(
                "Saved Recipe changed; reselect the saved Recipe"
            )
