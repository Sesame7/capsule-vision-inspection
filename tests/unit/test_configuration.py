"""Synthetic contracts only: no SDK, local model, images or calibration."""

import hashlib
import json
from collections.abc import Callable
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest
from tests.support.configuration import machine, model, registry

from capsule_vision.configuration import (
    CheckConfiguration,
    ConfigurationError,
    ConfigurationIOError,
    ImageArchiveSettings,
    ModelArtifactError,
    ModelDefinition,
    ModelRegistry,
    RecipeDefinition,
    ViMoCheckConfig,
    load_machine_layout,
    load_model_definition,
    load_recipe,
    resolve_configuration,
    save_machine_layout,
    save_model_definition,
    save_recipe,
    validate_model_artifact,
)
from capsule_vision.inference import BoundingRect
from capsule_vision.inspection.production import (
    CheckPolicy,
)


def test_image_archive_optional_strict_roundtrip_and_resolution(tmp_path: Path) -> None:
    path = tmp_path / "recipe.json"
    path.write_text(
        '{"display_name":"R","defaults":{}}',
        encoding="utf-8",
    )
    original = replace(load_recipe(path), model_id="m")
    assert original.image_archive == ImageArchiveSettings()
    settings = ImageArchiveSettings(str(tmp_path / "archive"), True, False, True, True)
    recipe = replace(original, image_archive=settings)
    save_recipe(path, recipe)
    assert load_recipe(path) == recipe
    registry = ModelRegistry((ModelDefinition("m", "M", "x", "a" * 64, "2"),))
    assert resolve_configuration(machine(), recipe, registry).image_archive == settings
    data = json.loads(path.read_text(encoding="utf-8"))
    legacy = json.loads(path.read_text(encoding="utf-8"))
    del legacy["image_archive"]["save_empty"]
    path.write_text(json.dumps(legacy), encoding="utf-8")
    assert load_recipe(path).image_archive == replace(settings, save_empty=False)
    for invalid in (
        None,
        {"root_directory": 5},
        {"save_ok": 1},
        {"save_ng": True},
        {"save_error": "true"},
        {"save_empty": 1},
        {"save_empty": True},
        {"unknown": False},
        {"root_directory": "relative"},
    ):
        data["image_archive"] = invalid
        path.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(ConfigurationError):
            load_recipe(path)


def test_minimal_recipe_and_compact_settings_keep_effective_defaults(
    tmp_path: Path,
) -> None:
    from capsule_vision.inspection.production.capsule import (
        CapsuleSettings,
        SizeSettings,
    )

    path = tmp_path / "incomplete.json"
    minimal = RecipeDefinition("incomplete", "Incomplete")
    save_recipe(path, minimal)
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "display_name": "Incomplete",
    }
    assert load_recipe(path) == minimal
    changed = replace(
        minimal,
        defaults=CheckConfiguration(ViMoCheckConfig(required=False)),
        capsule=CapsuleSettings(size=SizeSettings(target_width=12)),
    )
    save_recipe(path, changed)
    value = json.loads(path.read_text(encoding="utf-8"))
    assert value["defaults"] == {"vimo": {"required": False}}
    assert value["capsule"] == {"size": {"target_width": 12}}
    assert "image_archive" not in value
    assert load_recipe(path) == changed
    resolved = resolve_configuration(
        machine(), replace(changed, model_id="general"), registry()
    )
    assert resolved.checks.vimo == ViMoCheckConfig(True, False, ())


@pytest.mark.parametrize(
    "field",
    ["schema_version", "model_override_id", "camera_overrides", "channel_overrides"],
)
@pytest.mark.parametrize("value", [None, [], {"invalid": True}, "old"])
def test_recipe_rejects_obsolete_fields(
    tmp_path: Path, field: str, value: object
) -> None:
    path = tmp_path / "recipe.json"
    path.write_text(json.dumps({"display_name": "R", field: value}), encoding="utf-8")
    with pytest.raises(ConfigurationError, match=field):
        load_recipe(path)


