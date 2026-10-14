"""Subprocess-only observer for normal entry/Qt exit, using isolated test data."""

import atexit
import ctypes
import faulthandler
import json
import os
import sys
from collections.abc import Callable
from concurrent.futures import Future
from ctypes import wintypes
from pathlib import Path
from threading import Event, Thread
from threading import enumerate as threads
from unittest.mock import patch

from PySide6.QtCore import QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication

from vimo_demo.application.session import EngineFactory
from vimo_demo.communication.operations import RequestKind
from vimo_demo.communication.simulator import FinsUdpSimulator
from vimo_demo.ui import bootstrap
from vimo_demo.ui.bootstrap import ApplicationData, Startup
from vimo_demo.ui.inference_bridge import FutureDelivery
from vimo_demo.ui.local_production import real_engine
from vimo_demo.ui.production_window import ProductionWindow


def install() -> None:
    """Installed by a temporary sitecustomize, never by the production launcher."""
    root = Path(os.environ["APP_CLOSE_PROBE"])
    scenario = os.environ["APP_CLOSE_SCENARIO"]
    output = (root / "boundaries.jsonl").open("w", encoding="utf-8", buffering=1)
    faulthandler.enable(output)

    def record(stage: str, **fields: object) -> None:
        output.write(
            json.dumps(
                {
                    "stage": stage,
                    "pid": os.getpid(),
                    "ppid": os.getppid(),
                    "threads": [t.name for t in threads() if not t.daemon],
                    **fields,
                }
            )
            + "\n"
        )

    base_executable: object = getattr(sys, "_base_executable", None)
    record("startup", executable=sys.executable, base_executable=base_executable)
    atexit.register(lambda: record("atexit"))
    original_load = bootstrap.load_startup
    original_create = bootstrap.create_window
    original_close = ProductionWindow.closeEvent

    def load(
        _data: ApplicationData,
        *,
        engine_factory: EngineFactory = real_engine,
        observe_source: Callable[[Future[None]], None] = lambda _: None,
    ) -> Startup:
        # Missing test data gives the real stopped UNCONFIGURED shell, no GPU.
        return original_load(
            ApplicationData.at_entry(root),
            engine_factory=engine_factory,
            observe_source=observe_source,
        )

    def close_event(self: ProductionWindow, event: QCloseEvent) -> None:
        record("close_enter")
        original_close(self, event)
        record(
            "close_return",
            accepted=event.isAccepted(),
            application_closed=self.bridge.closed,
            communication_closed=self.communication.closed,
        )

    def create(startup: Startup, delivery: FutureDelivery) -> ProductionWindow:
        window = original_create(startup, delivery)
        qt = QApplication.instance()
        assert isinstance(qt, QApplication)
        stop = Event()
        peer: Thread | None = None
        simulator: FinsUdpSimulator | None = None

        def native_close() -> None:
            # Exercise WM_CLOSE, not QWidget.close(): the latter masks reentrancy.
            user = ctypes.WinDLL("user32", use_last_error=True)
            post = user.PostMessageW
            post.argtypes = [
                wintypes.HWND,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPARAM,
            ]
            post.restype = wintypes.BOOL
            assert post(int(window.winId()), 0x0010, 0, 0)
            record("native_close", busy=window.communication.busy)

        def after_request() -> None:
            if window.communication.busy:
                return
            monitor.stop()
            if scenario == "timeout":
                assert "Timeout" in window.communication.status.toolTip()
            else:
                item = window.communication.values_table.item(0, 1)
                assert item is not None and item.text() == "1234"
            record("request_drained", scenario=scenario)
            if scenario == "switch":
                window.navigate(0)
            native_close()

        monitor = QTimer(window)
        monitor.setInterval(5)
        monitor.timeout.connect(after_request)

        def exercise() -> None:
            nonlocal peer, simulator
            if scenario != "unused":
                window.navigate(2)
            if scenario in ("success", "timeout", "pending", "switch"):
                simulator = FinsUdpSimulator(port=0)
                window.communication.port.setValue(simulator.port)
                window.communication.timeout.setValue(0.15)
                if scenario in ("success", "switch"):
                    peer = Thread(
                        target=lambda: simulator.serve(stop) if simulator else None,
                        name="AppCloseTestSimulator",
                    )
                    peer.start()
                assert window.communication.dispatch(RequestKind.READ)
                if scenario != "pending":
                    monitor.start()
                    return
            native_close()

        def last_window() -> None:
            record(
                "last_window_closed",
                application_closed=window.bridge.closed,
                communication_closed=window.communication.closed,
            )

        def diagnostics() -> None:
            record(
                "watchdog",
                visible=window.isVisible(),
                application_closed=window.bridge.closed,
                communication_closed=window.communication.closed,
            )
            faulthandler.dump_traceback(file=output, all_threads=True)

        def cleanup_peer() -> None:
            stop.set()
            if peer is not None:
                peer.join(2)
                assert not peer.is_alive()
            if simulator is not None:
                simulator.close()
            record("about_to_quit")

        watchdog = QTimer(window)
        watchdog.setSingleShot(True)
        watchdog.timeout.connect(diagnostics)
        watchdog.start(5000)
        qt.lastWindowClosed.connect(last_window)
        qt.aboutToQuit.connect(cleanup_peer)
        QTimer.singleShot(0, exercise)

        # These wrappers only observe actual application cleanup and fallback.
        original_application_close = startup.application.close
        original_join = startup.application.join

        def close_application() -> Future[None]:
            record("application_close")
            return original_application_close()

        def join(timeout: float | None = None) -> bool:
            result = original_join(timeout)
            if result:
                record("application_joined")
            return result

        startup.application.close = close_application
        startup.application.join = join
        return window

    class ObservedApplication(QApplication):
        @staticmethod
        def exec() -> int:
            record("exec_enter")
            code = QApplication.exec()
            record("exec_return", code=code)
            return code

    bootstrap.load_startup = load
    bootstrap.create_window = create
    application_class_patch = patch.object(
        bootstrap, "QApplication", ObservedApplication
    )
    application_class_patch.start()
    atexit.register(application_class_patch.stop)
    ProductionWindow.closeEvent = close_event
