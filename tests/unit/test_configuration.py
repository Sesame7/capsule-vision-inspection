"""Synthetic contracts only: no SDK, local model, images or calibration."""

import hashlib
import json
from collections.abc import Callable
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from vimo_demo.configuration import (
    CameraOverride,
    ChannelOverride,
    CheckConfiguration,
    CheckOverrides,
    ConfigurationError,
    ConfigurationIOError,
    MachineLayoutDefinition,
    ModelArtifactError,
    ModelDefinition,
    ModelRegistry,
    RecipeDefinition,
    ViMoCheckConfig,
    ViMoOverride,
    load_machine_layout,
    load_model_definition,
    load_recipe,
    resolve_configuration,
    save_machine_layout,
    save_model_definition,
    save_recipe,
    validate_model_artifact,
)
from vimo_demo.inference import BoundingRect
from vimo_demo.inspection import CameraId
from vimo_demo.inspection.production import (
    CameraLayout,
    ChannelId,
    ChannelROI,
    CheckPolicy,
    InspectionLayout,
)


def machine(*, mirrored: bool = False) -> MachineLayoutDefinition:
    return MachineLayoutDefinition(
        InspectionLayout(
            tuple(
                CameraLayout(
                    CameraId(f"CAM{camera + 1}"),
                    tuple(
                        ChannelROI(
                            slot,
                            ChannelId(
                                camera * 6 + (6 - slot if mirrored else slot + 1)
                            ),
                            BoundingRect(slot * 10, 0, 8, 10),
                        )
                        for slot in range(6)
                    ),
                )
                for camera in range(6)
            )
        )
    )


def model(model_id: str = "general") -> ModelDefinition:
    return ModelDefinition(
        model_id, "通用模型", "models/synthetic.vimosln", "0" * 64, "2"
    )


def registry() -> ModelRegistry:
    return ModelRegistry((model("special"), model()))


def test_defaults_share_model_and_resolve_without_override_chains() -> None:
    base = machine()
    first = RecipeDefinition("a", "产品甲")
    resolved = resolve_configuration(base, first, registry(), "general")
    second = resolve_configuration(
        base, replace(first, recipe_id="b"), registry(), "general"
    )
    assert resolved.model == second.model == model()
    assert resolved.layout == base.layout
    assert len(resolved.channels) == 36
    assert [row.channel_id for row in resolved.channels] == list(range(1, 37))
    assert all(row.checks == first.defaults for row in resolved.channels)
    assert all(type(row.checks) is CheckConfiguration for row in resolved.channels)
    assert resolved == resolve_configuration(base, first, registry(), "general")
    assert not resolved.capsule.enabled
    assert resolved.channels[0].checks.vimo.policy == CheckPolicy(True, True)
    with pytest.raises(FrozenInstanceError):
        # Deliberately violate static typing to verify runtime immutability.
        resolved.channels[0].checks.vimo.enabled = False  # pyright: ignore[reportAttributeAccessIssue]
    with pytest.raises(FrozenInstanceError):
        resolved.channels = ()  # pyright: ignore[reportAttributeAccessIssue]


def test_precedence_false_and_empty_filter_override() -> None:
    recipe = RecipeDefinition(
        "a",
        "A",
        defaults=CheckConfiguration(
            vimo=ViMoCheckConfig(disabled_labels=("黑点",)),
        ),
        camera_overrides=(
            CameraOverride(
                CameraId("CAM3"),
                CheckOverrides(
                    vimo=ViMoOverride(enabled=False, disabled_labels=("黑点", "压痕"))
                ),
            ),
        ),
        channel_overrides=(
            ChannelOverride(
                ChannelId(17),
                CheckOverrides(
                    vimo=ViMoOverride(enabled=True, required=False, disabled_labels=()),
                ),
            ),
        ),
    )
    resolved = resolve_configuration(machine(), recipe, registry(), "general")
    assert resolved.channels[0].checks.vimo.enabled
    assert not resolved.channels[12].checks.vimo.enabled
    special = resolved.channels[16].checks
    assert special.vimo == ViMoCheckConfig(True, False, ())
    assert special.vimo.label_enabled("黑点")
    assert not resolved.channels[12].checks.vimo.label_enabled("黑点")