@pytest.mark.parametrize(
    "path",
    [
        "",
        "relative",
        "C:relative",
        "C:/bad?path",
        "C:/bad\npath",
        "C:/CON",
        "C:/x/../y",
        "C:/trailing.",
    ],
)
def test_enabled_archive_requires_valid_absolute_directory(path: str) -> None:
    with pytest.raises(ConfigurationError):
        ImageArchiveSettings(path, save_ok=True)
    with pytest.raises(ConfigurationError):
        ImageArchiveSettings(path, save_empty=True)


@pytest.mark.parametrize("count", [4, 7])
def test_unsupported_machine_count_is_not_a_configuration(count: int) -> None:
    with pytest.raises(ConfigurationError):
        machine(count=count)


def test_defaults_share_model_and_resolve_without_override_chains() -> None:
    base = machine()
    first = RecipeDefinition("a", "产品甲", model_id="general")
    resolved = resolve_configuration(base, first, registry())
    second = resolve_configuration(base, replace(first, recipe_id="b"), registry())
    assert resolved.model == second.model == model()
    assert resolved.layout is base.layout
    assert [
        slot.channel_id for camera in resolved.layout.cameras for slot in camera.slots
    ] == list(range(1, 37))
    assert resolved.checks is first.defaults
    assert resolved == resolve_configuration(base, first, registry())
    assert not resolved.capsule.enabled
    assert resolved.checks.vimo.policy == CheckPolicy(True, True)
    with pytest.raises(FrozenInstanceError):
        # Deliberately violate static typing to verify runtime immutability.
        resolved.checks.vimo.enabled = False  # pyright: ignore[reportAttributeAccessIssue]
    with pytest.raises(FrozenInstanceError):
        resolved.layout = base.layout  # pyright: ignore[reportAttributeAccessIssue]


def test_machine_roi_preserves_mirrored_identity_for_all_recipes() -> None:
    base = machine(mirrored=True)
    original = {slot.channel_id: slot for c in base.layout.cameras for slot in c.slots}
    for recipe in (
        RecipeDefinition("a", "A", model_id="general"),
        RecipeDefinition(
            "b",
            "B",
            model_id="general",
        ),
    ):
        resolved = resolve_configuration(base, recipe, registry())
        for camera in resolved.layout.cameras:
            for slot in camera.slots:
                assert slot.rect == original[slot.channel_id].rect
                assert slot.slot == original[slot.channel_id].slot
        assert resolved.layout.cameras[2].camera_id == "CAM3"
        assert resolved.layout.cameras[2].slots[4].channel_id == 17
        assert resolved.layout.cameras[2].slots[4].slot == 4


def test_optional_enabled_is_independent_and_model_selection_is_explicit() -> None:
    recipe = RecipeDefinition(
        "a",
        "A",
        model_id="special",
        defaults=CheckConfiguration(ViMoCheckConfig(True, False)),
    )
    resolved = resolve_configuration(machine(), recipe, registry())
    assert resolved.model == model("special")
    assert resolved.checks.vimo.policy == CheckPolicy(True, False)
    fixtures = Path(__file__).parents[1] / "fixtures" / "configuration"
    models = ModelRegistry(
        tuple(
            load_model_definition(fixtures / "models" / name / "model.json")
            for name in ("general", "special")
        )
    )
    base = load_machine_layout(fixtures / "machine.tsv")
    for filename, model_id in (
        ("product-a.json", "general"),
        ("product-b.json", "special"),
    ):
        persisted = resolve_configuration(
            base, load_recipe(fixtures / filename), models
        )
        assert persisted.model.model_id == model_id
        assert persisted.layout == base.layout
        assert sum(len(camera.slots) for camera in persisted.layout.cameras) == 36
        assert persisted.layout.cameras[2].slots[4].rect == BoundingRect(40, 0, 8, 10)
        assert persisted.checks == CheckConfiguration()


@pytest.mark.parametrize(
    "model_id,message",
    [("missing", "Unknown model_id"), (None, "model_id is required")],
)
def test_invalid_references(model_id: str | None, message: str) -> None:
    with pytest.raises(ConfigurationError, match=message):
        resolve_configuration(
            machine(), RecipeDefinition("a", "A", model_id=model_id), registry()
        )


