"""Independent source/inspection lifetimes using the real scheduler and fake SDK."""

import sys
from collections import deque
from collections.abc import Sequence
from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path
from threading import Condition, Event, Thread, get_ident
from threading import enumerate as threads
from time import perf_counter

import numpy as np
import pytest
from numpy.typing import NDArray
from PySide6.QtWidgets import QApplication
from tests.application_support import WAIT
from tests.bootstrap_support import saved_data
from tests.cycle_support import image_result
from tests.qt_support import until
from tests.reporting_support import seed, server

from vimo_demo.application import (
    ApplicationError,
    ApplicationState,
    ApplicationStateError,
    ProductionCounters,
    _basler_sdk,
)
from vimo_demo.application.basler import (
    AcquisitionMetadata,
    BaslerAcquisitionSource,
    CameraCleanup,
    CameraReadiness,
    CameraSettings,
)
from vimo_demo.application.basler_connection import (
    ConnectionState,
    SourceConnectionCommands,
)
from vimo_demo.application.basler_control import BaslerControlSnapshot
from vimo_demo.application.local_replay import ReplayState
from vimo_demo.communication.reporting import (
    LocalReportingSettings,
    ProductionReportingCommands,
    ReportingSelection,
    ReportStage,
)
from vimo_demo.configuration import (
    load_camera_sources,
    save_camera_sources,
)
from vimo_demo.inference import ImageResult
from vimo_demo.inference._validation import uint8_array
from vimo_demo.inspection import CameraId
from vimo_demo.ui import local_production
from vimo_demo.ui.bootstrap import create_window, load_startup
from vimo_demo.ui.inference_bridge import FutureDelivery
from vimo_demo.ui.production_bridge import QtProductionBridge
from vimo_demo.ui.production_style import Language, Theme, text

# Provisional field mapping in configured CAM1..CAM6 order; explicitly includes
# the CAM5/CAM6 order that differs from sorting serials. This is test data only.
SERIALS = ("25334173", "25334174", "25334178", "25334180", "25334188", "25334184")


class Engine:
    def __init__(self) -> None:
        self.warm, self.release_warm = Event(), Event()
        self.active, self.release_active = Event(), Event()
        self.release_active.set()
        self.calls: list[tuple[int, int]] = []
        self.closed = False
        self.fail_warmup = False
        self.created_on = get_ident()
        self.closed_on: int | None = None
        self.shapes: list[tuple[int, ...]] = []
        self.zeros: list[bool] = []

    def infer(self, image_bgr: NDArray[np.generic]) -> ImageResult:
        image = uint8_array(image_bgr)
        self.calls.append((int(image[0, 0, 0]), get_ident()))
        self.shapes.append(image.shape)
        self.zeros.append(not bool(np.any(image)))
        if len(self.calls) == 1:
            self.warm.set()
            assert self.release_warm.wait(WAIT)
            if self.fail_warmup:
                raise ValueError("Warm-up failed")
        if len(self.calls) == 3:
            self.active.set()
            assert self.release_active.wait(WAIT)
        assert not image.flags.writeable
        return image_result(image)

    def close(self) -> None:
        self.closed = True
        self.closed_on = get_ident()


def save_basler(path: Path) -> None:
    source = load_camera_sources(path)
    save_camera_sources(
        path,
        replace(
            source,
            mode="basler",
            cameras=tuple(
                replace(c, basler_serial=s)
                for c, s in zip(source.cameras, SERIALS, strict=True)
            ),
        ),
    )


def test_normal_saved_source_switch_updates_controls_only_after_save_apply(
    qapp: QApplication, tmp_path: Path
) -> None:
    data = saved_data(tmp_path)
    engine = Engine()
    startup = load_startup(data, engine_factory=lambda _: engine)
    assert isinstance(startup.application, SourceConnectionCommands)
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    window.show()
    try:
        assert window.bridge.replay_enabled and window.buttons["secondary"].isHidden()
        window.navigate(4)
        workspace = window.parameters.roi_workspace
        assert workspace is not None and workspace.sources is not None
        until(lambda: not window.parameters.busy)
        for i in range(1, 7):
            workspace.sources.set_serial(CameraId(f"CAM{i}"), SERIALS[i - 1])
        workspace.open_source_settings()
        assert workspace.source_dialog is not None
        workspace.source_dialog.mode.setCurrentIndex(1)
        workspace.source_dialog.accept()
        assert window.bridge.replay_enabled  # Draft is not the saved source selection.
        assert not window.buttons["operation"].isEnabled()
        workspace.save_and_apply()
        until(lambda: not window.parameters.busy)
        window.bridge.refresh()
        assert (
            not window.bridge.replay_enabled and window.buttons["secondary"].isHidden()
        )
        assert (
            not engine.calls and not startup.application.source_snapshot().thread_alive
        )
        workspace.open_source_settings()
        workspace.source_dialog.mode.setCurrentIndex(0)
        workspace.source_dialog.accept()
        workspace.save_and_apply()
        until(lambda: not window.parameters.busy)
        window.bridge.refresh()
        assert window.bridge.replay_enabled and window.buttons["secondary"].isHidden()
        assert (
            workspace.sources.saved.camera(CameraId("CAM5")).basler_serial == SERIALS[4]
        )
        assert (
            not engine.calls and not startup.application.source_snapshot().thread_alive
        )
        window.navigate(0)
        assert window.buttons["secondary"].isHidden()
    finally:
        window.close()
        until(lambda: window.bridge.closed)
        assert startup.application.join(0)
        delivery.detach()


