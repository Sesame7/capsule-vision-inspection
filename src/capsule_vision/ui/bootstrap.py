"""Persistent real-inference entry composition; disk preparation precedes Qt."""

import argparse
import re
import sys
import traceback
from collections.abc import Sequence
from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QLoggingCategory, QObject, QSize, Slot
from PySide6.QtWidgets import QApplication, QWidget

from capsule_vision.application import PreparedConfiguration
from capsule_vision.application.startup import (
    ApplicationData,
    Startup,
    create_web_runtime,
    load_startup,
)
from capsule_vision.configuration import (
    ConfigurationError,
)
from capsule_vision.configuration.preferences import (
    ApplicationPreferences,
    read_preferences,
    save_preferences,
)
from capsule_vision.localization.languages import is_language
from capsule_vision.web.runtime import WebRuntime, WebRuntimeSnapshot

from .future_delivery import FutureDelivery
from .main_window import MainWindow
from .production_bridge import QtProductionBridge
from .source_notifications import SourceNotifications
from .style import Style


class _Arguments(argparse.Namespace):
    window_size: tuple[int, int] | None = None


def parse_window_size(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"([0-9]{1,8})[xX]([0-9]{1,8})", value)
    if match is None:
        raise argparse.ArgumentTypeError("expected WIDTHxHEIGHT in Qt logical pixels")
    width, height = int(match[1]), int(match[2])
    # QWidget's maximum dimension; reject overflows before creating any owners.
    if not (
        Style.minimum[0] <= width <= 16_777_215
        and Style.minimum[1] <= height <= 16_777_215
    ):
        raise argparse.ArgumentTypeError(
            f"minimum window size is {Style.minimum[0]}x{Style.minimum[1]}; "
            "each dimension must be at most 16777215 Qt logical pixels"
        )
    return width, height


