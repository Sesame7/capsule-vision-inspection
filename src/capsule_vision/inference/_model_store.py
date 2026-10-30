"""Verified managed loading and isolated staging for external model inputs."""

import hashlib
import os
import shutil
from pathlib import Path
from tempfile import NamedTemporaryFile


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def managed_model(source: Path, expected_sha256: str) -> tuple[Path, str]:
    """Verify an explicitly managed artifact without making a runtime copy."""
    source = source.absolute()

    def unlinked(path: Path) -> None:
        if path.is_symlink() or path.is_junction() or path.resolve() != path:
            raise ValueError(f"Managed path must not traverse a link: {path}")

    unlinked(source)
    if source.name != "model.vimosln" or not source.is_file():
        raise ValueError(f"Expected the managed model.vimosln: {source}")
    if (source.parent / "vimo_params.json").exists():
        raise ValueError("Managed loading does not accept a vimo_params.json overlay")
    cache = source.parent / "vimo_trt_cache"
    unlinked(cache)

    def inaccessible(error: OSError) -> None:
        raise error

    if cache.exists():
        for root, directories, files in os.walk(cache, onerror=inaccessible):
            for name in (*directories, *files):
                unlinked(Path(root) / name)
    digest = sha256(source)
    if digest != expected_sha256:
        raise ValueError("Managed model SHA-256 differs from configured identity")
    return source, digest


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