def test_normal_folder_runtime_needs_no_pypylon(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "pypylon", None)

    def no_hardware(
        serials: Sequence[str], *, timeout: float = 30
    ) -> BaslerAcquisitionSource:
        raise AssertionError("Folder runtime must not construct a hardware source")

    monkeypatch.setattr(_basler_sdk, "PylonBackend", no_hardware)
    data = saved_data(tmp_path)
    engine = Engine()
    engine.release_warm.set()
    startup = load_startup(data, engine_factory=lambda _: engine)
    assert isinstance(startup.application, SourceConnectionCommands)
    original = data.sources.read_bytes()
    try:
        startup.application.start().result(WAIT)
        until(lambda: startup.application.snapshot().state is ApplicationState.READY)
        assert startup.application.snapshot().counters.cycles_completed > 0
        assert engine.closed and data.sources.read_bytes() == original
        assert "pypylon.pylon" not in sys.modules
    finally:
        startup.application.close().result(WAIT)
        assert startup.application.join(WAIT)


def test_developer_fixed_images_override_saved_basler_without_false_status(
    qapp: QApplication, tmp_path: Path
) -> None:
    from vimo_demo.application.folder_replay import folder_images
    from vimo_demo.ui.production_window import ProductionWindow

    data = saved_data(tmp_path)
    save_basler(data.sources)
    original = data.sources.read_bytes()
    engine = Engine()
    engine.release_active.clear()
    composition = local_production.compose(
        data.models / "general.json",
        data.machine,
        data.recipes / "local.recipe.json",
        folder_images(tmp_path / "frames")[:6],
        sources_file=data.sources,
        engine_factory=lambda _: engine,
        replay_controls=True,
    )
    bridge = QtProductionBridge(
        composition.application,
        composition.store,
        local_replay=composition.application,
        poll_ms=5,
    )
    window = ProductionWindow(
        bridge, (composition.choice,), recipe_editor=composition.editor
    )
    window.show()
    try:
        bridge.start()
        until(engine.warm.is_set)
        assert "Basler" not in window.event_label.accessibleName()
        engine.release_warm.set()
        until(engine.active.is_set)
        assert (
            bridge.replay_enabled
            and not composition.application.source_snapshot().thread_alive
        )
        assert "Basler" not in window.event_label.accessibleName()
        assert data.sources.read_bytes() == original
    finally:
        engine.release_warm.set()
        engine.release_active.set()
        window.close()
        until(lambda: bridge.closed)
        assert composition.application.join(0)


class PassiveBackend:
    """SDK FIFO fake; exercises the real source/control/session implementation."""

    def __init__(self) -> None:
        self.condition = Condition()
        self.queues = {
            s: deque[tuple[AcquisitionMetadata, NDArray[np.uint8]]]() for s in SERIALS
        }
        self.armed = Event()
        self.closed = False
        self.prepared: list[str] = []
        self.dimensions = {serial: (60, 20) for serial in SERIALS}
        self.fail_prepare = ""
        self.failure = self.unsafe = False
        self.owner_ids: list[int] = []
        self.cleanup_calls = 0
        self.cleanup_entered, self.release_cleanup = Event(), Event()
        self.release_cleanup.set()
        self.prepare_entered, self.release_prepare = Event(), Event()
        self.release_prepare.set()

    def inventory(self, serials: tuple[str, ...]) -> None:
        self.owner_ids.append(get_ident())
        assert serials == SERIALS

    def prepare(self, serial: str) -> CameraSettings:
        self.prepared.append(serial)
        self.owner_ids.append(get_ident())
        self.prepare_entered.set()
        assert self.release_prepare.wait(WAIT)
        if serial == self.fail_prepare:
            raise RuntimeError("Injected partial Open failure")
        width, height = self.dimensions[serial]
        return CameraSettings(serial, {"Width": width, "Height": height}, {})

    def arm(self) -> tuple[CameraReadiness, ...]:
        self.owner_ids.append(get_ident())
        self.armed.set()
        return tuple(CameraReadiness(s, True, True, perf_counter()) for s in SERIALS)

    def available(self, serial: str, timeout_ms: int) -> bool:
        if self.failure:
            raise RuntimeError("Injected source loss")
        with self.condition:
            if timeout_ms:
                self.condition.wait_for(
                    lambda: bool(self.queues[serial]), timeout_ms / 1000
                )
            return bool(self.queues[serial])

    def retrieve(self, serial: str) -> tuple[AcquisitionMetadata, NDArray[np.uint8]]:
        self.owner_ids.append(get_ident())
        with self.condition:
            return self.queues[serial].popleft()

    def expose(self, ordinal: int, cameras: tuple[int, ...] = tuple(range(6))) -> None:
        with self.condition:
            for index in cameras:
                width, height = self.dimensions[SERIALS[index]]
                pixels = np.full(
                    (height, width, 3), ordinal * 10 + index, dtype=np.uint8
                )
                pixels.setflags(write=False)
                self.queues[SERIALS[index]].append(
                    (
                        AcquisitionMetadata(
                            SERIALS[index],
                            perf_counter(),
                            True,
                            0,
                            "",
                            width,
                            height,
                            1,
                            3600,
                            ordinal - 1,
                            ordinal,
                            ordinal * 1000,
                            0,
                        ),
                        pixels,
                    )
                )
            self.condition.notify_all()

    def cleanup(self) -> tuple[CameraCleanup, ...]:
        self.owner_ids.append(get_ident())
        self.cleanup_calls += 1
        self.cleanup_entered.set()
        assert self.release_cleanup.wait(WAIT)
        self.closed = not self.unsafe
        return tuple(
            CameraCleanup(s, True, not self.unsafe, not self.unsafe, not self.unsafe)
            for s in self.prepared
        )


