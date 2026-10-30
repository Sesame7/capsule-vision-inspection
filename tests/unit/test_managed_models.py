"""Managed files, identities and resource fences; no vendor engine or GPU."""

import hashlib
import inspect
import json
import os
import re
import subprocess
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from threading import Event

import numpy as np
import pytest
from numpy.typing import NDArray
from tests.support.application import WAIT, FakeSession, close, prepared
from tests.support.cycles import image_result

from capsule_vision.application import (
    ApplicationStateError,
    ModelPreparationRequest,
    ProductionApplication,
)
from capsule_vision.configuration import (
    ConfigurationError,
    ModelDefinition,
    RecipeDefinition,
    load_model_definition,
    load_recipe,
    save_model_definition,
    save_recipe,
)
from capsule_vision.configuration import catalog as recipe_catalog_module
from capsule_vision.configuration import managed_models as implementation
from capsule_vision.configuration.catalog import RecipeCatalog
from capsule_vision.configuration.managed_models import ManagedModels
from capsule_vision.inference import ImageResult, SegmentationModuleInfo
from capsule_vision.inference._model_store import managed_model


class NoInferenceEngine:
    def __init__(self) -> None:
        self.closed = False

    def infer(self, image_bgr: NDArray[np.generic]) -> ImageResult:
        raise AssertionError("Initialization-only test must not infer")

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[ManagedModels, Path]:
    def inspect(
        *_args: object, **_kwargs: object
    ) -> tuple[SegmentationModuleInfo, ...]:
        return (SegmentationModuleInfo("2", "Segmentation", ("defect",)),)

    monkeypatch.setattr(
        implementation,
        "read_model_modules",
        inspect,
    )
    source = tmp_path / "engineering.vimosln"
    source.write_bytes(b"synthetic import source")
    return ManagedModels(tmp_path / "data/models"), source


def imported(models: tuple[ManagedModels, Path]) -> ModelDefinition:
    catalog, source = models
    pending = catalog.inspect_import(source)
    return catalog.publish_import(pending, "2", "Imported")


@pytest.mark.parametrize("reserved", [0, 1, 2])
def test_import_uses_full_digest_and_shortest_unused_prefix(
    models: tuple[ManagedModels, Path], reserved: int
) -> None:
    catalog, source = models
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    catalog.directory.mkdir(parents=True)
    blockers: list[Path] = []
    for n in range(reserved):
        path = catalog.directory / digest[: 12 + n * 4].upper()
        path.mkdir()
        (path / "invalid.txt").write_text("retain", encoding="utf-8")
        blockers.append(path)
    pending = catalog.inspect_import(source)
    assert len(tuple(catalog.directory.iterdir())) == reserved
    model = catalog.publish_import(pending, "2", "New")
    assert model.model_id == digest[: 12 + reserved * 4]
    assert model.expected_sha256 == digest
    assert load_model_definition(Path(model.model_path).parent / "model.json") == model
    assert Path(model.model_path).read_bytes() == source.read_bytes()
    assert not pending.directory.exists()
    assert all(
        (p / "invalid.txt").read_text(encoding="utf-8") == "retain" for p in blockers
    )


@pytest.mark.parametrize("metadata", ["valid", "mismatch", "malformed"])
def test_duplicate_content_is_rejected_under_legacy_id_and_other_module(
    models: tuple[ManagedModels, Path],
    metadata: str,
) -> None:
    catalog, source = models
    directory = catalog.directory / "legacy-model"
    directory.mkdir(parents=True)
    artifact = directory / "model.vimosln"
    artifact.write_bytes(source.read_bytes())
    model = ModelDefinition(
        "legacy-model",
        "Legacy",
        str(artifact),
        hashlib.sha256(source.read_bytes()).hexdigest()
        if metadata == "valid"
        else "0" * 64,
        "unselected-module",
    )
    save_model_definition(directory / "model.json", model)
    if metadata == "malformed":
        (directory / "model.json").write_text("{", encoding="utf-8")
    before = {p: p.read_bytes() for p in directory.iterdir()}
    with pytest.raises(ConfigurationError, match="Duplicate"):
        catalog.inspect_import(source)
    assert all(p.read_bytes() == data for p, data in before.items())
    assert not tuple(catalog.directory.parent.glob(".model-import-*"))


