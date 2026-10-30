"""Shared file mechanics and the codecs' deliberately distinct IO policies."""

import os
from collections.abc import Callable
from pathlib import Path

import pytest

from capsule_vision.configuration import (
    ConfigurationError,
    ConfigurationIOError,
    RecipeDefinition,
    load_recipe,
    save_recipe,
)
from capsule_vision.configuration._io import atomic_write, read_json
from capsule_vision.configuration.communication_preferences import (
    CommunicationPreferences,
    load_communication_preferences,
    save_communication_preferences,
)
from capsule_vision.configuration.preferences import (
    ApplicationPreferences,
    read_preferences,
    save_preferences,
)

SAVERS: tuple[tuple[Callable[[Path], None], str, bool], ...] = (
    (lambda p: save_recipe(p, RecipeDefinition("probe", "Recipe")), "Save ", False),
    (
        lambda p: save_preferences(p, ApplicationPreferences("probe", "en")),
        "Save application preferences ",
        True,
    ),
    (
        lambda p: save_communication_preferences(p, CommunicationPreferences()),
        "Cannot save communication settings ",
        True,
    ),
)


def test_publication_flushes_and_closes_before_replacing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "target.json"
    path.write_bytes(b"previous")
    content = '{"name":"图像"}\n'
    descriptors: list[int] = []
    original_fsync, original_replace = os.fsync, os.replace

    def fsync(fd: int) -> None:
        assert os.fstat(fd).st_size == len(content.encode("utf-8"))
        descriptors.append(fd)
        original_fsync(fd)

    def replace(source: Path, target: Path) -> None:
        assert len(descriptors) == 1
        with pytest.raises(OSError):
            os.fstat(descriptors[0])
        assert source.parent == target.parent == tmp_path
        assert source.read_bytes() == content.encode("utf-8")
        assert target.read_bytes() == b"previous"
        original_replace(source, target)

    monkeypatch.setattr("capsule_vision.configuration._io.os.fsync", fsync)
    monkeypatch.setattr("capsule_vision.configuration._io.os.replace", replace)
    atomic_write(path, content, newline="\n")
    assert path.read_bytes() == content.encode("utf-8")
    assert tuple(tmp_path.iterdir()) == (path,)


def test_exclusive_publication_never_overwrites_existing_target(tmp_path: Path) -> None:
    path = tmp_path / "target.json"
    atomic_write(path, "first", exclusive=True)
    with pytest.raises(FileExistsError):
        atomic_write(path, "replacement", exclusive=True)
    assert path.read_text(encoding="utf-8") == "first"
    assert tuple(tmp_path.iterdir()) == (path,)


@pytest.mark.parametrize("save,context,creates_parent", SAVERS)
def test_codec_parent_creation_policy(
    tmp_path: Path, save: Callable[[Path], None], context: str, creates_parent: bool
) -> None:
    path = tmp_path / "missing" / "probe.json"
    if creates_parent:
        save(path)
        assert path.is_file()
    else:
        with pytest.raises(ConfigurationIOError, match=context):
            save(path)
        assert not path.parent.exists()


@pytest.mark.parametrize("save,context,creates_parent", SAVERS)
def test_fsync_failure_retains_target_and_cleans_temporary_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    save: Callable[[Path], None],
    context: str,
    creates_parent: bool,
) -> None:
    path = tmp_path / "probe.json"
    path.write_bytes(b"previous")

    def fail(fd: int) -> None:
        raise OSError("controlled fsync failure")

    monkeypatch.setattr("capsule_vision.configuration._io.os.fsync", fail)
    with pytest.raises(ConfigurationIOError, match="controlled fsync") as failure:
        save(path)
    assert str(failure.value).startswith(context + str(path))
    assert isinstance(failure.value.__cause__, OSError)
    assert path.read_bytes() == b"previous"
    assert tuple(tmp_path.iterdir()) == (path,)


@pytest.mark.parametrize("save,context,creates_parent", SAVERS)
def test_failed_publication_keeps_cleanup_failure_observable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    save: Callable[[Path], None],
    context: str,
    creates_parent: bool,
) -> None:
    path = tmp_path / "probe.json"
    path.write_bytes(b"previous")

    def replace(source: Path, target: Path) -> None:
        raise PermissionError("controlled publication failure")

    def unlink(self: Path, missing_ok: bool = False) -> None:
        raise PermissionError("controlled cleanup failure")

    with monkeypatch.context() as patch:
        patch.setattr("capsule_vision.configuration._io.os.replace", replace)
        patch.setattr(Path, "unlink", unlink)
        with pytest.raises(ConfigurationIOError, match="publication") as failure:
            save(path)
    assert str(failure.value).startswith(context + str(path))
    cause = failure.value.__cause__
    assert isinstance(cause, PermissionError)
    temporary = tuple(p for p in tmp_path.iterdir() if p != path)
    assert len(temporary) == 1
    assert any(
        "controlled cleanup failure" in note and str(temporary[0]) in note
        for note in cause.__notes__
    )
    assert path.read_bytes() == b"previous"


def test_exclusive_cleanup_failure_preserves_completed_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "target.json"

    def unlink(self: Path, missing_ok: bool = False) -> None:
        raise PermissionError("controlled cleanup failure")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", unlink)
        with pytest.raises(PermissionError, match="cleanup") as failure:
            atomic_write(path, "complete", exclusive=True)
    assert path.read_text(encoding="utf-8") == "complete"
    assert len(tuple(tmp_path.iterdir())) == 2
    assert any("cleanup failure" in note for note in failure.value.__notes__)


READERS: tuple[tuple[Callable[[Path], object], str, str], ...] = (
    (load_recipe, '{"display_name":"A","display_name":"B"}', "Configuration"),
    (read_preferences, '{"language":"zh","language":"en"}', "Application preferences"),
    (
        load_communication_preferences,
        '{"web":{"port":8765,"port":8766}}',
        "Invalid communication settings",
    ),
)


@pytest.mark.parametrize("load,content,context", READERS)
def test_duplicate_keys_keep_codec_and_path_context(
    tmp_path: Path, load: Callable[[Path], object], content: str, context: str
) -> None:
    path = tmp_path / "probe.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigurationError, match="Duplicate") as failure:
        load(path)
    assert str(failure.value).startswith(context + " " + str(path))


def test_json_reader_leaves_root_shape_validation_to_codecs(tmp_path: Path) -> None:
    path = tmp_path / "values.json"
    path.write_text('[{"value":"图像"}, null, 2]', encoding="utf-8")
    assert read_json(path) == [{"value": "图像"}, None, 2]
    path.write_text('[{"value":1,"value":2}]', encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate JSON key"):
        read_json(path)
