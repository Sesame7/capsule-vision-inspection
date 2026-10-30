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
from tests.support.application import WAIT, ControlledEngine, ReplayEngine
from tests.support.qt import until
from tests.support.reporting import readback, seed, server, settings
from tests.support.source_inputs import input_files, replay_files

from capsule_vision.application import (
    ApplicationState,
    ApplicationStateError,
    FrameSetIngress,
    ProductionCounters,
    production_session,
)
from capsule_vision.application import composition as local_module
from capsule_vision.application.composition import compose
from capsule_vision.application.image_archive import ImageArchiveCommands
from capsule_vision.application.source_control import SourceControlState
from capsule_vision.communication.reporting import ReportingCapacityError, ReportStage
from capsule_vision.configuration import (
    ImageArchiveSettings,
    load_recipe,
    save_recipe,
)
from capsule_vision.display import matching_camera
from capsule_vision.inference import ImageResult
from capsule_vision.inspection import CycleId
from capsule_vision.inspection.production import TechnicalStatus
from capsule_vision.localization.catalog import text
from capsule_vision.runtime import CoordinatorCapacityError
from capsule_vision.ui.future_delivery import FutureDelivery
from capsule_vision.ui.main_window import MainWindow
from capsule_vision.ui.production_bridge import QtProductionBridge
from capsule_vision.ui.source_notifications import SourceNotifications


def test_folder_replay_never_creates_production_archive(
    qapp: QApplication, tmp_path: Path
) -> None:
    model, machine, recipe, paths = input_files(tmp_path)
    root = tmp_path / "must-not-exist"
    save_recipe(
        recipe,
        replace(
            load_recipe(recipe),
            image_archive=ImageArchiveSettings(str(root), True, True, True, True),
        ),
    )
    engine = ControlledEngine()
    engine.release_warm.set()
    engine.release_active.set()
    composition = compose(
        model, machine, recipe, paths, engine_factory=lambda _: engine
    )
    app = composition.application
    assert isinstance(app, ImageArchiveCommands)
    try:
        app.start().result(WAIT)
        until(lambda: app.snapshot().counters.cycles_completed >= 1)
        app.stop().result(WAIT)
        assert app.archive_snapshot() is None and not root.exists()
    finally:
        app.close().result(WAIT)
        assert app.join(WAIT)


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
    window = MainWindow(
        bridge,
    )
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
        window.buttons["stop"].click()
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
        window.buttons["stop"].click()
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
    window = MainWindow(
        bridge,
    )
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

    def reject(self: FrameSetIngress, images: object) -> None:
        nonlocal calls
        calls += 1
        raise CoordinatorCapacityError("test capacity")

    monkeypatch.setattr(FrameSetIngress, "submit", reject)
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


@pytest.mark.parametrize("phase", ["starting", "stepping", "paused"])
def test_replay_nonblocking_close_at_each_phase(
    qapp: QApplication, tmp_path: Path, phase: str
) -> None:
    original_threads = {t.ident for t in threads()}
    engine = ReplayEngine()
    engine.hold_call = {"starting": 1, "stepping": 3, "paused": None}[phase]
    composition = compose(
        *input_files(tmp_path), engine_factory=lambda _: engine, replay_controls=True
    )
    app = composition.application
    bridge = QtProductionBridge(app, composition.store, poll_ms=5)
    window = MainWindow(
        bridge,
    )
    window.show()
    try:
        window.bridge.step_source()  # Existing single-step session setup.
        if phase == "paused":
            until(lambda: replay_state(composition) is SourceControlState.PAUSED)
        else:
            until(engine.entered.is_set)
        bridge.refresh()
        if phase == "starting":
            assert not window.buttons["operation"].isEnabled()
            assert not window.buttons["pause"].isEnabled()
            window.buttons["pause"].click()  # Disabled during startup.
            assert app.snapshot().counters == ProductionCounters()
        if phase == "stepping":
            assert window.state_label.text() != text("state_replay_paused", "zh")
        window.close()
        qapp.processEvents()
        if phase != "paused":
            assert window.isVisible() and not bridge.closed and not engine.closed
        engine.release.set()
        until(lambda: bridge.closed)
        assert app.join(0) and engine.closed
        assert app.snapshot().counters.cycles_completed == (
            0 if phase == "starting" else 1
        )
    finally:
        engine.release.set()
        window.close()
        until(lambda: bridge.closed)
        window.deleteLater()
    assert not {t.ident for t in threads()} - original_threads


def replay_state(
    application: local_module.EngineeringComposition,
) -> SourceControlState | None:
    snapshot = application.application.control_snapshot()
    return snapshot.state if snapshot else None


def finish_local(composition: local_module.EngineeringComposition) -> None:
    composition.application.close().result(WAIT)
    assert composition.application.join(WAIT)


