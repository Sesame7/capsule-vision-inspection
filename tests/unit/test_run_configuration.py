"""Run attribution from frozen inputs, using isolated owners and no native work."""

import json
import socket
import sqlite3
from collections.abc import Generator
from contextlib import closing
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from threading import Event

import pytest
from tests.integration.test_process_preview import saved_target
from tests.support.application import WAIT, FakeSession
from tests.support.fins_tcp import TcpPeer
from tests.support.startup import authenticated_startup, close, saved_data
from tests.unit.test_history import finish

from capsule_vision.application import (
    ApplicationError,
    ApplicationState,
    PreparedConfiguration,
)
from capsule_vision.application.audit import (
    AuditOperation,
    AuditQuery,
    RevisionEvidence,
)
from capsule_vision.application.history import ProductionHistory
from capsule_vision.application.identity_models import (
    AuthorizationError,
    StaleSessionError,
)
from capsule_vision.application.production_application import (
    SourceProductionApplication,
)
from capsule_vision.application.run_configuration import (
    RunConfiguration,
    capture_prepared,
    capture_run,
)
from capsule_vision.application.startup import Startup
from capsule_vision.communication.endpoints import FinsTcpEndpoint
from capsule_vision.communication.reporting import (
    ReportingMode,
    ReportingSelection,
    ReportingSettings,
)
from capsule_vision.configuration import (
    ConfigurationError,
    load_camera_sources,
    save_camera_sources,
    save_recipe,
)
from capsule_vision.configuration.communication_preferences import (
    CommunicationPreferences,
    communication_publication,
    load_communication_preferences,
)


@pytest.fixture
def plc() -> Generator[TcpPeer]:
    with TcpPeer() as peer:
        yield peer


@pytest.fixture
def startup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[Startup]:
    value = authenticated_startup(saved_data(tmp_path))

    # Exercise normal command/admission and persistence, with no engine/source IO.
    def factory(_prepared: PreparedConfiguration, _generation: int) -> FakeSession:
        return FakeSession()

    monkeypatch.setattr(value.application, "_factory", factory)
    try:
        yield value
    finally:
        close(value)


def run(startup: Startup) -> tuple[str, RunConfiguration]:
    app = startup.application
    assert app.history is not None
    app.start().result(WAIT)
    identity = app.snapshot().run_id
    assert identity is not None
    evidence = app.history.run_configuration(identity).result(WAIT)
    assert evidence is not None
    app.stop().result(WAIT)
    return identity, evidence


def test_two_runs_exact_revisions_frozen_values_and_restart(
    startup: Startup, plc: TcpPeer
) -> None:
    app, editor = startup.application, startup.editor
    assert editor and editor.entry and app.identity and app.history
    assert isinstance(app, SourceProductionApplication)
    owner = app.identity.audit
    app.start_batch("Traceable Batch").result(WAIT)
    captured: list[tuple[str, RunConfiguration]] = []
    revisions: list[str] = []
    saved_target(startup.data.root, plc.port)
    for hue in (12, 95):
        editor.draft.process_parameters = {"running_speed": str(hue)}
        editor.draft.capsule = replace(
            editor.draft.capsule,
            color=replace(editor.draft.capsule.color, target_hue=hue),
        )
        saved = editor.save()
        assert saved
        request = app.observe_saved_recipe(
            saved.recipe, app.identity.snapshot().revision
        )
        assert request is not None and request.operation is None
        prepared = editor.prepare_saved(saved.recipe.recipe_id)
        app.install_recipe_selection(request, prepared).result(WAIT)
        publication = communication_publication(
            startup.data.communication,
            load_communication_preferences(startup.data.communication),
        )
        app.persist_reporting_selection(
            CommunicationPreferences().reporting,
            publication.publish,
            evidence=RevisionEvidence(
                "communication", "communication.json", publication.content
            ),
        )
        result = run(startup)
        captured.append(result)
        manifest = result[1]
        assert manifest.complete and manifest.revision_lookup == "completed"
        resources = {r.artifact: r for r in manifest.resources}
        recipe = resources["recipe"]
        assert recipe.content == saved.path.read_bytes()
        assert recipe.content is not None
        assert recipe.sha256 == sha256(recipe.content).hexdigest()
        assert recipe.revision_id is not None
        revisions.append(recipe.revision_id)
        assert resources["model"].sha256 == prepared.configuration.model.expected_sha256
        assert resources["model"].content is None
        assert resources["machine"].content == startup.data.machine.read_bytes()
        assert resources["sources"].content == startup.data.sources.read_bytes()
        assert resources["communication"].revision_id is not None
        effective = json.loads(manifest.effective)
        assert effective["inspection"]["capsule"]["color"]["target_hue"] == hue
        assert effective["inspection"]["process_parameters"] == [["running_speed", hue]]
        assert not effective["reporting"]["enabled"]
    assert len(set(revisions)) == 2
    assert not owner.page(AuditQuery(action="production.recipe")).result(WAIT)
    editor.entry.path.unlink()
    startup.data.machine.write_bytes(b"later invalid file")
    startup.data.sources.unlink()
    startup.data.communication.write_bytes(b"{}")
    for identity, original in captured:
        assert app.history.run_configuration(identity).result(WAIT) == original
    path = startup.data.history
    close(startup)
    history = ProductionHistory(path, clock=lambda: 10**11)
    try:
        history.query().result(WAIT)  # Retention runs; immutable Run evidence remains.
        for identity, original in captured:
            assert history.run_configuration(identity).result(WAIT) == original
    finally:
        finish(history)