def test_machine_roi_preserves_mirrored_identity_for_all_recipes() -> None:
    base = machine(mirrored=True)
    original = {slot.channel_id: slot for c in base.layout.cameras for slot in c.slots}
    for recipe in (
        RecipeDefinition("a", "A"),
        RecipeDefinition("b", "B", channel_overrides=(ChannelOverride(ChannelId(17)),)),
    ):
        resolved = resolve_configuration(base, recipe, registry(), "general")
        for row in resolved.channels:
            assert row.roi == original[row.channel_id].rect
            assert row.slot == original[row.channel_id].slot
        assert resolved.channels[16].camera_id == "CAM3"
        assert resolved.channels[16].slot == 1


def test_optional_enabled_is_independent_and_model_override_selects() -> None:
    recipe = RecipeDefinition(
        "a",
        "A",
        model_override_id="special",
        channel_overrides=(
            ChannelOverride(
                ChannelId(1), CheckOverrides(vimo=ViMoOverride(True, False))
            ),
        ),
    )
    resolved = resolve_configuration(machine(), recipe, registry(), "general")
    assert resolved.model == model("special")
    assert resolved.channels[0].checks.vimo.policy == CheckPolicy(True, False)
    assert resolved.channels[1].checks.vimo.required


@pytest.mark.parametrize(
    "recipe,default,message",
    [
        (
            RecipeDefinition("a", "A", model_override_id="missing"),
            "general",
            "Unknown model_id",
        ),
        (RecipeDefinition("a", "A"), "missing", "Unknown model_id"),
        (
            RecipeDefinition("a", "A", model_override_id="special"),
            "missing",
            "Unknown model_id",
        ),
        (
            RecipeDefinition(
                "a", "A", camera_overrides=(CameraOverride(CameraId("missing")),)
            ),
            "general",
            "unknown cameras",
        ),
        (
            RecipeDefinition(
                "a", "A", channel_overrides=(ChannelOverride(ChannelId(37)),)
            ),
            "general",
            "unknown channels",
        ),
    ],
)
def test_invalid_references(
    recipe: RecipeDefinition, default: str, message: str
) -> None:
    with pytest.raises(ConfigurationError, match=message):
        resolve_configuration(machine(), recipe, registry(), default)


@pytest.mark.parametrize(
    "construct",
    [
        lambda: RecipeDefinition("", "A"),
        lambda: ModelDefinition("a", "A", "m", "bad-hash", "2"),
        lambda: replace(model(), schema_version=2),
        lambda: replace(machine(), schema_version=2),
        lambda: RecipeDefinition("a", "A", schema_version=2),
        lambda: ViMoCheckConfig(disabled_labels=("黑点", "黑点")),
        lambda: ViMoOverride(disabled_labels=("",)),
        lambda: ViMoOverride(disabled_labels=("bad\nlabel",)),
        lambda: CameraOverride(CameraId(" ")),
        lambda: ChannelOverride(ChannelId(0)),
        lambda: ModelRegistry((model(), model())),
        lambda: RecipeDefinition(
            "a",
            "A",
            camera_overrides=(
                CameraOverride(CameraId("CAM1")),
                CameraOverride(CameraId("CAM1")),
            ),
        ),
        lambda: RecipeDefinition(
            "a",
            "A",
            channel_overrides=(
                ChannelOverride(ChannelId(1)),
                ChannelOverride(ChannelId(1)),
            ),
        ),
    ],
)
def test_domain_rejects_invalid_configuration(construct: Callable[[], object]) -> None:
    with pytest.raises(ConfigurationError):
        construct()


def test_registry_and_override_order_are_canonical() -> None:
    cameras = (CameraOverride(CameraId("CAM4")), CameraOverride(CameraId("CAM1")))
    channels = (ChannelOverride(ChannelId(30)), ChannelOverride(ChannelId(2)))
    first = RecipeDefinition(
        "a", "A", camera_overrides=cameras, channel_overrides=channels
    )
    second = replace(
        first,
        camera_overrides=tuple(reversed(cameras)),
        channel_overrides=tuple(reversed(channels)),
    )
    assert first == second
    assert registry() == ModelRegistry(tuple(reversed(registry().models)))
    assert resolve_configuration(
        machine(), first, registry(), "general"
    ) == resolve_configuration(machine(), second, registry(), "general")


