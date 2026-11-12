"""Queued process-application feedback, saved Recipe isolation and actual Close."""

from pathlib import Path

from PySide6.QtWidgets import QApplication
from tests.integration.test_process_parameters_ui import process_window
from tests.integration.test_process_preview import saved_target
from tests.support.fins_tcp import TcpPeer
from tests.support.qt import until

from capsule_vision.application.audit import AuditQuery
from capsule_vision.configuration import load_recipe
from capsule_vision.localization.catalog import text


def test_selected_save_applies_without_selection_audit_and_blocks_start_until_verified(
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
        plc.hold_next("read", start=102)
        try:
            session.draft.process_parameters["running_speed"] = "73"
            window.recipe_page.save()
            until(plc.entered.is_set)
            window.navigate(0)
            window.bridge.refresh()
            qapp.processEvents()
            assert window.editing.busy and app.snapshot().process_required
            assert not window.buttons["operation"].isEnabled()
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
            assert not selections.result()
            application = identity.audit_page(AuditQuery(action="recipe.process_apply"))
            until(application.done)
            assert len(application.result()) == 1
            packets = plc.packets
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
        assert not session.dirty and app.snapshot().process_required
        assert text("saved", window.language) in window.recipe_page.status.text()
        assert "Machine running" in window.recipe_page.status.toolTip()
        window.navigate(0)
        window.bridge.refresh()
        qapp.processEvents()
        assert not window.buttons["operation"].isEnabled()
        assert window.production_recipes.recipes.currentData() == "local"
        plc.set_words(16, (0,))
        picker = window.production_recipes
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
            until(plc.entered.is_set)
            window.close()
            assert not window.bridge.closed and window.editing.busy
            plc.release.set()
            until(lambda: window.bridge.closed and window.communication.closed)
            assert len(plc.packets) == 3
            assert startup.application.snapshot().process_required
        finally:
            plc.release.set()
