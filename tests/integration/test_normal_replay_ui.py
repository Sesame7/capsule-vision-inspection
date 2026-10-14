"""Normal restored Folder source uses the existing replay gate and Qt commands."""

import json
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from threading import enumerate as threads

import pytest
from PySide6.QtWidgets import QApplication
from tests.application_support import WAIT
from tests.integration.test_application_bootstrap_ui import close_window
from tests.integration.test_local_production_ui import ReplayEngine, replay_files
from tests.qt_support import ensure_test_fonts, until
from tests.reporting_support import readback, seed, server
from tests.unit.test_application_bootstrap import saved_data

from vimo_demo.application import (
    ApplicationState,
    PreparedConfiguration,
    ProductionCounters,
)
from vimo_demo.application.local_replay import ReplayState
from vimo_demo.communication.reporting import ReportStage
from vimo_demo.configuration import (
    CameraFolder,
    load_camera_sources,
    save_camera_sources,
)
from vimo_demo.ui.bootstrap import ApplicationData, Startup, create_window, load_startup
from vimo_demo.ui.inference_bridge import FutureDelivery
from vimo_demo.ui.local_production import compose
from vimo_demo.ui.production_style import Language, Style, text
from vimo_demo.ui.production_window import ProductionWindow


@contextmanager
def normal_replay(
    root: Path,
    *,
    loop: bool = True,
    first_camera_images: int = 2,
    hold_call: int | None = None,
    fail: str | None = None,
) -> Generator[tuple[Startup, ProductionWindow, list[ReplayEngine]]]:
    original_threads = {thread.ident for thread in threads()}
    data = saved_data(root)
    replay_files(root / "frames", loop=loop, first_camera_images=first_camera_images)
    sources = load_camera_sources(root / "frames" / "camera_sources.json")
    save_camera_sources(
        data.sources,
        replace(
            sources,
            cameras=tuple(
                CameraFolder(camera.camera_id, f"../../frames/{camera.folder}")
                for camera in sources.cameras
            ),
        ),
    )
    engines: list[ReplayEngine] = []

    def factory(_prepared: PreparedConfiguration) -> ReplayEngine:
        engine = ReplayEngine(fail=fail)
        engine.hold_call = hold_call if not engines else None
        engines.append(engine)
        return engine

    delivery = FutureDelivery()
    startup = load_startup(
        data,
        engine_factory=factory,
        observe_source=lambda future: delivery.watch("folder_source", future),
    )
    window = create_window(startup, delivery)
    window.show()
    try:
        yield startup, window, engines
    finally:
        for engine in engines:
            engine.release.set()
        close_window(window)
        delivery.detach()
        assert all(engine.closed for engine in engines)
        assert not {thread.ident for thread in threads()} - original_threads


def replay_state(startup: Startup) -> ReplayState | None:
    assert startup.local_replay is not None
    snapshot = startup.local_replay.replay_snapshot()
    return snapshot.state if snapshot else None


