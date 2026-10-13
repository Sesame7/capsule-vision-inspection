"""Opt-in saved local dataset preload/restart smoke; no benchmark or asset writes."""

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from threading import current_thread, get_ident
from threading import enumerate as threads
from time import perf_counter

import numpy as np
import pytest
from numpy.typing import NDArray
from PySide6.QtWidgets import QApplication
from tests.integration.test_reference import ROOT, unavailable
from tests.qt_support import until

from vimo_demo.application import (
    ApplicationError,
    ApplicationState,
    PreparedConfiguration,
    folder_replay,
)
from vimo_demo.application.folder_cache import CacheState, FolderCacheStatus
from vimo_demo.configuration import load_camera_sources
from vimo_demo.inference import (
    ImageResult,
    VimoInitializationError,
    VimoSegmentationEngine,
)
from vimo_demo.ui.local_production import compose


class RecordedEngine(VimoSegmentationEngine):
    def __init__(self, prepared: PreparedConfiguration) -> None:
        self.owner = get_ident()
        self.owner_name = current_thread().name
        self.scalars: list[
            tuple[str, dict[str, int], dict[str, int], tuple[object, ...]]
        ] = []
        self.timings: list[float] = []
        super().__init__(
            prepared.model_path, module_id=prepared.configuration.model.module_id
        )

    def infer(self, image_bgr: NDArray[np.generic]) -> ImageResult:
        assert get_ident() == self.owner and not image_bgr.flags.writeable
        started = perf_counter()
        result = super().infer(image_bgr)
        self.timings.append((perf_counter() - started) * 1000)
        self.scalars.append(
            (
                hashlib.sha256(image_bgr.tobytes()).hexdigest(),
                dict(result.label_map),
                result.regions_per_label,
                tuple((r.label, r.area, r.bounding_rect) for r in result.regions),
            )
        )
        return result

    def close(self) -> None:
        assert get_ident() == self.owner
        super().close()


@pytest.mark.gpu
def test_saved_loop_dataset_preload_and_restart(
    qapp: QApplication,
    pytestconfig: pytest.Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = ROOT / "data/models/model.json"
    machine = ROOT / "data/machine/machine.json"
    recipe = ROOT / "data/recipes/local.recipe.json"
    source = ROOT / "data/sources/camera_sources.json"
    if not all(p.is_file() for p in (model, machine, recipe, source)):
        unavailable(
            pytestconfig,
            "Saved local Production model/machine/Recipe/sources unavailable",
        )
    saved = load_camera_sources(source)
    if not saved.loop:
        unavailable(
            pytestconfig,
            "Saved source Loop is off; do not change it for GPU validation",
        )
    count = sum(
        len(folder_replay.folder_images(c.path(source.parent))) for c in saved.cameras
    )
    notices: list[FolderCacheStatus] = []
    engines: list[RecordedEngine] = []
    decode = folder_replay.decode_image
    decoded = 0

    def recorded_decode(path: Path):
        nonlocal decoded
        decoded += 1
        return decode(path)

    def engine_factory(prepared: PreparedConfiguration) -> RecordedEngine:
        assert notices[-1].state in (CacheState.READY, CacheState.HIT)
        engine = RecordedEngine(prepared)
        engines.append(engine)
        return engine

    monkeypatch.setattr(folder_replay, "decode_image", recorded_decode)
    before = {t.ident for t in threads()}
    composition = compose(
        model,
        machine,
        recipe,
        sources_file=source,
        engine_factory=engine_factory,
        observe_cache=notices.append,
    )
    app = composition.application
    cycle_counts: list[int] = []
    starts: list[float] = []
    try:
        for generation in (1, 2):
            baseline = app.snapshot().counters.cycles_completed
            started = perf_counter()
            try:
                app.start().result(120)
            except ApplicationError as exc:
                cause = exc.__cause__
                if isinstance(cause, VimoInitializationError):
                    vendor = cause.__cause__
                    if isinstance(vendor, (ImportError, OSError)) or (
                        type(vendor).__name__ == "VimoException"
                        and getattr(vendor, "code", None) == 40000
                    ):
                        unavailable(
                            pytestconfig, f"ViMo runtime/license unavailable:{cause}"
                        )
                raise
            starts.append(perf_counter() - started)
            assert app.snapshot().state is ApplicationState.RUNNING
            assert app.snapshot().generation == generation
            until(
                lambda target=baseline + 3: (
                    app.snapshot().counters.cycles_completed >= target
                ),
                120_000,
            )
            app.stop().result(120)
            assert (
                app.snapshot().state is ApplicationState.READY
                and not app.snapshot().runtime_owned
            )
            assert (
                decoded == count
            )  # Second Start and every cached production cycle decode nothing.
            engine = engines[-1]
            completed = app.snapshot().counters.cycles_completed - baseline
            cycle_counts.append(completed)
            assert len(engine.scalars) == 2 + completed * 6 and engine.closed
            assert engine.owner != get_ident()
            result = app.snapshot().latest_production_result
            assert result is not None
            assert tuple(c.camera_id for c in result.cameras) == tuple(
                f"CAM{i}" for i in range(1, 7)
            )
            assert all(
                c.frame_id
                and c.frame_id.cycle_id == completed
                and c.frame_id.sequence == 0
                for c in result.cameras
            )
            assert not app.snapshot().counters.technical_error_cycles
        assert (
            engines[0] is not engines[1]
            and engines[0].owner_name != engines[1].owner_name
        )
        common = min(len(e.scalars) for e in engines)
        assert engines[0].scalars[:common] == engines[1].scalars[:common]
        assert notices[-1].state is CacheState.HIT and notices[-1].image_count == count
        cold = next(s for s in notices if s.state is CacheState.READY)
        evidence = {
            "cache": asdict(cold) | {"state": cold.state.name},
            "decoded_calls": decoded,
            "start_seconds": starts,
            "production_cycles": cycle_counts,
            "exact_common_inference_signatures": common,
            "per_session_sdk_calls": [len(e.scalars) for e in engines],
            "inference_ms": [e.timings for e in engines],
            "owned_worker_names": [e.owner_name for e in engines],
            "model_sha256": composition.choice.prepared.configuration.model.expected_sha256,
            "performance_conclusion": "Bounded wiring observation only; not an FPS benchmark",
        }
    finally:
        app.close().result(120)
        assert app.join(120)
    assert app.replay_cache.snapshot().state is CacheState.RELEASED
    assert not {t.ident for t in threads()} - before
    evidence["cache_released_and_all_threads_terminated"] = True
    (tmp_path / "cache-smoke.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(evidence, ensure_ascii=True))
