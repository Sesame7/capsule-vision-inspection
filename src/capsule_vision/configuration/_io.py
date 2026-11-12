"""Private strict JSON decoding and single-file publication mechanics."""

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .errors import ConfigurationIOError


@dataclass(frozen=True, slots=True)
class ConfigurationPublication:
    """The codec's exact bytes, ready for the existing single-file publication."""

    path: Path
    content: bytes
    exclusive: bool = False
    create_parent: bool = False
    error_context: str = "Save"

    def publish(self) -> None:
        try:
            if self.create_parent:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(self.path, self.content, exclusive=self.exclusive)
        except (OSError, ValueError, TypeError) as exc:
            raise ConfigurationIOError(
                f"{self.error_context} {self.path}: {exc}"
            ) from exc


def read_json(
    path: Path, *, duplicate_message: str = "Duplicate JSON key: {key!r}"
) -> object:
    return decode_json(path.read_text(encoding="utf-8"), duplicate_message)


def decode_json(
    content: str, duplicate_message: str = "Duplicate JSON key: {key!r}"
) -> object:
    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(duplicate_message.format(key=key))
            result[key] = value
        return result

    return json.loads(content, object_pairs_hook=unique)


def atomic_write(
    path: Path,
    content: str | bytes,
    *,
    exclusive: bool = False,
    newline: str | None = None,
) -> None:
    """Publish closed, flushed data; the caller owns parent and error policies."""
    temporary: Path | None = None
    try:
        if isinstance(content, str):
            content = content.replace(
                "\n", os.linesep if newline is None else newline or "\n"
            ).encode("utf-8")
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            os.link(temporary, path)
            temporary.unlink()
        else:
            os.replace(temporary, path)
    except (OSError, ValueError, TypeError) as exc:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as cleanup_error:
                exc.add_note(
                    f"Could not remove temporary file {temporary}: {cleanup_error}"
                )
        raise