@pytest.mark.parametrize("language,size", [("zh", (1080, 1920)), ("en", (1920, 1080))])
def test_normal_ready_step_warms_up_once_then_steps_in_same_generation(
    qapp: QApplication, tmp_path: Path, language: Language, size: tuple[int, int]
) -> None:
    ensure_test_fonts()
    with normal_replay(tmp_path, hold_call=1) as (startup, window, engines):
        app = startup.application
        window.set_language(language)
        window.resize(*size)
        window.bridge.refresh()
        primary, secondary = window.buttons["operation"], window.buttons["secondary"]
        assert window.bridge.local_replay is startup.local_replay
        assert primary.text() == text("replay_start", language)
        assert secondary.text() == text("replay_step", language)
        assert primary.isEnabled() and secondary.isEnabled() and not engines
        before = {path: path.read_bytes() for path in startup.data.root.rglob("*.json")}
        qapp.processEvents()
        assert window.grab().save(str(tmp_path / f"normal-replay-{language}-ready.png"))
        secondary.click()
        until(lambda: bool(engines) and engines[0].entered.is_set())
        window.bridge.refresh()
        assert app.snapshot().state is ApplicationState.STARTING
        assert app.snapshot().counters == ProductionCounters()
        assert not primary.isEnabled() and not secondary.isEnabled()
        secondary.click()  # Disabled startup must not queue another step permission.
        engines[0].release.set()
        until(lambda: replay_state(startup) is ReplayState.PAUSED)
        window.bridge.refresh()
        assert app.snapshot().state is ApplicationState.RUNNING
        assert (
            app.snapshot().generation == 1
            and app.snapshot().counters.cycles_completed == 1
        )
        assert engines[0].calls == [10, 10, 10, 20, 30, 40, 50, 60]
        assert primary.text() == text("replay_continue", language)
        assert secondary.text() == text("replay_step", language)
        assert (
            not window.buttons["batch"].isEnabled() and not window.parameters.editable()
        )
        panel = window.communication.reporting_panel
        assert (
            panel is not None
            and not panel.enabled.isEnabled()
            and not panel.port.isEnabled()
        )
        assert panel.controller.reporting_snapshot() is None
        for cycle, expected in (
            (2, [11, 21, 31, 41, 51, 61]),
            (3, [10, 20, 32, 42, 50, 62]),
        ):
            secondary.click()
            until(
                lambda expected_cycle=cycle: (
                    app.snapshot().counters.cycles_completed == expected_cycle
                    and replay_state(startup) is ReplayState.PAUSED
                )
            )
            window.bridge.refresh()
            assert len(engines) == 1 and not engines[0].closed
            assert engines[0].calls[2 + (cycle - 1) * 6 : 2 + cycle * 6] == expected
            result = app.snapshot().latest_production_result
            assert result is not None and result.cycle_id == cycle
            assert app.snapshot().generation == 1
            assert [camera.camera_id for camera in result.cameras] == [
                f"CAM{i}" for i in range(1, 7)
            ]
            for camera in result.cameras:
                assert camera.frame_id is not None
                assert (
                    camera.frame_id.cycle_id == cycle
                    and camera.frame_id.camera_id == camera.camera_id
                )
        assert {path: path.read_bytes() for path in before} == before
        qapp.processEvents()
        for button in (primary, secondary):
            assert button.height() >= Style.touch
            assert (
                button.text() == button.accessibleName() and not button.icon().isNull()
            )
            assert (
                button.width()
                >= button.fontMetrics().horizontalAdvance(button.text()) + 20
            )
        assert window.grab().save(
            str(tmp_path / f"normal-replay-{language}-paused.png")
        )


def test_normal_pause_continue_end_and_restart_preserve_existing_cursor_rules(
    qapp: QApplication, tmp_path: Path
) -> None:
    with normal_replay(tmp_path, hold_call=3) as (startup, window, engines):
        app = startup.application
        primary, secondary = window.buttons["operation"], window.buttons["secondary"]
        primary.click()
        until(lambda: bool(engines) and engines[0].entered.is_set())
        window.bridge.refresh()
        assert primary.text() == text("replay_pause", window.language)
        assert secondary.text() == text("replay_end", window.language)
        primary.click()
        assert replay_state(startup) is ReplayState.PAUSING and not primary.isEnabled()
        assert app.snapshot().counters.cycles_completed == 0
        engines[0].release.set()
        until(lambda: replay_state(startup) is ReplayState.PAUSED)
        window.bridge.refresh()
        engines[0].hold_call = 9
        engines[0].entered.clear()
        engines[0].release.clear()
        primary.click()  # Continue uses the same engine/cursor, without warm-up.
        until(engines[0].entered.is_set)
        window.bridge.refresh()
        assert (
            app.snapshot().generation == 1
            and app.snapshot().counters.cycles_started == 2
        )
        secondary.click()  # End requests real Stop, even with an active fake native call.
        assert app.snapshot().state is ApplicationState.STOPPING
        assert not primary.isEnabled() and not secondary.isEnabled()
        engines[0].release.set()
        until(lambda: app.snapshot().state is ApplicationState.READY)
        window.bridge.refresh()
        assert engines[0].closed and app.snapshot().counters.cycles_completed == 2
        assert engines[0].calls[8:] == [11, 21, 31, 41, 51, 61]
        secondary.click()
        until(lambda: replay_state(startup) is ReplayState.PAUSED)
        assert len(engines) == 2 and app.snapshot().generation == 2
        assert engines[1].calls == [10, 10, 10, 20, 30, 40, 50, 60]
        result = app.snapshot().latest_production_result
        assert result is not None and result.cycle_id == 1
        assert app.snapshot().counters.cycles_completed == 3


