"""Fake normal Folder composition; Web is observation, never result delivery."""

import json
import socket
from collections.abc import Callable
from http.client import HTTPConnection
from pathlib import Path
from threading import enumerate as threads

import pytest
from PySide6.QtWidgets import QApplication, QWidget
from tests.support.qt import until
from tests.support.reporting import readback, seed, server
from tests.support.startup import normal_replay, replay_state, saved_data

from capsule_vision.application import ApplicationState
from capsule_vision.application import startup as startup_module
from capsule_vision.application.source_control import SourceControlState
from capsule_vision.application.startup import ApplicationData, load_startup
from capsule_vision.communication.reporting import ReportStage
from capsule_vision.configuration.communication_preferences import (
    CommunicationPreferences,
    WebPreferences,
    save_communication_preferences,
)
from capsule_vision.ui import bootstrap
from capsule_vision.web import ProductionSnapshotService
from capsule_vision.web.runtime import WebRuntime, WebState


class StartupWindow(QWidget):
    """Keep the bootstrap probe small while observing the real shell handoff."""

    runtime: WebRuntime | None = None

    def bind_web_runtime(self, runtime: WebRuntime) -> None:
        self.runtime = runtime

    def finish_close(self) -> None:
        self.close()


def run_startup(
    qapp: QApplication,
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    observe: Callable[[WebRuntime], None],
) -> None:
    original_threads = {thread.ident for thread in threads()}
    window = StartupWindow()

    def existing_qt(_args: object) -> QApplication:
        return qapp

    def existing_window(*_args: object) -> QWidget:
        return window

    def observe_before_close() -> int:
        assert window.isVisible()
        assert window.isMaximized()
        runtime = window.runtime
        assert runtime is not None
        until(lambda: not runtime.snapshot().busy)
        observe(runtime)
        return 0

    monkeypatch.setattr(bootstrap, "QApplication", existing_qt)
    monkeypatch.setattr(bootstrap, "create_window", existing_window)
    monkeypatch.setattr(qapp, "exec", observe_before_close)
    try:
        assert bootstrap.main(root, []) == 0
        assert window.runtime is not None and window.runtime.closed
        assert not window.runtime.snapshot().owned
        assert not window.isVisible()
    finally:
        window.close()
    assert not {thread.ident for thread in threads()} - original_threads


def test_stop_restart_and_close_keep_web_independent_of_production(
    qapp: QApplication, tmp_path: Path
) -> None:
    original_threads = {thread.ident for thread in threads()}
    with normal_replay(tmp_path) as (startup, window, engines):
        service = ProductionSnapshotService(
            startup.application, startup.frames, port=0, interval=0.02
        )
        service.start()
        try:
            for generation in (1, 2):
                window.bridge.refresh()
                assert window.buttons["operation"].isEnabled()
                window.bridge.step_source()  # Deterministic one-cycle source setup.
                until(lambda: replay_state(startup) is SourceControlState.PAUSED)
                window.bridge.refresh()
                assert startup.application.snapshot().state is ApplicationState.RUNNING
                assert (
                    startup.application.snapshot().counters.cycles_completed
                    == generation
                )

                def observed(expected_generation: int = generation) -> bool:
                    value = service.publisher.latest()
                    if value is None:
                        return False
                    metadata = json.loads(value.metadata)
                    return metadata["generation"] == expected_generation and all(
                        row["result_status"] == "matched" for row in metadata["cameras"]
                    )

                until(observed)
                window.bridge.stop()
                until(
                    lambda: (
                        startup.application.snapshot().state is ApplicationState.READY
                    )
                )
                assert service.publisher_alive
                assert (
                    startup.application.snapshot().counters.cycles_completed
                    == generation
                )
            assert len(engines) == 2 and all(engine.closed for engine in engines)
        finally:
            service.request_stop()
            assert service.join(3)
    assert not {thread.ident for thread in threads()} - original_threads


