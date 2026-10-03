"""Small, Qt-free Recipe file catalog around the authoritative v1 codec."""

import hashlib
from dataclasses import dataclass
from pathlib import Path

from vimo_demo.configuration import (
    ConfigurationError,
    ConfigurationIOError,
    RecipeDefinition,
    load_recipe,
    save_recipe,
)


@dataclass(frozen=True, slots=True)
class RecipeEntry:
    path: Path
    recipe: RecipeDefinition


@dataclass(frozen=True, slots=True)
class CatalogIssue:
    path: Path
    message: str


@dataclass(frozen=True, slots=True)
class CatalogSnapshot:
    entries: tuple[RecipeEntry, ...]
    issues: tuple[CatalogIssue, ...]


class RecipeCatalog:
    """Only *.recipe.json, nonrecursive; discovery never creates/modifies files.

    One local editor owns writes. External changes require explicit reload; this
    is not cross-process conflict resolution. IDs never become filesystem paths.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def discover(self) -> CatalogSnapshot:
        try:
            paths = sorted(
                (
                    p
                    for p in self.directory.iterdir()
                    if p.is_file() and p.name.lower().endswith(".recipe.json")
                ),
                key=lambda p: (p.name.casefold(), p.name),
            )
        except OSError as exc:
            raise ConfigurationIOError(f"Recipe directory: {exc}") from exc
        entries: list[RecipeEntry] = []
        issues: list[CatalogIssue] = []
        by_id: dict[str, list[RecipeEntry]] = {}
        for path in paths:
            try:
                recipe = load_recipe(path)
            except ConfigurationError as exc:
                issues.append(CatalogIssue(path, str(exc)))
            else:
                by_id.setdefault(recipe.recipe_id, []).append(RecipeEntry(path, recipe))
        for recipe_id, group in by_id.items():
            if len(group) != 1:
                issues.extend(
                    CatalogIssue(e.path, f"Duplicate Recipe ID: {recipe_id}")
                    for e in group
                )
            else:
                entries.extend(group)
        entries.sort(
            key=lambda e: (e.recipe.display_name.casefold(), e.recipe.recipe_id)
        )
        return CatalogSnapshot(tuple(entries), tuple(issues))

    def save(
        self, recipe: RecipeDefinition, *, original: RecipeEntry | None
    ) -> RecipeEntry:
        snapshot = self.discover()
        # Ambiguous/malformed files must be resolved before writing new identities.
        if snapshot.issues:
            raise ConfigurationError(
                "Recipe catalog has invalid or duplicate files; reload after repair"
            )
        existing = next(
            (e for e in snapshot.entries if e.recipe.recipe_id == recipe.recipe_id),
            None,
        )
        if original is not None:
            if (
                recipe.recipe_id != original.recipe.recipe_id
                or existing is None
                or existing.path != original.path
            ):
                raise ConfigurationError(
                    "Saved identity changed; use Save As or reload"
                )
            path = original.path
        else:
            if existing is not None:
                raise ConfigurationError("Recipe ID already exists; choose a new ID")
            digest = hashlib.sha256(recipe.recipe_id.encode("utf-8")).hexdigest()
            path = self.directory / f"{digest}.recipe.json"
            if path.exists():
                raise ConfigurationError("Recipe target already exists")
        save_recipe(path, recipe)
        return RecipeEntry(path, recipe)
