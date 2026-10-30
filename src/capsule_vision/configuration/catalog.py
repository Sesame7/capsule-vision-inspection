"""Recipe and Model file catalogs around the authoritative strict codecs."""

import stat
from base64 import b32encode
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from secrets import token_bytes

from .errors import ConfigurationError, ConfigurationIOError
from .models import ModelDefinition, ModelRegistry, RecipeDefinition, persistent_id
from .persistence import (
    load_model_definition,
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


class RecipeIdentityCollision(ConfigurationError):
    """Only an uncreated generated identity may be reallocated."""


class RecipeCatalog:
    """Immediate <recipe_id>.json files; discovery is read-only.

    One local editor owns writes. External definition changes require reload;
    this is not cross-process conflict resolution. IDs are validated components.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def discover(self) -> CatalogSnapshot:
        try:
            paths = sorted(
                (p for p in self.directory.iterdir() if p.suffix.lower() == ".json"),
                key=lambda p: (p.name.casefold(), p.name),
            )
        except FileNotFoundError:
            return CatalogSnapshot((), ())
        except OSError as exc:
            return CatalogSnapshot((), (CatalogIssue(self.directory, str(exc)),))
        entries: list[RecipeEntry] = []
        issues: list[CatalogIssue] = []
        by_id: dict[str, list[Path]] = {}
        for path in paths:
            by_id.setdefault(path.stem.casefold(), []).append(path)
        for group in by_id.values():
            if len(group) != 1:
                issues.extend(
                    CatalogIssue(p, "Ambiguous Recipe filename identity") for p in group
                )
                continue
            path = group[0]
            try:
                recipe = load_recipe(path)
            except ConfigurationError as exc:
                issues.append(CatalogIssue(path, str(exc)))
            else:
                entries.append(RecipeEntry(path, recipe))
        entries.sort(
            key=lambda e: (e.recipe.display_name.casefold(), e.recipe.recipe_id)
        )
        return CatalogSnapshot(tuple(entries), tuple(issues))

    def _current(self, identity: str) -> RecipeEntry | None:
        """Unrelated bad files are visible to discovery, not write blockers."""
        snapshot = self.discover()
        if any(
            i.path == self.directory or i.path.stem.casefold() == identity.casefold()
            for i in snapshot.issues
        ):
            raise ConfigurationError(
                "Recipe target is invalid or ambiguous; reload after repair"
            )
        return next(
            (
                e
                for e in snapshot.entries
                if e.recipe.recipe_id.casefold() == identity.casefold()
            ),
            None,
        )

    def new_identity(self) -> str:
        try:
            reserved = {p.stem.casefold() for p in self.directory.iterdir()}
        except FileNotFoundError:
            reserved = set[str]()
        for _ in range(32):
            identity = (
                datetime.now().strftime("%Y%m%d-")
                + b32encode(token_bytes(5)).decode("ascii").lower()
            )
            if identity.casefold() not in reserved:
                return identity
        raise ConfigurationError("Could not allocate an unused Recipe identity")

    def save(
        self, recipe: RecipeDefinition, *, original: RecipeEntry | None
    ) -> RecipeEntry:
        identity = persistent_id(recipe.recipe_id, "recipe_id")
        path = self.directory / f"{identity}.json"
        if original is None:
            try:
                matches = [
                    p
                    for p in self.directory.iterdir()
                    if p.stem.casefold() == identity.casefold()
                ]
                if len(matches) > 1:
                    raise RecipeIdentityCollision(
                        "Recipe target is ambiguous; reload after repair"
                    )
                if matches:
                    raise RecipeIdentityCollision("Recipe ID/target already exists")
            except FileNotFoundError:
                pass
        existing = self._current(identity)
        if original is not None:
            if (
                identity != original.recipe.recipe_id
                or original.path.parent != self.directory
                or existing != original
            ):
                raise ConfigurationError(
                    "Recipe changed externally; reload before Save"
                )
            path = original.path
        elif existing is not None or path.exists():
            raise RecipeIdentityCollision(
                "Recipe ID/target already exists; choose a new ID"
            )
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ConfigurationIOError(f"Recipe directory: {exc}") from exc
        try:
            save_recipe(path, recipe, exclusive=original is None)
        except ConfigurationIOError as exc:
            if original is None and isinstance(exc.__cause__, FileExistsError):
                raise RecipeIdentityCollision(
                    "Recipe target appeared during creation"
                ) from exc
            raise
        return RecipeEntry(path, recipe)

    def save_generated(self, recipe: RecipeDefinition) -> RecipeEntry:
        for _ in range(32):
            try:
                return self.save(recipe, original=None)
            except RecipeIdentityCollision:
                recipe = replace(recipe, recipe_id=self.new_identity())
        raise RecipeIdentityCollision("Could not reserve an unused Recipe target")

    def delete(self, entry: RecipeEntry, *, protected_ids: frozenset[str]) -> None:
        """Delete only the unchanged selected Recipe, never a shared asset."""
        if entry.recipe.recipe_id in protected_ids:
            raise ConfigurationError("Active/applied Recipe cannot be deleted")
        existing = self._current(entry.recipe.recipe_id)
        if existing != entry:
            raise ConfigurationError("Recipe changed; reload before Delete")
        if entry.path.resolve().parent != self.directory.resolve():
            raise ConfigurationError("Recipe is outside its catalog")
        try:
            entry.path.unlink()
        except OSError as exc:
            raise ConfigurationIOError(f"Delete Recipe {entry.path}: {exc}") from exc


@dataclass(frozen=True, slots=True)
class ModelCatalogSnapshot:
    registry: ModelRegistry
    issues: tuple[CatalogIssue, ...]


def discover_models(directory: Path) -> ModelCatalogSnapshot:
    """Immediate managed directories; isolate each unreadable/invalid entry."""
    models: list[ModelDefinition] = []
    issues: list[CatalogIssue] = []
    try:
        paths = sorted(directory.iterdir(), key=lambda p: (p.name.casefold(), p.name))
    except FileNotFoundError:
        return ModelCatalogSnapshot(ModelRegistry(()), ())
    except OSError as exc:
        return ModelCatalogSnapshot(
            ModelRegistry(()), (CatalogIssue(directory, str(exc)),)
        )
    groups: dict[str, list[Path]] = {}
    unreadable: set[str] = set()
    for path in paths:
        try:
            if stat.S_ISDIR(path.stat().st_mode):
                groups.setdefault(path.name.casefold(), []).append(path)
        except OSError as exc:
            issues.append(CatalogIssue(path, str(exc)))
            # Its identity is known even when the entry type cannot be read.
            # Do not expose a case-colliding directory as an unambiguous Model.
            unreadable.add(path.name.casefold())
    for group in groups.values():
        if len(group) != 1 or group[0].name.casefold() in unreadable:
            issues.extend(
                CatalogIssue(p, "Ambiguous Model directory identity") for p in group
            )
            continue
        path = group[0] / "model.json"
        try:
            models.append(load_model_definition(path))
        except ConfigurationError as exc:
            issues.append(CatalogIssue(path, str(exc)))
    return ModelCatalogSnapshot(ModelRegistry(tuple(models)), tuple(issues))