def test_runtime_restart_during_inspection_preserves_production_and_reporting(
    qapp: QApplication,
    tmp_path: Path,
) -> None:
    with (
        server() as peer,
        normal_replay(tmp_path, hold_call=3) as (startup, window, engines),
    ):
        seed(peer, 0)
        page = window.communication
        panel = page.reporting_panel
        assert panel is not None
        panel.port.setValue(peer.port)
        panel.enabled.setChecked(True)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        page.web_panel.port.setValue(port)
        page.persist_settings()
        effective = panel.controller.reporting_selection()
        runtime = WebRuntime(
            lambda value: ProductionSnapshotService(
                startup.application,
                startup.frames,
                host=value.host,
                port=value.port,
                interval=value.interval_ms / 1000,
            )
        )
        window.bind_web_runtime(runtime)
        page.web_panel.power.click()  # Explicitly save enablement; first use is off.
        until(lambda: runtime.snapshot().state is WebState.RUNNING)
        settings = page.web_panel.saved
        assert settings.enabled and runtime.snapshot().configuration == settings
        window.bridge.step_source()
        until(lambda: bool(engines) and engines[0].entered.is_set())
        assert startup.application.snapshot().state is ApplicationState.RUNNING
        restart = runtime.apply(settings)
        until(restart.done)
        assert restart.result().state is WebState.RUNNING
        assert len(engines) == 1 and not engines[0].closed
        assert startup.application.snapshot().counters.cycles_completed == 0
        assert panel.controller.reporting_selection() is effective
        engines[0].release.set()
        until(lambda: replay_state(startup) is SourceControlState.PAUSED)
        until(
            lambda: (
                (report := panel.controller.reporting_snapshot()) is not None
                and report.acknowledged_count == 1
            )
        )
        assert readback(peer)[0] == 1
        stop = runtime.stop()
        until(stop.done)
        assert stop.result().state is WebState.STOPPED
        assert startup.application.snapshot().state is ApplicationState.RUNNING
        peer.hold_next(ReportStage.SEQUENCE)
        window.bridge.step_source()
        try:
            until(
                lambda: (
                    startup.application.snapshot().counters.cycles_completed == 2
                    and replay_state(startup) is SourceControlState.PAUSED
                    and peer.entered.is_set()
                )
            )
            report = panel.controller.reporting_snapshot()
            assert report is not None and report.acknowledged_count == 1
        finally:
            peer.release.set()
        # Folder terminal accounting/PAUSED is not the asynchronous report ACK.
        until(
            lambda: (
                (report := panel.controller.reporting_snapshot()) is not None
                and report.acknowledged_count == 2
            )
        )
        assert readback(peer)[0] == 2
        assert len(engines) == 1 and panel.controller.reporting_selection() is effective
        report = panel.controller.reporting_snapshot()
        assert (
            report is not None
            and report.acknowledged_count == 2
            and report.failed_count == 0
        )


