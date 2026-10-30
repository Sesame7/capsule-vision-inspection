"""Strict JSON/TSV codecs and same-directory atomic file replacement."""

import json
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import cast

from capsule_vision.inference import BoundingRect
from capsule_vision.inspection import CameraId
from capsule_vision.inspection.production import (
    CameraLayout,
    ChannelId,
    ChannelROI,
    InspectionLayout,
)
from capsule_vision.inspection.production.capsule import (
    CapsuleSettings,
    ColorSettings,
    SizeSettings,
)

from ._io import atomic_write, read_json
from .errors import ConfigurationError, ConfigurationIOError
from .models import (
    CheckConfiguration,
    ImageArchiveSettings,
    MachineLayoutDefinition,
    ModelDefinition,
    RecipeDefinition,
    ViMoCheckConfig,
    boolean,
    integer,
    persistent_id,
    text,
)
from .sources import CameraFolder, CameraSources


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


def _defaults(value: object) -> CheckConfiguration:
    data = _object(value, "vimo")
    vimo = _object(data.get("vimo", {}), "enabled required disabled_labels")
    enabled = _optional(vimo.get("enabled"), _bool)
    required = _optional(vimo.get("required"), _bool)
    disabled_labels = _optional(vimo.get("disabled_labels"), _labels)
    return CheckConfiguration(
        ViMoCheckConfig(
            True if enabled is None else enabled,
            True if required is None else required,
            () if disabled_labels is None else disabled_labels,
        )
    )


def _model(value: object, path: Path) -> ModelDefinition:
    fields = "display_name expected_sha256 module_id"
    data = _object(value, fields, fields)
    return ModelDefinition(
        _model_id(path),
        _str(data["display_name"]),
        str(path.resolve().parent / "model.vimosln"),
        _str(data["expected_sha256"]),
        _str(data["module_id"]),
    )


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


def _image_archive(value: object) -> ImageArchiveSettings:
    data = _object(value, "root_directory save_ok save_ng save_error save_empty")
    root = data.get("root_directory", "")
    if not isinstance(root, str):
        raise ConfigurationError("image_archive.root_directory: expected string")
    return ImageArchiveSettings(
        root,
        _bool(data.get("save_ok", False)),
        _bool(data.get("save_ng", False)),
        _bool(data.get("save_error", False)),
        _bool(data.get("save_empty", False)),
    )


def _recipe(value: object, path: Path) -> RecipeDefinition:
    data = _object(
        value,
        "display_name defaults product_code model_id capsule image_archive",
        "display_name",
    )
    return RecipeDefinition(
        _recipe_id(path),
        _str(data["display_name"]),
        _defaults(data.get("defaults", {})),
        _optional(data.get("product_code"), _str),
        _optional(data.get("model_id"), _str),
        capsule=_capsule(data.get("capsule", {})),
        image_archive=_image_archive(data.get("image_archive", {})),
    )


def _load[T](path: Path, parse: Callable[[object], T]) -> T:
    try:
        value = read_json(path)
        return parse(value)
    except OSError as exc:
        raise ConfigurationIOError(f"Read {path}: {exc}") from exc
    except (ValueError, TypeError, KeyError) as exc:
        raise ConfigurationError(f"Configuration {path}: {exc}") from exc


def load_recipe(path: Path) -> RecipeDefinition:
    return _load(path, lambda value: _recipe(value, path))


def load_recipe_model_reference(path: Path) -> str | None:
    """Read only the persisted reference for destructive Model protection."""

    def reference(value: object) -> str | None:
        if not isinstance(value, dict):
            raise ConfigurationError("Expected Recipe JSON object")
        # The shared JSON decoder guarantees string keys and rejects duplicates.
        model_id = cast(dict[str, object], value).get("model_id")
        if model_id is not None and not isinstance(model_id, str):
            raise ConfigurationError("Recipe model_id must be a string or null")
        return model_id

    return _load(path, reference)


