"""Normal restored Folder source uses the existing replay gate and Qt commands."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication
from tests.support.application import WAIT, ReplayEngine
from tests.support.qt import close_window, ensure_test_fonts, until
from tests.support.reporting import readback, seed, server
from tests.support.startup import normal_replay, replay_state, saved_data

from capsule_vision.application import (
    ApplicationState,
)
from capsule_vision.application.composition import compose
from capsule_vision.application.source_control import (
    SourceControlCommands,
    SourceControlState,
)
from capsule_vision.application.startup import ApplicationData, load_startup
from capsule_vision.communication.reporting import ReportStage
from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import Language
from capsule_vision.ui.bootstrap import create_window
from capsule_vision.ui.editing import EditingCoordinator
from capsule_vision.ui.future_delivery import FutureDelivery
from capsule_vision.ui.style import Style


@pytest.mark.parametrize("language,size", [("zh", (1080, 1920)), ("en", (1920, 1080))])
def test_normal_start_pause_then_steps_in_same_generation(
    qapp: QApplication,
    tmp_path: Path,
    language: Language,
    size: tuple[int, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_test_fonts()
    with normal_replay(tmp_path, hold_call=3) as (startup, window, engines):
        app = startup.application
        window.set_language(language)
        window.resize(*size)
        window.bridge.refresh()
        primary, secondary = window.buttons["operation"], window.buttons["pause"]
        assert window.bridge.source_control is app
        assert primary.text() == text("replay_start", language)
        assert secondary.accessibleName() == text("replay_pause", language)
        assert primary.isEnabled() and not secondary.isEnabled() and not engines
        assert not window.buttons["secondary"].isVisible()

        def preparing(_editing: EditingCoordinator) -> bool:
            return True

        with monkeypatch.context() as blocked:
            blocked.setattr(EditingCoordinator, "busy", property(preparing))
            window.editing.changed.emit()
            assert not primary.isEnabled() and not secondary.isEnabled()
            primary.click()
            assert not engines
        window.editing.changed.emit()
        before = {path: path.read_bytes() for path in startup.data.root.rglob("*.json")}
        qapp.processEvents()
        geometry = tuple(
            (button.mapTo(window.summary, QPoint()), button.size())
            for button in (primary, secondary)
        )
        region = window.operation_group.geometry()
        window.grab()
        primary.click()
        until(lambda: bool(engines) and engines[0].entered.is_set())
        window.bridge.refresh()
        assert app.snapshot().state is ApplicationState.RUNNING
        assert app.snapshot().counters.cycles_completed == 0
        assert engines[0].calls == [10, 10, 10]  # Warm-up plus held first frame.
        assert not primary.isEnabled() and secondary.isEnabled()
        assert secondary.text() == text("replay_pause", language)
        qapp.processEvents()
        assert (
            tuple(
                (button.mapTo(window.summary, QPoint()), button.size())
                for button in (primary, secondary)
            )
            == geometry
        )
        assert window.operation_group.geometry() == region
        secondary.click()
        assert replay_state(startup) is SourceControlState.PAUSING
        assert not secondary.isEnabled()
        secondary.click()  # Pending pause must not grant a step permission.
        engines[0].release.set()
        until(lambda: replay_state(startup) is SourceControlState.PAUSED)
        window.bridge.refresh()
        assert app.snapshot().state is ApplicationState.RUNNING
        assert (
            app.snapshot().generation == 1
            and app.snapshot().counters.cycles_completed == 1
        )
        assert engines[0].calls == [10, 10, 10, 20, 30, 40, 50, 60]
        assert primary.text() == text("replay_continue", language)
        assert secondary.accessibleName() == text("replay_step", language)
        assert window.state_label.text() == text("state_replay_paused", language)
        assert text("replay_paused", language) in window.state_label.accessibleName()
        assert not window.state_label.toolTip()
        assert Style.warning in window.state_label.styleSheet()
        qapp.processEvents()
        assert (
            window.state_label.width()
            >= window.state_label.fontMetrics().horizontalAdvance(
                window.state_label.text()
            )
        )
        assert (
            tuple(
                (button.mapTo(window.summary, QPoint()), button.size())
                for button in (primary, secondary)
            )
            == geometry
        )
        assert window.operation_group.geometry() == region
        identity = window.identity_group
        center = identity.mapTo(window.summary, QPoint()).x() + identity.width() / 2
        if (
            2 * window.operation_group.width() + identity.width() + 2 * Style.space
            <= window.summary.width()
        ):
            assert abs(center - window.summary.width() / 2) <= 1
        else:
            assert window.batch_group.geometry().right() < identity.geometry().left()
            assert (
                identity.geometry().right() < window.operation_group.geometry().left()
            )
        assert (
            window.batch_group.mapTo(window.summary, QPoint()).x()
            == window.summary.contentsRect().left()
            + window.summary_layout.contentsMargins().left()
        )
        window.bridge.refresh()
        assert all(not card.viewport.image.isNull() for card in window.cards)
        images = tuple(card.viewport.image.copy() for card in window.cards)
        results = tuple(card.result for card in window.cards)
        conversions = tuple(card.viewport.conversions for card in window.cards)
        for _ in range(3):
            window.bridge.refresh()
        assert tuple(card.viewport.image for card in window.cards) == images
        assert tuple(card.result for card in window.cards) == results
        assert tuple(card.viewport.conversions for card in window.cards) == conversions
        assert (
            not window.buttons["batch"].isEnabled()
            and not window.recipe_page.editable()
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
            engines[0].hold_call = 2 + (cycle - 1) * 6 + 1
            engines[0].entered.clear()
            engines[0].release.clear()
            secondary.click()
            until(engines[0].entered.is_set)
            window.bridge.refresh()
            assert replay_state(startup) is SourceControlState.STEPPING
            assert not secondary.isEnabled() and not primary.isEnabled()
            assert secondary.text() == text("replay_step", language)
            secondary.click()  # Disabled middle button cannot queue another cycle.
            engines[0].release.set()
            until(
                lambda expected_cycle=cycle: (
                    app.snapshot().counters.cycles_completed == expected_cycle
                    and replay_state(startup) is SourceControlState.PAUSED
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
                button.toolTip()
                and button.accessibleName()
                and not button.icon().isNull()
            )
            assert (
                button.width()
                >= button.fontMetrics().horizontalAdvance(button.text()) + 20
            )
        window.grab()


def test_normal_pause_continue_end_and_restart_preserve_existing_cursor_rules(
    qapp: QApplication, tmp_path: Path
) -> None:
    ensure_test_fonts()
    with normal_replay(tmp_path, hold_call=3) as (startup, window, engines):
        window.resize(1080, 1920)  # Supported minimum with real font metrics.
        app = startup.application
        primary, secondary = window.buttons["operation"], window.buttons["pause"]
        qapp.processEvents()
        geometry = tuple(
            (button.mapTo(window.summary, QPoint()), button.size())
            for button in (primary, secondary)
        )

        def assert_geometry() -> None:
            qapp.processEvents()
            assert (
                tuple(
                    (button.mapTo(window.summary, QPoint()), button.size())
                    for button in (primary, secondary)
                )
                == geometry
            )
            identity = window.identity_group
            if (
                2 * window.operation_group.width() + identity.width() + 2 * Style.space
                <= window.summary.width()
            ):
                assert (
                    abs(
                        identity.mapTo(window.summary, QPoint()).x()
                        + identity.width() / 2
                        - window.summary.width() / 2
                    )
                    <= 1
                )
            else:
                assert (
                    window.batch_group.geometry().right() < identity.geometry().left()
                )
                assert (
                    identity.geometry().right()
                    < window.operation_group.geometry().left()
                )

        primary.click()
        until(lambda: bool(engines) and engines[0].entered.is_set())
        window.bridge.refresh()
        assert (
            window.buttons["pause"].isEnabled() and window.buttons["stop"].isEnabled()
        )
        assert_geometry()
        window.grab()
        window.buttons["pause"].click()
        assert (
            replay_state(startup) is SourceControlState.PAUSING
            and not primary.isEnabled()
        )
        assert app.snapshot().counters.cycles_completed == 0
        assert_geometry()
        engines[0].release.set()
        until(lambda: replay_state(startup) is SourceControlState.PAUSED)
        window.bridge.refresh()
        assert_geometry()
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
        assert_geometry()
        window.buttons[
            "stop"
        ].click()  # Real Stop, even with an active fake native call.
        assert app.snapshot().state is ApplicationState.STOPPING
        assert_geometry()
        window.grab()
        assert not primary.isEnabled() and not secondary.isEnabled()
        engines[0].release.set()
        until(lambda: app.snapshot().state is ApplicationState.READY)
        window.bridge.refresh()
        assert engines[0].closed and app.snapshot().counters.cycles_completed == 2
        assert engines[0].calls[8:] == [11, 21, 31, 41, 51, 61]
        window.bridge.step_source()  # Exercise existing restart/cursor contract.
        until(lambda: replay_state(startup) is SourceControlState.PAUSED)
        assert len(engines) == 2 and app.snapshot().generation == 2
        assert engines[1].calls == [10, 10, 10, 20, 30, 40, 50, 60]
        result = app.snapshot().latest_production_result
        assert result is not None and result.cycle_id == 1
        assert app.snapshot().counters.cycles_completed == 3


def test_normal_and_internal_composition_use_same_saved_folder_step_semantics(
    qapp: QApplication, tmp_path: Path
) -> None:
    with normal_replay(tmp_path) as (startup, window, engines):
        developer_engine = ReplayEngine()
        developer = compose(
            startup.data.models / "general" / "model.json",
            startup.data.machine,
            startup.data.recipes / "local.json",
            sources_file=startup.data.sources,
            engine_factory=lambda _: developer_engine,
            replay_controls=True,
        )
        try:
            assert isinstance(startup.application, SourceControlCommands)
            for cycle in range(1, 4):
                startup.application.step_source().result(WAIT)
                developer.application.step_source().result(WAIT)
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
                assert replay_state(startup) is SourceControlState.PAUSED
                snapshot = developer.application.control_snapshot()
                assert (
                    snapshot is not None and snapshot.state is SourceControlState.PAUSED
                )
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
        window.bridge.step_source()
        until(
            lambda: (
                startup.application.snapshot().state is ApplicationState.READY
                and bool(engines)
            )
        )
        assert replay_state(startup) is SourceControlState.STOPPED
        assert startup.application.snapshot().counters.cycles_completed == 1
        assert len(engines[0].calls) == 8 and engines[0].closed


def test_normal_source_failure_from_paused_step_stops_and_reports_camera(
    qapp: QApplication, tmp_path: Path
) -> None:
    with normal_replay(tmp_path, loop=False) as (startup, window, engines):
        window.bridge.step_source()
        until(lambda: replay_state(startup) is SourceControlState.PAUSED)
        (tmp_path / "frames" / "CAM2" / "1.png").write_bytes(
            b"corrupt isolated test image"
        )
        window.bridge.refresh()
        window.buttons["pause"].click()
        until(lambda: startup.application.snapshot().state is ApplicationState.READY)
        until(lambda: "CAM2" in window.event_label.toolTip())
        assert replay_state(startup) is SourceControlState.STOPPED
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
        window.bridge.step_source()
        until(
            lambda: (
                bool(engines)
                and engines[0].closed
                and startup.application.snapshot().state is state
                and not startup.application.snapshot().runtime_owned
            )
        )
        window.bridge.refresh()
        assert replay_state(startup) is SourceControlState.STOPPED
        assert startup.application.snapshot().counters.cycles_completed == 0
        assert startup.application.snapshot().last_issue is not None
        assert window.event_label.text() and window.event_label.toolTip()
        assert window.buttons["operation"].text() == text(
            "replay_start", window.language
        )
        if failure == "fatal":
            assert window.buttons["secondary"].isVisible()
            assert window.buttons["secondary"].accessibleName() == text(
                "reset", window.language
            )


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
        window.communication.persist_settings()
        selected = panel.controller.reporting_selection()
        panel.controller.set_reporting_selection(
            replace(selected, settings=replace(selected.settings, max_pending=1))
        )
        window.bridge.step_source()
        until(lambda: replay_state(startup) is SourceControlState.PAUSED)
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
        assert (
            readback(peer)[0] == 100
            and replay_state(startup) is SourceControlState.PAUSED
        )
        peer.release.clear()
        peer.entered.clear()
        peer.hold_next(ReportStage.BITMAP)
        assert isinstance(startup.application, SourceControlCommands)
        startup.application.step_source().result(WAIT)
        assert peer.entered.wait(WAIT)
        startup.application.step_source().result(WAIT)  # One queued report.
        overloaded = startup.application.step_source()
        until(lambda: startup.application.snapshot().state is ApplicationState.STOPPING)
        assert overloaded.done()
        assert (
            overloaded.exception() is not None
            and replay_state(startup) is SourceControlState.STOPPED
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
def test_unconfigured_normal_window_retains_deferred_source_controls(
    qapp: QApplication, tmp_path: Path, kind: str
) -> None:
    data = (
        saved_data(tmp_path)
        if kind == "unsupported"
        else ApplicationData.at_entry(tmp_path)
    )
    if kind == "unsupported":
        value = json.loads(data.sources.read_text(encoding="utf-8"))
        value["CAM1"]["source_type"] = "unsupported"
        data.sources.write_text(json.dumps(value), encoding="utf-8")
    startup = load_startup(data)
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    window.show()
    try:
        assert window.bridge.controls_enabled
        assert window.bridge.control_snapshot() is None
        assert not window.buttons["secondary"].isVisible()
        assert not window.buttons["pause"].isEnabled()
        assert window.snapshot.state is ApplicationState.UNCONFIGURED
        assert not window.buttons["operation"].isEnabled()
    finally:
        close_window(window)
        delivery.detach()
