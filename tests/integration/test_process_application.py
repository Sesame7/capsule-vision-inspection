"""Saved-candidate application against an isolated FINS/TCP peer, never equipment."""

from collections.abc import Generator
from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path

import pytest
from tests.integration.test_process_preview import saved_target
from tests.support.application import WAIT, FakeSession
from tests.support.fins_tcp import TcpPeer
from tests.support.recipe import editor
from tests.support.startup import (
    TEST_PASSWORD,
    authenticated_startup,
    close,
    provision_account,
    saved_data,
)

from capsule_vision.application import (
    ApplicationStateError,
    PreparedConfiguration,
    ProductionApplication,
)
from capsule_vision.application.audit import (
    AuditOperation,
    AuditQuery,
    AuditUnavailable,
    RevisionEvidence,
)
from capsule_vision.application.editing import RecipeEditorSession
from capsule_vision.application.identity_models import AuthorizationError, Role
from capsule_vision.application.process_application import (
    ProcessApplicationError,
    ProcessWrite,
)
from capsule_vision.communication.endpoints import FinsTcpEndpoint
from capsule_vision.configuration import load_recipe, save_recipe
from capsule_vision.configuration.communication_preferences import (
    CommunicationPreferences,
    StatusPreferences,
    save_communication_preferences,
)


def saved_candidate(
    session: RecipeEditorSession, values: dict[str, str]
) -> PreparedConfiguration:
    session.draft.process_parameters = values
    session.save()
    assert session.entry
    return session.prepare_saved(session.entry.recipe.recipe_id)


@pytest.fixture
def peer() -> Generator[TcpPeer]:
    with TcpPeer() as plc:
        yield plc


def application(
    root: Path, plc: TcpPeer
) -> tuple[ProductionApplication, RecipeEditorSession]:
    session = editor(root)
    path = saved_target(root, plc.port)
    app = ProductionApplication(
        lambda _prepared, _generation: FakeSession(), communication_path=path
    )
    return app, session


def finish(app: ProductionApplication) -> None:
    app.close().result(WAIT)
    assert app.join(WAIT)


@pytest.mark.parametrize("running", (0, 1, 4, 65533))
def test_stopped_bit_only_then_pairs_readback_and_omitted_words(
    tmp_path: Path, peer: TcpPeer, running: int
) -> None:
    app, session = application(tmp_path, peer)
    peer.set_words(16, (running,))
    peer.set_words(108, (0x1234, 0x5678), area=0x82)
    try:
        candidate = saved_candidate(
            session, {"running_speed": "0", "photo_phase": "36000"}
        )
        app.configure(candidate).result(WAIT)
        assert not app.snapshot().process_required
        assert peer.connections == 1
        packets = peer.packets
        assert [(p[10:13], int.from_bytes(p[13:15])) for p in packets] == [
            (b"\x01\x01\xb1", 16),
            (b"\x01\x02\x82", 102),
            (b"\x01\x01\x82", 102),
            (b"\x01\x02\x82", 114),
            (b"\x01\x01\x82", 114),
        ]
        assert peer.words(102, 2, 0x82) == (0, 0)
        assert peer.words(114, 2, 0x82) == (36000, 0)
        assert peer.words(108, 2, 0x82) == (0x1234, 0x5678)
        app.start().result(WAIT)
        assert app.snapshot().active_prepared is candidate
        app.stop().result(WAIT)
        assert len(peer.packets) == 5  # No application during Start/Stop.
    finally:
        finish(app)


@pytest.mark.parametrize("running", (2, 3, 65535))
def test_running_blocks_before_writes(
    tmp_path: Path, peer: TcpPeer, running: int
) -> None:
    app, session = application(tmp_path, peer)
    peer.set_words(16, (running,))
    try:
        with pytest.raises(ProcessApplicationError, match="Machine running"):
            app.configure(saved_candidate(session, {"running_speed": "1"})).result(WAIT)
        assert len(peer.packets) == 1
        assert app.snapshot().process_required and app.snapshot().last_issue
        with pytest.raises(ApplicationStateError, match="readback"):
            app.start()
    finally:
        finish(app)