def load_model_definition(path: Path) -> ModelDefinition:
    return _load(path, lambda value: _model(value, path))


def _recipe_id(path: Path) -> str:
    persistent_id(path.stem, "Recipe filename")
    if path.suffix.lower() != ".json" or path.is_symlink():
        raise ConfigurationError("Recipe requires a regular <recipe_id>.json file")
    return persistent_id(path.resolve().stem, "Recipe filename")


def _model_id(path: Path) -> str:
    persistent_id(path.absolute().parent.name, "Model directory")
    if (
        path.resolve().name != "model.json"
        or path.is_symlink()
        or path.parent.is_symlink()
    ):
        raise ConfigurationError("Model requires <model_id>/model.json")
    return persistent_id(path.resolve().parent.name, "Model directory")


_MACHINE_HEADER = "channel\tx\ty\twidth\theight"


def load_machine_layout(path: Path) -> MachineLayoutDefinition:
    """Read explicit ROI rows; channel identity alone determines camera and slot."""
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
        if not lines or lines[0] != _MACHINE_HEADER:
            raise ConfigurationError(
                "Expected TSV header: channel, x, y, width, height"
            )
        count = len(lines) - 1
        if count not in (30, 36):
            raise ConfigurationError(
                "Machine TSV requires exactly 30 or 36 channel rows"
            )
        rectangles: dict[int, BoundingRect] = {}
        for line_number, line in enumerate(lines[1:], 2):
            fields = line.split("\t")
            if len(fields) != 5 or any(
                not cell.isascii() or not cell.isdecimal() for cell in fields
            ):
                raise ConfigurationError(
                    f"TSV row {line_number}: expected five unsigned decimal integers"
                )
            channel, x, y, width, height = map(int, fields)
            if channel in rectangles:
                raise ConfigurationError(
                    f"TSV row {line_number}: duplicate channel {channel}"
                )
            rect = BoundingRect(x, y, width, height)
            ChannelROI((channel - 1) % 6, ChannelId(channel), rect)
            rectangles[channel] = rect
        if set(rectangles) != set(range(1, count + 1)):
            raise ConfigurationError(
                "Machine TSV channels must be contiguous 1..30 or 1..36"
            )
        return MachineLayoutDefinition(
            InspectionLayout(
                tuple(
                    CameraLayout(
                        CameraId(f"CAM{camera + 1}"),
                        tuple(
                            ChannelROI(
                                slot,
                                ChannelId(camera * 6 + slot + 1),
                                rectangles[camera * 6 + slot + 1],
                            )
                            for slot in range(6)
                        ),
                    )
                    for camera in range(count // 6)
                )
            )
        )
    except OSError as exc:
        raise ConfigurationIOError(f"Read {path}: {exc}") from exc
    except (ValueError, TypeError) as exc:
        raise ConfigurationError(f"Machine layout {path}: {exc}") from exc


def _checks_data(checks: CheckConfiguration) -> dict[str, object]:
    row = checks.vimo
    data: dict[str, object] = {}
    for key, value in (
        ("enabled", row.enabled),
        ("required", row.required),
        ("disabled_labels", row.disabled_labels),
    ):
        if value == getattr(ViMoCheckConfig(), key):
            continue
        data[key] = list(value) if isinstance(value, tuple) else value
    return {"vimo": data} if data else {}


def _nondefault(
    value: dict[str, object], defaults: dict[str, object]
) -> dict[str, object]:
    return {key: item for key, item in value.items() if item != defaults[key]}


def _save(
    path: Path, value: dict[str, object] | str, *, exclusive: bool = False
) -> None:
    try:
        content = (
            value
            if isinstance(value, str)
            else json.dumps(
                value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False
            )
            + "\n"
        )
        atomic_write(path, content, exclusive=exclusive, newline="\n")
    except (OSError, ValueError, TypeError) as exc:
        raise ConfigurationIOError(f"Save {path}: {exc}") from exc


def save_model_definition(path: Path, model: ModelDefinition) -> None:
    if _model_id(path) != model.model_id:
        raise ConfigurationError("Model identity must match its owning directory")
    artifact = Path(model.model_path)
    if not artifact.is_absolute():
        artifact = path.parent / artifact
    if artifact.resolve() != path.resolve().parent / "model.vimosln":
        raise ConfigurationError("Model artifact must be its owning model.vimosln")
    _save(
        path,
        {
            "display_name": model.display_name,
            "expected_sha256": model.expected_sha256,
            "module_id": model.module_id,
        },
    )


def save_machine_layout(path: Path, machine: MachineLayoutDefinition) -> None:
    rows = [_MACHINE_HEADER]
    for camera in machine.layout.cameras:
        for slot in camera.slots:
            rect = slot.rect
            rows.append(
                f"{slot.channel_id}\t{rect.x}\t{rect.y}\t{rect.width}\t{rect.height}"
            )
    _save(path, "\n".join(rows) + "\n")


def save_recipe(
    path: Path, recipe: RecipeDefinition, *, exclusive: bool = False
) -> None:
    if _recipe_id(path) != recipe.recipe_id:
        raise ConfigurationError("Recipe identity must match its filename")
    data: dict[str, object] = {
        "display_name": recipe.display_name,
    }
    defaults = _checks_data(recipe.defaults)
    if defaults:
        data["defaults"] = defaults
    capsule: dict[str, object] = {}
    for name in ("size", "color"):
        settings = _nondefault(
            asdict(getattr(recipe.capsule, name)),
            asdict(getattr(CapsuleSettings(), name)),
        )
        if settings:
            capsule[name] = settings
    if capsule:
        data["capsule"] = capsule
    archive = _nondefault(asdict(recipe.image_archive), asdict(ImageArchiveSettings()))
    if archive:
        data["image_archive"] = archive
    for key, value in (
        ("product_code", recipe.product_code),
        ("model_id", recipe.model_id),
    ):
        if value is not None:
            data[key] = value
    _save(path, data, exclusive=exclusive)


def _sources(value: object) -> CameraSources:
    data = _object(value, "mode loop target_fps CAM1 CAM2 CAM3 CAM4 CAM5 CAM6")
    mode = data.get("mode", "folder")
    if mode not in ("folder", "basler"):
        raise ConfigurationError("Expected global folder/basler mode")
    rows: list[CameraFolder] = []
    for camera_id, value in data.items():
        if camera_id in ("mode", "loop", "target_fps"):
            continue
        row = _object(value, "folder basler_serial")
        folder, serial = row.get("folder", ""), row.get("basler_serial", "")
        if not isinstance(folder, str) or not isinstance(serial, str):
            raise ConfigurationError("Folder path and Basler serial must be text")
        rows.append(CameraFolder(CameraId(camera_id), folder, serial))
    loop, fps = data.get("loop", True), data.get("target_fps", 8.0)
    if (
        type(loop) is not bool
        or not isinstance(fps, (int, float))
        or isinstance(fps, bool)
    ):
        raise ConfigurationError("Invalid replay settings")
    return CameraSources(
        tuple(rows), loop, float(fps), mode="basler" if mode == "basler" else "folder"
    )


def load_camera_sources(path: Path) -> CameraSources:
    return _load(path, _sources)


def save_camera_sources(path: Path, sources: CameraSources) -> None:
    data: dict[str, object] = {
        c.camera_id: {
            **({"folder": c.folder} if c.folder else {}),
            **({"basler_serial": c.basler_serial} if c.basler_serial else {}),
        }
        for c in sources.cameras
    }
    if sources.mode != "folder":
        data["mode"] = sources.mode
    if not sources.loop:
        data["loop"] = sources.loop
    if sources.target_fps != 8.0:
        data["target_fps"] = sources.target_fps
    _save(path, data)
