"""Fake normal Folder composition; Web is observation, never result delivery."""

import json
from pathlib import Path
from threading import enumerate as threads

from PySide6.QtWidgets import QApplication
from tests.integration.test_normal_replay_ui import normal_replay, replay_state
from tests.qt_support import until

from vimo_demo.application import ApplicationState
from vimo_demo.application.local_replay import ReplayState
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
