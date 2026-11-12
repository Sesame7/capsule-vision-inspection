"""Real save/preparation/Run owners with synthetic replay, never revision SQL inserts."""

import json
from concurrent.futures import Future
from dataclasses import replace
from datetime import datetime
from hashlib import sha256
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QLineEdit
from tests.support.application import ReplayEngine
from tests.support.fins_tcp import TcpPeer
from tests.support.qt import until
from tests.support.startup import (
    TEST_PASSWORD,
    authenticated_startup,
    close,
    normal_replay,
    provision_account,
)

from capsule_vision.application import ApplicationState
from capsule_vision.application.audit import (
    AuditOperation,
    AuditQuery,
    AuditTrail,
    AuditUnavailable,
)
from capsule_vision.application.identity_models import AuthorizationError, Role
from capsule_vision.application.run_configuration import RunConfiguration
from capsule_vision.configuration import ConfigurationIOError
from capsule_vision.configuration._io import ConfigurationPublication
from capsule_vision.localization.catalog import text
from capsule_vision.ui.main_window import MainWindow


def delivered[T](future: Future[T]) -> T:
    until(future.done, timeout_ms=10000)
    return future.result()


def create_recipe(window: MainWindow, name: str) -> str:
    """Use the existing Duplicate form and Save, preserving the synthetic Model."""
    window.navigate(1)
    until(lambda: not window.editing.busy and not window.recipe_page.labels_loading)
    page = window.recipe_page
    page.create_dialog(True)
    dialog = page.findChild(QDialog)
    assert dialog is not None
    field = dialog.findChild(QLineEdit)
    buttons = dialog.findChild(QDialogButtonBox)
    assert field is not None and buttons is not None
    field.setText(name)
    buttons.button(QDialogButtonBox.StandardButton.Ok).click()
    until(lambda: not dialog.isVisible())
    page.save()
    until(lambda: not window.editing.busy)
    editor = window.editing.session
    assert editor and editor.entry and not editor.dirty
    assert editor.entry.recipe.display_name == name
    return editor.entry.recipe.recipe_id


def confirmed(
    audit: AuditTrail, artifact: str, resource: str, path: Path, actor: str
) -> str:
    revision = delivered(audit.revisions(artifact, resource))[0]
    content = delivered(audit.revision_content(revision.record.operation.id))
    assert revision.confirmed and content == path.read_bytes()
    assert revision.sha256 == sha256(content).hexdigest()
    assert revision.record.operation.actor_id == actor
    assert datetime.fromisoformat(
        revision.record.operation.utc
    ) <= datetime.fromisoformat(revision.record.completed_utc)
    return revision.record.operation.id


