"""Decoded dataset semantics without Qt, inference or large allocations."""

import weakref
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from tests.support.application import prepared
from tests.support.source_inputs import folders, write_image

from capsule_vision.application import folder_replay
from capsule_vision.application.folder_cache import (
    AvailableMemory,
    CacheState,
    FolderLoadCancelled,
    FolderReplayCache,
)
from capsule_vision.application.folder_replay import FolderReplay, FolderSourceError
from capsule_vision.inspection import CameraId


def plenty() -> AvailableMemory:
    return AvailableMemory(16 * 1024**3, 32 * 1024**3)


def test_exact_order_pixels_full_preload_hits_and_immutable_views(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources = folders(tmp_path)
    layout = prepared(tmp_path).configuration.layout
    original = FolderReplay(layout, sources, tmp_path)
    expected = [original.next_group() for _ in range(7)]
    decode = folder_replay.decode_image
    calls: list[Path] = []

    def record(path: Path):
        calls.append(path)
        return decode(path)

    monkeypatch.setattr(folder_replay, "decode_image", record)
    cache = FolderReplayCache(memory=plenty)
    replay = cache.replay(layout, sources, tmp_path)
    assert replay.cached and cache.snapshot().state is CacheState.READY
    assert len(calls) == cache.snapshot().image_count == 15
    assert cache.snapshot().decoded_bytes == 15 * 20 * 60 * 3
    first = replay.next_group()
    assert first is not None
    for actual, known in zip(
        (first, *(replay.next_group() for _ in range(6))), expected, strict=True
    ):
        assert known is not None
        assert actual is not None
        assert all(np.array_equal(a, b) for a, b in zip(actual, known, strict=True))
    for image in first:
        assert not image.flags.writeable
        with pytest.raises(ValueError):
            image[0, 0, 0] = 99
        with pytest.raises(ValueError):
            image.setflags(write=True)
    hit = cache.replay(layout, sources, tmp_path)
    assert cache.snapshot().state is CacheState.HIT and len(calls) == 15
    next_hit = hit.next_group()
    assert next_hit is not None
    assert all(a is b for a, b in zip(first, next_hit, strict=True))
    assert hit.indices == [1] * 6


@pytest.mark.parametrize("change", ["add", "remove", "replace", "metadata", "sources"])
def test_start_invalidates_source_metadata_and_keeps_old_replay_frozen(
    tmp_path: Path, change: str
) -> None:
    sources = folders(tmp_path)
    layout = prepared(tmp_path).configuration.layout
    cache = FolderReplayCache(memory=plenty)
    old = cache.replay(layout, sources, tmp_path)
    identity = cache.snapshot().dataset_id
    first = old.next_group()
    assert first is not None
    directory = sources.camera(CameraId("CAM1")).path(tmp_path)
    if change == "add":
        write_image(directory / "unrelated0.png", 99)
    elif change == "remove":
        (directory / "unrelated1.png").unlink()
    elif change == "replace":
        temp = directory / "replacement.png"
        write_image(temp, 99)
        temp.replace(directory / "unrelated1.png")
    elif change == "metadata":
        import os

        path = directory / "unrelated1.png"
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    else:
        sources = replace(sources, target_fps=9)
    new = cache.replay(layout, sources, tmp_path)
    assert (
        cache.snapshot().state is CacheState.READY
        and cache.snapshot().dataset_id != identity
    )
    # Disk changes never modify a previously published snapshot or camera association.
    assert int(first[0][0, 0, 0]) == 1
    next_new = new.next_group()
    assert next_new is not None
    assert int(next_new[0][0, 0, 0]) == (
        99 if change in ("add", "replace") else 2 if change == "remove" else 1
    )
    next_old = old.next_group()
    assert next_old is not None
    assert [int(i[0, 0, 0]) for i in next_old] == [2, 12, 22, 32, 42, 52]


@pytest.mark.parametrize("bad", ["corrupt", "shape", "bounds"])
def test_failed_rebuild_never_publishes_partial_or_stale_pixels(
    tmp_path: Path, bad: str
) -> None:
    sources = folders(tmp_path)
    layout = prepared(tmp_path).configuration.layout
    cache = FolderReplayCache(memory=plenty)
    cache.replay(layout, sources, tmp_path)
    path = sources.camera(CameraId("CAM6")).path(tmp_path) / "unrelated2.png"
    if bad == "corrupt":
        path.write_bytes(b"broken")
    else:
        write_image(path, 0, (21, 60) if bad == "shape" else (1, 1))
    with pytest.raises(FolderSourceError, match="CAM6"):
        cache.replay(layout, sources, tmp_path)
    assert (
        cache.snapshot().state is CacheState.FAILED
        and cache.snapshot().decoded_bytes == 0
    )
    write_image(path, 52)
    assert cache.replay(layout, sources, tmp_path).cached
    assert cache.snapshot().state is CacheState.READY


def test_memory_fallback_discards_partial_arrays_and_retries_on_next_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources = folders(tmp_path)
    layout = prepared(tmp_path).configuration.layout
    checks = 0

    def memory() -> AvailableMemory:
        nonlocal checks
        checks += 1
        return plenty() if checks < 6 else AvailableMemory(0, 32 * 1024**3)

    refs: list[
        weakref.ReferenceType[np.ndarray[tuple[int, ...], np.dtype[np.uint8]]]
    ] = []
    decode = folder_replay.decode_image

    def record(path: Path):
        image = decode(path)
        refs.append(weakref.ref(image))
        return image

    monkeypatch.setattr(folder_replay, "decode_image", record)
    cache = FolderReplayCache(memory=memory)
    fallback = cache.replay(layout, sources, tmp_path)
    assert not fallback.cached and cache.snapshot().state is CacheState.FALLBACK
    assert cache.snapshot().decoded_bytes == 0 and cache.snapshot().reason
    assert all(r() is None for r in refs)
    assert (
        fallback.next_group() is not None
    )  # Original first-group/late-error semantics.
    monkeypatch.setattr(cache, "_memory", plenty)
    assert cache.replay(layout, sources, tmp_path).cached
    assert cache.snapshot().state is CacheState.READY


@pytest.mark.parametrize("stop", ["cancel", "data_change"])
def test_preload_atomicity_cancellation_and_traceback_pixel_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop: str
) -> None:
    sources = folders(tmp_path)
    layout = prepared(tmp_path).configuration.layout
    cache = FolderReplayCache(memory=plenty)
    decode = folder_replay.decode_image
    refs: list[
        weakref.ReferenceType[np.ndarray[tuple[int, ...], np.dtype[np.uint8]]]
    ] = []
    calls = 0

    def record(path: Path):
        nonlocal calls
        calls += 1
        image = decode(path)
        refs.append(weakref.ref(image))
        if calls == 4 and stop == "data_change":
            write_image(
                sources.camera(CameraId("CAM1")).path(tmp_path) / "added.png", 7
            )
        return image

    monkeypatch.setattr(folder_replay, "decode_image", record)
    with pytest.raises(FolderSourceError) as failure:
        cache.replay(
            layout, sources, tmp_path, cancelled=lambda: stop == "cancel" and calls == 4
        )
    assert isinstance(failure.value, FolderLoadCancelled) == (stop == "cancel")
    assert cache.snapshot().state is (
        CacheState.CANCELLED if stop == "cancel" else CacheState.FAILED
    )
    # Retaining the exception must not retain preceding decoded groups.
    assert all(r() is None for r in refs[:-1])
    assert cache.snapshot().decoded_bytes == 0


