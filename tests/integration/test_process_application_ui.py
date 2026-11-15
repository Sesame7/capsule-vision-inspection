"""Queued process-application feedback, saved Recipe isolation and actual Close."""

from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication
from tests.integration.test_process_parameters_ui import process_window
from tests.integration.test_process_preview import saved_target
from tests.support.fins_tcp import TcpPeer
from tests.support.qt import until

from capsule_vision.application import ApplicationStateError
from capsule_vision.application.audit import AuditQuery
from capsule_vision.configuration import load_recipe
from capsule_vision.localization.catalog import text


def test_selected_save_has_no_plc_io_and_reselection_blocks_start_until_verified(
    qapp: QApplication, tmp_path: Path
) -> None:
    with TcpPeer() as plc, process_window(tmp_path) as (startup, window):
        app, session, identity = (
            startup.application,
            startup.editor,
            startup.application.identity,
        )
        assert session and session.entry and identity
        saved_target(startup.data.root, plc.port)
        before = app.snapshot()
        plc.hold_next("read", start=102)
        try:
            session.draft.process_parameters["running_speed"] = "73"
            window.recipe_page.save()
            until(lambda: not window.editing.busy)
            saved = app.snapshot()
            assert saved.candidate_prepared is before.candidate_prepared
            assert saved.candidate_requires_preparation and not saved.process_applying
            assert not plc.connections and not plc.packets
            assert not plc.entered.is_set()
            assert window.recipe_page.status.text() == text("saved", window.language)
            with pytest.raises(ApplicationStateError, match="re-preparation"):
                app.start()
            selections = identity.audit_page(AuditQuery(action="production.recipe"))
            until(selections.done)
            assert not selections.result()
            application = identity.audit_page(AuditQuery(action="recipe.process_apply"))
            until(application.done)
            assert not application.result()

            window.navigate(0)
            picker = window.production_recipes
            picker.recipes.activated.emit(picker.recipes.currentIndex())
            until(plc.entered.is_set)
            window.bridge.refresh()
            qapp.processEvents()
            assert window.editing.busy and app.snapshot().process_required
            assert not window.buttons["operation"].isEnabled()
            assert window.buttons["operation"].accessibleDescription() == text(
                "process_applying", window.language
            )
            with pytest.raises(ApplicationStateError, match="readback"):
                app.start()
            assert (
                load_recipe(session.entry.path).process_parameters
                == session.entry.recipe.process_parameters
            )
            assert not session.dirty
            plc.release.set()
            until(lambda: not window.editing.busy)
            window.bridge.refresh()
            qapp.processEvents()
            assert not app.snapshot().process_required
            assert window.buttons["operation"].isEnabled()
            assert plc.words(102, 2, 0x82) == (73, 0)
            selections = identity.audit_page(AuditQuery(action="production.recipe"))
            until(selections.done)
            (selection,) = selections.result()
            assert selection.outcome == "success"
            application = identity.audit_page(AuditQuery(action="recipe.process_apply"))
            until(application.done)
            assert len(application.result()) == 1
            assert plc.connections == 1 and len(plc.packets) == 5
            packets = plc.packets
            window.navigate(1)
            window.recipe_page.save()  # Unchanged Save is inert.
            qapp.processEvents()
            assert not window.editing.busy and plc.packets == packets
        finally:
            plc.release.set()


def test_failure_preserves_save_then_same_id_selection_recovers(
    qapp: QApplication, tmp_path: Path
) -> None:
    with TcpPeer() as plc, process_window(tmp_path) as (startup, window):
        app, session = startup.application, startup.editor
        assert session and session.entry
        saved_target(startup.data.root, plc.port)
        plc.set_words(16, (2,))
        session.draft.process_parameters["running_speed"] = "74"
        window.recipe_page.save()
        until(lambda: not window.editing.busy)
        assert not session.dirty and app.snapshot().candidate_requires_preparation
        assert window.recipe_page.status.text() == text("saved", window.language)
        assert not plc.connections and not plc.packets
        window.navigate(0)
        picker = window.production_recipes
        picker.select(picker.recipes.findData("local"))
        until(lambda: not window.editing.busy)
        window.bridge.refresh()
        qapp.processEvents()
        assert app.snapshot().process_required
        assert picker.feedback and picker.feedback[0] == "process_apply_failed"
        assert "Machine running" in picker.recipes.toolTip()
        assert "Machine running" not in window.recipe_page.status.toolTip()
        assert not window.buttons["operation"].isEnabled()
        assert picker.recipes.currentData() == "local"
        plc.set_words(16, (0,))
        picker.select(picker.recipes.findData("local"))
        until(lambda: not window.editing.busy)
        assert not app.snapshot().process_required and app.snapshot().last_issue is None
        assert plc.words(102, 2, 0x82) == (74, 0)


def test_close_waits_for_admitted_io_and_cancels_remaining_pairs(
    qapp: QApplication, tmp_path: Path
) -> None:
    with TcpPeer() as plc, process_window(tmp_path) as (startup, window):
        session = startup.editor
        assert session
        saved_target(startup.data.root, plc.port)
        plc.hold_next("read", start=102)
        try:
            session.draft.process_parameters["running_speed"] = "75"
            window.recipe_page.save()
            until(lambda: not window.editing.busy)
            assert not plc.connections and not plc.packets
            window.navigate(0)
            picker = window.production_recipes
            picker.select(picker.recipes.findData("local"))
            until(plc.entered.is_set)
            window.close()
            assert not window.bridge.closed and window.editing.busy
            plc.release.set()
            until(lambda: window.bridge.closed and window.communication.closed)
            assert len(plc.packets) == 3
            assert startup.application.snapshot().process_required
        finally:
            plc.release.set()
