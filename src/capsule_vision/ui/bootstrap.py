"""Persistent real-inference entry composition; disk preparation precedes Qt."""

import argparse
import sys
import traceback
from collections.abc import Sequence
from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QLoggingCategory, QObject, Slot
from PySide6.QtWidgets import QApplication

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
    parser.parse_args(argv)
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
        # Keep Qt's native handler and external logging-rule precedence unchanged.
        QLoggingCategory.setFilterRules("qt.text.font.db.info=false")
        qt = QApplication(sys.argv[:1])
        delivery = FutureDelivery()
        web = startup.communication.web

        runtime = create_web_runtime(startup)
        window = create_window(startup, delivery)
        window.bind_web_runtime(runtime)
        if web.enabled:

            def started(_future: Future[WebRuntimeSnapshot]) -> None:
                # This callback runs on the control thread, never mutates widgets.
                assert runtime is not None
                observation = runtime.snapshot()
                if observation.error:
                    print(observation.error, file=sys.stderr)

            runtime.start(web).add_done_callback(started)
        window.showMaximized()
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
