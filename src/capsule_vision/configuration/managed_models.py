"""Explicit managed Model file operations; callers arbitrate runtime ownership."""

import hashlib
import os
import shutil
import tempfile
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, replace
from pathlib import Path
from typing import NoReturn

from capsule_vision.inference import SegmentationModuleInfo, read_model_modules

from .errors import ConfigurationError
from .models import ModelDefinition, persistent_id
from .persistence import (
    load_model_definition,
    load_recipe_model_reference,
    save_model_definition,
)


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _safe(path: Path) -> None:
    if path.is_symlink() or path.is_junction() or path.resolve() != path.absolute():
        raise ConfigurationError(f"Managed path must not traverse a link: {path}")


def _tree(path: Path) -> tuple[Path, ...]:
    """Inspect without following links; reject the entire operation before deletion."""
    _safe(path)
    if not path.exists():
        return ()
    entries: list[Path] = []

    def inaccessible(error: OSError) -> NoReturn:
        raise error

    for root, directories, files in os.walk(
        path, followlinks=False, onerror=inaccessible
    ):
        for name in (*directories, *files):
            child = Path(root) / name
            _safe(child)
            entries.append(child)
    return tuple(entries)


def _remove_tree(path: Path) -> None:
    entries = _tree(path)
    for child in reversed(entries):
        _safe(child)
        child.rmdir() if child.is_dir() else child.unlink()
    if path.exists():
        _safe(path)
        path.rmdir()


@dataclass(frozen=True, slots=True)
class PendingModelImport:
    directory: Path
    digest: str
    display_name: str
    modules: tuple[SegmentationModuleInfo, ...]


