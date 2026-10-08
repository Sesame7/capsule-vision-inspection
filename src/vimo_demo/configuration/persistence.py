"""Explicit strict JSON v1 codecs and same-directory atomic file replacement."""

import json
import os
import tempfile
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import cast

from vimo_demo.inference import BoundingRect
from vimo_demo.inspection import CameraId
from vimo_demo.inspection.production import (
    CameraLayout,
    ChannelId,
    ChannelROI,
    InspectionLayout,
)
from vimo_demo.inspection.production.capsule import (
    CapsuleSettings,
    ColorSettings,
    SizeSettings,
)

from .errors import ConfigurationError, ConfigurationIOError
from .models import (
    BlobCheckConfig,
    BlobOverride,
    CameraOverride,
    ChannelOverride,
    CheckConfig,
    CheckConfiguration,
    CheckOverride,
    CheckOverrides,
    MachineLayoutDefinition,
    ModelDefinition,
    RecipeDefinition,
    ViMoCheckConfig,
    ViMoOverride,
    boolean,
    integer,
    text,
    version,
)


def _object(value: object, allowed: str, required: str = "") -> dict[str, object]:
    if not isinstance(value, dict):
        raise ConfigurationError("Expected JSON object")
    # JSON is an untyped boundary. Inspect keys before narrowing the dictionary;
    # every individual value is validated by the explicit schema below.
    raw = cast(dict[object, object], value)
    if any(not isinstance(key, str) for key in raw):
        raise ConfigurationError("Object keys must be strings")
    result = cast(dict[str, object], raw)
    unknown = set(result) - set(allowed.split())
    missing = set(required.split()) - set(result)
    if unknown or missing:
        raise ConfigurationError(
            f"Unknown fields: {sorted(unknown)}; missing fields: {sorted(missing)}"
        )
    return result


