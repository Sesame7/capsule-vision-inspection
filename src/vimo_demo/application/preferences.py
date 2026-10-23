"""Project-local startup preferences; independent of domain configuration codecs."""

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, TypeGuard, cast

from vimo_demo.configuration import ConfigurationError, ConfigurationIOError
from vimo_demo.configuration.models import text

type Language = Literal[
    "zh",
    "en",
    "ja",
    "ko",
    "es",
    "pt-BR",
    "it",
    "id",
    "fr",
    "de",
    "ru",
    "tr",
    "zh-TW",
    "nl",
    "pl",
    "uk",
    "ro",
    "cs",
    "hu",
    "sv",
    "da",
    "fi",
    "nb",
    "sk",
    "bg",
    "hr",
    "sr-Latn",
    "el",
    "pt-PT",
    "vi",
    "ms",
    "fil",
    "sw",
    "th",
    "hi",
    "bn",
    "ta",
    "te",
    "mr",
    "ne",
    "si",
    "am",
    "km",
    "lt",
    "lv",
    "et",
    "sl",
    "sq",
    "ca",
    "af",
    "zu",
    "ha",
    "kk",
    "uz",
    "mn",
    "so",
    "tk",
    "tg",
    "az-Latn",
    "mg",
    "ky",
]

LANGUAGES: tuple[Language, ...] = (
    "zh",
    "en",
    "ja",
    "ko",
    "es",
    "pt-BR",
    "it",
    "id",
    "fr",
    "de",
    "ru",
    "tr",
    "zh-TW",
    "nl",
    "pl",
    "uk",
    "ro",
    "cs",
    "hu",
    "sv",
    "da",
    "fi",
    "nb",
    "sk",
    "bg",
    "hr",
    "sr-Latn",
    "el",
    "pt-PT",
    "vi",
    "ms",
    "fil",
    "sw",
    "th",
    "hi",
    "bn",
    "ta",
    "te",
    "mr",
    "ne",
    "si",
    "am",
    "km",
    "lt",
    "lv",
    "et",
    "sl",
    "sq",
    "ca",
    "af",
    "zu",
    "ha",
    "kk",
    "uz",
    "mn",
    "so",
    "tk",
    "tg",
    "az-Latn",
    "mg",
    "ky",
)


def is_language(value: object) -> TypeGuard[Language]:
    return isinstance(value, str) and value in LANGUAGES


@dataclass(frozen=True, slots=True)
class ApplicationPreferences:
    default_model_id: str | None = None
    last_applied_recipe_id: str | None = None
    language: Language = "zh"
    schema_version: int = 1

    def __post_init__(self) -> None:
        for name, value in (
            ("default_model_id", self.default_model_id),
            ("last_applied_recipe_id", self.last_applied_recipe_id),
        ):
            if value is not None:
                text(value, name)
        if not is_language(self.language):
            raise ConfigurationError("Unsupported UI language")
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ConfigurationError("Unsupported application preferences version")


@dataclass(frozen=True, slots=True)
class LoadedPreferences:
    preferences: ApplicationPreferences
    invalid_language: bool = False


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ConfigurationError(f"Duplicate application preference key: {key!r}")
        result[key] = value
    return result


def read_preferences(path: Path) -> LoadedPreferences:
    """Only language is optional/recoverable; never guess production identities."""
    try:
        value: object = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object
        )
    except OSError as exc:
        raise ConfigurationIOError(f"Application preferences {path}: {exc}") from exc
    except ValueError as exc:
        raise ConfigurationError(f"Application preferences {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigurationError("Application preferences must be an object")
    # JSON boundary only; keys and all field values are checked below.
    data = cast(dict[object, object], value)
    fields = {
        "schema_version",
        "default_model_id",
        "last_applied_recipe_id",
        "language",
    }
    if set(data) - fields or fields - {"language"} - set(data):
        raise ConfigurationError("Application preferences have missing/unknown fields")
    default = data["default_model_id"]
    last = data["last_applied_recipe_id"]
    language, version = data.get("language", "zh"), data["schema_version"]
    if default is not None and not isinstance(default, str):
        raise ConfigurationError("default_model_id must be text or null")
    if last is not None and not isinstance(last, str):
        raise ConfigurationError("last_applied_recipe_id must be text or null")
    invalid_language = not is_language(language)
    selected: Language = language if is_language(language) else "zh"
    if type(version) is not int:
        raise ConfigurationError("Application preferences version must be an integer")
    return LoadedPreferences(
        ApplicationPreferences(default, last, selected, version),
        invalid_language,
    )


def load_preferences(path: Path) -> ApplicationPreferences:
    return read_preferences(path).preferences


def save_preferences(path: Path, preferences: ApplicationPreferences) -> None:
    """Same-directory fsync/replace, matching the domain codecs' atomic convention."""
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(
                json.dumps(asdict(preferences), ensure_ascii=False, indent=2) + "\n"
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as cleanup_error:
                exc.add_note(f"Temporary preferences cleanup: {cleanup_error}")
        raise ConfigurationIOError(
            f"Save application preferences {path}: {exc}"
        ) from exc