def test_normal_and_developer_entry_use_same_saved_folder_step_semantics(
    qapp: QApplication, tmp_path: Path
) -> None:
    with normal_replay(tmp_path) as (startup, window, engines):
        developer_engine = ReplayEngine()
        developer = compose(
            startup.data.models / "general.json",
            startup.data.machine,
            startup.data.recipes / "local.recipe.json",
            sources_file=startup.data.sources,
            engine_factory=lambda _: developer_engine,
            replay_controls=True,
        )
        try:
            assert startup.local_replay is not None
            for cycle in range(1, 4):
                startup.local_replay.step_replay().result(WAIT)
                developer.application.step_replay().result(WAIT)
                assert engines[0].calls == developer_engine.calls
                assert (
                    startup.application.snapshot().counters
                    == developer.application.snapshot().counters
                )
                normal_result = startup.application.snapshot().latest_production_result
                developer_result = (
                    developer.application.snapshot().latest_production_result
                )
                assert normal_result is not None and developer_result is not None
                assert normal_result.cycle_id == developer_result.cycle_id == cycle
                assert replay_state(startup) is ReplayState.PAUSED
                snapshot = developer.application.replay_snapshot()
                assert snapshot is not None and snapshot.state is ReplayState.PAUSED
            window.bridge.refresh()
            assert window.buttons["operation"].text() == text(
                "replay_continue", window.language
            )
        finally:
            developer.application.close().result(WAIT)
            assert developer.application.join(WAIT) and developer_engine.closed


def test_normal_nonloop_exhaustion_does_not_wait_in_paused_state(
    qapp: QApplication, tmp_path: Path
) -> None:
    with normal_replay(tmp_path, loop=False, first_camera_images=1) as (
        startup,
        window,
        engines,
    ):
        window.buttons["secondary"].click()
        until(
            lambda: (
                startup.application.snapshot().state is ApplicationState.READY
                and bool(engines)
            )
        )
        assert replay_state(startup) is ReplayState.STOPPED
        assert startup.application.snapshot().counters.cycles_completed == 1
        assert len(engines[0].calls) == 8 and engines[0].closed


def test_normal_source_failure_from_paused_step_stops_and_reports_camera(
    qapp: QApplication, tmp_path: Path
) -> None:
    with normal_replay(tmp_path, loop=False) as (startup, window, engines):
        window.buttons["secondary"].click()
        until(lambda: replay_state(startup) is ReplayState.PAUSED)
        (tmp_path / "frames" / "CAM2" / "1.png").write_bytes(
            b"corrupt isolated test image"
        )
        window.bridge.refresh()
        window.buttons["secondary"].click()
        until(lambda: startup.application.snapshot().state is ApplicationState.READY)
        until(lambda: "CAM2" in window.event_label.toolTip())
        assert replay_state(startup) is ReplayState.STOPPED
        assert startup.application.snapshot().counters.cycles_completed == 1
        assert len(engines[0].calls) == 8 and engines[0].closed