@pytest.mark.parametrize(
    "outcome", [None, "failed", "cancelled", "uncertain", "success"]
)
def test_only_exact_confirmed_revision_matches(
    startup: Startup, outcome: str | None
) -> None:
    app = startup.application
    assert app.identity and startup.prepared
    prepared = startup.prepared
    evidence = prepared.saved_content[0]
    audit = app.identity.audit
    op = AuditOperation(
        "saved-actor", "Recorded Engineer", "recipe.save", evidence.resource
    )
    audit.intent(op, evidence).result(WAIT)
    if outcome:
        audit.complete(op, outcome).result(WAIT)
    wrong = AuditOperation("other", "Other", "recipe.save", evidence.resource)
    audit.intent(wrong, replace(evidence, content=evidence.content + b" ")).result(WAIT)
    audit.complete(wrong, "success").result(WAIT)
    unrelated = AuditOperation("other", "Other", "recipe.save", "another")
    audit.intent(unrelated, replace(evidence, resource="another")).result(WAIT)
    audit.complete(unrelated, "success").result(WAIT)
    app.start_batch("Batch").result(WAIT)
    _, manifest = run(startup)
    actual = next(r for r in manifest.resources if r.artifact == evidence.artifact)
    assert actual.revision_id == (op.id if outcome == "success" else None)
    assert actual.content == evidence.content


def test_capture_uses_prepared_bytes_not_later_file_formatting(
    startup: Startup,
) -> None:
    prepared = startup.prepared
    assert prepared and prepared.recipe_dependencies
    path = prepared.recipe_dependencies.entry.path
    original = path.read_bytes()
    path.write_text(json.dumps(json.loads(original), indent=5), encoding="utf8")
    startup.application.start_batch("Batch").result(WAIT)
    _, manifest = run(startup)
    recipe = next(r for r in manifest.resources if r.artifact == "recipe")
    assert recipe.content == original != path.read_bytes()


def test_capture_rejects_changed_definition_during_preparation(
    startup: Startup,
) -> None:
    prepared = startup.prepared
    assert prepared and prepared.recipe_dependencies
    entry = prepared.recipe_dependencies.entry
    save_recipe(entry.path, replace(entry.recipe, display_name="Changed"))
    with pytest.raises(ConfigurationError, match="changed during"):
        capture_prepared(prepared)
    startup.application.start_batch("Batch").result(WAIT)
    with pytest.raises(ApplicationError):
        startup.application.start().result(WAIT)
    assert startup.application.snapshot().active is None
    close(startup)
    with closing(sqlite3.connect(startup.data.history)) as db:
        assert db.execute("SELECT count(*) FROM runs").fetchone() == (0,)
        assert db.execute("SELECT count(*) FROM run_configurations").fetchone() == (0,)


def test_unavailable_audit_retains_exact_configuration(startup: Startup) -> None:
    app = startup.application
    assert app.identity
    app.start_batch("Batch").result(WAIT)
    app.identity.audit.close()
    _, manifest = run(startup)
    assert manifest.complete and manifest.revision_lookup == "unavailable"
    assert all(r.revision_id is None for r in manifest.resources)
    assert all(r.sha256 for r in manifest.resources)