@pytest.mark.parametrize("failure", ["metadata", "changed", "selection", "publish"])
def test_failed_or_cancelled_import_never_publishes_partial_model(
    models: tuple[ManagedModels, Path], monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    catalog, source = models
    if failure == "metadata":

        def fail(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError("SDK metadata failed")

        monkeypatch.setattr(implementation, "read_model_modules", fail)
        with pytest.raises(RuntimeError, match="metadata"):
            catalog.inspect_import(source)
    else:
        pending = catalog.inspect_import(source)
        if failure == "changed":
            (pending.directory / "model.vimosln").write_bytes(b"changed")
        elif failure == "publish":

            def fail_publication(*_args: object) -> None:
                raise OSError("publication denied")

            monkeypatch.setattr(os, "rename", fail_publication)
        with pytest.raises((ConfigurationError, OSError)):
            catalog.publish_import(
                pending, "missing" if failure == "selection" else "2", "New"
            )
        catalog.cancel_import(pending)
    assert not catalog.directory.exists() or not tuple(catalog.directory.iterdir())
    assert not tuple(catalog.directory.parent.glob(".model-import-*"))
    assert source.read_bytes() == b"synthetic import source"


def test_adjacent_cache_rename_and_protected_model_deletion(
    models: tuple[ManagedModels, Path], tmp_path: Path
) -> None:
    catalog, source = models
    model = imported(models)
    renamed = catalog.rename(model, "Renamed")
    assert renamed == replace(model, display_name="Renamed")
    runtime = catalog.model_path(renamed)
    cache = runtime.parent / "vimo_trt_cache"
    cache.mkdir()
    (cache / "engine.trt").write_bytes(b"opaque")
    historical = tmp_path / "outputs/vimo_cache/old"
    historical.mkdir(parents=True)
    (historical / "keep").write_bytes(b"retain")
    assert Path(model.model_path).read_bytes() == source.read_bytes()
    assert (historical / "keep").read_bytes() == b"retain"
    recipes = tmp_path / "data/recipes"
    recipes.mkdir()
    unrelated = recipes / "unrelated-invalid.json"
    unrelated.write_text(
        '{"display_name": 17, "model_id": "elsewhere"}', encoding="utf-8"
    )
    save_recipe(
        recipes / "other.json",
        RecipeDefinition("other", "Other", model_id=model.model_id),
    )
    with pytest.raises(ConfigurationError, match="other"):
        catalog.delete(renamed, recipes)
    assert runtime.is_file() and (cache / "engine.trt").read_bytes() == b"opaque"
    (recipes / "other.json").unlink()
    catalog.delete(renamed, recipes)
    assert not Path(model.model_path).parent.exists()
    assert not runtime.exists()
    assert source.read_bytes() == b"synthetic import source"
    assert (historical / "keep").read_bytes() == b"retain"
    assert (
        unrelated.read_text(encoding="utf-8")
        == '{"display_name": 17, "model_id": "elsewhere"}'
    )


@pytest.mark.parametrize(
    "reference", ["target", "other", "missing", "null", "case", "padded", "nested"]
)
def test_deletion_checks_unambiguous_model_reference_without_full_recipe_validation(
    models: tuple[ManagedModels, Path], tmp_path: Path, reference: str
) -> None:
    catalog, source = models
    model = imported(models)
    directory = Path(model.model_path).parent
    cache = directory / "vimo_trt_cache"
    cache.mkdir()
    (cache / "engine.trt").write_bytes(b"opaque")
    before = {p: p.read_bytes() for p in directory.rglob("*") if p.is_file()}
    recipes = tmp_path / "recipes"
    recipes.mkdir()
    path = recipes / "incomplete.json"
    # All rows have invalid unrelated fields, including an unknown field.
    data: dict[str, object] = {"display_name": 17, "capsule": False, "unrelated": {}}
    if reference not in ("missing", "nested"):
        data["model_id"] = {
            "target": model.model_id,
            "other": "another-model",
            "null": None,
            "case": model.model_id.upper(),
            "padded": " " + model.model_id,
        }[reference]
    if reference == "nested":
        data["unrelated"] = {"model_id": model.model_id}
    path.write_text(json.dumps(data), encoding="utf-8")
    saved = path.read_bytes()
    with pytest.raises(ConfigurationError):
        load_recipe(path)  # Normal Recipe validation remains strict.
    if reference == "target":
        with pytest.raises(ConfigurationError) as error:
            catalog.delete(model, recipes)
        assert str(path) in str(error.value)
        assert {
            p: p.read_bytes() for p in directory.rglob("*") if p.is_file()
        } == before
    else:
        if reference == "case":
            assert model.model_id.upper() != model.model_id
        catalog.delete(model, recipes)
        assert not directory.exists()
    assert path.read_bytes() == saved
    assert source.read_bytes() == b"synthetic import source"


@pytest.mark.parametrize(
    "content",
    [
        b"{",
        b"\xff",
        b"[]",
        b"null",
        b'{"model_id": 17}',
        b'{"model_id": true}',
        b'{"model_id": {}}',
        b'{"model_id": []}',
        b'{"model_id": "TARGET", "model_id": "other"}',
        b'{"model_id": "other", "model_id": "TARGET"}',
        b'{"model_id": null, "display_name": "A", "display_name": "B"}',
    ],
    ids=[
        "syntax",
        "encoding",
        "array-root",
        "null-root",
        "number-reference",
        "boolean-reference",
        "object-reference",
        "array-reference",
        "duplicate-target-first",
        "duplicate-target-last",
        "duplicate-unrelated-key",
    ],
)
def test_deletion_rejects_ambiguous_recipe_before_removing_any_model_files(
    models: tuple[ManagedModels, Path], tmp_path: Path, content: bytes
) -> None:
    catalog, source = models
    model = imported(models)
    directory = Path(model.model_path).parent
    cache = directory / "vimo_trt_cache"
    cache.mkdir()
    (cache / "engine.trt").write_bytes(b"opaque")
    before = {p: p.read_bytes() for p in directory.rglob("*") if p.is_file()}
    recipes = tmp_path / "recipes"
    recipes.mkdir()
    (recipes / "unrelated.json").write_text('{"model_id":"other"}', encoding="utf-8")
    path = recipes / "ambiguous.json"
    saved = content.replace(b"TARGET", model.model_id.encode())
    path.write_bytes(saved)
    with pytest.raises(ConfigurationError) as error:
        catalog.delete(model, recipes)
    assert str(path) in str(error.value)
    assert {p: p.read_bytes() for p in directory.rglob("*") if p.is_file()} == before
    assert path.read_bytes() == saved
    assert source.read_bytes() == b"synthetic import source"


def test_deletion_rejects_unreadable_recipe(
    models: tuple[ManagedModels, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog, _source = models
    model = imported(models)
    directory = Path(model.model_path).parent
    recipes = tmp_path / "recipes"
    recipes.mkdir()
    path = recipes / "unreadable.json"
    path.write_text('{"model_id":null}', encoding="utf-8")
    before = {p: p.read_bytes() for p in directory.rglob("*") if p.is_file()}
    read_text = Path.read_text

    def denied(file: Path, encoding: str | None = None) -> str:
        if file == path:
            raise PermissionError("Recipe read denied")
        return read_text(file, encoding=encoding)

    monkeypatch.setattr(Path, "read_text", denied)
    with pytest.raises(ConfigurationError) as error:
        catalog.delete(model, recipes)
    assert str(path) in str(error.value) and "Recipe read denied" in str(error.value)
    assert {p: p.read_bytes() for p in directory.rglob("*") if p.is_file()} == before


def test_failed_import_cleanup_retains_owner_until_actual_removal(
    models: tuple[ManagedModels, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog, source = models
    pending = catalog.inspect_import(source)

    def denied(_path: Path) -> None:
        raise PermissionError("cleanup denied")

    with monkeypatch.context() as patch:
        patch.setattr(implementation, "_remove_tree", denied)
        with pytest.raises(PermissionError, match="cleanup denied"):
            catalog.cancel_import(pending)
        assert pending.directory.is_dir()
        with pytest.raises(ConfigurationError, match="pending import"):
            catalog.inspect_import(source)
    catalog.cancel_import(pending)
    assert not pending.directory.exists()
    assert source.read_bytes() == b"synthetic import source"


@pytest.mark.parametrize("conflict", [True, False])
def test_first_recipe_publication_never_overwrites_or_retries_other_io_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conflict: bool
) -> None:
    catalog = RecipeCatalog(tmp_path)
    identity = catalog.new_identity()
    target = tmp_path / f"{identity}.json"
    original_link = os.link
    attempts: list[Path] = []

    def publish(source: Path, destination: Path) -> None:
        attempts.append(destination)
        if len(attempts) == 1:
            if not conflict:
                raise PermissionError("publication denied")
            destination.write_bytes(b"concurrent occupant")
        original_link(source, destination)

    monkeypatch.setattr(os, "link", publish)
    if conflict:
        entry = catalog.save_generated(RecipeDefinition(identity, "New"))
        assert len(attempts) == 2 and entry.path != target
        assert target.read_bytes() == b"concurrent occupant"
        assert entry.recipe == RecipeDefinition(entry.path.stem, "New")
    else:
        with pytest.raises(ConfigurationError, match="publication denied") as caught:
            catalog.save_generated(RecipeDefinition(identity, "New"))
        assert isinstance(caught.value.__cause__, PermissionError)
        assert len(attempts) == 1 and not target.exists()
    assert not tuple(tmp_path.glob(".*.tmp"))


def test_model_delete_rejects_external_cache_link_before_removing_any_file(
    models: tuple[ManagedModels, Path], tmp_path: Path
) -> None:
    catalog, source = models
    model = imported(models)
    cache = Path(model.model_path).parent / "vimo_trt_cache"
    cache.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "protected").write_bytes(b"unchanged")
    link = cache / "external"
    if os.name == "nt":
        command = f"New-Item -ItemType Junction -Path '{str(link).replace(chr(39), chr(39) * 2)}' -Target '{str(outside).replace(chr(39), chr(39) * 2)}' | Out-Null"
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            check=True,
            capture_output=True,
        )
    else:
        link.symlink_to(outside, target_is_directory=True)
    try:
        with pytest.raises(ValueError, match="link"):
            managed_model(Path(model.model_path), model.expected_sha256)
        with pytest.raises(ConfigurationError, match="link"):
            catalog.delete(model, tmp_path / "recipes")
        assert Path(model.model_path).read_bytes() == source.read_bytes()
        assert (outside / "protected").read_bytes() == b"unchanged"
    finally:
        link.rmdir() if os.name == "nt" else link.unlink()


def test_model_file_work_fences_start_preparation_and_close(tmp_path: Path) -> None:
    config = prepared(tmp_path)
    app = ProductionApplication(lambda _p, _g: FakeSession())
    app.configure(config)
    try:
        with pytest.raises(ApplicationStateError, match="Candidate"):
            with app.model_management(
                config.configuration.model.model_id, deleting=True
            ):
                pytest.fail("Protected Model admitted")
        with app.model_management(config.configuration.model.model_id):
            with pytest.raises(ApplicationStateError):
                app.start()
            with pytest.raises(ApplicationStateError):
                app.prepare_model_cache(
                    ModelPreparationRequest(
                        config.configuration.model, config.model_path
                    )
                )
            ending = app.close()
            assert not ending.done()
        ending.result(WAIT)
        assert app.join(WAIT)
    finally:
        close(app)


def test_generated_recipe_ids_check_casefold_collisions_and_stay_stable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Clock:
        @staticmethod
        def now() -> datetime:
            return datetime(2026, 10, 25)

    monkeypatch.setattr(recipe_catalog_module, "datetime", Clock)
    values = iter([bytes(5), bytes([1]) * 5, bytes([2]) * 5])

    def random_bytes(_n: int) -> bytes:
        return next(values)

    monkeypatch.setattr(recipe_catalog_module, "token_bytes", random_bytes)
    (tmp_path / "20261025-AAAAAAAA.json").write_text(
        "invalid retained", encoding="utf-8"
    )
    catalog = RecipeCatalog(tmp_path)
    identity = catalog.new_identity()
    assert re.fullmatch(r"20261025-[a-z2-7]{8}", identity)
    assert identity != "20261025-aaaaaaaa"
    # A late collision is reallocated only before the object's first publication.
    (tmp_path / f"{identity}.json").write_text("late occupant", encoding="utf-8")
    entry = catalog.save_generated(RecipeDefinition(identity, "New"))
    assert entry.recipe.recipe_id != identity
    updated = catalog.save(
        replace(entry.recipe, display_name="Renamed"), original=entry
    )
    assert (
        updated.path == entry.path
        and updated.recipe.recipe_id == entry.recipe.recipe_id
    )
    assert (tmp_path / f"{identity}.json").read_text(
        encoding="utf-8"
    ) == "late occupant"


def test_different_full_hash_with_same_prefix_extends_only_the_new_id(
    models: tuple[ManagedModels, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog, source = models
    old_digest, new_digest = "abcdef123456" + "1" * 52, "abcdef123456" + "2" * 52
    directory = catalog.directory / old_digest[:12]
    directory.mkdir(parents=True)
    old_artifact = directory / "model.vimosln"
    old_artifact.write_bytes(b"old")
    save_model_definition(
        directory / "model.json",
        ModelDefinition(old_digest[:12], "Old", str(old_artifact), old_digest, "2"),
    )

    def digest(path: Path) -> str:
        return old_digest if path.read_bytes() == b"old" else new_digest

    monkeypatch.setattr("capsule_vision.configuration.managed_models._digest", digest)
    pending = catalog.inspect_import(source)
    new = catalog.publish_import(pending, "2", "New")
    assert new.model_id == new_digest[:16] and new.expected_sha256 == new_digest
    assert old_artifact.read_bytes() == b"old"
    assert load_model_definition(directory / "model.json").model_id == old_digest[:12]


def test_production_and_preparation_use_the_same_direct_managed_model(
    models: tuple[ManagedModels, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from capsule_vision.application import composition as local_production
    from capsule_vision.application import model_preparation
    from capsule_vision.application import session as application_session

    model = imported(models)
    monkeypatch.chdir(tmp_path)
    config = prepared(tmp_path)
    config = replace(
        config,
        configuration=replace(config.configuration, model=model),
        model_path=Path(model.model_path),
        managed_model=True,
    )
    seen: list[tuple[Path, str, str]] = []

    class Engine(NoInferenceEngine):
        def infer(self, image_bgr: NDArray[np.generic]) -> ImageResult:
            return image_result(np.asarray(image_bgr, dtype=np.uint8))

    class Factory:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pytest.fail("Managed execution must not call the staging constructor")

        @staticmethod
        def from_managed_model(
            path: Path, *, module_id: str, expected_sha256: str, device_id: int = 0
        ) -> NoInferenceEngine:
            assert device_id == 0 and expected_sha256 == model.expected_sha256
            seen.append((path, expected_sha256, module_id))
            return Engine()

    monkeypatch.setattr(model_preparation, "VimoSegmentationEngine", Factory)
    monkeypatch.setattr(application_session, "VimoSegmentationEngine", Factory)
    assert (
        inspect.signature(local_production.compose).parameters["engine_factory"].default
        is application_session.real_engine
    )
    application_session.real_engine(config).close()
    session = application_session.ProductionRuntimeSession(
        config, 1, warmup=lambda _p: [np.zeros((2, 3, 3), dtype=np.uint8)]
    )
    try:
        session.start().result(WAIT)
    finally:
        session.request_stop()
        assert session.join(WAIT) and session.cleanup_safe
    model_preparation.real_preparation_engine(
        ModelPreparationRequest(model, Path(model.model_path), managed_model=True)
    ).close()
    assert seen == [(Path(model.model_path), model.expected_sha256, "2")] * 3
    assert not (Path(model.model_path).parent / "cache").exists()
    assert not (tmp_path / "outputs").exists()


def test_native_initialization_keeps_model_management_rejected_until_cleanup(
    tmp_path: Path,
) -> None:
    config = prepared(tmp_path)
    entered, release = Event(), Event()
    engine = NoInferenceEngine()

    def factory(_request: ModelPreparationRequest) -> NoInferenceEngine:
        entered.set()
        assert release.wait(WAIT)
        return engine

    app = ProductionApplication(
        lambda _p, _g: FakeSession(), preparation_factory=factory
    )
    request = ModelPreparationRequest(config.configuration.model, config.model_path)
    operation = app.prepare_model_cache(request)
    assert entered.wait(WAIT)
    try:
        with pytest.raises(ApplicationStateError):
            with app.model_management(request.model.model_id):
                pytest.fail("Native cache owner must be protected")
        ending = app.close()
        assert not ending.done() and not engine.closed
    finally:
        release.set()
    ending.result(WAIT)
    assert app.join(WAIT) and operation.done() and engine.closed


def test_active_model_remains_protected_after_another_candidate_is_installed(
    tmp_path: Path,
) -> None:
    config = prepared(tmp_path)
    app = ProductionApplication(lambda _p, _g: FakeSession())
    app.configure(config)
    try:
        app.start().result(WAIT)
        app.stop().result(WAIT)
        other = replace(
            config,
            configuration=replace(
                config.configuration,
                model=replace(config.configuration.model, model_id="other"),
            ),
        )
        app.configure(other)
        with pytest.raises(ApplicationStateError, match="active"):
            with app.model_management(
                config.configuration.model.model_id, deleting=True
            ):
                pytest.fail("Last active identity must remain protected")
    finally:
        close(app)