@pytest.mark.parametrize(
    "construct",
    [
        lambda: RecipeDefinition("", "A", model_id="general"),
        lambda: ModelDefinition("a", "A", "m", "bad-hash", "2"),
        lambda: ModelDefinition("a", "A", "", "0" * 64, "2"),
        lambda: ModelDefinition("a", "A", "model", "0" * 64, ""),
        lambda: RecipeDefinition("a", "A", model_id=""),
        lambda: ViMoCheckConfig(disabled_labels=("黑点", "黑点")),
        lambda: ViMoCheckConfig(disabled_labels=("",)),
        lambda: ViMoCheckConfig(disabled_labels=("bad\nlabel",)),
        lambda: ModelRegistry((model(), model())),
    ],
)
def test_domain_rejects_invalid_configuration(construct: Callable[[], object]) -> None:
    with pytest.raises(ConfigurationError):
        construct()


def test_registry_order_is_canonical() -> None:
    assert registry() == ModelRegistry(tuple(reversed(registry().models)))


def test_round_trip_all_families_and_deterministic_utf8(tmp_path: Path) -> None:
    recipe = RecipeDefinition(
        "a",
        "产品甲",
        product_code="产品-01",
        model_id="special",
        defaults=CheckConfiguration(vimo=ViMoCheckConfig(True, False, ("压痕",))),
    )
    path = tmp_path / "a.json"
    save_recipe(path, recipe)
    first = path.read_bytes()
    assert "产品甲" in first.decode("utf-8") and first.endswith(b"\n")
    assert load_recipe(path) == recipe
    save_recipe(path, load_recipe(path))
    assert path.read_bytes() == first
    model_path = tmp_path / "general" / "model.json"
    model_path.parent.mkdir()
    save_model_definition(model_path, model())
    assert load_model_definition(model_path) == replace(
        model(), model_path=str(model_path.parent / "model.vimosln")
    )
    assert set(json.loads(model_path.read_text(encoding="utf-8"))) == {
        "display_name",
        "expected_sha256",
        "module_id",
    }
    machine_path = tmp_path / "machine.tsv"
    save_machine_layout(machine_path, machine(mirrored=True))
    assert load_machine_layout(machine_path) == machine(mirrored=True)
    assert set(tmp_path.iterdir()) == {path, model_path.parent, machine_path}


@pytest.mark.parametrize(
    "content,message",
    [
        ("{", "Configuration"),
        ("[]", "object"),
        ('{"schema_version": 1}', "missing fields"),
        (
            '{"display_name": "A", "defaults": {}, "typo": 1}',
            "Unknown fields",
        ),
        (
            '{"display_name": "B", "display_name": "A", "defaults": {}}',
            "Duplicate JSON key",
        ),
    ],
)
def test_malformed_recipe_json(tmp_path: Path, content: str, message: str) -> None:
    path = tmp_path / "bad.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigurationError, match=message) as failure:
        load_recipe(path)
    assert failure.value.__cause__ is not None


