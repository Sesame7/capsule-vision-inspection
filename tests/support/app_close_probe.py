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

from capsule_vision.application.session import EngineFactory, real_engine
from capsule_vision.application.startup import ApplicationData, Startup, load_startup
from capsule_vision.communication.operations import RequestKind
from capsule_vision.communication.simulator import FinsUdpSimulator
from capsule_vision.ui import bootstrap
from capsule_vision.ui.future_delivery import FutureDelivery
from capsule_vision.ui.main_window import MainWindow


def install() -> None:
    """Installed by a temporary sitecustomize, never by the production launcher."""
    root = Path(os.environ["APP_CLOSE_PROBE"])
    scenario = os.environ["APP_CLOSE_SCENARIO"]
    output = (root / "boundaries.jsonl").open("w", encoding="utf-8", buffering=1)
    # Windows may report handled first-chance COM exceptions during maximize.
    # Preserve stacks separately so diagnostics never corrupt structured boundaries.
    stacks = (root / "stacks.log").open("w", encoding="utf-8", buffering=1)
    faulthandler.enable(stacks)

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
    original_load = load_startup
    original_create = bootstrap.create_window
    original_close = MainWindow.closeEvent

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

    def close_event(self: MainWindow, event: QCloseEvent) -> None:
        record("close_enter")
        original_close(self, event)
        record(
            "close_return",
            accepted=event.isAccepted(),
            application_closed=self.bridge.closed,
            communication_closed=self.communication.closed,
        )

    def create(startup: Startup, delivery: FutureDelivery) -> MainWindow:
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
                assert "Timeout" in window.communication.feedback_error
            else:
                observed = window.communication.last_read
                assert observed is not None and observed.values == (1234,)
            record("request_drained", scenario=scenario)
            if scenario == "switch":
                window.navigate(0)
            native_close()

        monitor = QTimer(window)
        monitor.setInterval(5)
        monitor.timeout.connect(after_request)

        def exercise() -> None:
            nonlocal peer, simulator
            assert window.isMaximized()
            record("window_shown", maximized=window.isMaximized())
            if scenario != "unused":
                window.navigate(2)
            if scenario in ("success", "timeout", "pending", "switch"):
                simulator = FinsUdpSimulator(port=0)
                window.communication.port.setValue(simulator.port)
                window.communication.persist_settings()
                window.communication.timeout.setValue(0.15)
                window.communication.persist_settings()
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
            faulthandler.dump_traceback(file=stacks, all_threads=True)

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

    startup_patch = patch.object(bootstrap, "load_startup", load)
    startup_patch.start()
    atexit.register(startup_patch.stop)
    bootstrap.create_window = create
    application_class_patch = patch.object(
        bootstrap, "QApplication", ObservedApplication
    )
    application_class_patch.start()
    atexit.register(application_class_patch.stop)
    MainWindow.closeEvent = close_event