def test_round_trip_all_families_and_deterministic_utf8(tmp_path: Path) -> None:
    recipe = RecipeDefinition(
        "a",
        "产品甲",
        product_code="产品-01",
        model_override_id="special",
        defaults=CheckConfiguration(vimo=ViMoCheckConfig(True, False, ("压痕",))),
        camera_overrides=(
            CameraOverride(
                CameraId("CAM3"),
                CheckOverrides(vimo=ViMoOverride(disabled_labels=("黑点",))),
            ),
        ),
        channel_overrides=(
            ChannelOverride(
                ChannelId(17),
                CheckOverrides(vimo=ViMoOverride(enabled=False, disabled_labels=())),
            ),
        ),
    )
    path = tmp_path / "recipe.json"
    save_recipe(path, recipe)
    first = path.read_bytes()
    assert "产品甲" in first.decode("utf-8") and first.endswith(b"\n")
    assert load_recipe(path) == recipe
    save_recipe(path, load_recipe(path))
    assert path.read_bytes() == first
    save_model_definition(path, model())
    assert load_model_definition(path) == model()
    save_machine_layout(path, machine(mirrored=True))
    assert load_machine_layout(path) == machine(mirrored=True)
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize(
    "content,message",
    [
        ("{", "Configuration"),
        ("[]", "object"),
        ('{"schema_version": 1}', "missing fields"),
        (
            '{"schema_version": 1, "recipe_id": "a", "display_name": "A", "defaults": {}, "typo": 1}',
            "Unknown fields",
        ),
        (
            '{"schema_version": 1, "recipe_id": "a", "recipe_id": "b", "display_name": "A", "defaults": {}}',
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
                "schema_version": 1,
                "recipe_id": "a",
                "display_name": "A",
                "defaults": defaults,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError):
        load_recipe(path)


@pytest.mark.parametrize("schema_version", [2, True, 1.0, "1", None])
def test_schema_type_and_version(tmp_path: Path, schema_version: object) -> None:
    path = tmp_path / "bad.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": schema_version,
                "recipe_id": "a",
                "display_name": "A",
                "defaults": {},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="schema_version"):
        load_recipe(path)


def test_absent_and_null_override_inherit(tmp_path: Path) -> None:
    path = tmp_path / "recipe.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "recipe_id": "a",
                "display_name": "A",
                "defaults": {"vimo": {"enabled": False, "disabled_labels": ["黑点"]}},
                "channel_overrides": [
                    {
                        "channel_id": 1,
                        "checks": {"vimo": {"enabled": None, "disabled_labels": None}},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    resolved = resolve_configuration(
        machine(), load_recipe(path), registry(), "general"
    )
    assert resolved.channels[0].checks.vimo == ViMoCheckConfig(False, True, ("黑点",))


def test_atomic_replace_failure_preserves_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "recipe.json"
    original = RecipeDefinition("a", "A")
    save_recipe(path, original)

    def denied(source: object, destination: object) -> None:
        raise PermissionError("simulated replacement failure")

    monkeypatch.setattr("vimo_demo.configuration.persistence.os.replace", denied)
    with pytest.raises(ConfigurationIOError) as failure:
        save_recipe(path, replace(original, recipe_id="b"))
    assert isinstance(failure.value.__cause__, PermissionError)
    assert load_recipe(path) == original
    assert list(tmp_path.iterdir()) == [path]


def test_missing_configuration_io_is_typed(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationIOError):
        load_recipe(tmp_path / "missing.json")
    with pytest.raises(ConfigurationIOError):
        save_recipe(tmp_path / "missing" / "recipe.json", RecipeDefinition("a", "A"))


def test_artifact_validation_without_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_engine(*args: object, **kwargs: object) -> None:
        pytest.fail("Configuration must not create an engine")

    monkeypatch.setattr("vimo_demo.inference.VimoSegmentationEngine", no_engine)
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


@pytest.mark.parametrize("family", ["model", "machine"])
@pytest.mark.parametrize(
    "bad_field,value", [("schema_version", 2), ("schema_version", True), ("typo", 1)]
)
def test_other_persisted_families_reject_version_and_unknown_fields(
    tmp_path: Path, family: str, bad_field: str, value: object
) -> None:
    path = tmp_path / "bad.json"
    if family == "model":
        save_model_definition(path, model())
    else:
        save_machine_layout(path, machine())
    data: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    data[bad_field] = value
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ConfigurationError):
        if family == "model":
            load_model_definition(path)
        else:
            load_machine_layout(path)


@pytest.mark.parametrize(
    "field,value",
    [
        ("slot", True),
        ("slot", 1.0),
        ("slot", 6),
        ("channel_id", 2),
        ("channel_id", 0),
        ("typo", 1),
        ("roi", {"x": -1, "y": 0, "width": 1, "height": 1}),
        ("roi", {"x": 0, "y": 0, "width": 0, "height": 1}),
        ("roi", {"x": 0, "y": 0, "width": 1.5, "height": 1}),
    ],
)
def test_machine_layout_geometry_and_mapping_validation(
    tmp_path: Path, field: str, value: object
) -> None:
    slots: list[dict[str, object]] = [
        {
            "slot": i,
            "channel_id": i + 1,
            "roi": {"x": i * 10, "y": 0, "width": 8, "height": 10},
        }
        for i in range(6)
    ]
    slots[0][field] = value
    path = tmp_path / "machine.json"
    path.write_text(
        json.dumps(
            {"schema_version": 1, "cameras": [{"camera_id": "CAM1", "slots": slots}]}
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError):
        load_machine_layout(path)


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
    path = tmp_path / "recipe.json"
    recipe = RecipeDefinition(
        "a",
        "A",
        defaults=CheckConfiguration(vimo=ViMoCheckConfig(True, False, ("压痕",))),
    )
    save_recipe(path, recipe)
    data: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps(dict(reversed(tuple(data.items())))), encoding="utf-8")
    assert resolve_configuration(
        machine(), load_recipe(path), registry(), "general"
    ) == resolve_configuration(machine(), recipe, registry(), "general")


def test_recipe_roi_field_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "old.recipe.json"
    path.write_text(
        '{"schema_version":1,"recipe_id":"a","display_name":"A","defaults":{},"channel_overrides":[{"channel_id":1,"roi":{"x":0,"y":0,"width":1,"height":1}}]}',
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="roi"):
        load_recipe(path)
    assert "roi" not in ChannelOverride.__dataclass_fields__
    save_recipe(
        path,
        RecipeDefinition("a", "A", channel_overrides=(ChannelOverride(ChannelId(1)),)),
    )
    assert '"roi"' not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("field", ["presence", "blob", "color"])
@pytest.mark.parametrize("scope", ["defaults", "camera", "channel"])
def test_removed_traditional_fields_rejected(
    tmp_path: Path, field: str, scope: str
) -> None:
    data: dict[str, object] = {
        "schema_version": 1,
        "recipe_id": "a",
        "display_name": "A",
        "defaults": {},
    }
    checks = {field: {"enabled": False, "required": True}}
    if scope == "defaults":
        data["defaults"] = checks
    elif scope == "camera":
        data["camera_overrides"] = [{"camera_id": "CAM1", "checks": checks}]
    else:
        data["channel_overrides"] = [{"channel_id": 1, "checks": checks}]
    path = tmp_path / "removed.recipe.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ConfigurationError, match=field):
        load_recipe(path)


@pytest.mark.parametrize("kind", ["size", "color"])
def test_capsule_required_and_overrides_are_not_supported(
    tmp_path: Path, kind: str
) -> None:
    data: dict[str, object] = {
        "schema_version": 1,
        "recipe_id": "a",
        "display_name": "A",
        "defaults": {},
        "capsule": {kind: {"required": False}},
    }
    path = tmp_path / "removed.recipe.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="required"):
        load_recipe(path)
    data.pop("capsule")
    data["camera_overrides"] = [{"camera_id": "CAM1", "checks": {"capsule": {}}}]
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="capsule"):
        load_recipe(path)
