"""Fixed local input -> existing application/runtime -> existing Qt presentation."""

from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path
from threading import Event, get_ident
from threading import enumerate as threads

import cv2
import numpy as np
import pytest
from numpy.typing import NDArray
from PySide6.QtWidgets import QApplication
from run_local_production_ui import SourceNotifications, compose
from tests.qt_support import until
from tests.unit.test_inspection_cycle import image_result
from tests.unit.test_production_application import WAIT, prepared

from vimo_demo.application import (
    ApplicationState,
    LocalCycleIngress,
    ProductionCounters,
)
from vimo_demo.configuration import (
    MachineLayoutDefinition,
    RecipeDefinition,
    save_machine_layout,
    save_model_definition,
    save_recipe,
)
from vimo_demo.inference import ImageResult
from vimo_demo.inference._validation import uint8_array
from vimo_demo.inspection import CycleId
from vimo_demo.inspection.production import TechnicalStatus
from vimo_demo.runtime import CoordinatorCapacityError
from vimo_demo.ui.inference_bridge import FutureDelivery
from vimo_demo.ui.production_bridge import QtProductionBridge
from vimo_demo.ui.production_presentation import matching_camera
from vimo_demo.ui.production_window import ProductionWindow


def input_files(folder: Path) -> tuple[Path, Path, Path, tuple[Path, ...]]:
    config = prepared(folder).configuration
    model, machine, recipe = (
        folder / name for name in ("model.json", "machine.json", "local.recipe.json")
    )
    save_model_definition(model, config.model)
    save_machine_layout(
        machine,
        MachineLayoutDefinition(config.layout),
    )
    save_recipe(recipe, RecipeDefinition("local", "Local test"))
    paths: list[Path] = []
    for index in range(6):
        path = folder / f"图像-{index}.png"
        ok, encoded = cv2.imencode(
            ".png", np.full((20, 60, 3), index + 1, dtype=np.uint8)
        )
        assert ok
        encoded.tofile(path)
        paths.append(path)
    return model, machine, recipe, tuple(paths)


class ControlledEngine:
    def __init__(self, *, fail_warmup: bool = False, fail_image: bool = False) -> None:
        self.warm = Event()
        self.release_warm = Event()
        self.active = Event()
        self.release_active = Event()
        self.calls: list[tuple[int, int]] = []
        self.closed = False
        self.fail_warmup, self.fail_image = fail_warmup, fail_image

    def infer(self, image_bgr: NDArray[np.generic]) -> ImageResult:
        self.calls.append((int(image_bgr[0, 0, 0]), get_ident()))
        if len(self.calls) == 1:
            self.warm.set()
            assert self.release_warm.wait(WAIT)
            if self.fail_warmup:
                raise ValueError("warmup failed")
        if len(self.calls) == 3:
            self.active.set()
            assert self.release_active.wait(WAIT)
            if self.fail_image:
                raise ValueError("single image failure")
        return image_result(uint8_array(image_bgr))

    def close(self) -> None:
        self.closed = True


