"""Application-lifetime cache wiring with deterministic controlled native calls."""

from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path
from threading import Event, current_thread, get_ident
from threading import enumerate as threads

import numpy as np
import pytest
from numpy.typing import NDArray
from PySide6.QtWidgets import QApplication
from tests.application_support import WAIT
from tests.integration.test_local_production_ui import ControlledEngine
from tests.local_input_support import input_files
from tests.production_support import inferred
from tests.qt_support import until
from tests.unit.test_folder_replay import folders, write_image

from vimo_demo.application import (
    ApplicationError,
    ApplicationState,
    ConfiguredProductionPipeline,
    ProductionCounters,
    folder_replay,
)
from vimo_demo.application.folder_cache import (
    AvailableMemory,
    CacheState,
    FolderCacheStatus,
    FolderReplayCache,
)
from vimo_demo.application.folder_replay import FolderReplay
from vimo_demo.configuration import save_camera_sources
from vimo_demo.inference import BoundingRect, ImageResult
from vimo_demo.inspection import CameraId, CycleCollector, CycleId, Frame, FrameId
from vimo_demo.inspection.production import ProductionInspectionResult
from vimo_demo.ui.local_production import compose


class InputEngine(ControlledEngine):
    def __init__(self) -> None:
        super().__init__()
        self.owner = get_ident()
        self.thread_name = current_thread().name
        self.inputs: list[NDArray[np.generic]] = []
        self.release_warm.set()

    def infer(self, image_bgr: NDArray[np.generic]) -> ImageResult:
        assert get_ident() == self.owner and not image_bgr.flags.writeable
        self.inputs.append(image_bgr)
        super().infer(image_bgr)
        return (
            inferred(BoundingRect(1, 1, 3, 4))
            if int(image_bgr[0, 0, 0]) % 2
            else inferred()
        )

    def close(self) -> None:
        assert get_ident() == self.owner
        super().close()