class ManagedModels:
    def __init__(
        self,
        directory: Path,
        *,
        authorize: Callable[[int | None], int | None] = lambda _: None,
        change: Callable[
            [str, str, int | None, str, str], AbstractContextManager[None]
        ] = lambda _a, _t, _r, _b, _v: nullcontext(),
        on_cancel: Callable[[str, int | None], Callable[[], None]] = lambda _d, _r: (
            lambda: None
        ),
    ) -> None:
        self.directory = directory.absolute()
        self._authorize = authorize
        self._change = change
        self._on_cancel = on_cancel
        self._imports: dict[Path, tuple[int | None, Callable[[], None]]] = {}

    def _paths(self) -> tuple[Path, ...]:
        _safe(self.directory)
        try:
            return tuple(self.directory.iterdir())
        except FileNotFoundError:
            return ()

    def _check_duplicate(self, digest: str) -> None:
        for path in self._paths():
            try:
                model = load_model_definition(path / "model.json")
            except ConfigurationError:
                model = None  # An invalid definition can still contain duplicate bytes.
            if model is not None and model.expected_sha256 == digest:
                raise ConfigurationError(
                    f"Duplicate model content: {model.model_id} ({digest})"
                )
            try:
                _safe(path)
                artifact = path / "model.vimosln"
                _safe(artifact)
                actual = _digest(artifact)
            except ConfigurationError, OSError:
                continue
            if actual == digest:
                raise ConfigurationError(
                    f"Duplicate model bytes: {path.name} ({digest})"
                )

    def inspect_import(
        self, source: Path, *, revision: int | None = None
    ) -> PendingModelImport:
        revision = self._authorize(revision)
        if self._imports:
            raise ConfigurationError(
                "A pending import still owns its staging directory"
            )
        if source.suffix.lower() != ".vimosln" or not source.is_file():
            raise ConfigurationError("Select an existing .vimosln file")
        digest = _digest(source)
        self._check_duplicate(digest)
        with self._change("model.stage", digest, revision, "", source.name):
            return self._stage(source, digest, revision)

    def _stage(
        self, source: Path, digest: str, revision: int | None
    ) -> PendingModelImport:
        self._authorize(revision)
        cancelled = self._on_cancel(digest, revision)
        _safe(self.directory.parent)
        self.directory.parent.mkdir(parents=True, exist_ok=True)
        work = Path(
            tempfile.mkdtemp(prefix=".model-import-", dir=self.directory.parent)
        )
        self._imports[work] = revision, cancelled
        try:
            target = work / "model.vimosln"
            with source.open("rb") as original, target.open("xb") as copied:
                shutil.copyfileobj(original, copied)
                copied.flush()
                os.fsync(copied.fileno())
            if _digest(target) != digest:
                raise ConfigurationError(
                    "Model changed while copying; import cancelled"
                )
            self._authorize(revision)
            modules = read_model_modules(target, expected_sha256=digest)
            if not modules or len({m.module_id for m in modules}) != len(modules):
                raise ConfigurationError("No uniquely identified segmentation modules")
            return PendingModelImport(work, digest, source.stem, modules)
        except BaseException as exc:
            try:
                _remove_tree(work)
                self._imports.pop(work, None)
            except (OSError, ConfigurationError) as cleanup:
                raise ConfigurationError(
                    f"Import failed: {exc}; cleanup failed at {work}: {cleanup}"
                ) from exc
            raise

    def cancel_import(self, pending: PendingModelImport) -> None:
        if (
            pending.directory not in self._imports
            or pending.directory.parent != self.directory.parent
            or not pending.directory.name.startswith(".model-import-")
        ):
            raise ConfigurationError("Import directory is outside its owner")
        _remove_tree(pending.directory)
        _, cancelled = self._imports.pop(pending.directory)
        cancelled()

    def publish_import(
        self, pending: PendingModelImport, module_id: str, name: str
    ) -> ModelDefinition:
        ownership = self._imports.get(pending.directory)
        revision = self._authorize(ownership[0] if ownership else None)
        if ownership is None:
            raise ConfigurationError("Import staging is not owned by this catalog")
        if module_id not in {m.module_id for m in pending.modules}:
            raise ConfigurationError("Select one inspected segmentation module")
        self._check_duplicate(pending.digest)
        reserved = {p.name.casefold() for p in self._paths()}
        identity = next(
            (
                pending.digest[:n]
                for n in range(12, 65, 4)
                if pending.digest[:n] not in reserved
            ),
            None,
        )
        if identity is None:
            raise ConfigurationError(
                "Model identity conflicts with existing catalog entries"
            )
        _safe(pending.directory)
        source = pending.directory / "model.vimosln"
        _safe(source)
        if _digest(source) != pending.digest:
            raise ConfigurationError("Copied model checksum changed before publication")
        target = self.directory / identity
        with self._change(
            "model.publish",
            identity,
            revision,
            "",
            f"{name} · {module_id} · {pending.digest}",
        ):
            # Build outside discovery, then recheck admission before atomic publication.
            self._authorize(revision)
            prepared = pending.directory / identity
            prepared.mkdir()
            source.rename(prepared / "model.vimosln")
            model = ModelDefinition(
                identity,
                name,
                str(prepared / "model.vimosln"),
                pending.digest,
                module_id,
            )
            save_model_definition(prepared / "model.json", model)
            load_model_definition(prepared / "model.json")
            self._authorize(revision)
            self.directory.mkdir(parents=True, exist_ok=True)
            _safe(self.directory)
            if any(p.name.casefold() == identity for p in self._paths()):
                raise ConfigurationError("Model directory appeared before publication")
            os.rename(prepared, target)
        pending.directory.rmdir()
        self._imports.pop(pending.directory)
        return load_model_definition(target / "model.json")

    def current(self, model: ModelDefinition) -> Path:
        identity = persistent_id(model.model_id, "model_id")
        matches = [p for p in self._paths() if p.name.casefold() == identity.casefold()]
        if len(matches) != 1 or matches[0].name != identity:
            raise ConfigurationError("Model is absent or ambiguous; refresh")
        directory = matches[0]
        _safe(directory)
        if load_model_definition(directory / "model.json") != model:
            raise ConfigurationError("Model changed externally; refresh before editing")
        _safe(directory / "model.vimosln")
        return directory

    def model_path(self, model: ModelDefinition) -> Path:
        identity = persistent_id(model.model_id, "model_id")
        directory = self.directory / identity
        if Path(model.model_path).absolute() != directory / "model.vimosln":
            raise ConfigurationError("Model artifact is outside its managed owner")
        return directory / "model.vimosln"

    def rename(
        self, model: ModelDefinition, name: str, *, revision: int | None = None
    ) -> ModelDefinition:
        revision = self._authorize(revision)
        directory = self.current(model)
        updated = replace(model, display_name=name)
        self._authorize(revision)
        with self._change(
            "model.rename", model.model_id, revision, model.display_name, name
        ):
            save_model_definition(directory / "model.json", updated)
        return updated

    def delete(
        self, model: ModelDefinition, recipes: Path, *, revision: int | None = None
    ) -> None:
        revision = self._authorize(revision)
        directory = self.current(model)
        try:
            paths = tuple(recipes.iterdir())
        except FileNotFoundError:
            paths = ()
        for path in paths:
            if path.suffix.lower() != ".json":
                continue
            if load_recipe_model_reference(path) == model.model_id:
                raise ConfigurationError(f"Model is referenced by Recipe {path}")
        self._authorize(revision)
        with self._change(
            "model.delete",
            model.model_id,
            revision,
            f"{model.display_name} · {model.expected_sha256}",
            "",
        ):
            _remove_tree(directory)