@pytest.mark.parametrize(
    "failure,state",
    [("warmup", ApplicationState.READY), ("fatal", ApplicationState.FAULT)],
)
def test_normal_step_failure_retains_authoritative_lifecycle(
    qapp: QApplication, tmp_path: Path, failure: str, state: ApplicationState
) -> None:
    with normal_replay(tmp_path, fail=failure) as (startup, window, engines):
        window.buttons["secondary"].click()
        until(
            lambda: (
                bool(engines)
                and engines[0].closed
                and startup.application.snapshot().state is state
                and not startup.application.snapshot().runtime_owned
            )
        )
        window.bridge.refresh()
        assert replay_state(startup) is ReplayState.STOPPED
        assert startup.application.snapshot().counters.cycles_completed == 0
        assert startup.application.snapshot().last_issue is not None
        assert window.event_label.text() and window.event_label.toolTip()
        assert window.buttons["operation"].text() == text(
            "replay_start" if failure == "warmup" else "reset", window.language
        )
        if failure == "fatal":
            assert not window.buttons["secondary"].isVisible()


def test_normal_paused_reporting_ack_and_overload_use_genuine_stop(
    qapp: QApplication, tmp_path: Path
) -> None:
    with server() as peer, normal_replay(tmp_path) as (startup, window, engines):
        seed(peer, 99)
        peer.hold_next(ReportStage.BITMAP)
        panel = window.communication.reporting_panel
        assert panel is not None
        panel.port.setValue(peer.port)
        panel.timeout.setValue(2)
        panel.enabled.setChecked(True)
        selected = panel.controller.reporting_selection()
        panel.controller.set_reporting_selection(
            replace(selected, settings=replace(selected.settings, max_pending=1))
        )
        window.buttons["secondary"].click()
        until(lambda: replay_state(startup) is ReplayState.PAUSED)
        assert peer.entered.wait(WAIT)
        observed = panel.controller.reporting_snapshot()
        assert observed is not None and observed.acknowledged_count == 0
        window.bridge.refresh()
        assert not panel.port.isEnabled()
        # Inspection is paused while the independently owned report remains active.
        assert startup.application.snapshot().state is ApplicationState.RUNNING
        assert len(engines[0].calls) == 8
        peer.release.set()
        until(
            lambda: (
                (s := panel.controller.reporting_snapshot()) is not None
                and s.acknowledged_count == 1
            )
        )
        assert readback(peer)[0] == 100 and replay_state(startup) is ReplayState.PAUSED
        peer.release.clear()
        peer.entered.clear()
        peer.hold_next(ReportStage.BITMAP)
        assert startup.local_replay is not None
        startup.local_replay.step_replay().result(WAIT)
        assert peer.entered.wait(WAIT)
        startup.local_replay.step_replay().result(WAIT)  # One queued report.
        overloaded = startup.local_replay.step_replay()
        until(lambda: startup.application.snapshot().state is ApplicationState.STOPPING)
        assert overloaded.done()
        assert (
            overloaded.exception() is not None
            and replay_state(startup) is ReplayState.STOPPED
        )
        assert startup.application.snapshot().counters.cycles_completed == 4
        peer.release.set()
        until(lambda: startup.application.snapshot().state is ApplicationState.READY)
        until(lambda: "ReportingCapacityError" in window.event_label.toolTip())
        assert engines[0].closed
        observed = panel.controller.reporting_snapshot()
        assert (
            observed is not None
            and not observed.thread_alive
            and observed.rejected_count == 1
        )


@pytest.mark.parametrize("kind", ["missing", "unsupported"])
def test_normal_without_folder_capability_keeps_generic_controls(
    qapp: QApplication, tmp_path: Path, kind: str
) -> None:
    data = (
        saved_data(tmp_path)
        if kind == "unsupported"
        else ApplicationData.at_entry(tmp_path)
    )
    if kind == "unsupported":
        value = json.loads(data.sources.read_text(encoding="utf-8"))
        value["cameras"][0]["source_type"] = "unsupported"
        data.sources.write_text(json.dumps(value), encoding="utf-8")
    startup = load_startup(data)
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    window.show()
    try:
        assert startup.local_replay is None and window.bridge.local_replay is None
        assert "secondary" not in window.buttons
        assert window.snapshot.state is ApplicationState.UNCONFIGURED
        assert not window.buttons["operation"].isEnabled()
    finally:
        close_window(window)
        delivery.detach()