@pytest.fixture
def passive(monkeypatch: pytest.MonkeyPatch) -> PassiveBackend:
    backend = PassiveBackend()

    def sdk(*, continuous: bool = False) -> PassiveBackend:
        assert continuous
        return backend

    monkeypatch.setattr(_basler_sdk, "PylonBackend", sdk)
    return backend


def without_folders(path: Path) -> None:
    source = load_camera_sources(path)
    save_camera_sources(
        path,
        replace(source, cameras=tuple(replace(c, folder="") for c in source.cameras)),
    )


def test_synthetic_warmup_uses_actual_distinct_shapes_on_single_engine_owner(
    qapp: QApplication, tmp_path: Path, passive: PassiveBackend
) -> None:
    data = saved_data(tmp_path)
    save_basler(data.sources)
    without_folders(data.sources)
    passive.dimensions[SERIALS[4]] = 64, 24
    engines: list[Engine] = []

    def factory(_prepared: object) -> Engine:
        engine = Engine()
        engine.release_warm.set()
        engines.append(engine)
        return engine

    startup = load_startup(data, engine_factory=factory)
    app = startup.application
    assert isinstance(app, SourceConnectionCommands)
    try:
        app.connect_source()
        app.start().result(WAIT)
        engine = engines[0]
        assert engine.shapes == [(20, 60, 3)] * 2 + [(24, 64, 3)] * 2
        assert all(engine.zeros)
        assert (
            app.snapshot().counters == ProductionCounters()
            and not startup.frames.snapshot().frames
        )
        app.stop().result(WAIT)
        assert {
            engine.created_on,
            engine.closed_on,
            *[owner for _, owner in engine.calls],
        } == {engine.created_on}
        assert engine.created_on != get_ident() and not passive.closed
    finally:
        app.close().result(WAIT)
        assert app.join(WAIT)


