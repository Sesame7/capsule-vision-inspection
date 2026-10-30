"""Peer workspace dispatch and actual setup-thread settlement."""

from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import replace
from threading import Event, Thread, get_ident

import pytest
from PySide6.QtWidgets import QApplication
from tests.support.application import ControlledEngine
from tests.support.qt import until
from tests.support.recipe_ui import Screen
from tests.support.recipe_ui import camera_screen as camera_screen
from tests.support.recipe_ui import configured_screen as configured_screen

from capsule_vision.application import PreparedConfiguration
from capsule_vision.configuration import (
    ConfigurationError,
    load_camera_sources,
    load_machine_layout,
)
from capsule_vision.inference import BoundingRect
from capsule_vision.ui.future_delivery import FutureDelivery
from capsule_vision.ui.main_window import MainWindow
from capsule_vision.ui.vision_page import VisionPage


def test_vision_uses_shared_drafts_and_dispatch_without_recipe_backend_callbacks(
    camera_screen: tuple[MainWindow, VisionPage, ControlledEngine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, vision, engine = camera_screen
    editing, recipe = window.editing, window.recipe_page
    assert editing.session is not None
    assert editing.session is recipe.session is vision.session
    assert vision.editing is editing
    assert vision.draft is editing.session.machine_editor
    assert recipe.model_controls.editing is editing
    assert vision.parentWidget() is window.vision_page
    assert not hasattr(recipe, "roi_workspace") and not hasattr(recipe, "settings")
    assert not hasattr(recipe, "run_setup_io") and not hasattr(recipe, "_thread")

    def retired(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Vision reached through Recipe for backend work")

    monkeypatch.setattr(recipe, "save_and_apply", retired)
    monkeypatch.setattr(recipe, "reload", retired)
    before = window.bridge.application.snapshot()
    old = vision.draft.camera(vision.camera).slots[0].rect
    vision.set_rect(BoundingRect(old.x + 1, old.y, old.width - 1, old.height))
    assert vision.sources is not None
    vision.loop.setChecked(not vision.loop.isChecked())
    vision.buttons["save"].click()
    until(lambda: not editing.busy)
    assert not vision.vision_dirty and vision.draft.path is not None
    assert load_machine_layout(vision.draft.path) == vision.draft.definition
    assert load_camera_sources(vision.sources.path) == vision.sources.definition
    assert window.bridge.application.snapshot() == before
    applied: list[object] = []
    editing.applied.connect(applied.append)
    vision.buttons["save_apply"].click()
    until(lambda: bool(applied) and not editing.busy)
    assert isinstance(applied[0], PreparedConfiguration)
    assert applied[0].configuration.layout == vision.draft.definition.layout
    assert not engine.calls


def test_setup_future_delivery_waits_for_actual_thread_exit_and_ignores_old_tokens(
    configured_screen: Screen,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, _session, _runtimes = configured_screen
    editing = window.editing
    reached_exit, release_exit = Event(), Event()
    workers: list[Thread] = []
    callbacks: list[tuple[object, BaseException | None, int]] = []
    drained: list[bool] = []

    def held_thread(*, target: Callable[[], None], name: str, daemon: bool) -> Thread:
        def run() -> None:
            target()  # Future already terminal and its queued delivery announced.
            reached_exit.set()
            assert release_exit.wait(5)

        thread = Thread(target=run, name=name, daemon=daemon)
        workers.append(thread)
        return thread

    monkeypatch.setattr("capsule_vision.ui.editing.Thread", held_thread)
    deliveries: list[tuple[FutureDelivery, str]] = []
    original_watch = FutureDelivery.watch

    def watch(delivery: FutureDelivery, token: str, future: Future[object]) -> None:
        deliveries.append((delivery, token))
        original_watch(delivery, token, future)

    monkeypatch.setattr(FutureDelivery, "watch", watch)
    owner = get_ident()

    def completed(value: object, error: BaseException | None) -> None:
        assert not workers[-1].is_alive()
        callbacks.append((value, error, get_ident()))

    assert editing.run_io(lambda: "first", completed)
    try:
        until(reached_exit.is_set)
        QApplication.processEvents()
        assert editing.busy and not callbacks
        assert not editing.run_io(lambda: "duplicate", completed)
    finally:
        release_exit.set()
    until(lambda: not editing.busy)
    assert callbacks == [("first", None, owner)]
    reached_exit.clear()
    release_exit.clear()
    assert editing.run_io(lambda: "second", completed)
    try:
        until(reached_exit.is_set)
        old_delivery, old_token = deliveries[0]
        old_delivery.emitter.completed.emit(old_token, "stale", None)
        editing.drain(lambda: drained.append(True))
        QApplication.processEvents()
        assert not drained and len(callbacks) == 1
        assert workers[-1].is_alive() and not editing.editable()
    finally:
        release_exit.set()
    until(lambda: bool(drained))
    assert callbacks[-1][0] == "second"
    assert isinstance(callbacks[-1][1], RuntimeError)
    assert callbacks[-1][2] == owner
    assert all(not thread.is_alive() for thread in workers)
    editing.resume()


def test_prepared_result_cannot_cross_recipe_selection_or_close(
    configured_screen: Screen,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, session, _runtimes = configured_screen
    assert session.entry is not None
    prepared = session.prepare(session.entry.recipe)
    before = window.bridge.application.snapshot()
    entered, release = Event(), Event()
    errors: list[BaseException | None] = []

    def blocked(_recipe: object):
        entered.set()
        assert release.wait(5)
        return prepared

    monkeypatch.setattr(session, "prepare", blocked)
    window.editing.apply(errors.append)
    try:
        until(entered.is_set)
        session.entry = replace(session.entry)
    finally:
        release.set()
    until(lambda: not window.editing.busy)
    assert len(errors) == 1 and isinstance(errors[0], ConfigurationError)
    assert window.bridge.application.snapshot() == before
    entered.clear()
    release.clear()
    errors.clear()
    window.editing.apply(errors.append)
    closed: list[bool] = []
    try:
        until(entered.is_set)
        window.request_editing_close(lambda: closed.append(True))
        assert not closed
    finally:
        release.set()
    until(lambda: bool(closed))
    assert len(errors) == 1 and isinstance(errors[0], ConfigurationError)
    assert not window.editing.busy and not window.recipe_page.labels_loading
    assert window.bridge.application.snapshot() == before