@pytest.mark.parametrize(
    "defaults",
    [
        {"vimo": {"requird": True}},
        {"vimo": {"enabled": 1}},
        {"vimo": {"required": "true"}},
        {"vimo": {"disabled_labels": "黑点"}},
        {"vimo": {"disabled_labels": [1]}},
        {"vimo": {"disabled_labels": ["黑点", "黑点"]}},
    ],
)
def test_strict_nested_types(tmp_path: Path, defaults: dict[str, object]) -> None:
    path = tmp_path / "bad.json"
    path.write_text(
        json.dumps(
            {
                "display_name": "A",
                "defaults": defaults,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError):
        load_recipe(path)


@pytest.mark.parametrize(
    "leaves", [{}, {"enabled": None, "required": None, "disabled_labels": None}]
)
def test_absent_and_null_default_leaves_keep_effective_defaults(
    tmp_path: Path, leaves: dict[str, object]
) -> None:
    path = tmp_path / "recipe.json"
    path.write_text(
        json.dumps(
            {"display_name": "A", "model_id": "general", "defaults": {"vimo": leaves}}
        ),
        encoding="utf-8",
    )
    resolved = resolve_configuration(machine(), load_recipe(path), registry())
    assert resolved.checks == CheckConfiguration()


def test_atomic_replace_failure_preserves_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "a.json"
    original = RecipeDefinition("a", "A", model_id="general")
    save_recipe(path, original)

    def denied(source: object, destination: object) -> None:
        raise PermissionError("simulated replacement failure")

    monkeypatch.setattr("capsule_vision.configuration._io.os.replace", denied)
    with pytest.raises(ConfigurationIOError) as failure:
        save_recipe(path, replace(original, display_name="B"))
    assert isinstance(failure.value.__cause__, PermissionError)
    assert load_recipe(path) == original
    assert list(tmp_path.iterdir()) == [path]


def test_missing_configuration_io_is_typed(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationIOError):
        load_recipe(tmp_path / "missing.json")
    with pytest.raises(ConfigurationIOError):
        save_recipe(
            tmp_path / "missing" / "a.json",
            RecipeDefinition("a", "A", model_id="general"),
        )


def test_artifact_validation_without_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_engine(*args: object, **kwargs: object) -> None:
        pytest.fail("Configuration must not create an engine")

    monkeypatch.setattr("capsule_vision.inference.VimoSegmentationEngine", no_engine)
    path = tmp_path / "synthetic.vimosln"
    content = b"synthetic artifact, not a model"
    path.write_bytes(content)
    metadata = replace(
        model(),
        model_path=path.name,
        expected_sha256=hashlib.sha256(content).hexdigest(),
    )
    assert validate_model_artifact(metadata, base_directory=tmp_path) == path.resolve()
    with pytest.raises(ModelArtifactError, match="SHA-256 mismatch"):
        validate_model_artifact(
            replace(metadata, expected_sha256="0" * 64), base_directory=tmp_path
        )
    with pytest.raises(ModelArtifactError) as failure:
        validate_model_artifact(
            replace(metadata, model_path="missing"), base_directory=tmp_path
        )
    assert isinstance(failure.value.__cause__, FileNotFoundError)


@pytest.mark.parametrize(
    "bad_field,value", [("schema_version", 2), ("schema_version", True), ("typo", 1)]
)
def test_model_definition_rejects_version_and_unknown_fields(
    tmp_path: Path, bad_field: str, value: object
) -> None:
    path = tmp_path / "general" / "model.json"
    path.parent.mkdir()
    save_model_definition(path, model())
    data = json.loads(path.read_text(encoding="utf-8"))
    data[bad_field] = value
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_model_definition(path)


@pytest.mark.parametrize(
    "defect",
    [
        "header",
        "extra_header",
        "reordered_header",
        "json",
        "missing",
        "extra",
        "duplicate",
        "gap",
        "zero",
        "negative",
        "float",
        "bool",
        "space",
        "unicode",
        "empty",
        "extra_cell",
        "missing_cell",
        "zero_width",
        "zero_height",
        "blank_row",
    ],
)
def test_machine_tsv_rejects_malformed_tables(tmp_path: Path, defect: str) -> None:
    path = tmp_path / "machine.tsv"
    save_machine_layout(path, machine())
    rows = path.read_text(encoding="utf-8").splitlines()
    fields = rows[1].split("\t")
    if defect == "header":
        rows[0] = rows[0].replace("channel", "channel_id")
    elif defect == "extra_header":
        rows[0] += "\tslot"
    elif defect == "reordered_header":
        rows[0] = "x\tchannel\ty\twidth\theight"
    elif defect == "json":
        rows = ['{"cameras": []}']
    elif defect == "missing":
        rows.pop()
    elif defect == "extra":
        rows.append("37\t0\t0\t8\t10")
    elif defect == "duplicate":
        rows[-1] = rows[-2]
    elif defect == "gap":
        rows[-1] = "37\t0\t0\t8\t10"
    elif defect == "blank_row":
        rows[1] = ""
    else:
        if defect in ("zero", "negative", "float", "bool", "space", "unicode", "empty"):
            fields[0] = {
                "zero": "0",
                "negative": "-1",
                "float": "1.0",
                "bool": "true",
                "space": " 1",
                "unicode": "１",
                "empty": "",
            }[defect]
        elif defect == "extra_cell":
            fields.append("0")
        elif defect == "missing_cell":
            fields.pop()
        elif defect == "zero_width":
            fields[3] = "0"
        elif defect == "zero_height":
            fields[4] = "0"
        rows[1] = "\t".join(fields)
    payload = "\n".join(rows) + "\n"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_machine_layout(path)
    assert path.read_text(encoding="utf-8") == payload


@pytest.mark.parametrize("count", [30, 36])
def test_machine_tsv_canonical_mapping_and_roi_preservation(
    tmp_path: Path, count: int
) -> None:
    path = tmp_path / "machine.tsv"
    definition = machine(count=count // 6, mirrored=True)
    save_machine_layout(path, definition)
    rows = path.read_text(encoding="utf-8").splitlines()
    assert rows[0] == "channel\tx\ty\twidth\theight"
    assert [int(r.split("\t")[0]) for r in rows[1:]] == list(range(1, count + 1))
    # Table order is not a second camera identity mapping.
    path.write_text(
        "\n".join([rows[0], *reversed(rows[1:])]) + "\n", encoding="utf-8-sig"
    )
    loaded = load_machine_layout(path)
    assert loaded == definition
    assert loaded.layout.camera_ids == tuple(
        f"CAM{i}" for i in range(1, count // 6 + 1)
    )
    for index, camera in enumerate(loaded.layout.cameras):
        assert [(s.slot, s.channel_id) for s in camera.slots] == [
            (slot, index * 6 + slot + 1) for slot in range(6)
        ]
    resolved = resolve_configuration(
        loaded, RecipeDefinition("r", "R", model_id="general"), registry()
    )
    assert resolved.layout is loaded.layout
    assert [
        slot.channel_id for camera in resolved.layout.cameras for slot in camera.slots
    ] == list(range(1, count + 1))
    save_machine_layout(path, loaded)
    assert path.read_text(encoding="utf-8").splitlines() == rows


def test_machine_tsv_failed_replace_preserves_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "machine.tsv"
    original = machine()
    save_machine_layout(path, original)
    payload = path.read_bytes()

    def fail(*_args: object) -> None:
        raise PermissionError("TSV replace denied")

    monkeypatch.setattr("capsule_vision.configuration._io.os.replace", fail)
    with pytest.raises(ConfigurationIOError, match="TSV replace denied"):
        save_machine_layout(path, machine(mirrored=True))
    assert path.read_bytes() == payload and list(tmp_path.iterdir()) == [path]


def test_unreadable_artifact_preserves_os_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def denied(*args: object, **kwargs: object) -> None:
        raise PermissionError("simulated unreadable artifact")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", denied)
        with pytest.raises(ModelArtifactError) as failure:
            validate_model_artifact(model(), base_directory=tmp_path)
    assert isinstance(failure.value.__cause__, PermissionError)


def test_json_key_order_does_not_change_resolution(tmp_path: Path) -> None:
    path = tmp_path / "a.json"
    recipe = RecipeDefinition(
        "a",
        "A",
        defaults=CheckConfiguration(vimo=ViMoCheckConfig(True, False, ("压痕",))),
        model_id="general",
    )
    save_recipe(path, recipe)
    data: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps(dict(reversed(tuple(data.items())))), encoding="utf-8")
    assert resolve_configuration(
        machine(), load_recipe(path), registry()
    ) == resolve_configuration(machine(), recipe, registry())


def test_recipe_roi_field_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "a.json"
    path.write_text(
        '{"display_name":"A","roi":{"x":0,"y":0,"width":1,"height":1}}',
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="roi"):
        load_recipe(path)
    save_recipe(path, RecipeDefinition("a", "A", model_id="general"))
    assert '"roi"' not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("field", ["presence", "blob", "color"])
def test_removed_traditional_fields_rejected(tmp_path: Path, field: str) -> None:
    data = {
        "display_name": "A",
        "defaults": {field: {"enabled": False, "required": True}},
    }
    path = tmp_path / "removed.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ConfigurationError, match=field):
        load_recipe(path)


@pytest.mark.parametrize("kind", ["size", "color"])
def test_capsule_required_is_not_supported(tmp_path: Path, kind: str) -> None:
    data: dict[str, object] = {
        "display_name": "A",
        "defaults": {},
        "capsule": {kind: {"required": False}},
    }
    path = tmp_path / "removed.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="required"):
        load_recipe(path)


@pytest.mark.parametrize(
    "group",
    ["defaults", "capsule", "image_archive"],
)
def test_optional_recipe_groups_reject_invalid_present_values(
    tmp_path: Path, group: str
) -> None:
    path = tmp_path / "recipe.json"
    path.write_text(
        json.dumps({"display_name": "R", group: None}),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError):
        load_recipe(path)


@pytest.mark.parametrize(
    "identity",
    [
        "..",
        ".",
        "../outside",
        "a/b",
        "a\\b",
        "C:drive",
        "NUL",
        "CON.txt",
        "LPT²",
        "a?b",
        "a|b",
        "last.",
        " space",
        "space ",
    ],
)
def test_persistent_id_rejects_unsafe_filesystem_components(identity: str) -> None:
    with pytest.raises(ConfigurationError, match="ID"):
        RecipeDefinition(identity, "Display name")
    with pytest.raises(ConfigurationError, match="ID"):
        replace(model(), model_id=identity)
    with pytest.raises(ConfigurationError, match="ID"):
        RecipeDefinition("safe", "Display", model_id=identity)


def test_filesystem_identity_and_minimal_model_metadata(tmp_path: Path) -> None:
    path = tmp_path / "模型-01" / "model.json"
    path.parent.mkdir()
    artifact = path.parent / "model.vimosln"
    artifact.write_bytes(b"synthetic model, not vendor data")
    definition = ModelDefinition(
        path.parent.name,
        "Name may differ",
        str(artifact),
        hashlib.sha256(artifact.read_bytes()).hexdigest(),
        "2",
    )
    save_model_definition(path, definition)
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "display_name": definition.display_name,
        "expected_sha256": definition.expected_sha256,
        "module_id": "2",
    }
    assert load_model_definition(path) == definition
    assert (
        validate_model_artifact(
            load_model_definition(path), base_directory=tmp_path / "unrelated"
        )
        == artifact
    )
    recipe_path = tmp_path / "stable-id.json"
    recipe = RecipeDefinition(
        "stable-id", "Name can be renamed", model_id=definition.model_id
    )
    save_recipe(recipe_path, recipe)
    assert "recipe_id" not in json.loads(recipe_path.read_text(encoding="utf-8"))
    assert load_recipe(recipe_path) == recipe
    save_recipe(recipe_path, replace(recipe, display_name="Renamed"))
    assert load_recipe(recipe_path).recipe_id == "stable-id"
    with pytest.raises(ConfigurationError, match="filename"):
        save_recipe(recipe_path, replace(recipe, recipe_id="other"))
    with pytest.raises(ConfigurationError, match="directory"):
        save_model_definition(path, replace(definition, model_id="other"))
    with pytest.raises(ConfigurationError, match="artifact"):
        save_model_definition(path, replace(definition, model_path="elsewhere.vimosln"))
    assert load_model_definition(path) == definition


@pytest.mark.parametrize("field", ["model_id", "model_path"])
def test_model_json_rejects_redundant_identity_and_artifact_path(
    tmp_path: Path, field: str
) -> None:
    path = tmp_path / "general" / "model.json"
    path.parent.mkdir()
    save_model_definition(path, model())
    data = json.loads(path.read_text(encoding="utf-8"))
    data[field] = "general" if field == "model_id" else "model.vimosln"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ConfigurationError, match=field):
        load_model_definition(path)


def test_recipe_json_rejects_redundant_identity(tmp_path: Path) -> None:
    path = tmp_path / "stable.json"
    path.write_text('{"display_name":"Name","recipe_id":"stable"}', encoding="utf-8")
    with pytest.raises(ConfigurationError, match="recipe_id"):
        load_recipe(path)


def test_windows_case_aliases_load_the_existing_filesystem_identity(
    tmp_path: Path,
) -> None:
    path = tmp_path / "General" / "model.json"
    path.parent.mkdir()
    definition = replace(model(), model_id="General")
    save_model_definition(path, definition)
    alias = tmp_path / "GENERAL" / "MODEL.JSON"
    # On case-sensitive filesystems there is no alias to inspect.
    if alias.exists():
        loaded = load_model_definition(alias)
        assert loaded.model_id == "General"
        assert Path(loaded.model_path) == path.parent / "model.vimosln"
    recipe_path = tmp_path / "Stable.json"
    save_recipe(recipe_path, RecipeDefinition("Stable", "Name"))
    alias_recipe = tmp_path / "STABLE.JSON"
    if alias_recipe.exists():
        assert load_recipe(alias_recipe).recipe_id == "Stable"
