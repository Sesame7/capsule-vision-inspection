"""Private strict JSON decoding and single-file publication mechanics."""

import json
import os
import tempfile
from pathlib import Path


def read_json(
    path: Path, *, duplicate_message: str = "Duplicate JSON key: {key!r}"
) -> object:
    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(duplicate_message.format(key=key))
            result[key] = value
        return result

    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)


def atomic_write(
    path: Path,
    content: str,
    *,
    exclusive: bool = False,
    newline: str | None = None,
) -> None:
    """Publish closed, flushed data; the caller owns parent and error policies."""
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline=newline,
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