@pytest.mark.parametrize("named", [False, True])
def test_history_manifest_failure_is_atomic_and_preserves_admission_policy(
    startup: Startup, monkeypatch: pytest.MonkeyPatch, named: bool
) -> None:
    app = startup.application
    assert app.history
    monkeypatch.setattr(app, "_records_unbatched_runs", lambda: True)
    if named:
        app.start_batch("Named").result(WAIT)

    from capsule_vision.application import run_configuration

    original_store = run_configuration.store

    def fail(
        _db: sqlite3.Connection, _run: str, _value: RunConfiguration | None
    ) -> None:
        original_store(_db, _run, _value)
        raise OSError("isolated manifest failure")

    monkeypatch.setattr("capsule_vision.application.run_configuration.store", fail)
    if named:
        with pytest.raises(ApplicationError):
            app.start().result(WAIT)
        assert app.snapshot().active is None
    else:
        app.start().result(WAIT)
        assert app.snapshot().run_id and app.snapshot().unbatched_run_uncertain
        assert "manifest failure" in app.history.snapshot().error
        app.stop().result(WAIT)
    close(startup)
    with closing(sqlite3.connect(startup.data.history)) as db:
        for table in (
            "runs",
            "run_configurations",
            "run_configuration_resources",
            "run_start_accounts",
            "run_positions",
        ):
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)


def test_stop_during_manifest_lookup_never_activates_and_removes_provisional_evidence(
    startup: Startup, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = startup.application
    assert app.identity and app.history
    app.start_batch("Batch").result(WAIT)
    entered, release = Event(), Event()
    lookup = app.identity.audit.match_run_revisions

    def blocked(evidence: tuple[RevisionEvidence, ...]):
        entered.set()
        assert release.wait(WAIT)
        return lookup(evidence)

    monkeypatch.setattr(app.identity.audit, "match_run_revisions", blocked)
    starting = app.start()
    try:
        assert entered.wait(WAIT)
        stopping = app.stop()
        assert not stopping.done() and app.snapshot().active is None
    finally:
        release.set()
    with pytest.raises(ApplicationError):
        starting.result(WAIT)
    stopping.result(WAIT)
    assert app.snapshot().state is ApplicationState.READY
    close(startup)
    with closing(sqlite3.connect(startup.data.history)) as db:
        for table in ("runs", "run_configurations", "run_configuration_resources"):
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)