@pytest.mark.parametrize("failure", ("read", "connect", "write", "readback"))
def test_failure_no_retry_and_partial_evidence(
    tmp_path: Path, peer: TcpPeer, failure: str
) -> None:
    app, session = application(tmp_path, peer)
    if failure == "connect":
        peer.fail_next("handshake", "reject")
    elif failure == "read":
        peer.fail_next("read", "plc", start=16)
    elif failure == "write":
        peer.fail_next("sequence", "drop_after", start=114)
    else:
        peer.fail_next("read", "plc", start=114)
    try:
        with pytest.raises(ProcessApplicationError) as caught:
            app.configure(
                saved_candidate(session, {"running_speed": "1", "photo_phase": "2"})
            ).result(WAIT)
        error = caught.value
        assert len(error.verified) == (1 if failure in ("write", "readback") else 0)
        assert len(error.attempted) == (2 if failure in ("write", "readback") else 0)
        assert peer.connections == 1 and app.snapshot().process_required
        if error.attempted:
            assert "may have been written" in str(error)
            assert peer.words(114, 2, 0x82) == (2, 0)  # No rollback.
        with pytest.raises(ApplicationStateError):
            app.start()
    finally:
        finish(app)


def test_readback_mismatch_and_signed_catalog_override(
    tmp_path: Path, peer: TcpPeer
) -> None:
    app, session = application(tmp_path, peer)
    session.catalog.process_catalog_path = tmp_path / "process_parameters.json"
    session.catalog.process_catalog_path.write_text(
        '{"overrides":[{"id":"running_speed","address":300,"minimum":-2147483648,"maximum":2147483647}]}',
        encoding="utf-8",
    )
    session.discard()
    candidate = saved_candidate(session, {"running_speed": "-2147483648"})
    peer.hold_next("read", start=300)
    try:
        future = app.configure(candidate)
        assert peer.entered.wait(WAIT)
        assert peer.words(300, 2, 0x82) == (0, 32768)
        peer.set_words(300, (1, 0), area=0x82)
        peer.release.set()
        with pytest.raises(ProcessApplicationError, match="Readback mismatch"):
            future.result(WAIT)
        assert app.snapshot().process_required
        app.configure(candidate).result(WAIT)
        assert peer.words(300, 2, 0x82) == (0, 32768)
    finally:
        peer.release.set()
        finish(app)


@pytest.mark.parametrize(
    "value", (-2147483648, -65536, -1, 0, 65535, 65536, 2147483647)
)
def test_dint_pairs(value: int) -> None:
    from capsule_vision.application.process_preview import signed_dint

    assert signed_dint(*ProcessWrite("test", 300, value).words) == value


def test_no_overrides_requires_no_saved_target_and_no_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = editor(tmp_path)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("No PLC IO for omitted overrides")

    monkeypatch.setattr("socket.socket.connect", forbidden)
    app = ProductionApplication(lambda _prepared, _generation: FakeSession())
    try:
        app.configure(saved_candidate(session, {})).result(WAIT)
        app.start().result(WAIT)
        app.stop().result(WAIT)
    finally:
        finish(app)


@pytest.mark.parametrize("supersede", ("empty", "replacement", "close", "cancel"))
def test_pending_application_cannot_mark_a_later_candidate_ready(
    tmp_path: Path, peer: TcpPeer, supersede: str
) -> None:
    app, session = application(tmp_path, peer)
    first = saved_candidate(session, {"running_speed": "10"})
    peer.hold_next("read", start=102)
    try:
        pending = app.configure(first)
        assert peer.entered.wait(WAIT)
        with pytest.raises(ApplicationStateError):
            app.start()
        later = None
        if supersede in ("empty", "replacement"):
            next_candidate = saved_candidate(
                session, {} if supersede == "empty" else {"running_speed": "20"}
            )
            later = app.configure(next_candidate)
            with pytest.raises(ApplicationStateError):
                app.start()
        elif supersede == "close":
            closed = app.close()
            assert not closed.done() and not app.join(0.01)
        else:
            app.cancel_process_application()
        peer.release.set()
        with pytest.raises(ProcessApplicationError):
            pending.result(WAIT)
        if later is not None:
            later.result(WAIT)
            assert not app.snapshot().process_required
            assert app.snapshot().last_issue is None
            assert peer.words(102, 2, 0x82) == (
                (20, 0) if supersede == "replacement" else (10, 0)
            )
        elif supersede == "cancel":
            assert app.snapshot().process_required
    finally:
        peer.release.set()
        finish(app)