def test_repeated_cycles_start_gate_order_display_and_restart(
    qapp: QApplication, tmp_path: Path
) -> None:
    engine = ControlledEngine()
    source_futures: list[Future[None]] = []
    original_threads = {t.ident for t in threads()}
    composition = compose(
        *input_files(tmp_path),
        engine_factory=lambda _: engine,
        observe_source=source_futures.append,
    )
    app = composition.application
    bridge = QtProductionBridge(app, composition.store, poll_ms=5)
    window = ProductionWindow(bridge, (composition.choice,))
    window.show()
    try:
        assert app.snapshot().state is ApplicationState.READY
        assert not engine.calls
        window.buttons["operation"].click()
        until(engine.warm.is_set)
        assert app.snapshot().state is ApplicationState.STARTING
        assert app.snapshot().counters == ProductionCounters()
        engine.release_warm.set()
        until(engine.active.is_set)
        assert app.snapshot().state is ApplicationState.RUNNING
        assert app.snapshot().counters.cycles_started == 1
        engine.release_active.set()
        until(lambda: app.snapshot().counters.cycles_completed >= 2)
        window.buttons["operation"].click()
        until(lambda: app.snapshot().state is ApplicationState.READY)
        snapshot = app.snapshot()
        result = snapshot.latest_production_result
        assert result is not None and result.cycle_id >= 2
        assert tuple(c.camera_id for c in result.cameras) == composition.store.cameras
        assert composition.store.cameras == tuple(f"CAM{i}" for i in range(1, 7))
        assert [v for v, _ in engine.calls][2:] == list(
            range(1, 7)
        ) * snapshot.counters.cycles_completed
        assert len({thread for _, thread in engine.calls}) == 1
        assert engine.calls[0][1] != get_ident() and engine.closed
        for frame in composition.store.snapshot().frames:
            assert matching_camera(frame, snapshot) is not None
            assert matching_camera(frame, replace(snapshot, generation=2)) is None
        assert source_futures[0].done() and source_futures[0].exception() is None
        calls = len(engine.calls)
        bridge.refresh()
        qapp.processEvents()
        assert len(engine.calls) == calls  # No background production after Stop.
        window.buttons["operation"].click()
        until(
            lambda: (
                app.snapshot().generation == 2
                and app.snapshot().latest_production_result is not None
            )
        )
        until(
            lambda: any(f.generation == 2 for f in composition.store.snapshot().frames)
        )
        window.buttons["operation"].click()
        until(lambda: app.snapshot().state is ApplicationState.READY)
        latest = app.snapshot().latest_production_result
        assert latest is not None and latest.cycle_id == CycleId(1)
        assert len(source_futures) == 2
    finally:
        engine.release_warm.set()
        engine.release_active.set()
        window.close()
        until(lambda: bridge.closed)
        assert app.join(0)
        window.deleteLater()
    assert not {t.ident for t in threads()} - original_threads


@pytest.mark.parametrize("close_window", [False, True])
def test_stop_close_wait_for_active_native_work(
    qapp: QApplication, tmp_path: Path, close_window: bool
) -> None:
    engine = ControlledEngine()
    engine.release_warm.set()
    sources: list[Future[None]] = []
    composition = compose(
        *input_files(tmp_path),
        engine_factory=lambda _: engine,
        observe_source=sources.append,
    )
    app = composition.application
    bridge = QtProductionBridge(app, composition.store, poll_ms=5)
    window = ProductionWindow(bridge, (composition.choice,))
    window.show()
    try:
        bridge.start()
        until(engine.active.is_set)
        window.close() if close_window else bridge.stop()
        assert app.snapshot().state is ApplicationState.STOPPING
        qapp.processEvents()
        assert not bridge.closed and not sources[0].done() and not engine.closed
        engine.release_active.set()
        until(
            lambda: (
                bridge.closed
                if close_window
                else app.snapshot().state is ApplicationState.READY
            )
        )
        assert sources[0].done() and sources[0].exception() is None
        assert engine.closed and len(engine.calls) == 8
    finally:
        engine.release_active.set()
        window.close()
        until(lambda: bridge.closed)
        assert app.join(0)
        window.deleteLater()


def test_failed_start_produces_no_cycles(qapp: QApplication, tmp_path: Path) -> None:
    engine = ControlledEngine(fail_warmup=True)
    engine.release_warm.set()
    composition = compose(*input_files(tmp_path), engine_factory=lambda _: engine)
    app = composition.application
    try:
        startup = app.start()
        until(startup.done)
        assert startup.exception() is not None
        assert app.snapshot().state is ApplicationState.READY
        assert app.snapshot().active is None
        assert app.snapshot().counters == ProductionCounters()
    finally:
        app.close().result(WAIT)
        assert app.join(WAIT)