def test_loop_off_is_unchanged_and_current_roi_bounds_revalidated(
    tmp_path: Path,
) -> None:
    sources = folders(tmp_path)
    layout = prepared(tmp_path).configuration.layout
    cache = FolderReplayCache(memory=plenty)
    cache.replay(layout, sources, tmp_path)
    finite = cache.replay(layout, replace(sources, loop=False), tmp_path)
    assert (
        not finite.cached
        and finite.next_group() is not None
        and finite.next_group() is not None
    )
    assert finite.next_group() is None
    cache.replay(layout, sources, tmp_path)
    first = layout.cameras[0]
    slots = tuple(
        replace(slot, rect=replace(slot.rect, width=100)) for slot in first.slots
    )
    changed = replace(
        layout, cameras=(replace(first, slots=slots), *layout.cameras[1:])
    )
    with pytest.raises(FolderSourceError, match="CAM1.*outside"):
        cache.replay(changed, sources, tmp_path)
    assert cache.snapshot().state is CacheState.FAILED


def test_unavailable_memory_and_allocation_error_are_observable_fallbacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources = folders(tmp_path)
    layout = prepared(tmp_path).configuration.layout
    cache = FolderReplayCache(memory=lambda: None)
    assert not cache.replay(layout, sources, tmp_path).cached
    assert cache.snapshot().state is CacheState.FALLBACK

    def failed(_path: Path):
        raise MemoryError("allocation failed")

    monkeypatch.setattr(folder_replay, "decode_image", failed)
    cache = FolderReplayCache(memory=plenty)
    assert not cache.replay(layout, sources, tmp_path).cached
    assert "allocation" in (cache.snapshot().reason or "")


def test_warm_cache_validation_cancel_retains_valid_cache_but_rechecks_next_start(
    tmp_path: Path,
) -> None:
    sources = folders(tmp_path)
    layout = prepared(tmp_path).configuration.layout
    cache = FolderReplayCache(memory=plenty)
    cache.replay(layout, sources, tmp_path)
    identity = cache.snapshot().dataset_id
    with pytest.raises(FolderLoadCancelled):
        cache.replay(layout, sources, tmp_path, cancelled=lambda: True)
    assert cache.snapshot().decoded_bytes == 54000
    cache.replay(layout, sources, tmp_path)
    assert (
        cache.snapshot().state is CacheState.HIT
        and cache.snapshot().dataset_id == identity
    )
    cache.clear()
    assert cache.snapshot().decoded_bytes == 0


def test_insufficient_reserve_on_hit_discards_old_cache_and_does_not_reuse_stale_pixels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources = folders(tmp_path)
    layout = prepared(tmp_path).configuration.layout
    cache = FolderReplayCache(memory=plenty)
    cache.replay(layout, sources, tmp_path)
    monkeypatch.setattr(cache, "_memory", lambda: AvailableMemory(0, 32 * 1024**3))
    assert not cache.replay(layout, sources, tmp_path).cached
    assert (
        cache.snapshot().state is CacheState.FALLBACK
        and cache.snapshot().decoded_bytes == 0
    )