def _array(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ConfigurationError("Expected JSON array")
    return cast(list[object], value)


def _optional[T](value: object, parse: Callable[[object], T]) -> T | None:
    return None if value is None else parse(value)


def _bool(value: object) -> bool:
    return boolean(value, "check policy")


def _int(value: object) -> int:
    return integer(value, "area/coordinate")


def _str(value: object) -> str:
    return text(value, "text field")


def _labels(value: object) -> tuple[str, ...]:
    return tuple(_str(item) for item in _array(value))


def _rect(value: object) -> BoundingRect:
    data = _object(value, "x y width height", "x y width height")
    return BoundingRect(*(_int(data[key]) for key in ("x", "y", "width", "height")))


def _overrides(value: object) -> CheckOverrides:
    data = _object(value, "presence blob color vimo")

    def policy(name: str) -> CheckOverride:
        row = _object(data.get(name, {}), "enabled required")
        return CheckOverride(
            _optional(row.get("enabled"), _bool), _optional(row.get("required"), _bool)
        )

    blob = _object(data.get("blob", {}), "enabled required min_area max_area")
    vimo = _object(data.get("vimo", {}), "enabled required disabled_labels")
    return CheckOverrides(
        presence=policy("presence"),
        blob=BlobOverride(
            _optional(blob.get("enabled"), _bool),
            _optional(blob.get("required"), _bool),
            _optional(blob.get("min_area"), _int),
            _optional(blob.get("max_area"), _int),
        ),
        color=policy("color"),
        vimo=ViMoOverride(
            _optional(vimo.get("enabled"), _bool),
            _optional(vimo.get("required"), _bool),
            _optional(vimo.get("disabled_labels"), _labels),
        ),
    )


def _defaults(value: object) -> CheckConfiguration:
    # At the root there is no parent. Omitted/null fields use the documented
    # constructor defaults; the same strict parser is shared with overrides.
    row = _overrides(value)

    def policy(item: CheckOverride, enabled: bool = False) -> CheckConfig:
        return CheckConfig(
            enabled if item.enabled is None else item.enabled,
            True if item.required is None else item.required,
        )

    blob, vimo = policy(row.blob), policy(row.vimo, True)
    return CheckConfiguration(
        policy(row.presence),
        BlobCheckConfig(
            blob.enabled, blob.required, row.blob.min_area, row.blob.max_area
        ),
        policy(row.color),
        ViMoCheckConfig(vimo.enabled, vimo.required, row.vimo.disabled_labels or ()),
    )


def _schema(value: object, allowed: str, required: str) -> dict[str, object]:
    data = _object(value, "schema_version " + allowed, "schema_version " + required)
    version(integer(data["schema_version"], "schema_version"))
    return data


def _model(value: object) -> ModelDefinition:
    fields = "model_id display_name model_path expected_sha256 module_id"
    data = _schema(value, fields, fields)
    return ModelDefinition(
        _str(data["model_id"]),
        _str(data["display_name"]),
        _str(data["model_path"]),
        _str(data["expected_sha256"]),
        _str(data["module_id"]),
    )


def _machine(value: object) -> MachineLayoutDefinition:
    data = _schema(value, "cameras", "cameras")
    cameras: list[CameraLayout] = []
    for entry in _array(data["cameras"]):
        camera = _object(entry, "camera_id slots", "camera_id slots")
        slots: list[ChannelROI] = []
        for entry_slot in _array(camera["slots"]):
            slot = _object(entry_slot, "slot channel_id roi", "slot channel_id roi")
            slots.append(
                ChannelROI(
                    _int(slot["slot"]),
                    ChannelId(_int(slot["channel_id"])),
                    _rect(slot["roi"]),
                )
            )
        cameras.append(CameraLayout(CameraId(_str(camera["camera_id"])), tuple(slots)))
    return MachineLayoutDefinition(InspectionLayout(tuple(cameras)))


def _capsule(value: object) -> CapsuleSettings:
    row = _object(value, "size color")
    size = _object(
        row.get("size", {}),
        "enabled target_width width_tolerance target_height height_tolerance",
    )
    color = _object(row.get("color", {}), "enabled target_hue hue_tolerance")
    return CapsuleSettings(
        SizeSettings(
            _bool(size.get("enabled", False)),
            _int(size.get("target_width", 1)),
            _int(size.get("width_tolerance", 0)),
            _int(size.get("target_height", 1)),
            _int(size.get("height_tolerance", 0)),
        ),
        ColorSettings(
            _bool(color.get("enabled", False)),
            _int(color.get("target_hue", 0)),
            _int(color.get("hue_tolerance", 0)),
        ),
    )


def _recipe(value: object) -> RecipeDefinition:
    data = _schema(
        value,
        "recipe_id display_name defaults product_code model_override_id camera_overrides channel_overrides capsule",
        "recipe_id display_name defaults",
    )
    cameras: list[CameraOverride] = []
    channels: list[ChannelOverride] = []
    for entry in _array(data.get("camera_overrides", [])):
        row = _object(entry, "camera_id checks", "camera_id checks")
        cameras.append(
            CameraOverride(CameraId(_str(row["camera_id"])), _overrides(row["checks"]))
        )
    for entry in _array(data.get("channel_overrides", [])):
        row = _object(entry, "channel_id checks", "channel_id")
        channels.append(
            ChannelOverride(
                ChannelId(_int(row["channel_id"])),
                _overrides(row.get("checks", {})),
            )
        )
    return RecipeDefinition(
        _str(data["recipe_id"]),
        _str(data["display_name"]),
        _defaults(data["defaults"]),
        _optional(data.get("product_code"), _str),
        _optional(data.get("model_override_id"), _str),
        tuple(cameras),
        tuple(channels),
        capsule=_capsule(data.get("capsule", {})),
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ConfigurationError(f"Duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _load[T](path: Path, parse: Callable[[object], T]) -> T:
    try:
        value: object = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object
        )
        return parse(value)
    except OSError as exc:
        raise ConfigurationIOError(f"Read {path}: {exc}") from exc
    except (ValueError, TypeError, KeyError) as exc:
        raise ConfigurationError(f"Configuration {path}: {exc}") from exc


def load_recipe(path: Path) -> RecipeDefinition:
    return _load(path, _recipe)


def load_model_definition(path: Path) -> ModelDefinition:
    return _load(path, _model)


def load_machine_layout(path: Path) -> MachineLayoutDefinition:
    return _load(path, _machine)


def _rect_data(rect: BoundingRect) -> dict[str, object]:
    return {"x": rect.x, "y": rect.y, "width": rect.width, "height": rect.height}


def _checks_data(checks: CheckConfiguration | CheckOverrides) -> dict[str, object]:
    def policy(row: CheckConfig | CheckOverride) -> dict[str, object]:
        return {
            key: value
            for key, value in (("enabled", row.enabled), ("required", row.required))
            if value is not None
        }

    blob = policy(checks.blob)
    blob.update(
        {
            key: value
            for key, value in (
                ("min_area", checks.blob.min_area),
                ("max_area", checks.blob.max_area),
            )
            if value is not None
        }
    )
    vimo = policy(checks.vimo)
    if checks.vimo.disabled_labels is not None:
        vimo["disabled_labels"] = list(checks.vimo.disabled_labels)
    return {
        key: value
        for key, value in (
            ("presence", policy(checks.presence)),
            ("blob", blob),
            ("color", policy(checks.color)),
            ("vimo", vimo),
        )
        if value
    }


def _save(path: Path, value: dict[str, object]) -> None:
    temporary: Path | None = None
    try:
        content = (
            json.dumps(
                value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False
            )
            + "\n"
        )
        # The parent must already exist. Same filesystem and closed handle are
        # necessary for os.replace, particularly on Windows.
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except (OSError, ValueError, TypeError) as exc:
        error = ConfigurationIOError(f"Save {path}: {exc}")
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as cleanup_error:
                error.add_note(
                    f"Could not remove temporary file {temporary}: {cleanup_error}"
                )
        raise error from exc


def save_model_definition(path: Path, model: ModelDefinition) -> None:
    _save(
        path,
        {
            "schema_version": model.schema_version,
            "model_id": model.model_id,
            "display_name": model.display_name,
            "model_path": model.model_path,
            "expected_sha256": model.expected_sha256,
            "module_id": model.module_id,
        },
    )


def save_machine_layout(path: Path, machine: MachineLayoutDefinition) -> None:
    _save(
        path,
        {
            "schema_version": machine.schema_version,
            "cameras": [
                {
                    "camera_id": camera.camera_id,
                    "slots": [
                        {
                            "slot": slot.slot,
                            "channel_id": slot.channel_id,
                            "roi": _rect_data(slot.rect),
                        }
                        for slot in camera.slots
                    ],
                }
                for camera in machine.layout.cameras
            ],
        },
    )


def save_recipe(path: Path, recipe: RecipeDefinition) -> None:
    data: dict[str, object] = {
        "schema_version": recipe.schema_version,
        "recipe_id": recipe.recipe_id,
        "display_name": recipe.display_name,
        "defaults": _checks_data(recipe.defaults),
        "capsule": asdict(recipe.capsule),
    }
    for key, value in (
        ("product_code", recipe.product_code),
        ("model_override_id", recipe.model_override_id),
    ):
        if value is not None:
            data[key] = value
    if recipe.camera_overrides:
        data["camera_overrides"] = [
            {"camera_id": row.camera_id, "checks": _checks_data(row.checks)}
            for row in recipe.camera_overrides
        ]
    if recipe.channel_overrides:
        channels: list[dict[str, object]] = []
        for row in recipe.channel_overrides:
            entry: dict[str, object] = {"channel_id": row.channel_id}
            checks = _checks_data(row.checks)
            if checks:
                entry["checks"] = checks
            channels.append(entry)
        data["channel_overrides"] = channels
    _save(path, data)
