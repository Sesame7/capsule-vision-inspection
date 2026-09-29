from pathlib import Path

import pytest

from vimo_demo.inference._model_store import stage_model


def test_model_is_staged_once_without_changing_source(tmp_path: Path) -> None:
    source = tmp_path / "vendor/model.vimosln"
    source.parent.mkdir()
    source.write_bytes(b"small fake model for file handling only")
    runtime, digest = stage_model(source, tmp_path / "cache")
    assert runtime != source
    assert runtime.parent.name == digest
    assert runtime.read_bytes() == source.read_bytes()
    before = runtime.stat().st_mtime_ns
    assert stage_model(source, tmp_path / "cache") == (runtime, digest)
    assert runtime.stat().st_mtime_ns == before
    assert list(source.parent.iterdir()) == [source]


def test_corrupt_cached_model_is_not_used_or_overwritten(tmp_path: Path) -> None:
    source = tmp_path / "model.vimosln"
    source.write_bytes(b"model")
    runtime, _ = stage_model(source, tmp_path / "cache")
    runtime.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="checksum mismatch"):
        stage_model(source, tmp_path / "cache")
    assert source.read_bytes() == b"model"
    assert runtime.read_bytes() == b"corrupt"