@pytest.mark.parametrize("role", list(Role))
def test_operator_admission_guest_restoration_and_audit(
    tmp_path: Path, peer: TcpPeer, role: Role
) -> None:
    data = saved_data(tmp_path)
    path = data.recipes / "local.json"
    recipe = load_recipe(path)
    save_recipe(
        path,
        replace(
            recipe,
            display_name="Long Recipe " * 256
            if role is Role.OPERATOR
            else recipe.display_name,
            process_parameters=(("running_speed", 0),),
        ),
    )
    saved_target(data.root, peer.port)
    startup = authenticated_startup(data)
    app, owner, session = (
        startup.application,
        startup.application.identity,
        startup.editor,
    )
    assert owner and session
    try:
        assert (
            not peer.connections and app.snapshot().process_required
        )  # startup Guest, even after login
        if role is Role.GUEST:
            owner.logout()
        elif role is not Role.ADMINISTRATOR:
            account = provision_account(owner, role.name, role, TEST_PASSWORD)
            owner.login(account.username, TEST_PASSWORD).result(WAIT)
        if role is Role.GUEST:
            with pytest.raises(AuthorizationError):
                app.begin_recipe_selection("local")
            assert not peer.connections
        else:
            request = app.begin_recipe_selection("local")
            app.install_recipe_selection(
                request, session.prepare_saved("local")
            ).result(WAIT)
            assert not app.snapshot().process_required and peer.connections == 1
        if role < Role.ADMINISTRATOR:
            owner.login("TestAdmin", TEST_PASSWORD).result(WAIT)
        attempts = owner.audit_page(AuditQuery(action="recipe.process_apply")).result(
            WAIT
        )
        values = owner.audit_page(AuditQuery(action="recipe.process_value")).result(
            WAIT
        )
        assert len(attempts) == len(values) == (0 if role is Role.GUEST else 1)
        if attempts:
            assert attempts[0].outcome == values[0].outcome == "success"
            assert attempts[0].operation.target == "local"
            assert "D102–D103 = 0" in values[0].operation.after
            assert attempts[0].operation.id in values[0].operation.before
            candidate = app.snapshot().candidate_prepared
            assert candidate
            recipe_hash = next(
                r.sha256 for r in candidate.saved_content if r.artifact == "recipe"
            )
            assert f"Recipe SHA256 {recipe_hash}" in attempts[0].operation.after
    finally:
        close(startup)


@pytest.mark.parametrize("change", ("logout", "role", "disable"))
def test_identity_change_during_write_readback_blocks_later_operations(
    tmp_path: Path, peer: TcpPeer, change: str
) -> None:
    startup = authenticated_startup(saved_data(tmp_path))
    app, session, owner = (
        startup.application,
        startup.editor,
        startup.application.identity,
    )
    assert session and owner
    saved_target(startup.data.root, peer.port)
    candidate = saved_candidate(session, {"running_speed": "4", "photo_phase": "5"})
    if change != "logout":
        provision_account(owner, "PendingEngineer", Role.ENGINEER, TEST_PASSWORD)
        owner.login("PendingEngineer", TEST_PASSWORD).result(WAIT)
    peer.hold_next("read", start=102)
    try:
        request = app.begin_recipe_selection("local")
        pending = app.install_recipe_selection(request, candidate)
        assert peer.entered.wait(WAIT)
        if change == "logout":
            owner.logout()
        else:
            actor = owner.snapshot().account
            assert actor
            owner.login("TestAdmin", TEST_PASSWORD).result(WAIT)
            if change == "role":
                owner.set_role(actor.account_id, Role.OPERATOR).result(WAIT)
            else:
                owner.set_enabled(actor.account_id, False).result(WAIT)
        peer.release.set()
        with pytest.raises(ProcessApplicationError):
            pending.result(WAIT)
        assert len(peer.packets) == 3 and app.snapshot().process_required
        owner.login("TestAdmin", TEST_PASSWORD).result(WAIT)
        attempts = owner.audit_page(AuditQuery(action="recipe.process_apply")).result(
            WAIT
        )
        assert attempts[0].outcome == "stale"
    finally:
        peer.release.set()
        close(startup)