def show_sized_window(window: QWidget, size: tuple[int, int]) -> None:
    requested = QSize(*size)
    minimum = window.minimumSize()
    if requested.width() < minimum.width() or requested.height() < minimum.height():
        raise ValueError(
            f"requested {size[0]}x{size[1]} is below the current window minimum "
            f"{minimum.width()}x{minimum.height()} Qt logical pixels"
        )
    maximum = window.maximumSize()
    if requested.width() > maximum.width() or requested.height() > maximum.height():
        raise ValueError(
            f"cannot apply requested {size[0]}x{size[1]} Qt logical pixels; "
            f"current window maximum is {maximum.width()}x{maximum.height()}"
        )
    window.resize(requested)
    window.show()
    handle = window.windowHandle()
    native_minimum = handle.minimumSize()
    # Windows can clamp the native client to desktop limits while QWidget still
    # reports the requested size. Supply native minimum bounds for this resize
    # only, preserving QWidget constraints and the native maximize action.
    try:
        handle.setMinimumSize(requested)
        window.resize(requested)
        if window.size() != requested:
            raise ValueError(
                f"cannot apply requested {size[0]}x{size[1]} Qt logical pixels; "
                f"current window constraints require {window.width()}x{window.height()}"
            )
        frame = window.frameGeometry()
        available = window.screen().availableGeometry()
        window.move(
            available.left() + max(0, (available.width() - frame.width()) // 2),
            available.top() + max(0, (available.height() - frame.height()) // 2),
        )
    finally:
        handle.setMinimumSize(native_minimum)


class PreferenceNotifications(QObject):
    """Reuse existing successful Apply/language signals, not a selection manager."""

    def __init__(
        self,
        window: MainWindow,
        path: Path,
        preferences: ApplicationPreferences,
        *,
        writable: bool = True,
    ) -> None:
        super().__init__(window)
        self.window, self.path, self.preferences = window, path, preferences
        self.writable = writable
        window.editing.applied.connect(self.applied)
        window.language_changed.connect(self.language)

    def _save(self, value: ApplicationPreferences) -> None:
        try:
            if not self.writable:
                raise ConfigurationError(
                    "Repair app.json externally and restart before saving preferences"
                )
            try:
                current = read_preferences(self.path)
            except ConfigurationError as exc:
                if not isinstance(exc.__cause__, FileNotFoundError):
                    raise
            else:
                if current.invalid_language or current.preferences != self.preferences:
                    raise ConfigurationError(
                        "Application preferences changed/invalid; repair and restart"
                    )
            save_preferences(self.path, value)
        except ConfigurationError as exc:
            traceback.print_exception(exc)
            self.window.bridge.command_failed.emit("preferences", exc)
        else:
            self.preferences = value
            self.window.bridge.command_finished.emit("preferences")

    @Slot(object)
    def applied(self, prepared: object) -> None:
        if isinstance(prepared, PreparedConfiguration):
            self._save(
                replace(
                    self.preferences,
                    last_applied_recipe_id=prepared.configuration.recipe_id,
                )
            )

    @Slot(str)
    def language(self, value: str) -> None:
        if is_language(value):
            self._save(replace(self.preferences, language=value))


def create_window(startup: Startup, delivery: FutureDelivery) -> MainWindow:
    bridge = QtProductionBridge(startup.application, startup.frames)
    SourceNotifications(bridge, delivery)
    window = MainWindow(
        bridge,
        language=startup.preferences.language if startup.preferences else "zh",
        recipe_editor=startup.editor,
        layout=startup.machine.layout if startup.machine else None,
        configuration_issue="\n".join(i.detail for i in startup.issues),
        configuration_messages=tuple(dict.fromkeys(i.key for i in startup.issues)),
        configuration_notices=tuple((i.key, i.detail) for i in startup.issues),
    )
    window.communication.configure_preferences(
        startup.data.communication, startup.communication, startup.communication_error
    )
    PreferenceNotifications(
        window,
        startup.data.preferences,
        startup.preferences or ApplicationPreferences(),
        writable=startup.preferences_writable,
    )
    return window


def main(entry_directory: Path, argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Capsule Vision Inspection with persisted configuration"
    )
    parser.add_argument(
        "--window-size",
        type=parse_window_size,
        metavar="WIDTHxHEIGHT",
        help="show a normal window with this logical client size (UI validation; not persisted)",
    )
    args = parser.parse_args(argv, namespace=_Arguments())
    delivery: FutureDelivery | None = None

    def source_finished(future: Future[None]) -> None:
        assert delivery is not None
        delivery.watch("folder_source", future)

    # Complete startup artifact IO before Qt creation.
    startup = load_startup(
        ApplicationData.at_entry(entry_directory),
        observe_source=source_finished,
    )
    runtime: WebRuntime | None = None
    window: MainWindow | None = None
    try:
        for issue in startup.issues:
            print(issue.detail, file=sys.stderr)
        if startup.security_error:
            print(startup.security_error, file=sys.stderr)
        # Keep Qt's native handler and external logging-rule precedence unchanged.
        QLoggingCategory.setFilterRules("qt.text.font.db.info=false")
        qt = QApplication(sys.argv[:1])
        delivery = FutureDelivery()
        window = create_window(startup, delivery)
        runtime = create_web_runtime(startup, restore_saved=True)
        window.bind_web_runtime(runtime)
        if runtime.restoration is not None:

            def started(_future: Future[WebRuntimeSnapshot]) -> None:
                # This callback runs on the control thread, never mutates widgets.
                assert runtime is not None
                observation = runtime.snapshot()
                if observation.error:
                    print(observation.error, file=sys.stderr)

            runtime.restoration.add_done_callback(started)
        if args.window_size is None:
            window.showMaximized()
        else:
            try:
                show_sized_window(window, args.window_size)
            except ValueError as exc:
                parser.error(str(exc))
        return qt.exec()
    finally:
        try:
            if window is not None:
                window.finish_close()
        finally:
            if runtime is not None:
                runtime.request_close()
            # Outside Qt dispatch, retaining ownership until native/source work exits.
            try:
                startup.application.close().result()
                startup.application.join()
            finally:
                if runtime is not None:
                    runtime.join()
                if delivery is not None:
                    delivery.detach()