def test_source_lifetime_idle_discard_restart_recipe_and_batch(
    qapp: QApplication, tmp_path: Path, passive: PassiveBackend
) -> None:
    data = saved_data(tmp_path)
    save_basler(data.sources)
    without_folders(data.sources)
    engines: list[Engine] = []

    def factory(_prepared: object) -> Engine:
        engine = Engine()
        engine.release_warm.set()
        engines.append(engine)
        return engine

    startup = load_startup(data, engine_factory=factory)
    app = startup.application
    assert isinstance(app, SourceConnectionCommands)
    original = data.sources.read_bytes(), data.machine.read_bytes()
    try:
        assert not startup.issues and not passive.prepared and not engines
        with pytest.raises(ApplicationStateError, match="Connect"):
            app.start()
        app.connect_source()
        connected = app.source_snapshot()
        assert connected.state is ConnectionState.CONNECTED and connected.thread_alive
        assert [
            (c.camera_id, c.serial, c.width, c.height) for c in connected.cameras
        ] == [(f"CAM{i + 1}", serial, 60, 20) for i, serial in enumerate(SERIALS)]
        with pytest.raises(ApplicationStateError, match="Disconnect"):
            app.connect_source()
        passive.expose(1)
        until(lambda: app.source_snapshot().discarded_frames == 6)
        assert (
            app.snapshot().counters == ProductionCounters()
            and not startup.frames.snapshot().frames
        )
        assert not engines
        for ordinal in (2, 4):
            app.start().result(WAIT)
            assert app.source_snapshot().state is ConnectionState.CONNECTED
            assert [value for value, _ in engines[-1].calls] == [0, 0]
            passive.expose(ordinal, (5, 2, 4, 0, 3, 1))
            until(
                lambda ordinal=ordinal: (
                    app.snapshot().counters.cycles_completed == ordinal // 2
                )
            )
            result = app.snapshot().latest_production_result
            assert (
                result is not None
                and result.cycle_id == ordinal
                and len(result.channels) == 36
            )
            assert [c.camera_id for c in result.cameras] == [
                f"CAM{i}" for i in range(1, 7)
            ]
            assert all(
                c.frame_id is not None and c.frame_id.sequence == ordinal
                for c in result.cameras
            )
            assert {value for value, _ in engines[-1].calls[2:]} == {
                ordinal * 10 + i for i in range(6)
            }
            with pytest.raises(ApplicationStateError):
                app.disconnect_source()
            app.stop().result(WAIT)
            assert engines[-1].closed and not passive.closed
            assert app.source_snapshot().inspection_generation is None
            if ordinal == 2:
                passive.expose(3)
                until(lambda: app.source_snapshot().discarded_frames == 12)
                assert app.snapshot().counters.cycles_completed == 1
                assert startup.choice is not None
                app.configure(
                    replace(
                        startup.choice.prepared,
                        configuration=replace(
                            startup.choice.prepared.configuration, recipe_id="next"
                        ),
                    )
                )
                assert app.source_snapshot().state is ConnectionState.CONNECTED
        app.start_batch("new-batch")
        assert app.snapshot().counters == ProductionCounters() and not passive.closed
        assert len(passive.prepared) == 6
        assert all(len({owner for _, owner in engine.calls}) == 1 for engine in engines)
        assert len(set(passive.owner_ids)) == 1 and passive.owner_ids[0] != get_ident()
        assert not set(passive.owner_ids) & {
            owner for engine in engines for _, owner in engine.calls
        }
        app.disconnect_source()
        assert passive.closed and not app.source_snapshot().thread_alive
        assert app.source_snapshot().state is ConnectionState.DISCONNECTED
        assert original == (data.sources.read_bytes(), data.machine.read_bytes())
    finally:
        for engine in engines:
            engine.release_warm.set()
            engine.release_active.set()
        app.close().result(WAIT)
        assert app.join(WAIT)
        assert not any(t.name == "BaslerSourceOwner" for t in threads())