def test_read_access_and_queued_session_fence(
    startup: Startup, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = startup.application
    assert app.history and app.identity
    app.start_batch("Batch").result(WAIT)
    identity, _ = run(startup)
    entered, release = Event(), Event()
    from capsule_vision.application import run_configuration

    original = run_configuration.read

    def blocked(db: sqlite3.Connection, run: str):
        entered.set()
        assert release.wait(WAIT)
        return original(db, run)

    monkeypatch.setattr(run_configuration, "read", blocked)
    query = app.history.run_configuration(identity)
    try:
        assert entered.wait(WAIT)
        app.identity.logout()
    finally:
        release.set()
    with pytest.raises(StaleSessionError):
        query.result(WAIT)
    with pytest.raises(AuthorizationError):
        app.history.run_configuration(identity)


def test_reporting_selection_is_frozen_across_warmup(
    startup: Startup, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = startup.application
    assert isinstance(app, SourceProductionApplication) and app.history
    app.start_batch("Batch").result(WAIT)
    first = CommunicationPreferences()
    publication = communication_publication(startup.data.communication, first)
    app.persist_reporting_selection(
        first.reporting,
        publication.publish,
        evidence=RevisionEvidence(
            "communication", "communication.json", publication.content
        ),
    )
    session = FakeSession(automatic_start=False)

    def factory(_prepared: PreparedConfiguration, _generation: int) -> FakeSession:
        return session

    monkeypatch.setattr(app, "_factory", factory)
    started = app.start()
    try:
        assert session.start_entered.wait(WAIT)
        # Independent saved Web settings can change while warm-up is pending.
        second = replace(first, web=replace(first.web, port=9876))
        newer = communication_publication(startup.data.communication, second)
        app.persist_reporting_selection(
            second.reporting,
            newer.publish,
            evidence=RevisionEvidence(
                "communication", "communication.json", newer.content
            ),
        )
    finally:
        session.ready.set_result(None)
    started.result(WAIT)
    run_id = app.snapshot().run_id
    assert run_id
    manifest = app.history.run_configuration(run_id).result(WAIT)
    assert manifest
    resource = next(r for r in manifest.resources if r.artifact == "communication")
    assert resource.content == publication.content != newer.content
    assert resource.revision_id
    app.stop().result(WAIT)


def test_engineering_without_saved_inputs_marks_partial_evidence(
    tmp_path: Path,
) -> None:
    from tests.support.application import prepared

    candidate: PreparedConfiguration = prepared(tmp_path)
    manifest = capture_run(candidate, None, None, None)
    assert not manifest.complete
    assert json.loads(manifest.effective)["sources"] is None
    assert all(r.revision_id is None for r in manifest.resources)


def test_all_prepared_resources_match_without_using_pending_latest(
    startup: Startup,
) -> None:
    app = startup.application
    assert app.identity and startup.prepared
    audit = app.identity.audit
    expected: dict[str, str] = {}
    for evidence in startup.prepared.saved_content:
        op = AuditOperation(
            "engineer", "Engineer", "configuration.save", evidence.resource
        )
        audit.intent(op, evidence).result(WAIT)
        audit.complete(op, "success").result(WAIT)
        expected[evidence.artifact] = op.id
        pending = replace(op, id=op.id + "pending")
        audit.intent(pending, evidence).result(WAIT)
    app.start_batch("Batch").result(WAIT)
    _, manifest = run(startup)
    assert {
        r.artifact: r.revision_id for r in manifest.resources if r.artifact in expected
    } == expected


def test_failed_warmup_and_model_preparation_preserve_prior_run(
    startup: Startup, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, editor = startup.application, startup.editor
    assert app.history and editor and editor.entry and app.identity
    app.start_batch("Batch").result(WAIT)
    first, original = run(startup)
    session = FakeSession(automatic_start=False)
    session.ready.set_exception(RuntimeError("isolated warmup failure"))

    def factory(_prepared: PreparedConfiguration, _generation: int) -> FakeSession:
        return session

    monkeypatch.setattr(app, "_factory", factory)
    with pytest.raises(ApplicationError):
        app.start().result(WAIT)
    editor.draft.model_id = "missing-model"
    saved = editor.save()
    assert saved
    request = app.observe_saved_recipe(saved.recipe, app.identity.snapshot().revision)
    assert request
    with pytest.raises(ConfigurationError) as failed:
        editor.prepare_saved(saved.recipe.recipe_id)
    app.recipe_selection_failed(request, failed.value)
    assert app.snapshot().candidate_requires_preparation
    assert app.history.run_configuration(first).result(WAIT) == original
    close(startup)
    with closing(sqlite3.connect(startup.data.history)) as db:
        assert db.execute("SELECT uuid FROM runs").fetchall() == [(first,)]
        with pytest.raises(sqlite3.IntegrityError, match="Immutable"):
            db.execute("UPDATE run_configurations SET complete=0 WHERE run=?", (first,))


@pytest.mark.parametrize("mode", list(ReportingMode))
def test_frozen_reporting_modes_and_unbatched_run_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: ReportingMode
) -> None:
    data = saved_data(tmp_path)
    sources = load_camera_sources(data.sources)
    save_camera_sources(
        data.sources,
        replace(
            sources,
            mode="basler",
            cameras=tuple(
                replace(camera, basler_serial=f"90000{i}")
                for i, camera in enumerate(sources.cameras)
            ),
        ),
    )
    preferences = CommunicationPreferences(
        reporting=ReportingSelection(
            True, ReportingSettings(FinsTcpEndpoint(), 149, mode=mode)
        )
    )
    publication = communication_publication(data.communication, preferences)
    publication.publish()

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Configuration tracing must not communicate with a PLC")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "sendto", forbidden)
    value = authenticated_startup(data, select_basler_batch=False)

    def factory(_prepared: PreparedConfiguration, _generation: int) -> FakeSession:
        return FakeSession()

    monkeypatch.setattr(value.application, "_factory", factory)
    try:
        run_id, manifest = run(value)
        assert manifest.complete
        reporting = json.loads(manifest.effective)["reporting"]
        assert reporting["enabled"]
        assert reporting["settings"]["mode"] == mode.value
        assert reporting["settings"]["base_address"] == 149
        assert (
            next(r for r in manifest.resources if r.artifact == "communication").content
            == publication.content
        )
    finally:
        close(value)
    with closing(sqlite3.connect(data.history)) as db:
        assert db.execute(
            "SELECT batch,reporting_mode FROM runs WHERE uuid=?", (run_id,)
        ).fetchone() == (None, mode.value)
