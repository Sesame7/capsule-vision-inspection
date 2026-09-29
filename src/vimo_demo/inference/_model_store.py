"""Stage a checksum-verified model so TensorRT never writes beside the source."""

import hashlib
import shutil
from pathlib import Path
from tempfile import NamedTemporaryFile


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def stage_model(source: Path, cache_dir: Path) -> tuple[Path, str]:
    source = source.resolve(strict=True)
    if not source.is_file() or source.suffix.lower() != ".vimosln":
        raise ValueError(f"Expected a .vimosln file: {source}")
    digest = sha256(source)
    directory = cache_dir.resolve() / digest
    target = directory / "model.vimosln"
    if target.resolve() == source:
        raise ValueError("Runtime model must be separate from the source model")
    directory.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if sha256(target) != digest:
            raise ValueError(f"Staged model checksum mismatch: {target}")
        return target, digest
    with NamedTemporaryFile(dir=directory, suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            with source.open("rb") as original:
                shutil.copyfileobj(original, stream)
        except BaseException:
            stream.close()
            temporary.unlink(missing_ok=True)
            raise
    try:
        if sha256(temporary) != digest:
            raise ValueError("Source model changed while staging")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target, digest
