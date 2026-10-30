"""Saved startup preferences; startup and runtime ownership stay with callers."""

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import cast

from capsule_vision.localization import languages

from ._io import atomic_write, read_json
from .errors import ConfigurationError, ConfigurationIOError
from .models import text


@dataclass(frozen=True, slots=True)
class ApplicationPreferences:
    last_applied_recipe_id: str | None = None
    language: languages.Language = "zh"

    def __post_init__(self) -> None:
        if self.last_applied_recipe_id is not None:
            text(self.last_applied_recipe_id, "last_applied_recipe_id")
        if not languages.is_language(self.language):
            raise ConfigurationError("Unsupported UI language")


@dataclass(frozen=True, slots=True)
class LoadedPreferences:
    preferences: ApplicationPreferences
    invalid_language: bool = False


def read_preferences(path: Path) -> LoadedPreferences:
    """Missing preferences use empty selection/zh; malformed files stay observable."""
    try:
        value = read_json(
            path, duplicate_message="Duplicate application preference key: {key!r}"
        )
    except FileNotFoundError:
        return LoadedPreferences(ApplicationPreferences())
    except OSError as exc:
        raise ConfigurationIOError(f"Application preferences {path}: {exc}") from exc
    except ValueError as exc:
        raise ConfigurationError(f"Application preferences {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigurationError("Application preferences must be an object")
    # JSON boundary only; keys and all field values are checked below.
    data = cast(dict[object, object], value)
    fields = {"last_applied_recipe_id", "language"}
    if set(data) - fields:
        raise ConfigurationError("Application preferences have unknown fields")
    last = data.get("last_applied_recipe_id")
    language = data.get("language", "zh")
    if last is not None and not isinstance(last, str):
        raise ConfigurationError("last_applied_recipe_id must be text or null")
    invalid_language = not languages.is_language(language)
    selected: languages.Language = language if languages.is_language(language) else "zh"
    return LoadedPreferences(ApplicationPreferences(last, selected), invalid_language)


def load_preferences(path: Path) -> ApplicationPreferences:
    return read_preferences(path).preferences


def save_preferences(path: Path, preferences: ApplicationPreferences) -> None:
    """Same-directory fsync/replace, matching the domain codecs' atomic convention."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        content = (
            json.dumps(
                {
                    key: value
                    for key, value in asdict(preferences).items()
                    if value is not None
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
        atomic_write(path, content)
    except OSError as exc:
        raise ConfigurationIOError(
            f"Save application preferences {path}: {exc}"
        ) from exc
