"""Project-local startup preferences; independent of domain configuration codecs."""

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, cast

from vimo_demo.configuration import ConfigurationError, ConfigurationIOError
from vimo_demo.configuration.models import text


@dataclass(frozen=True, slots=True)
class ApplicationPreferences:
    default_model_id: str | None = None
    last_applied_recipe_id: str | None = None
    language: Literal["zh", "en"] = "zh"
    schema_version: int = 1

    def __post_init__(self) -> None:
        for name, value in (
            ("default_model_id", self.default_model_id),
            ("last_applied_recipe_id", self.last_applied_recipe_id),
        ):
            if value is not None:
                text(value, name)
        if self.language not in ("zh", "en"):
            raise ConfigurationError("UI language must be zh or en")
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ConfigurationError("Unsupported application preferences version")


def load_preferences(path: Path) -> ApplicationPreferences:
    try:
        value: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigurationIOError(f"Application preferences {path}: {exc}") from exc
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
    if set(data) != fields:
        raise ConfigurationError("Application preferences have missing/unknown fields")
    default = data["default_model_id"]
    last = data["last_applied_recipe_id"]
    language, version = data["language"], data["schema_version"]
    if default is not None and not isinstance(default, str):
        raise ConfigurationError("default_model_id must be text or null")
    if last is not None and not isinstance(last, str):
        raise ConfigurationError("last_applied_recipe_id must be text or null")
    if language not in ("zh", "en"):
        raise ConfigurationError("UI language must be zh or en")
    if type(version) is not int:
        raise ConfigurationError("Application preferences version must be an integer")
    return ApplicationPreferences(default, last, language, version)


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
        raise ConfigurationIOError(
            f"Save application preferences {path}: {exc}"
        ) from exc
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
