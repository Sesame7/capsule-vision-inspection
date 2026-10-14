"""Opt-in short E1 wiring check using saved six-camera Folder sources, read-only."""

import json
from collections import Counter, deque
from concurrent.futures import Future
from dataclasses import asdict
from pathlib import Path
from threading import enumerate as threads
from threading import get_ident

import numpy as np
import pytest
from numpy.typing import NDArray
from PySide6.QtWidgets import QApplication
from tests.integration.test_reference import ROOT, unavailable
from tests.qt_support import until

from vimo_demo.application import ApplicationState, PreparedConfiguration
from vimo_demo.inference import (
    BoundingRect,
    ImageResult,
    VimoInitializationError,
    VimoSegmentationEngine,
)
from vimo_demo.inspection.production import (
    OccupancyState,
    ProductJudgment,
    TechnicalStatus,
    TraditionalSkipReason,
)
from vimo_demo.ui.local_production import compose


@pytest.mark.gpu
def test_real_saved_sources_occupancy_stop_close(
    qapp: QApplication, pytestconfig: pytest.Config, tmp_path: Path
) -> None:
    paths = (
        ROOT / "data/models/model.json",
        ROOT / "data/machine/machine.json",
        ROOT / "data/recipes/local.recipe.json",
        ROOT / "data/sources/camera_sources.json",
    )
    if not all(path.is_file() for path in paths):
        unavailable(pytestconfig, "Saved local six-camera configuration unavailable")
    before = {thread.ident for thread in threads()}
    trace: list[tuple[str, int]] = []
    sources: list[Future[None]] = []
    # Bounded scalar oracle: no arrays, raw ImageResults or contours retained.
    raw: deque[tuple[tuple[str, int, BoundingRect], ...]] = deque(maxlen=24)

    class RecordedEngine(VimoSegmentationEngine):
        def __init__(self, prepared: PreparedConfiguration) -> None:
            trace.append(("create", get_ident()))
            super().__init__(
                prepared.model_path, module_id=prepared.configuration.model.module_id
            )

        def infer(self, image_bgr: NDArray[np.generic]) -> ImageResult:
            trace.append(("infer", get_ident()))
            result = super().infer(image_bgr)
            raw.append(
                tuple((r.label, r.area, r.bounding_rect) for r in result.regions)
            )
            return result

        def close(self) -> None:
            trace.append(("close", get_ident()))
            super().close()

    composition = compose(
        paths[0],
        paths[1],
        paths[2],
        sources_file=paths[3],
        engine_factory=RecordedEngine,
        observe_source=sources.append,
        observe_cache=lambda _: None,
    )
    app = composition.application
    try:
        assert app.snapshot().state is ApplicationState.READY
        started = app.start()
        until(started.done, 120_000)
        error = started.exception()
        if error is not None:
            if isinstance(error.__cause__, VimoInitializationError):
                cause = error.__cause__.__cause__
                if isinstance(cause, (ImportError, OSError)) or (
                    type(cause).__name__ == "VimoException"
                    and getattr(cause, "code", None) == 40000
                ):
                    unavailable(
                        pytestconfig, f"ViMo runtime/license unavailable: {error}"
                    )
            raise error
        until(lambda: app.snapshot().counters.cycles_completed >= 2, 120_000)
        assert app.snapshot().state is ApplicationState.RUNNING
        stopped = app.stop()
        until(stopped.done, 120_000)
        stopped.result()
        snapshot = app.snapshot()
        assert snapshot.state is ApplicationState.READY
        assert snapshot.last_issue is None and not snapshot.runtime_owned
        assert snapshot.counters.technical_error_cycles == 0
        result = snapshot.latest_production_result
        assert result is not None and len(result.channels) == 36
        assert result.layout == composition.choice.prepared.configuration.layout
        assert tuple(camera.camera_id for camera in result.cameras) == tuple(
            f"CAM{i}" for i in range(1, 7)
        )
        assert result.assessment.technical_status is TechnicalStatus.SUCCESS
        assert sources and all(f.done() and f.exception() is None for f in sources)
        # Last six native outputs belong to the last fully completed serial cycle.
        assert len(raw) >= 6
        for camera, regions in zip(result.cameras, tuple(raw)[-6:], strict=True):
            findings = {
                f.region_index: (f.label, f.area, f.bounding_rect)
                for f in (
                    *camera.unassigned_findings,
                    *(a.finding for a in camera.assigned_findings),
                )
            }
            assert tuple(findings[i] for i in sorted(findings)) == regions
            assert (
                camera.frame_id is not None
                and camera.frame_id.cycle_id == result.cycle_id
            )
            for channel in camera.channels:
                assert (
                    channel.occupancy is not None
                    and channel.occupancy.error_code is None
                )
                if channel.vimo_findings:
                    assert channel.assessment.judgment is ProductJudgment.NG
                if channel.occupancy.state is OccupancyState.EMPTY:
                    assert (
                        channel.checks[0].assessment.technical_status
                        is TechnicalStatus.DISABLED
                    )
                    assert (
                        channel.checks[0].measurements
                        == channel.checks[0].reasons
                        == ()
                    )
                    assert channel.traditional_skip is (
                        TraditionalSkipReason.CONFIRMED_EMPTY
                        if composition.choice.prepared.configuration.capsule.enabled
                        else None
                    )
        assert any(
            camera.assigned_findings or camera.unassigned_findings
            for camera in result.cameras
        ), "Local smoke needs an actual ViMo NG example"
        occupancy_ms = sum(camera.timing.occupancy_ms or 0 for camera in result.cameras)
        record = {
            "completed_cycles": snapshot.counters.cycles_completed,
            "technical_error_cycles": snapshot.counters.technical_error_cycles,
            "occupancy_states": dict(
                Counter(
                    row.occupancy.state.name
                    for row in result.channels
                    if row.occupancy is not None
                )
            ),
            "occupancy_stage_sum_ms": occupancy_ms,
            "timing_scope": "One completed real cycle; descriptive, not a benchmark",
            "channels": [
                {
                    "camera": row.camera_id,
                    "channel": row.channel_id,
                    "occupancy": row.occupancy.state.name if row.occupancy else None,
                    "traditional_skip": row.traditional_skip.name
                    if row.traditional_skip
                    else None,
                    "technical": row.assessment.technical_status.name,
                    "judgment": row.assessment.judgment.name,
                    "vimo_findings": len(row.vimo_findings),
                }
                for row in result.channels
            ],
            "camera_timings": [asdict(camera.timing) for camera in result.cameras],
            "stop_state": snapshot.state.name,
        }
        print(
            f"E1 real Folder sources: {snapshot.counters.cycles_completed} completed cycles; 0 technical errors; Occupancy stage {occupancy_ms:.3f} ms; NG retained; Stop -> READY"
        )
    finally:
        closed = app.close()
        until(closed.done, 120_000)
        closed.result()
        assert app.join(0)
    assert app.snapshot().state is ApplicationState.CLOSED
    assert not {thread.ident for thread in threads()} - before
    assert [name for name, _ in trace].count("create") == 1
    assert [name for name, _ in trace].count("close") == 1
    assert len({ident for _, ident in trace}) == 1 and trace[0][1] != get_ident()
    record["close_state"] = "CLOSED"
    record["residual_threads"] = 0
    (tmp_path / "occupancy-smoke.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print("Close -> CLOSED; one serial engine owner; no residual threads")