def test_save_refresh_replay_and_restart_keep_exact_run_revisions(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_network(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Traceability acceptance must not open a PLC connection")

    monkeypatch.setattr("socket.socket.sendto", no_network)
    runs: list[tuple[str, RunConfiguration]] = []
    with TcpPeer() as plc, normal_replay(tmp_path) as (startup, window, engines):
        app, editor = startup.application, startup.editor
        assert editor and editor.camera_sources and app.identity and app.history
        identity, history = app.identity, app.history
        account = provision_account(
            identity, "Acceptance Engineer", Role.ENGINEER, TEST_PASSWORD
        )
        delivered(identity.login(account.username, TEST_PASSWORD))
        window.identity_controls.refresh()
        audit = identity.audit
        recipe_id = create_recipe(window, "Acceptance Recipe A")
        assert editor.entry
        recipe_path = editor.entry.path
        created = confirmed(audit, "recipe", recipe_id, recipe_path, account.account_id)
        created_bytes = delivered(audit.revision_content(created))
        before = delivered(audit.revisions("recipe", recipe_id))
        window.recipe_page.save()
        until(lambda: not window.editing.busy)
        assert delivered(audit.revisions("recipe", recipe_id)) == before

        # Each Vision save is a real independent publication; neither selects A.
        window.navigate(4)
        vision = window.vision_workspace
        assert vision
        until(lambda: not window.editing.busy)
        machine = editor.machine_editor
        camera = machine.camera_ids[0]
        slots = machine.camera(camera).slots
        machine.replace_rects(
            camera,
            (replace(slots[0].rect, width=slots[0].rect.width - 1),)
            + tuple(slot.rect for slot in slots[1:]),
        )
        vision.save_setup()
        until(lambda: not window.editing.busy)
        machine_revision = confirmed(
            audit, "machine", "machine.tsv", startup.data.machine, account.account_id
        )
        assert not delivered(audit.revisions("sources", "camera_sources.json"))
        editor.camera_sources.set_fps(2)
        vision.save_setup()
        until(lambda: not window.editing.busy)
        source_revision = confirmed(
            audit,
            "sources",
            "camera_sources.json",
            startup.data.sources,
            account.account_id,
        )
        assert len(delivered(audit.revisions("machine", "machine.tsv"))) == 1

        window.navigate(2)
        communication = window.communication
        assert communication.reporting_panel
        communication.reporting_panel.merge_count.setCurrentIndex(0)
        communication.reporting_panel.port.setValue(19600)
        communication.alarm_panel.port.setValue(plc.port)
        communication.persist_settings()
        until(lambda: not communication.busy)
        communication_revision = confirmed(
            audit,
            "communication",
            "communication.json",
            startup.data.communication,
            account.account_id,
        )
        communication.persist_settings()
        until(lambda: not communication.busy)
        assert (
            len(delivered(audit.revisions("communication", "communication.json"))) == 1
        )

        window.navigate(0)
        picker = window.production_recipes
        picker.reload()
        picker.select(picker.recipes.findData(recipe_id))
        until(lambda: not window.editing.busy)
        initial = app.snapshot().candidate_prepared
        assert initial and initial.configuration.recipe_id == recipe_id
        selections = delivered(audit.page(AuditQuery(action="production.recipe")))
        assert len(selections) == 1 and selections[0].outcome == "success"
        delivered(app.start_batch("Acceptance Replay Batch"))

        for hue in (12, 95):
            window.navigate(1)
            editor.draft.process_parameters = {"running_speed": str(hue)}
            editor.draft.capsule = replace(
                editor.draft.capsule,
                color=replace(editor.draft.capsule.color, target_hue=hue),
            )
            window.recipe_page.save()
            until(lambda: not window.editing.busy)
            candidate = app.snapshot().candidate_prepared
            assert candidate and candidate is not initial
            assert candidate.configuration.recipe_id == recipe_id
            assert candidate.configuration.capsule.color.target_hue == hue
            assert not app.snapshot().candidate_requires_preparation
            recipe_revision = confirmed(
                audit, "recipe", recipe_id, recipe_path, account.account_id
            )
            window.navigate(0)
            previous_completed = app.snapshot().counters.cycles_completed
            window.bridge.start()
            until(lambda: app.snapshot().state is ApplicationState.RUNNING)
            run_id = app.snapshot().run_id
            assert run_id
            manifest = delivered(history.run_configuration(run_id))
            assert (
                manifest
                and manifest.complete
                and manifest.revision_lookup == "completed"
            )
            resources = {row.artifact: row for row in manifest.resources}
            assert {
                key: resources[key].revision_id
                for key in ("recipe", "machine", "sources", "communication")
            } == {
                "recipe": recipe_revision,
                "machine": machine_revision,
                "sources": source_revision,
                "communication": communication_revision,
            }
            for path, artifact in (
                (recipe_path, "recipe"),
                (startup.data.machine, "machine"),
                (startup.data.sources, "sources"),
                (startup.data.communication, "communication"),
            ):
                assert resources[artifact].content == path.read_bytes()
                assert (
                    resources[artifact].sha256 == sha256(path.read_bytes()).hexdigest()
                )
            assert resources["model"].resource == candidate.configuration.model.model_id
            assert (
                resources["model"].sha256
                == candidate.configuration.model.expected_sha256
            )
            assert resources["model"].content is None
            effective = json.loads(manifest.effective)
            assert effective["inspection"]["process_parameters"] == [
                ["running_speed", hue]
            ]
            assert effective["reporting"]["enabled"] is False
            assert effective["reporting"]["settings"]["mode"] == "per_cycle"
            assert effective["reporting"]["settings"]["endpoint"]["port"] == 19600
            assert effective["sources"]["mode"] == "folder"
            assert effective["sources"]["target_fps"] == 2
            assert app.snapshot().active_prepared is candidate
            assert not window.recipe_page.editable()
            until(
                lambda previous=previous_completed: (
                    app.snapshot().counters.cycles_completed > previous
                )
            )
            window.bridge.stop()
            until(lambda: app.snapshot().state is ApplicationState.READY)
            assert delivered(history.run_configuration(run_id)) == manifest
            runs.append((run_id, manifest))
            assert delivered(audit.revision_content(created)) == created_bytes
            assert (
                delivered(audit.page(AuditQuery(action="production.recipe")))
                == selections
            )
            assert not editor.dirty

        assert len(engines) == 2 and all(engine.closed for engine in engines)
        assert len(delivered(audit.revisions("recipe", recipe_id))) == 3
        assert runs[0][1] != runs[1][1]
        before_unrelated = app.snapshot().candidate_prepared
        other_id = create_recipe(window, "Acceptance Recipe B")
        assert (
            other_id != recipe_id
            and app.snapshot().candidate_prepared is before_unrelated
        )

        # An actual rejected publication leaves pending content, never a success.
        assert editor.entry
        other_path = editor.entry.path
        previous_file = other_path.read_bytes()
        publication = ConfigurationPublication.publish

        def refuse(value: ConfigurationPublication) -> None:
            if value.path == other_path:
                raise ConfigurationIOError("Controlled acceptance publication refusal")
            publication(value)

        with monkeypatch.context() as failure:
            failure.setattr(ConfigurationPublication, "publish", refuse)
            editor.draft.display_name = "Unpublished change"
            window.recipe_page.save()
            until(lambda: not window.editing.busy)
        revisions = delivered(audit.revisions("recipe", other_id))
        assert len(revisions) == 2 and not revisions[0].confirmed
        assert revisions[0].record.outcome == "failed"
        assert editor.entry.path.read_bytes() == previous_file and editor.dirty
        assert (
            text("recipe_save_error", window.language)
            in window.recipe_page.status.text()
        )
        editor.discard()
        for run_id, manifest in runs:
            assert delivered(history.run_configuration(run_id)) == manifest

        # Restore A in the editor; this read does not change Production selection.
        editor.load(
            next(
                row
                for row in editor.catalog.discover().entries
                if row.recipe.recipe_id == recipe_id
            )
        )
        window.recipe_page.reload()
        assert delivered(history.query()).records

    restarted = authenticated_startup(
        startup.data, engine_factory=lambda _: ReplayEngine()
    )
    try:
        owner, history = restarted.application.identity, restarted.application.history
        assert owner and history
        owner.logout()
        with pytest.raises(AuthorizationError):
            history.run_configuration(runs[0][0])
        delivered(owner.login("Acceptance Engineer", TEST_PASSWORD))
        for run_id, manifest in runs:
            assert delivered(history.run_configuration(run_id)) == manifest
        assert delivered(owner.audit.revision_content(created)) == created_bytes
    finally:
        close(restarted)


def test_published_unconfirmed_recipe_remains_unmatched_after_explicit_preparation(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with normal_replay(tmp_path) as (startup, window, _engines):
        app, editor = startup.application, startup.editor
        assert editor and editor.entry and app.identity and app.history
        audit = app.identity.audit
        original = app.snapshot().candidate_prepared
        complete = audit.complete

        def refuse_confirmation(
            operation: AuditOperation, outcome: str, detail: str = ""
        ) -> Future[None]:
            if operation.action == "recipe.save":
                failed: Future[None] = Future()
                failed.set_exception(
                    AuditUnavailable("Controlled confirmation failure")
                )
                return failed
            return complete(operation, outcome, detail)

        with monkeypatch.context() as failure:
            failure.setattr(audit, "complete", refuse_confirmation)
            editor.draft.display_name = "Published, confirmation unavailable"
            window.recipe_page.save()
            until(lambda: not window.editing.busy)
        assert app.snapshot().candidate_prepared is original
        assert app.snapshot().candidate_requires_preparation and editor.dirty
        assert window.recipe_page.status.text() == text(
            "configuration_confirmation_incomplete", window.language
        )
        (pending,) = delivered(audit.revisions("recipe", "local"))
        assert not pending.confirmed and pending.record.outcome == "unresolved"
        content = delivered(audit.revision_content(pending.record.operation.id))
        assert content == editor.entry.path.read_bytes()
        editor.discard()
        window.recipe_page.reload()
        picker = window.production_recipes
        picker.reload()
        picker.select(picker.recipes.findData("local"))
        until(lambda: not window.editing.busy)
        assert not app.snapshot().candidate_requires_preparation
        delivered(app.start_batch("Unconfirmed replay evidence"))
        window.bridge.start()
        until(lambda: app.snapshot().state is ApplicationState.RUNNING)
        run_id = app.snapshot().run_id
        assert run_id
        manifest = delivered(app.history.run_configuration(run_id))
        assert (
            manifest and manifest.complete and manifest.revision_lookup == "completed"
        )
        recipe = next(row for row in manifest.resources if row.artifact == "recipe")
        assert recipe.content == content and recipe.revision_id is None
        assert all(row.revision_id is None for row in manifest.resources)
        window.bridge.stop()
        until(lambda: app.snapshot().state is ApplicationState.READY)
        assert delivered(app.history.run_configuration(run_id)) == manifest
        assert delivered(audit.revisions("recipe", "local")) == (pending,)
