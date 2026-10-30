"""Immutable configuration definitions; no runtime state or calibrated defaults."""

import re
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import cast

from capsule_vision.inspection.production import CheckPolicy, InspectionLayout
from capsule_vision.inspection.production.capsule import CapsuleSettings

from .errors import ConfigurationError


def text(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or any(ord(c) < 32 for c in value)
    ):
        raise ConfigurationError(
            f"{name}: expected nonempty text without control characters"
        )
    return value


def integer(value: object, name: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ConfigurationError(f"{name}: expected integer >= {minimum}")
    return value


def persistent_id(value: object, name: str) -> str:
    """Exact, portable Windows filesystem component, never a display name."""
    result = text(value, name)
    if (
        result != result.strip()
        or result in (".", "..")
        or result.endswith(".")
        or any(c in '<>:"/\\|?*' or ord(c) == 127 for c in result)
        or result.split(".")[0].upper()
        in {
            "CON",
            "PRN",
            "AUX",
            "NUL",
            *(f"{p}{n}" for p in ("COM", "LPT") for n in "123456789¹²³"),
        }
    ):
        raise ConfigurationError(f"{name}: expected a safe single-component ID")
    return result


def boolean(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise ConfigurationError(f"{name}: expected boolean")
    return value


def labels(values: tuple[str, ...]) -> tuple[str, ...]:
    # A string is not a sequence of label entries.
    if isinstance(values, str):
        raise ConfigurationError("disabled_labels: expected a sequence of labels")
    result = tuple(text(value, "disabled_labels entry") for value in values)
    if len(set(result)) != len(result):
        raise ConfigurationError("disabled_labels: duplicate label")
    return tuple(sorted(result))


@dataclass(frozen=True, slots=True)
class ViMoCheckConfig:
    enabled: bool = True
    required: bool = True
    disabled_labels: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        boolean(self.enabled, "enabled")
        boolean(self.required, "required")
        object.__setattr__(self, "disabled_labels", labels(self.disabled_labels))

    @property
    def policy(self) -> CheckPolicy:
        return CheckPolicy(self.enabled, self.required)

    def label_enabled(self, label: str) -> bool:
        """Explicit consumption policy; never mutate SDK settings or evidence."""
        return self.enabled and text(label, "finding label") not in self.disabled_labels


@dataclass(frozen=True, slots=True)
class CheckConfiguration:
    vimo: ViMoCheckConfig = field(default_factory=ViMoCheckConfig)


@dataclass(frozen=True, slots=True)
class ImageArchiveSettings:
    root_directory: str = ""
    save_ok: bool = False
    save_ng: bool = False
    save_error: bool = False
    save_empty: bool = False

    def __post_init__(self) -> None:
        for name in ("save_ok", "save_ng", "save_error", "save_empty"):
            boolean(getattr(self, name), name)
        root = cast(object, self.root_directory)
        if not isinstance(root, str):
            raise ConfigurationError("image_archive.root_directory: expected string")
        if not root:
            if self.enabled:
                raise ConfigurationError("Image archive requires an absolute directory")
            return
        text(root, "image_archive.root_directory")
        path = PureWindowsPath(root)
        if not Path(root).is_absolute() or any(
            any(c in part for c in '<>:"|?*')
            or part.endswith((".", " "))
            or part in (".", "..")
            or part.split(".")[0].upper()
            in {
                "CON",
                "PRN",
                "AUX",
                "NUL",
                *(f"COM{i}" for i in range(1, 10)),
                *(f"LPT{i}" for i in range(1, 10)),
            }
            for part in path.parts[1:]
        ):
            raise ConfigurationError(
                "Image archive requires a valid absolute directory"
            )

    @property
    def enabled(self) -> bool:
        return self.save_ok or self.save_ng or self.save_error or self.save_empty


@dataclass(frozen=True, slots=True)
class RecipeDefinition:
    recipe_id: str
    display_name: str
    defaults: CheckConfiguration = field(default_factory=CheckConfiguration)
    product_code: str | None = None
    model_id: str | None = None
    capsule: CapsuleSettings = field(default_factory=CapsuleSettings)
    image_archive: ImageArchiveSettings = field(default_factory=ImageArchiveSettings)

    def __post_init__(self) -> None:
        persistent_id(self.recipe_id, "recipe_id")
        text(self.display_name, "display_name")
        for name, value in (
            ("product_code", self.product_code),
            ("model_id", self.model_id),
        ):
            if value is not None:
                (persistent_id if name == "model_id" else text)(value, name)


@dataclass(frozen=True, slots=True)
class ModelDefinition:
    model_id: str
    display_name: str
    model_path: str
    expected_sha256: str
    module_id: str

    def __post_init__(self) -> None:
        for name, value in (
            ("model_id", self.model_id),
            ("display_name", self.display_name),
            ("model_path", self.model_path),
            ("module_id", self.module_id),
        ):
            text(value, name)
        persistent_id(self.model_id, "model_id")
        if not re.fullmatch(
            r"[0-9a-fA-F]{64}", text(self.expected_sha256, "expected_sha256")
        ):
            raise ConfigurationError(
                "expected_sha256: expected 64 hexadecimal characters"
            )
        object.__setattr__(self, "expected_sha256", self.expected_sha256.lower())


@dataclass(frozen=True, slots=True)
class MachineLayoutDefinition:
    layout: InspectionLayout

    def __post_init__(self) -> None:
        count = len(self.layout.cameras)
        if count not in (5, 6) or self.layout.camera_ids != tuple(
            f"CAM{i}" for i in range(1, count + 1)
        ):
            raise ConfigurationError("Machine requires CAM1–CAM5 or CAM1–CAM6")
        for index, camera in enumerate(self.layout.cameras):
            text(camera.camera_id, "camera_id")
            if any(
                slot.channel_id != index * 6 + slot.slot + 1 for slot in camera.slots
            ):
                raise ConfigurationError(
                    "Machine requires canonical CameraId/Slot/ChannelId mapping"
                )


@dataclass(frozen=True, slots=True)
class ModelRegistry:
    models: tuple[ModelDefinition, ...]

    def __post_init__(self) -> None:
        models = tuple(sorted(self.models, key=lambda m: m.model_id))
        if len({m.model_id.casefold() for m in models}) != len(models):
            raise ConfigurationError("Duplicate model IDs")
        object.__setattr__(self, "models", models)

    def get(self, model_id: str) -> ModelDefinition:
        text(model_id, "model_id")
        for model in self.models:
            if model.model_id == model_id:
                return model
        raise ConfigurationError(f"Unknown model_id: {model_id!r}")


@dataclass(frozen=True, slots=True)
class ResolvedInspectionConfiguration:
    recipe_id: str
    display_name: str
    product_code: str | None
    model: ModelDefinition
    checks: CheckConfiguration
    layout: InspectionLayout
    capsule: CapsuleSettings = field(default_factory=CapsuleSettings)
    image_archive: ImageArchiveSettings = field(default_factory=ImageArchiveSettings)