@pytest.mark.parametrize("enabled", [True, False])
def test_normal_startup_restores_saved_web_enablement_and_interval(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    services: list[ProductionSnapshotService] = []

    class ObservedService(ProductionSnapshotService):
        def start(self) -> None:
            services.append(self)
            super().start()

    monkeypatch.setattr(startup_module, "ProductionSnapshotService", ObservedService)

    def observe(runtime: WebRuntime) -> None:
        assert runtime.snapshot().state is (
            WebState.RUNNING if enabled else WebState.STOPPED
        )
        assert len(services) == int(enabled)
        if services:
            service = services[0]
            assert service.http_alive and service.publisher_alive
            until(lambda: service.publisher.latest() is not None)
            connection = HTTPConnection(service.host, service.port, timeout=2)
            try:
                connection.request("GET", "/api/snapshot")
                response = connection.getresponse()
                assert response.status == 200
                metadata = json.loads(response.read())
            finally:
                connection.close()
            assert metadata["refresh_interval_ms"] == 500
            assert metadata["publisher_id"] == service.publisher.publisher_id
            publication = service.publisher.latest()
            assert publication is not None
            assert json.loads(publication.metadata)["refresh_interval_ms"] == 500

    with socket.socket() as available:
        available.bind(("127.0.0.1", 0))
        port = available.getsockname()[1]
    save_communication_preferences(
        ApplicationData.at_entry(tmp_path).communication,
        CommunicationPreferences(web=WebPreferences(enabled=enabled, port=port)),
    )
    run_startup(qapp, tmp_path, monkeypatch, observe)
    assert len(services) == int(enabled)
    if services:
        service = services[0]
        assert service.interval == 0.5
        assert service.stopping.is_set() and service.join(0)
        assert service.publisher.latest() is None
        assert not service.http_alive and not service.publisher_alive
        with socket.socket() as probe:
            assert probe.connect_ex(("127.0.0.1", port)) != 0
    assert "Read-only Production snapshots:" not in capsys.readouterr().err


def test_missing_communication_starts_quietly_without_web_or_file_creation(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data = saved_data(tmp_path)
    files = {path: path.read_bytes() for path in data.root.rglob("*.json")}

    def forbidden(*_args: object, **_kwargs: object) -> ProductionSnapshotService:
        pytest.fail("Missing settings must not create a Web service")

    monkeypatch.setattr(startup_module, "ProductionSnapshotService", forbidden)

    def observe(runtime: WebRuntime) -> None:
        assert runtime.snapshot().state is WebState.STOPPED
        assert not runtime.snapshot().owned and runtime.snapshot().configuration is None

    run_startup(qapp, tmp_path, monkeypatch, observe)
    assert not data.communication.exists()
    assert {path: path.read_bytes() for path in files} == files
    captured = capsys.readouterr()
    assert not captured.out and not captured.err


@pytest.mark.parametrize(
    "enabled,interval_ms,expected_interval",
    [(True, 500, 0.5), (False, 500, None), (True, 2000, 2.0), (True, 1000, 1.0)],
)
def test_saved_web_interval_and_publication_metadata(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
    interval_ms: int,
    expected_interval: float | None,
) -> None:
    services: list[ProductionSnapshotService] = []

    class InlineService(ProductionSnapshotService):
        """Real constructor/publisher; no HTTP socket or publisher threads."""

        active = False

        def start(self) -> None:
            services.append(self)
            self.publisher.publish()
            self.active = True

        @property
        def http_alive(self) -> bool:
            return self.active

        @property
        def publisher_alive(self) -> bool:
            return self.active

        def request_stop(self) -> None:
            super().request_stop()
            self.active = False

    monkeypatch.setattr(startup_module, "ProductionSnapshotService", InlineService)
    settings = WebPreferences(enabled=enabled, interval_ms=interval_ms)
    path = ApplicationData.at_entry(tmp_path).communication
    save_communication_preferences(path, CommunicationPreferences(web=settings))
    saved = path.read_bytes()

    def observe(runtime: WebRuntime) -> None:
        snapshot = runtime.snapshot()
        assert snapshot.state is (WebState.RUNNING if enabled else WebState.STOPPED)
        assert snapshot.configuration == (settings if enabled else None)
        assert snapshot.owned is enabled
        assert len(services) == int(expected_interval is not None)
        if services:
            service = services[0]
            assert service.interval == expected_interval
            assert service.publisher.refresh_interval_ms == interval_ms
            publication = service.publisher.latest()
            assert publication is not None
            assert (
                json.loads(publication.metadata)["refresh_interval_ms"] == interval_ms
            )

    run_startup(qapp, tmp_path, monkeypatch, observe)
    assert path.read_bytes() == saved
    for service in services:
        assert service.stopping.is_set() and service.join(0)
        assert not service.http_alive and not service.publisher_alive


def test_occupied_web_port_does_not_prevent_normal_startup(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    window = StartupWindow()

    def existing_qt(_args: object) -> QApplication:
        return qapp

    def existing_window(*_args: object) -> QWidget:
        return window

    monkeypatch.setattr(bootstrap, "QApplication", existing_qt)
    monkeypatch.setattr(bootstrap, "create_window", existing_window)

    def observed_failure() -> int:
        assert window.isVisible()
        assert window.isMaximized()
        assert window.runtime is not None
        until(
            lambda: (
                window.runtime.snapshot().state is WebState.FAILED
                if window.runtime
                else False
            )
        )
        return 0

    monkeypatch.setattr(qapp, "exec", observed_failure)
    try:
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            port = occupied.getsockname()[1]
            save_communication_preferences(
                ApplicationData.at_entry(tmp_path).communication,
                CommunicationPreferences(web=WebPreferences(port=port)),
            )
            assert bootstrap.main(tmp_path, []) == 0
        assert not window.isVisible()
        assert window.runtime is not None
        assert "OSError" in capsys.readouterr().err
        assert "OSError" in window.runtime.snapshot().error
    finally:
        window.close()


def test_invalid_saved_interval_recovers_without_silent_overwrite(
    tmp_path: Path,
) -> None:
    path = ApplicationData.at_entry(tmp_path).communication
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps({"web": {"interval_ms": 100}})
    path.write_text(raw, encoding="utf-8")
    startup = load_startup(ApplicationData.at_entry(tmp_path))
    try:
        assert not startup.communication.web.enabled and startup.communication_error
        assert path.read_text(encoding="utf-8") == raw
    finally:
        startup.application.close().result(5)
        assert startup.application.join(5)