def test_changed_saved_target_invalidates_verified_candidate_before_start(
    tmp_path: Path, peer: TcpPeer
) -> None:
    app, session = application(tmp_path, peer)
    try:
        app.configure(saved_candidate(session, {"running_speed": "4"})).result(WAIT)
        save_communication_preferences(
            tmp_path / "communication.json",
            CommunicationPreferences(
                status=StatusPreferences(
                    endpoint=FinsTcpEndpoint(port=peer.port), running_address=17
                )
            ),
        )
        with pytest.raises(Exception, match="Status target changed"):
            app.start().result(WAIT)
        assert app.snapshot().process_required and len(peer.packets) == 3
    finally:
        finish(app)


def test_running_address_is_explicit_and_other_status_settings_do_not_invalidate(
    tmp_path: Path, peer: TcpPeer
) -> None:
    app, session = application(tmp_path, peer)
    status = StatusPreferences(
        endpoint=FinsTcpEndpoint(port=peer.port), running_address=317
    )
    save_communication_preferences(
        tmp_path / "communication.json", CommunicationPreferences(status=status)
    )
    peer.set_words(16, (2,))  # Default Running must not be read instead.
    try:
        app.configure(saved_candidate(session, {"running_speed": "6"})).result(WAIT)
        assert int.from_bytes(peer.packets[0][13:15]) == 317
        save_communication_preferences(
            tmp_path / "communication.json",
            CommunicationPreferences(
                status=replace(status, enabled=True, heartbeat_address=44)
            ),
        )
        app.start().result(WAIT)
        app.stop().result(WAIT)
        assert len(peer.packets) == 3
    finally:
        finish(app)


@pytest.mark.parametrize("stage", ("intent", "complete"))
def test_audit_failure_blocks_later_writes_and_preserves_partial_evidence(
    tmp_path: Path, peer: TcpPeer, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    startup = authenticated_startup(saved_data(tmp_path))
    app, owner, session = (
        startup.application,
        startup.application.identity,
        startup.editor,
    )
    assert owner and session
    saved_target(startup.data.root, peer.port)
    candidate = saved_candidate(session, {"running_speed": "7", "photo_phase": "8"})
    original_intent, original_complete = owner.audit.intent, owner.audit.complete

    def denied() -> Future[None]:
        future: Future[None] = Future()
        future.set_exception(AuditUnavailable("simulated audit failure"))
        return future

    def intent(
        op: AuditOperation, evidence: RevisionEvidence | None = None
    ) -> Future[None]:
        if op.action == "recipe.process_value" and "photo_phase" in op.after:
            return denied()
        return original_intent(op, evidence)

    def complete(op: AuditOperation, outcome: str, detail: str = "") -> Future[None]:
        if op.action == "recipe.process_value":
            return denied()
        return original_complete(op, outcome, detail)

    monkeypatch.setattr(owner.audit, stage, intent if stage == "intent" else complete)
    try:
        request = app.begin_recipe_selection("local")
        with pytest.raises(ProcessApplicationError) as caught:
            app.install_recipe_selection(request, candidate).result(WAIT)
        assert len(caught.value.verified) == 1
        assert len(caught.value.attempted) == 1
        assert len(peer.packets) == 3 and app.snapshot().process_required
        assert peer.words(114, 2, 0x82) == (0, 0)
    finally:
        close(startup)


def test_definite_write_rejection_is_not_reported_as_an_uncertain_write(
    tmp_path: Path, peer: TcpPeer
) -> None:
    app, session = application(tmp_path, peer)
    peer.fail_next("sequence", "plc", start=114)
    try:
        with pytest.raises(ProcessApplicationError) as caught:
            app.configure(
                saved_candidate(session, {"running_speed": "1", "photo_phase": "2"})
            ).result(WAIT)
        assert len(caught.value.verified) == len(caught.value.rejected) == 1
        assert not caught.value.uncertain
        assert "PLC rejected" in str(caught.value)
        assert peer.words(102, 2, 0x82) == (1, 0)
        assert peer.words(114, 2, 0x82) == (0, 0)
    finally:
        finish(app)
