"""Fake normal Folder composition; Web is observation, never result delivery."""

import json
import socket
from collections.abc import Callable
from http.client import HTTPConnection
from pathlib import Path
from threading import enumerate as threads

import pytest
from PySide6.QtWidgets import QApplication, QWidget
from tests.integration.test_normal_replay_ui import normal_replay, replay_state
from tests.qt_support import until
from tests.reporting_support import readback, seed, server

from vimo_demo.application import ApplicationState
from vimo_demo.application.communication_preferences import (
    CommunicationPreferences,
    WebPreferences,
    save_communication_preferences,
)
from vimo_demo.application.local_replay import ReplayState
from vimo_demo.communication.reporting import ReportStage
from vimo_demo.ui import bootstrap
from vimo_demo.web import ProductionSnapshotService
from vimo_demo.web.runtime import WebRuntime, WebState


class StartupWindow(QWidget):
    """Keep the bootstrap probe small while observing the real shell handoff."""

    runtime: WebRuntime | None = None

    def bind_web_runtime(self, runtime: WebRuntime) -> None:
        self.runtime = runtime


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
                window.bridge.step_replay()  # Deterministic one-cycle source setup.
                until(lambda: replay_state(startup) is ReplayState.PAUSED)
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
        settings = page.web_panel.preferences()
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
        start = runtime.start(settings)
        until(start.done)
        assert start.result().state is WebState.RUNNING
        window.bridge.step_replay()
        until(lambda: bool(engines) and engines[0].entered.is_set())
        assert startup.application.snapshot().state is ApplicationState.RUNNING
        restart = runtime.apply(settings)
        until(restart.done)
        assert restart.result().state is WebState.RUNNING
        assert len(engines) == 1 and not engines[0].closed
        assert startup.application.snapshot().counters.cycles_completed == 0
        assert panel.controller.reporting_selection() is effective
        engines[0].release.set()
        until(lambda: replay_state(startup) is ReplayState.PAUSED)
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
        window.bridge.step_replay()
        try:
            until(
                lambda: (
                    startup.application.snapshot().counters.cycles_completed == 2
                    and replay_state(startup) is ReplayState.PAUSED
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
def test_normal_startup_web_defaults_and_interval(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
) -> None:
    services: list[ProductionSnapshotService] = []

    class ObservedService(ProductionSnapshotService):
        def start(self) -> None:
            services.append(self)
            super().start()

    monkeypatch.setattr(bootstrap, "ProductionSnapshotService", ObservedService)

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

    with socket.socket() as available:
        available.bind(("127.0.0.1", 0))
        port = available.getsockname()[1]
    save_communication_preferences(
        bootstrap.ApplicationData.at_entry(tmp_path).communication,
        CommunicationPreferences(web=WebPreferences(enabled=enabled, port=port)),
    )
    run_startup(qapp, tmp_path, monkeypatch, observe)
    assert len(services) == int(enabled)
    if services:
        service = services[0]
        assert service.interval == 0.5
        publication = service.publisher.latest()
        assert publication is not None
        assert json.loads(publication.metadata)["refresh_interval_ms"] == 500
        assert service.stopping.is_set() and service.join(0)
        assert not service.http_alive and not service.publisher_alive
        with socket.socket() as probe:
            assert probe.connect_ex(("127.0.0.1", port)) != 0


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

    monkeypatch.setattr(bootstrap, "ProductionSnapshotService", InlineService)
    settings = WebPreferences(enabled=enabled, interval_ms=interval_ms)
    path = bootstrap.ApplicationData.at_entry(tmp_path).communication
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
                bootstrap.ApplicationData.at_entry(tmp_path).communication,
                CommunicationPreferences(web=WebPreferences(port=port)),
            )
            assert bootstrap.main(tmp_path, []) == 0
        assert window.isVisible()
        assert window.isMaximized()
        assert window.runtime is not None
        assert "OSError" in capsys.readouterr().err
        assert "OSError" in window.runtime.snapshot().error
    finally:
        window.close()


def test_invalid_saved_interval_recovers_without_silent_overwrite(
    tmp_path: Path,
) -> None:
    path = bootstrap.ApplicationData.at_entry(tmp_path).communication
    save_communication_preferences(path, CommunicationPreferences())
    raw = path.read_text(encoding="utf-8").replace(
        '"interval_ms": 500', '"interval_ms": 100'
    )
    path.write_text(raw, encoding="utf-8")
    startup = bootstrap.load_startup(bootstrap.ApplicationData.at_entry(tmp_path))
    try:
        assert not startup.communication.web.enabled and startup.communication_error
        assert path.read_text(encoding="utf-8") == raw
    finally:
        startup.application.close().result(5)
        assert startup.application.join(5)