@pytest.mark.parametrize("cached", [False, True])
def test_restart_reuses_pixels_but_not_engine_results_frames_or_sessions(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cached: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    before = {t.ident for t in threads()}
    model, machine, recipe, _ = input_files(tmp_path)
    sources = folders(tmp_path, loop=cached)
    save_camera_sources(tmp_path / "camera_sources.json", sources)
    engines: list[InputEngine] = []
    decode = folder_replay.decode_image
    decoded: list[Path] = []

    def record(path: Path):
        decoded.append(path)
        return decode(path)

    def factory(_config: object) -> InputEngine:
        engine = InputEngine()
        engines.append(engine)
        return engine

    monkeypatch.setattr(folder_replay, "decode_image", record)
    composition = compose(model, machine, recipe, engine_factory=factory)
    app = composition.application
    results: list[ProductionInspectionResult] = []
    try:
        for generation in (1, 2):
            # Unsaved UI edits must never select an alternate source for Start.
            draft = composition.editor.camera_sources
            assert draft is not None
            draft.set_folder(CameraId("CAM1"), "unsaved-missing")
            app.start().result(WAIT)
            engine = engines[-1]
            until(engine.active.is_set)
            assert app.snapshot().generation == generation
            count = len(decoded)
            assert count == (15 if cached else 6 * generation)
            stopped = app.stop()
            assert not stopped.done()
            engine.release_active.set()
            stopped.result(WAIT)
            assert (
                len(decoded) == count
            )  # Even the full active cycle uses borrowed cached pixels.
            result = app.snapshot().latest_production_result
            assert result is not None and result.cycle_id == 1
            assert tuple(c.camera_id for c in result.cameras) == tuple(
                f"CAM{i}" for i in range(1, 7)
            )
            assert [c.frame_id.sequence for c in result.cameras if c.frame_id] == [
                0
            ] * 6
            results.append(result)
            assert [v for v, _ in engine.calls][2:] == [1, 11, 21, 31, 41, 51]
            assert engine.closed and engine.owner != get_ident()
            assert not np.shares_memory(engine.inputs[0], engine.inputs[2])
            for image, frame in zip(
                engine.inputs[2:], composition.store.snapshot().frames, strict=True
            ):
                assert frame.generation == generation and frame.frame_id.cycle_id == 1
                assert np.array_equal(image, frame.image) and not np.shares_memory(
                    image, frame.image
                )
                assert not frame.image.flags.writeable
            assert app.snapshot().counters.cycles_completed == generation
            assert (
                app.replay_cache.snapshot().state
                is (CacheState.READY if generation == 1 else CacheState.HIT)
                if cached
                else app.replay_cache.snapshot().state is CacheState.RELEASED
            )
        assert results[0].assessment == results[1].assessment
        for first, restarted in zip(
            results[0].cameras, results[1].cameras, strict=True
        ):
            # Real monotonic stage durations differ; all inspection data stays exact.
            assert first.layout == restarted.layout
            assert first.frame_id == restarted.frame_id
            assert first.image_shape == restarted.image_shape
            assert first.channels == restarted.channels
            assert first.unassigned_findings == restarted.unassigned_findings
            assert first.collection_issue == restarted.collection_issue
        assert len(engines) == 2 and engines[0] is not engines[1]
        assert engines[0].thread_name != engines[1].thread_name
        assert all(e.closed for e in engines)
    finally:
        for engine in engines:
            engine.release_active.set()
        app.close().result(WAIT)
        assert app.join(WAIT)
    assert app.replay_cache.snapshot().state is CacheState.RELEASED
    assert app.replay_cache.snapshot().decoded_bytes == 0
    captured = capsys.readouterr()
    assert not captured.out and not captured.err
    assert not {t.ident for t in threads()} - before


@pytest.mark.parametrize("closing", [False, True])
def test_stop_close_during_preload_remains_async_and_never_constructs_engine(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closing: bool,
) -> None:
    model, machine, recipe, _ = input_files(tmp_path)
    save_camera_sources(tmp_path / "camera_sources.json", folders(tmp_path))
    entered, release = Event(), Event()
    decoder = folder_replay.decode_image
    calls = 0

    def blocked(path: Path):
        nonlocal calls
        calls += 1
        if calls == 3:
            entered.set()
            assert release.wait(WAIT)
        return decoder(path)

    def forbidden(_config: object) -> InputEngine:
        raise AssertionError("Incomplete/cancelled cache constructed a native engine")

    monkeypatch.setattr(folder_replay, "decode_image", blocked)
    composition = compose(model, machine, recipe, engine_factory=forbidden)
    app = composition.application
    try:
        startup = app.start()
        until(entered.is_set)
        assert app.snapshot().state is ApplicationState.STARTING
        assert app.replay_cache.snapshot().state is CacheState.LOADING
        stopped = app.close() if closing else app.stop()
        assert not stopped.done() and app.snapshot().state is ApplicationState.STOPPING
        assert not app.snapshot().runtime_owned
        qapp.processEvents()  # Qt remains responsive while lifecycle thread is decoding.
        release.set()
        stopped.result(WAIT)
        with pytest.raises(ApplicationError):
            startup.result(WAIT)
        assert calls == 3 and app.snapshot().counters == ProductionCounters()
        assert app.snapshot().active is None
        assert app.snapshot().state is (
            ApplicationState.CLOSED if closing else ApplicationState.READY
        )
        assert app.replay_cache.snapshot().decoded_bytes == 0
    finally:
        release.set()
        app.close().result(WAIT)
        assert app.join(WAIT)


def test_bad_full_dataset_never_creates_runtime_or_admits_partial_cycle(
    tmp_path: Path,
) -> None:
    model, machine, recipe, _ = input_files(tmp_path)
    sources = folders(tmp_path)
    save_camera_sources(tmp_path / "camera_sources.json", sources)
    (sources.camera(CameraId("CAM6")).path(tmp_path) / "unrelated3.png").write_bytes(
        b"broken"
    )
    creations = 0

    def forbidden(_config: object) -> InputEngine:
        nonlocal creations
        creations += 1
        return InputEngine()

    composition = compose(model, machine, recipe, engine_factory=forbidden)
    app = composition.application
    try:
        with pytest.raises(ApplicationError, match="CAM6"):
            app.start().result(WAIT)
        assert creations == 0 and app.snapshot().counters == ProductionCounters()
        assert (
            app.snapshot().state is ApplicationState.READY
            and not app.snapshot().runtime_owned
        )
        assert app.replay_cache.snapshot().state is CacheState.FAILED
    finally:
        app.close().result(WAIT)
        assert app.join(WAIT)


def test_memory_fallback_is_explicit_and_executes_original_source(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vimo_demo.ui import local_production

    notices: list[FolderCacheStatus] = []

    def limited(*, notify: Callable[[FolderCacheStatus], None]) -> FolderReplayCache:
        return FolderReplayCache(
            memory=lambda: AvailableMemory(0, 32 * 1024**3), notify=notify
        )

    monkeypatch.setattr(
        local_production,
        "FolderReplayCache",
        limited,
    )
    model, machine, recipe, _ = input_files(tmp_path)
    save_camera_sources(tmp_path / "camera_sources.json", folders(tmp_path))
    engines: list[InputEngine] = []

    def factory(_config: object) -> InputEngine:
        engine = InputEngine()
        engines.append(engine)
        return engine

    composition = compose(
        model,
        machine,
        recipe,
        engine_factory=factory,
        observe_cache=notices.append,
    )
    app = composition.application
    try:
        app.start().result(WAIT)
        engine = engines[0]
        until(engine.active.is_set)
        stopped = app.stop()
        engine.release_active.set()
        stopped.result(WAIT)
        assert notices[-1].state is CacheState.FALLBACK and notices[-1].reason
        assert app.replay_cache.snapshot().decoded_bytes == 0
        assert app.snapshot().counters.cycles_completed == 1 and len(engine.calls) == 8
    finally:
        for engine in engines:
            engine.release_active.set()
        app.close().result(WAIT)
        assert app.join(WAIT)


def test_cached_vs_on_demand_scalar_production_equivalence(tmp_path: Path) -> None:
    from tests.application_support import prepared

    config = prepared(tmp_path).configuration
    sources = folders(tmp_path)
    cached = FolderReplayCache().replay(config.layout, sources, tmp_path)
    ordinary = FolderReplay(config.layout, sources, tmp_path)
    pipeline = ConfiguredProductionPipeline(config)
    for index in range(1, 5):
        results: list[ProductionInspectionResult] = []
        for replay in (ordinary, cached):
            group = replay.next_group()
            assert group is not None
            collector = CycleCollector(
                config.layout.camera_ids, timeout=10, clock=lambda: 0
            )
            collector.begin(CycleId(index))
            complete = None
            for camera, image in zip(config.layout.camera_ids, group, strict=True):
                complete = collector.accept(
                    Frame(FrameId(CycleId(index), camera, 0), 0, image.copy())
                )
            assert complete is not None

            def infer(image: NDArray[np.uint8]) -> ImageResult:
                return (
                    inferred(BoundingRect(1, 1, 3, 4))
                    if int(image[0, 0, 0]) % 2
                    else inferred()
                )

            _, result = pipeline.inspect(complete, infer)
            results.append(result)
        assert results[0].assessment == results[1].assessment
        assert results[0].channels == results[1].channels
        assert results[0].unassigned_findings == results[1].unassigned_findings


def test_disk_edit_is_frozen_until_restart_then_reloaded(
    qapp: QApplication, tmp_path: Path
) -> None:
    model, machine, recipe, _ = input_files(tmp_path)
    sources = folders(tmp_path)
    save_camera_sources(tmp_path / "camera_sources.json", sources)
    engines: list[InputEngine] = []

    def factory(_config: object) -> InputEngine:
        engine = InputEngine()
        engines.append(engine)
        return engine

    composition = compose(model, machine, recipe, engine_factory=factory)
    app = composition.application
    try:
        app.start().result(WAIT)
        until(engines[0].active.is_set)
        identity = app.replay_cache.snapshot().dataset_id
        write_image(
            sources.camera(CameraId("CAM1")).path(tmp_path) / "unrelated1.png", 99
        )
        stopped = app.stop()
        engines[0].release_active.set()
        stopped.result(WAIT)
        assert (
            engines[0].calls[2][0] == 1
            and app.replay_cache.snapshot().dataset_id == identity
        )
        app.start().result(WAIT)
        until(engines[1].active.is_set)
        stopped = app.stop()
        engines[1].release_active.set()
        stopped.result(WAIT)
        assert (
            engines[1].calls[2][0] == 99
            and app.replay_cache.snapshot().dataset_id != identity
        )
    finally:
        for engine in engines:
            engine.release_active.set()
        app.close().result(WAIT)
        assert app.join(WAIT)


def test_close_retains_cache_until_actual_active_native_completion(
    qapp: QApplication, tmp_path: Path
) -> None:
    model, machine, recipe, _ = input_files(tmp_path)
    save_camera_sources(tmp_path / "camera_sources.json", folders(tmp_path))
    engines: list[InputEngine] = []

    def factory(_config: object) -> InputEngine:
        engine = InputEngine()
        engines.append(engine)
        return engine

    composition = compose(model, machine, recipe, engine_factory=factory)
    app = composition.application
    try:
        app.start().result(WAIT)
        until(engines[0].active.is_set)
        closing = app.close()
        assert not closing.done() and not app.join(0)
        assert (
            app.replay_cache.snapshot().decoded_bytes == 54000 and not engines[0].closed
        )
        engines[0].release_active.set()
        closing.result(WAIT)
        assert app.join(WAIT)
        assert (
            app.replay_cache.snapshot().state is CacheState.RELEASED
            and engines[0].closed
        )
    finally:
        for engine in engines:
            engine.release_active.set()
        app.close().result(WAIT)
        assert app.join(WAIT)


@pytest.mark.parametrize("mode", ["fixed", "folder", "cached"])
def test_local_source_modes_share_pacing_after_first_cycle(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    from vimo_demo.ui import local_production

    before = {t.ident for t in threads()}
    entered = Event()
    waits: list[tuple[float, float]] = []

    def pacing(stop: Event, started: float, fps: float) -> bool:
        waits.append((started, fps))
        entered.set()
        return stop.wait(WAIT)

    monkeypatch.setattr(local_production, "_wait_for_source_interval", pacing)
    model, machine, recipe, paths = input_files(tmp_path)
    if mode != "fixed":
        sources = replace(folders(tmp_path, loop=mode == "cached"), target_fps=4)
        save_camera_sources(tmp_path / "camera_sources.json", sources)
    engines: list[InputEngine] = []
    source_futures: list[Future[None]] = []

    def factory(_config: object) -> InputEngine:
        engine = InputEngine()
        engines.append(engine)
        return engine

    composition = compose(
        model,
        machine,
        recipe,
        paths if mode == "fixed" else (),
        engine_factory=factory,
        observe_source=source_futures.append,
    )
    app = composition.application
    try:
        app.start().result(WAIT)
        engine = engines[0]
        until(engine.active.is_set)
        # First production input is claimed before any pacing call.
        assert waits == [] and app.snapshot().counters.cycles_started == 1
        engine.release_active.set()
        assert entered.wait(WAIT)
        assert len(waits) == 1 and waits[0][1] == (8 if mode == "fixed" else 4)
        assert app.snapshot().counters.cycles_completed == 1
        assert [v for v, _ in engine.calls][2:] == (
            list(range(1, 7)) if mode == "fixed" else [1, 11, 21, 31, 41, 51]
        )
        assert len(composition.store.snapshot().frames) == 6
        app.stop().result(WAIT)
        assert app.snapshot().state is ApplicationState.READY and engine.closed
        assert source_futures[0].done() and source_futures[0].exception() is None
        assert app.snapshot().counters.cycles_completed == 1
    finally:
        for engine in engines:
            engine.release_active.set()
        app.close().result(WAIT)
        assert app.join(WAIT)
    assert not {t.ident for t in threads()} - before