@pytest.mark.parametrize("problem", ["partial_open", "dimensions", "sdk"])
def test_failed_connect_is_explicit_and_never_creates_an_engine(
    qapp: QApplication,
    tmp_path: Path,
    passive: PassiveBackend,
    problem: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = saved_data(tmp_path)
    save_basler(data.sources)
    if problem == "partial_open":
        passive.fail_prepare = SERIALS[2]
    elif problem == "dimensions":
        passive.dimensions[SERIALS[1]] = 0, 20
    else:
        monkeypatch.setitem(sys.modules, "pypylon", None)

        def unavailable(*, continuous: bool = False) -> PassiveBackend:
            raise ImportError("pypylon unavailable")

        monkeypatch.setattr(_basler_sdk, "PylonBackend", unavailable)
    engine = Engine()
    startup = load_startup(data, engine_factory=lambda _: engine)
    assert isinstance(startup.application, SourceConnectionCommands)
    app = startup.application
    assert isinstance(app, SourceConnectionCommands)
    try:
        with pytest.raises((RuntimeError, ValueError, ImportError)):
            app.connect_source()
        state = app.source_snapshot()
        assert (
            state.state is ConnectionState.FAULT
            and not state.thread_alive
            and state.cleanup_safe
        )
        assert state.error and not engine.calls
        with pytest.raises(ApplicationStateError):
            app.start()
        app.disconnect_source()
        assert app.source_snapshot().state is ConnectionState.DISCONNECTED
    finally:
        app.close().result(WAIT)
        assert app.join(WAIT)


def test_failed_cleanup_keeps_original_sdk_owner_until_explicit_retry(
    qapp: QApplication, tmp_path: Path, passive: PassiveBackend
) -> None:
    data = saved_data(tmp_path)
    save_basler(data.sources)
    startup = load_startup(data)
    assert isinstance(startup.application, SourceConnectionCommands)
    app = startup.application
    assert isinstance(app, SourceConnectionCommands)
    try:
        app.connect_source()
        passive.unsafe = True
        with pytest.raises(ApplicationStateError, match="cleanup"):
            app.disconnect_source()
        state = app.source_snapshot()
        assert (
            state.state is ConnectionState.FAULT
            and state.thread_alive
            and not state.cleanup_safe
        )
        attempts = passive.cleanup_calls
        with pytest.raises(ApplicationStateError):
            app.connect_source()
        assert passive.cleanup_calls == attempts  # No automatic reconnect/cleanup loop.
        with pytest.raises(ApplicationError, match="Close failed"):
            app.close().result(WAIT)
        assert app.snapshot().state is ApplicationState.FAULT and not app.join(0)
        passive.unsafe = False
        app.close().result(WAIT)
        assert app.join(WAIT) and passive.closed
        assert len(set(passive.owner_ids)) == 1
    finally:
        passive.unsafe = False
        app.close().result(WAIT)
        assert app.join(WAIT)


@pytest.mark.parametrize("problem", ["roi", "warmup", "loss_during_warmup"])
def test_start_failure_retains_connected_source_and_last_active_configuration(
    qapp: QApplication, tmp_path: Path, passive: PassiveBackend, problem: str
) -> None:
    data = saved_data(tmp_path)
    save_basler(data.sources)
    without_folders(data.sources)
    if problem == "roi":
        passive.dimensions[SERIALS[3]] = 57, 20
    engine = Engine()
    engine.fail_warmup = problem == "warmup"
    startup = load_startup(data, engine_factory=lambda _: engine)
    assert isinstance(startup.application, SourceConnectionCommands)
    app = startup.application
    assert isinstance(app, SourceConnectionCommands)
    try:
        app.connect_source()
        started = app.start()
        if problem != "roi":
            assert engine.warm.wait(WAIT)
            if problem == "loss_during_warmup":
                passive.failure = True
                until(lambda: app.source_snapshot().state is ConnectionState.FAULT)
            engine.release_warm.set()
        with pytest.raises(ApplicationError):
            started.result(WAIT)
        assert (
            app.snapshot().state is ApplicationState.READY
            and app.snapshot().active is None
        )
        assert app.snapshot().counters == ProductionCounters()
        if problem != "loss_during_warmup":
            assert (
                app.source_snapshot().state is ConnectionState.CONNECTED
                and not passive.closed
            )
        if problem == "roi":
            assert not engine.calls
        else:
            assert engine.closed
    finally:
        engine.release_warm.set()
        app.close().result(WAIT)
        assert app.join(WAIT)


def test_source_loss_during_inspection_stops_session_without_reconnect(
    qapp: QApplication, tmp_path: Path, passive: PassiveBackend
) -> None:
    data = saved_data(tmp_path)
    save_basler(data.sources)
    engine = Engine()
    engine.release_warm.set()
    startup = load_startup(data, engine_factory=lambda _: engine)
    assert isinstance(startup.application, SourceConnectionCommands)
    app = startup.application
    assert isinstance(app, SourceConnectionCommands)
    try:
        app.connect_source()
        app.start().result(WAIT)
        passive.failure = True
        until(lambda: app.snapshot().state is ApplicationState.READY)
        state = app.source_snapshot()
        assert state.state is ConnectionState.FAULT and state.error
        assert engine.closed and app.snapshot().counters == ProductionCounters()
        assert len(passive.prepared) == 6
    finally:
        app.close().result(WAIT)
        assert app.join(WAIT)


def test_stop_during_native_warmup_keeps_source_and_discards_arrivals(
    qapp: QApplication, tmp_path: Path, passive: PassiveBackend
) -> None:
    data = saved_data(tmp_path)
    save_basler(data.sources)
    engine = Engine()
    startup = load_startup(data, engine_factory=lambda _: engine)
    assert isinstance(startup.application, SourceConnectionCommands)
    app = startup.application
    assert isinstance(app, SourceConnectionCommands)
    try:
        app.connect_source()
        started = app.start()
        assert engine.warm.wait(WAIT)
        passive.expose(1)
        until(lambda: app.source_snapshot().discarded_frames == 6)
        stopped = app.stop()
        assert not stopped.done() and not engine.closed and not passive.closed
        engine.release_warm.set()
        stopped.result(WAIT)
        with pytest.raises(ApplicationError):
            started.result(WAIT)
        assert app.snapshot().counters == ProductionCounters()
        assert (
            app.source_snapshot().state is ConnectionState.CONNECTED and engine.closed
        )
    finally:
        engine.release_warm.set()
        app.close().result(WAIT)
        assert app.join(WAIT)


def test_close_during_connect_cancels_before_arm_and_joins_actual_owner(
    qapp: QApplication, tmp_path: Path, passive: PassiveBackend
) -> None:
    data = saved_data(tmp_path)
    save_basler(data.sources)
    passive.release_prepare.clear()
    startup = load_startup(data)
    app = startup.application
    assert isinstance(app, SourceConnectionCommands)
    connected: Future[None] = Future()

    def connect() -> None:
        try:
            app.connect_source()
            connected.set_result(None)
        except BaseException as exc:
            connected.set_exception(exc)

    caller = Thread(target=connect, name="TestConnectCaller")
    caller.start()
    try:
        assert passive.prepare_entered.wait(WAIT)
        assert app.source_snapshot().state is ConnectionState.CONNECTING
        with pytest.raises(ApplicationStateError):
            app.start()
        closed = app.close()
        assert not closed.done() and not app.join(0)
        passive.release_prepare.set()
        closed.result(WAIT)
        assert app.join(WAIT)
        caller.join(WAIT)
        assert not caller.is_alive() and not passive.armed.is_set()
        with pytest.raises(ApplicationStateError, match="cancelled"):
            connected.result(WAIT)
        assert not app.source_snapshot().thread_alive and passive.closed
    finally:
        passive.release_prepare.set()
        app.close().result(WAIT)
        assert app.join(WAIT)
        caller.join(WAIT)


def test_disconnect_marks_unready_before_waiting_for_native_cleanup(
    qapp: QApplication, tmp_path: Path, passive: PassiveBackend
) -> None:
    data = saved_data(tmp_path)
    save_basler(data.sources)
    startup = load_startup(data)
    app = startup.application
    assert isinstance(app, SourceConnectionCommands)
    app.connect_source()
    passive.release_cleanup.clear()
    caller = Thread(target=app.disconnect_source, name="TestDisconnectCaller")
    caller.start()
    try:
        assert passive.cleanup_entered.wait(WAIT)
        assert app.source_snapshot().state is ConnectionState.DISCONNECTING
        with pytest.raises(ApplicationStateError):
            app.start()
        assert app.source_snapshot().thread_alive
        passive.release_cleanup.set()
        caller.join(WAIT)
        assert (
            not caller.is_alive()
            and app.source_snapshot().state is ConnectionState.DISCONNECTED
        )
    finally:
        passive.release_cleanup.set()
        caller.join(WAIT)
        app.close().result(WAIT)
        assert app.join(WAIT)


def test_vision_connect_locks_sources_but_allows_reference_free_roi_save(
    qapp: QApplication, tmp_path: Path, passive: PassiveBackend
) -> None:
    data = saved_data(tmp_path)
    save_basler(data.sources)
    without_folders(data.sources)
    startup = load_startup(data)
    assert isinstance(startup.application, SourceConnectionCommands)
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    window.show()
    try:
        window.navigate(4)
        workspace = window.parameters.roi_workspace
        assert workspace is not None and workspace.sources is not None
        until(lambda: not window.parameters.busy)
        assert workspace.frame is None and workspace.editable()
        workspace.coordinates["X"].setText("1")
        workspace.apply_geometry()
        assert workspace.draft.dirty
        workspace.save_and_apply()
        until(lambda: not window.parameters.busy)
        assert not workspace.draft.dirty and not workspace.message.text()
        workspace.connect_source.click()
        until(lambda: not window.parameters.busy)
        assert startup.application.source_snapshot().state is ConnectionState.CONNECTED
        assert all(
            not card.serial.isEnabled() for card in workspace.camera_cards.values()
        )
        assert not workspace.source_settings.isEnabled() and workspace.editable()
        presentations: tuple[tuple[Language, Theme, tuple[int, int]], ...] = (
            ("zh", "dark", (1080, 1920)),
            ("zh", "light", (1080, 1920)),
            ("en", "dark", (1920, 1080)),
        )
        for language, theme, size in presentations:
            window.set_language(language)
            window.set_theme(theme)
            window.resize(*size)
            QApplication.processEvents()
            assert workspace.reference.currentText() == text("roi_no_frame", language)
            assert (
                workspace.connect_source.isVisible()
                and workspace.disconnect_source.isEnabled()
            )
            assert not workspace.connect_source.isEnabled()
            assert window.grab().save(
                str(tmp_path / f"connected-{language}-{theme}-{size[0]}.png")
            )
        saved = workspace.sources.definition
        workspace.set_source_mode("folder")
        workspace.serial_edited(CameraId("CAM1"), "999")
        assert workspace.sources.definition == saved
        workspace.coordinates["W"].setText("61")
        workspace.apply_geometry()
        assert not workspace.draft.dirty and workspace.message.isVisible()
        window.navigate(0)
        window.navigate(4)
        until(lambda: not window.parameters.busy)
        workspace = window.parameters.roi_workspace
        assert workspace is not None
        assert startup.application.source_snapshot().state is ConnectionState.CONNECTED
        workspace.disconnect_source.click()
        until(lambda: not window.parameters.busy)
        assert (
            startup.application.source_snapshot().state is ConnectionState.DISCONNECTED
        )
        assert workspace.source_modes["folder"].isEnabled()
    finally:
        window.close()
        until(lambda: window.bridge.closed)
        assert startup.application.join(WAIT)
        delivery.detach()


@pytest.mark.parametrize(
    "kind", ["complete", "partial", "native", "report", "ambiguous"]
)
def test_persistent_source_stop_settles_admitted_results_and_reports(
    qapp: QApplication, tmp_path: Path, passive: PassiveBackend, kind: str
) -> None:
    data = saved_data(tmp_path)
    save_basler(data.sources)
    engine = Engine()
    engine.release_warm.set()
    if kind == "native":
        engine.release_active.clear()
    startup = load_startup(data, engine_factory=lambda _: engine)
    app = startup.application
    assert isinstance(app, SourceConnectionCommands)
    assert isinstance(app, ProductionReportingCommands)
    with server() as peer:
        seed(peer, 199)
        app.set_reporting_selection(
            ReportingSelection(True, LocalReportingSettings(port=peer.port, timeout=2))
        )
        try:
            app.connect_source()
            app.start().result(WAIT)
            if kind == "report":
                peer.hold_next(ReportStage.BITMAP)
            passive.expose(
                2 if kind == "ambiguous" else 1,
                (0,) if kind in ("partial", "native") else tuple(range(6)),
            )
            if kind == "ambiguous":
                until(lambda: app.snapshot().state is ApplicationState.READY)
                observed = app.reporting_snapshot()
                assert (
                    observed is not None
                    and observed.queued_count == 0
                    and observed.acknowledged_count == 0
                )
                assert app.snapshot().counters == ProductionCounters()
                assert app.source_snapshot().state is ConnectionState.FAULT
                return
            until(lambda: app.snapshot().counters.cycles_started == 1)
            if kind == "native":
                assert engine.active.wait(WAIT)
            if kind == "report":
                assert peer.entered.wait(WAIT)
            stopped = app.stop()
            if kind in ("report", "native"):
                assert not stopped.done()
            peer.release.set()
            engine.release_active.set()
            stopped.result(WAIT)
            assert app.snapshot().state is ApplicationState.READY
            result = app.snapshot().latest_production_result
            assert (
                result is not None
                and result.cycle_id == 1
                and len(result.channels) == 36
            )
            assert app.snapshot().counters.cycles_completed == 1
            if kind in ("partial", "native"):
                assert all(camera.frame_id is None for camera in result.cameras[1:])
            observed = app.reporting_snapshot()
            assert (
                observed is not None
                and observed.acknowledged_count == 1
                and not observed.thread_alive
            )
            assert engine.closed and not passive.closed
            assert app.source_snapshot().state is ConnectionState.CONNECTED
        finally:
            peer.release.set()
            engine.release_active.set()
            app.close().result(WAIT)
            assert app.join(WAIT) and passive.closed


def test_normal_basler_player_drains_paused_frames_and_settles_reports(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = saved_data(tmp_path)
    save_basler(data.sources)
    backends: list[PassiveBackend] = []
    engines: list[Engine] = []

    def backend(*, continuous: bool = False) -> PassiveBackend:
        assert continuous
        value = PassiveBackend()
        backends.append(value)
        return value

    def factory(_prepared: object) -> Engine:
        value = Engine()
        value.release_warm.set()
        engines.append(value)
        return value

    monkeypatch.setattr(_basler_sdk, "PylonBackend", backend)
    delivery = FutureDelivery()
    startup = load_startup(data, engine_factory=factory)
    window = create_window(startup, delivery)
    window.show()
    app = startup.application
    assert isinstance(app, SourceConnectionCommands)
    assert startup.local_replay is not None

    def control() -> BaslerControlSnapshot:
        value = window.bridge.control_snapshot()
        assert isinstance(value, BaslerControlSnapshot)
        return value

    original_threads = {t.ident for t in threads()}
    with server() as peer:
        seed(peer, 99)
        panel = window.communication.reporting_panel
        assert panel is not None
        panel.controller.set_reporting_selection(
            ReportingSelection(True, LocalReportingSettings(port=peer.port, timeout=2))
        )
        try:
            app.connect_source()
            window.buttons["operation"].click()
            until(
                lambda: (
                    bool(backends)
                    and backends[0].armed.is_set()
                    and app.source_snapshot().inspection_generation is not None
                )
            )
            window.bridge.refresh()
            middle = window.buttons["pause"]
            assert text("basler_armed", window.language) in window.state_label.toolTip()
            window.buttons["pause"].click()
            until(lambda: control().state is ReplayState.PAUSED)
            window.bridge.refresh()
            position, size = middle.pos(), middle.size()
            assert (
                middle.text() == text("replay_step", window.language)
                and not window.buttons["secondary"].isVisible()
            )
            for ordinal in (1, 2, 3):
                backends[0].expose(ordinal)
                until(lambda ordinal=ordinal: control().received_frames == ordinal * 6)
            assert app.snapshot().counters == ProductionCounters()
            assert len(engines[0].calls) == 2 and not engines[0].closed
            observed = panel.controller.reporting_snapshot()
            assert observed is not None and observed.queued_count == 0
            peer.hold_next(ReportStage.BITMAP)
            middle.click()
            until(lambda: control().admission_armed)
            backends[0].expose(4)
            until(lambda: app.snapshot().counters.cycles_completed == 1)
            assert peer.entered.wait(WAIT)
            assert control().state is ReplayState.STEPPING
            assert not engines[0].closed and not backends[0].closed
            backends[0].expose(5)
            until(lambda: control().received_frames == 30)
            assert app.snapshot().counters.cycles_completed == 1
            peer.release.set()
            until(lambda: control().state is ReplayState.PAUSED)
            window.bridge.refresh()
            assert (middle.pos(), middle.size()) == (position, size)
            observed = panel.controller.reporting_snapshot()
            assert observed is not None and observed.acknowledged_count == 1
            result = app.snapshot().latest_production_result
            assert (
                result is not None
                and result.cycle_id == 4
                and len(result.channels) == 36
            )
            assert all(
                c.frame_id is not None and c.frame_id.sequence == 4
                for c in result.cameras
            )
            middle.click()
            until(lambda: control().admission_armed)
            backends[0].expose(6)
            until(lambda: control().state is ReplayState.PAUSED)
            assert app.snapshot().counters.cycles_completed == 2
            window.bridge.refresh()
            window.buttons["operation"].click()
            until(lambda: control().state is ReplayState.AUTOMATIC)
            for ordinal in (7, 8):
                backends[0].expose(ordinal)
                until(
                    lambda ordinal=ordinal: (
                        app.snapshot().counters.cycles_completed == ordinal - 4
                    )
                )
            backends[0].expose(9, (0,))
            until(lambda: app.snapshot().counters.cycles_started == 5)
            window.bridge.refresh()
            middle.click()
            assert control().state is ReplayState.PAUSING
            backends[0].expose(9, (5, 3, 4, 1, 2))
            until(lambda: control().state is ReplayState.PAUSED)
            assert app.snapshot().counters.cycles_completed == 5
            observed = panel.controller.reporting_snapshot()
            assert observed is not None and observed.acknowledged_count == 5
            window.bridge.refresh()
            window.buttons["stop"].click()
            until(lambda: app.snapshot().state is ApplicationState.READY)
            assert not backends[0].closed and engines[0].closed
            window.bridge.refresh()
            window.buttons["operation"].click()
            until(
                lambda: (
                    len(engines) == 2
                    and app.source_snapshot().inspection_generation is not None
                )
            )
            assert len(backends) == 1 and backends[0].armed.is_set()
            window.bridge.refresh()
            middle.click()
            until(lambda: control().state is ReplayState.PAUSED)
            window.bridge.refresh()
            middle.click()  # An idle Step needs no fabricated cycle or timeout.
            until(lambda: control().admission_armed)
            window.buttons["stop"].click()
            until(lambda: app.snapshot().state is ApplicationState.READY)
            assert app.snapshot().counters.cycles_completed == 5
            assert app.snapshot().last_issue is None
            assert all(e.closed for e in engines) and all(
                not b.closed for b in backends
            )
        finally:
            peer.release.set()
            for engine in engines:
                engine.release_active.set()
            window.close()
            until(lambda: window.bridge.closed)
            assert app.join(WAIT)
            delivery.detach()
    assert not any(
        t.name.startswith(
            (
                "EarlyCycleOwner-",
                "EarlyCameraInspector-",
                "VimoInferenceWorker-",
                "LocalProductionSource-",
                "FirstStationReporting-",
            )
        )
        for t in threads()
        if t.ident not in original_threads
    )


def test_basler_partial_step_waits_for_late_native_return_before_paused(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = saved_data(tmp_path)
    save_basler(data.sources)
    backend = PassiveBackend()
    engine = Engine()
    engine.release_warm.set()
    engine.release_active.clear()

    def sdk(*, continuous: bool = False) -> PassiveBackend:
        assert continuous
        return backend

    monkeypatch.setattr(_basler_sdk, "PylonBackend", sdk)
    startup = load_startup(data, engine_factory=lambda _: engine)
    assert isinstance(startup.application, SourceConnectionCommands)
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    window.show()
    app = startup.application
    assert isinstance(app, SourceConnectionCommands)

    def state() -> ReplayState:
        value = window.bridge.control_snapshot()
        assert isinstance(value, BaslerControlSnapshot)
        return value.state

    try:
        app.connect_source()
        window.buttons["operation"].click()
        until(
            lambda: (
                backend.armed.is_set()
                and app.source_snapshot().inspection_generation is not None
            )
        )
        window.bridge.refresh()
        window.buttons["pause"].click()
        until(lambda: state() is ReplayState.PAUSED)
        window.bridge.refresh()
        window.buttons["pause"].click()
        until(
            lambda: (
                (value := window.bridge.control_snapshot()) is not None
                and isinstance(value, BaslerControlSnapshot)
                and value.admission_armed
            )
        )
        backend.expose(1, (0,))
        assert engine.active.wait(WAIT)
        until(lambda: app.snapshot().counters.cycles_completed == 1)
        result = app.snapshot().latest_production_result
        assert result is not None and len(result.channels) == 36
        assert result.cameras[0].frame_id is not None
        assert all(c.frame_id is None for c in result.cameras[1:])
        assert (
            state() is ReplayState.STEPPING
        )  # A deadline result is not native return.
        engine.release_active.set()
        until(lambda: state() is ReplayState.PAUSED)
        assert len(engine.calls) == 3 and app.snapshot().counters.cycles_started == 1
        window.bridge.refresh()
        window.buttons["stop"].click()
        until(lambda: app.snapshot().state is ApplicationState.READY)
        assert (
            app.snapshot().counters.cycles_completed == 1
            and engine.closed
            and not backend.closed
        )
    finally:
        engine.release_active.set()
        window.close()
        until(lambda: window.bridge.closed)
        assert app.join(WAIT)
        delivery.detach()
