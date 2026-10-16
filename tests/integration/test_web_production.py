"""Fake normal Folder composition; Web is observation, never result delivery."""

import json
import socket
from pathlib import Path
from threading import enumerate as threads

import pytest
from PySide6.QtWidgets import QApplication, QWidget
from tests.integration.test_normal_replay_ui import normal_replay, replay_state
from tests.qt_support import until

from vimo_demo.application import ApplicationState
from vimo_demo.application.local_replay import ReplayState
from vimo_demo.ui import bootstrap
from vimo_demo.web import ProductionSnapshotService


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
                assert window.buttons["secondary"].isEnabled()
                window.buttons["secondary"].click()
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


@pytest.mark.parametrize(
    "arguments,expected_interval",
    [
        ([], 0.5),
        (["--web-snapshots"], 0.5),
        (["--no-web-snapshots"], None),
        (["--web-interval-ms", "2000"], 2.0),
        (["--web-interval-ms", "1000"], 1.0),
        (["--web-interval-ms", "500"], 0.5),
    ],
)
def test_normal_startup_web_defaults_and_interval(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
    expected_interval: float | None,
) -> None:
    services: list[ProductionSnapshotService] = []

    class ObservedService(ProductionSnapshotService):
        def start(self) -> None:
            services.append(self)
            super().start()

    window = QWidget()
    monkeypatch.setattr(bootstrap, "ProductionSnapshotService", ObservedService)

    def existing_qt(_args: object) -> QApplication:
        return qapp

    def existing_window(*_args: object) -> QWidget:
        return window

    def observe_before_close() -> int:
        if services:
            until(lambda: services[0].publisher.latest() is not None)
        return 0

    monkeypatch.setattr(bootstrap, "QApplication", existing_qt)
    monkeypatch.setattr(bootstrap, "create_window", existing_window)
    monkeypatch.setattr(qapp, "exec", observe_before_close)
    with socket.socket() as available:
        available.bind(("127.0.0.1", 0))
        port = available.getsockname()[1]
    try:
        assert bootstrap.main(tmp_path, [*arguments, "--web-port", str(port)]) == 0
        assert len(services) == int(expected_interval is not None)
        if services:
            service = services[0]
            assert service.interval == expected_interval
            publication = service.publisher.latest()
            assert publication is not None
            assert json.loads(publication.metadata)["refresh_interval_ms"] == (
                service.interval * 1000
            )
            assert service.stopping.is_set() and service.join(0)
            with socket.socket() as probe:
                assert probe.connect_ex(("127.0.0.1", port)) != 0
    finally:
        window.close()


def test_occupied_web_port_does_not_prevent_normal_startup(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    window = QWidget()

    def existing_qt(_args: object) -> QApplication:
        return qapp

    def existing_window(*_args: object) -> QWidget:
        return window

    monkeypatch.setattr(bootstrap, "QApplication", existing_qt)
    monkeypatch.setattr(bootstrap, "create_window", existing_window)
    monkeypatch.setattr(qapp, "exec", lambda: 0)
    try:
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            port = occupied.getsockname()[1]
            assert bootstrap.main(tmp_path, ["--web-port", str(port)]) == 0
        assert window.isVisible()
        assert "OSError" in capsys.readouterr().err
    finally:
        window.close()


def test_invalid_refresh_interval_rejected_before_startup(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as error:
        bootstrap.main(tmp_path, ["--web-interval-ms", "100"])
    assert error.value.code == 2