def test_source_admission_error_is_explicit_no_retry(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def reject(self: LocalCycleIngress, images: object) -> None:
        nonlocal calls
        calls += 1
        raise CoordinatorCapacityError("test capacity")

    monkeypatch.setattr(LocalCycleIngress, "submit", reject)
    engine = ControlledEngine()
    engine.release_warm.set()
    delivery = FutureDelivery()
    composition = compose(
        *input_files(tmp_path),
        engine_factory=lambda _: engine,
        observe_source=lambda f: delivery.watch("source", f),
    )
    app = composition.application
    bridge = QtProductionBridge(app, composition.store, poll_ms=5)
    notifications = SourceNotifications(bridge, delivery)
    failures: list[object] = []

    def failed(_name: str, error: object) -> None:
        failures.append(error)

    bridge.command_failed.connect(failed)
    try:
        bridge.start()
        until(lambda: bool(failures) and app.snapshot().state is ApplicationState.READY)
        assert isinstance(failures[0], CoordinatorCapacityError)
        assert calls == 1 and len(engine.calls) == 2
        assert app.snapshot().counters == ProductionCounters()
        assert app.snapshot().latest_production_result is None
    finally:
        bridge.close()
        until(lambda: bridge.closed)
        delivery.detach()
        bridge.deleteLater()
        del notifications


def test_image_failure_keeps_technical_evidence(
    qapp: QApplication, tmp_path: Path
) -> None:
    engine = ControlledEngine(fail_image=True)
    engine.release_warm.set()
    engine.release_active.set()
    composition = compose(*input_files(tmp_path), engine_factory=lambda _: engine)
    app = composition.application
    try:
        app.start().result(WAIT)
        until(lambda: app.snapshot().counters.cycles_completed >= 1)
        app.stop().result(WAIT)
        result = app.snapshot().latest_production_result
        assert result is not None
        assert result.cameras[0].assessment.technical_status is TechnicalStatus.ERROR
        assert result.cameras[-1].assessment.technical_status is TechnicalStatus.SUCCESS
        assert app.snapshot().counters.production_ng_cycles == 0
    finally:
        app.close().result(WAIT)
        assert app.join(WAIT)


def test_terminal_runtime_failure_is_fault(qapp: QApplication, tmp_path: Path) -> None:
    class TerminalFailure(BaseException):
        pass

    class BrokenEngine(ControlledEngine):
        def infer(self, image_bgr: NDArray[np.generic]) -> ImageResult:
            result = super().infer(image_bgr)
            if len(self.calls) > 2:
                raise TerminalFailure("runtime terminated")
            return result

    engine = BrokenEngine()
    engine.release_warm.set()
    engine.release_active.set()
    sources: list[Future[None]] = []
    composition = compose(
        *input_files(tmp_path),
        engine_factory=lambda _: engine,
        observe_source=sources.append,
    )
    app = composition.application
    try:
        app.start().result(WAIT)
        until(
            lambda: (
                app.snapshot().state is ApplicationState.FAULT
                and not app.snapshot().runtime_owned
            )
        )
        assert sources[0].done() and sources[0].exception() is not None
        assert app.snapshot().latest_production_result is None
        assert app.snapshot().counters.production_ng_cycles == 0
        assert engine.closed and len(engine.calls) == 3
    finally:
        app.close().result(WAIT)
        assert app.join(WAIT)


def test_stop_during_warmup_never_admits_local_cycle(
    qapp: QApplication, tmp_path: Path
) -> None:
    engine = ControlledEngine()
    composition = compose(*input_files(tmp_path), engine_factory=lambda _: engine)
    app = composition.application
    try:
        startup = app.start()
        until(engine.warm.is_set)
        stopped = app.stop()
        assert not stopped.done()
        engine.release_warm.set()
        until(stopped.done)
        assert stopped.exception() is None and startup.exception() is not None
        assert app.snapshot().state is ApplicationState.READY
        assert app.snapshot().counters == ProductionCounters()
        assert engine.closed and len(engine.calls) == 1
    finally:
        engine.release_warm.set()
        app.close().result(WAIT)
        assert app.join(WAIT)


@pytest.mark.parametrize("invalid", ["corrupt", "missing", "few", "outside_roi"])
def test_bad_input_never_starts_runtime(tmp_path: Path, invalid: str) -> None:
    model, machine, recipe, paths = input_files(tmp_path)
    if invalid == "corrupt":
        paths[0].write_bytes(b"not an image")
    elif invalid == "missing":
        paths[0].unlink()
    elif invalid == "few":
        paths = paths[:5]
    else:
        ok, pixels = cv2.imencode(".png", np.zeros((1, 1, 3), dtype=np.uint8))
        assert ok
        pixels.tofile(paths[0])
    with pytest.raises((ValueError, OSError)):
        compose(model, machine, recipe, paths)