def test_ready_step_warms_up_then_one_cycle_and_rapid_paused_steps_do_not_queue(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = ReplayEngine()
    engine.hold_call = 1
    composition = compose(
        *input_files(tmp_path), engine_factory=lambda _: engine, replay_controls=True
    )
    app = composition.application

    def no_step_pacing(stop: Event, started: float, fps: float) -> bool:
        raise AssertionError("Operator Step must not wait for automatic FPS pacing")

    monkeypatch.setattr(production_session, "_wait_for_source_interval", no_step_pacing)
    try:
        first = app.step_source()
        assert engine.entered.wait(WAIT)
        assert not first.done() and app.snapshot().state is ApplicationState.STARTING
        assert app.snapshot().counters == ProductionCounters()
        with pytest.raises(ApplicationStateError):
            app.step_source()  # Startup cannot acquire an extra permit.
        engine.release.set()
        first.result(WAIT)
        assert len(engine.calls) == 8  # Two warm-up calls plus exactly six frames.
        assert app.snapshot().state is ApplicationState.RUNNING
        assert replay_state(composition) is SourceControlState.PAUSED
        assert app.snapshot().counters.cycles_completed == 1
        engine.hold_call = 9
        engine.entered.clear()
        engine.release.clear()
        second = app.step_source()
        assert engine.entered.wait(WAIT)
        assert all(app.step_source() is second for _ in range(20))
        assert (
            not second.done()
            and replay_state(composition) is SourceControlState.STEPPING
        )
        engine.release.set()
        second.result(WAIT)
        assert app.snapshot().counters.cycles_completed == 2
        assert len(engine.calls) == 14
        assert replay_state(composition) is SourceControlState.PAUSED
    finally:
        engine.release.set()
        finish_local(composition)
    assert engine.closed


def test_folder_steps_preserve_independent_cursor_and_stop_start_rewinds(
    qapp: QApplication, tmp_path: Path
) -> None:
    engines: list[ReplayEngine] = []

    def factory(_prepared: object) -> ReplayEngine:
        engine = ReplayEngine()
        engines.append(engine)
        return engine

    composition = compose(
        *replay_files(tmp_path), engine_factory=factory, replay_controls=True
    )
    app = composition.application
    try:
        for cycle in range(1, 5):
            app.step_source().result(WAIT)
            expected = [
                camera * 10 + (cycle - 1) % length
                for camera, length in enumerate((1, 2, 3, 4, 2, 3), 1)
            ]
            assert engines[0].calls[2 + (cycle - 1) * 6 : 2 + cycle * 6] == expected
            latest = app.snapshot().latest_production_result
            assert latest is not None and latest.cycle_id == cycle
            assert [row.camera_id for row in latest.cameras] == [
                f"CAM{i}" for i in range(1, 7)
            ]
            assert replay_state(composition) is SourceControlState.PAUSED
        app.stop().result(WAIT)  # Backend Stop remains available while locally paused.
        assert engines[0].closed
        app.step_source().result(WAIT)
        assert app.snapshot().generation == 2
        latest = app.snapshot().latest_production_result
        assert latest is not None and latest.cycle_id == 1
        assert engines[1].calls[2:] == [10, 20, 30, 40, 50, 60]
        assert (
            app.snapshot().counters.cycles_completed == 5
        )  # Stop never resets counts.
    finally:
        finish_local(composition)


@pytest.mark.parametrize("single_step", [False, True])
def test_start_preserves_camera_source_startup_feedback(
    qapp: QApplication, tmp_path: Path, single_step: bool
) -> None:
    engine = ReplayEngine()
    composition = compose(
        *replay_files(tmp_path), engine_factory=lambda _: engine, replay_controls=True
    )
    (tmp_path / "CAM1" / "0.png").write_bytes(b"corrupt test-only image")
    app = composition.application
    bridge = QtProductionBridge(app, composition.store, poll_ms=5)
    window = MainWindow(
        bridge,
    )
    window.show()
    failures: list[str] = []

    def failed(command: str, _error: object) -> None:
        failures.append(command)

    bridge.command_failed.connect(failed)
    try:
        if single_step:
            bridge.step_source()
        else:
            window.buttons["operation"].click()
        until(lambda: "CAM1" in window.event_label.toolTip())
        assert ("source_step" if single_step else "start") in failures
        assert text("source_error", "zh") in window.event_label.toolTip()
        assert app.snapshot().state is ApplicationState.READY
        assert app.snapshot().active is None and not engine.calls
        assert app.snapshot().counters == ProductionCounters()
    finally:
        window.close()
        until(lambda: bridge.closed)
        assert app.join(0)
        window.deleteLater()


def test_nonloop_exhaustion_overrides_a_pause_requested_on_last_cycle(
    qapp: QApplication, tmp_path: Path
) -> None:
    engine = ReplayEngine()
    engine.hold_call = 9
    composition = compose(
        *replay_files(tmp_path, loop=False, first_camera_images=2),
        engine_factory=lambda _: engine,
        replay_controls=True,
    )
    app = composition.application
    try:
        app.start().result(WAIT)
        until(engine.entered.is_set)
        pause = app.pause_source()
        assert (
            not pause.done() and replay_state(composition) is SourceControlState.PAUSING
        )
        engine.release.set()
        pause.result(WAIT)
        until(lambda: app.snapshot().state is ApplicationState.READY)
        assert replay_state(composition) is SourceControlState.STOPPED
        assert app.snapshot().counters.cycles_completed == 2
        assert len(engine.calls) == 14 and engine.closed
    finally:
        engine.release.set()
        finish_local(composition)


@pytest.mark.parametrize("failure", ["warmup", "fatal"])
def test_step_startup_and_terminal_runtime_failure_remain_authoritative(
    qapp: QApplication, tmp_path: Path, failure: str
) -> None:
    engine = ReplayEngine(fail=failure)
    composition = compose(
        *input_files(tmp_path), engine_factory=lambda _: engine, replay_controls=True
    )
    app = composition.application
    try:
        stepped = app.step_source()
        until(lambda: not app.snapshot().runtime_owned and stepped.done())
        assert stepped.exception() is not None
        assert replay_state(composition) is SourceControlState.STOPPED
        assert app.snapshot().state is (
            ApplicationState.READY if failure == "warmup" else ApplicationState.FAULT
        )
        assert app.snapshot().counters.cycles_completed == 0 and engine.closed
    finally:
        finish_local(composition)


def test_step_completes_before_async_reporting_ack_and_pause_does_not_cancel_it(
    qapp: QApplication, tmp_path: Path
) -> None:
    with server() as peer:
        seed(peer, 99)
        peer.hold_next(ReportStage.BITMAP)
        engine = ReplayEngine()
        composition = compose(
            *input_files(tmp_path),
            engine_factory=lambda _: engine,
            reporting=settings(peer, timeout=2),
            replay_controls=True,
        )
        app = composition.application
        try:
            app.step_source().result(WAIT)
            assert peer.entered.wait(WAIT)
            assert replay_state(composition) is SourceControlState.PAUSED
            observed = app.reporting_snapshot()
            assert observed is not None and observed.acknowledged_count == 0
            app.pause_source().result(WAIT)
            peer.release.set()
            until(
                lambda: (
                    (s := app.reporting_snapshot()) is not None
                    and s.acknowledged_count == 1
                )
            )
            assert (
                readback(peer)[0] == 100
                and app.snapshot().state is ApplicationState.RUNNING
            )
            assert (
                len(engine.calls) == 8
                and replay_state(composition) is SourceControlState.PAUSED
            )
        finally:
            peer.release.set()
            finish_local(composition)


def test_reporting_failure_and_overload_do_not_become_local_pause_or_product_ng(
    qapp: QApplication, tmp_path: Path
) -> None:
    with server() as peer:
        peer.fail_next(ReportStage.BITMAP, "plc")
        engine = ReplayEngine()
        sources: list[Future[None]] = []
        composition = compose(
            *input_files(tmp_path),
            engine_factory=lambda _: engine,
            reporting=settings(peer, max_pending=1, timeout=2),
            replay_controls=True,
            observe_source=sources.append,
        )
        app = composition.application
        try:
            app.step_source().result(WAIT)
            until(
                lambda: (
                    (s := app.reporting_snapshot()) is not None and s.failed_count == 1
                )
            )
            assert app.snapshot().state is ApplicationState.RUNNING
            assert replay_state(composition) is SourceControlState.PAUSED
            peer.hold_next(ReportStage.BITMAP)
            app.step_source().result(WAIT)
            assert peer.entered.wait(WAIT)
            app.step_source().result(WAIT)  # One accepted pending report.
            overloaded = app.step_source()
            until(
                lambda: (
                    app.snapshot().state is ApplicationState.STOPPING
                    and app.snapshot().counters.cycles_completed == 4
                )
            )
            assert overloaded.done() and overloaded.exception() is not None
            assert replay_state(composition) is SourceControlState.STOPPED
            assert app.snapshot().counters.cycles_completed == 4
            assert app.snapshot().counters.business_ng == 0
            peer.release.set()
            until(lambda: app.snapshot().state is ApplicationState.READY)
            assert isinstance(sources[0].exception(), ReportingCapacityError)
            observed = app.reporting_snapshot()
            assert observed is not None and not observed.thread_alive
            assert (
                observed.failed_count
                == observed.rejected_count
                == observed.skipped_count
                == 1
            )
        finally:
            peer.release.set()
            finish_local(composition)
