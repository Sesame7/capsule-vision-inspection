"""Real local credentials/transactions, isolated roots and controlled worker races."""

import hashlib
import hmac
import sqlite3
import subprocess
import sys
from collections.abc import Generator
from concurrent.futures import Future
from contextlib import closing, contextmanager
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from threading import Event

import pytest
from tests.support.application import FakeSession, close

from capsule_vision.application import (
    ApplicationError,
    ApplicationState,
    ProductionApplication,
    passwords,
)
from capsule_vision.application import identity as identity_module
from capsule_vision.application._account_store import AccountStore
from capsule_vision.application.account_maintenance import (
    initialize_administrator,
    recover_administrator,
)
from capsule_vision.application.audit import AuditOperation
from capsule_vision.application.identity import LocalIdentity
from capsule_vision.application.identity_models import (
    AccountConflictError,
    AuthenticationError,
    AuthorizationError,
    IdentityError,
    IdentityValidationError,
    LastAdministratorError,
    Role,
    SecurityBusyError,
    SecurityStorageError,
    SessionSnapshot,
    StaleSessionError,
    StorageState,
)

WAIT = 10
SECRET = "isolated test credential"


@contextmanager
def opened(path: Path) -> Generator[LocalIdentity]:
    owner = LocalIdentity(path)
    try:
        owner.ready.result(WAIT)
        yield owner
    finally:
        owner.request_stop()
        assert owner.join(WAIT)


@pytest.fixture
def database(tmp_path: Path) -> Path:
    path = tmp_path / "data" / "security.sqlite3"
    initialize_administrator(path, "Admin", SECRET)
    return path


def test_password_verifier_is_salted_versioned_and_standard_library(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second = passwords.hash_password(SECRET), passwords.hash_password(SECRET)
    assert first.version == 1 and first.iterations == 600_000
    assert first.salt != second.salt and first.digest != second.digest
    assert first.digest == hashlib.pbkdf2_hmac(
        "sha256", SECRET.encode(), first.salt, first.iterations
    )
    compared: list[bool] = []
    original = hmac.compare_digest

    def compare(a: bytes, b: bytes) -> bool:
        compared.append(True)
        return original(a, b)

    monkeypatch.setattr(hmac, "compare_digest", compare)
    assert passwords.verify_password(SECRET, first)
    assert not passwords.verify_password("wrong", first)
    assert compared == [True, True]
    assert SECRET not in repr(first)
    with pytest.raises(SecurityStorageError):
        passwords.PasswordVerifier(2, first.iterations, first.salt, first.digest)
    with pytest.raises(SecurityStorageError):
        passwords.PasswordVerifier(
            1, passwords.MAX_WORK_FACTOR + 1, first.salt, first.digest
        )
    for invalid in ("", "a" * 1025, "\ud800"):
        with pytest.raises(IdentityValidationError):
            passwords.hash_password(invalid)


def test_authentication_honors_stored_work_factor(database: Path) -> None:
    with sqlite3.connect(database) as connection:
        salt: bytes = connection.execute("SELECT salt FROM accounts").fetchone()[0]
        digest = hashlib.pbkdf2_hmac("sha256", SECRET.encode(), salt, 100_000)
        connection.execute(
            "UPDATE accounts SET iterations=?, digest=?", (100_000, digest)
        )
    with opened(database) as owner:
        assert owner.login("Admin", SECRET).result(WAIT).role is Role.ADMINISTRATOR


def test_guest_missing_store_and_restart_never_restore_login(tmp_path: Path) -> None:
    path = tmp_path / "data/security.sqlite3"
    with opened(path) as owner:
        assert owner.storage_snapshot().state is StorageState.MISSING
        snapshot = owner.snapshot()
        assert snapshot.account is None and snapshot.role is Role.GUEST
        assert not path.exists()
        assert owner.logout().revision > snapshot.revision
        with pytest.raises(AuthorizationError):
            owner.create_account("Admin", Role.ADMINISTRATOR, SECRET)
    admin = initialize_administrator(path, "Admin", SECRET)
    with opened(path) as owner:
        assert owner.snapshot().role is Role.GUEST
        session = owner.login("aDmIn", SECRET).result(WAIT)
        assert session.account is not None and session.revision > 0
        assert replace(session.account, last_login_utc=None) == admin
        with pytest.raises(FrozenInstanceError):
            session.__setattr__("revision", -1)
    with opened(path) as owner:
        assert owner.snapshot().role is Role.GUEST
        current = owner.login("ADMIN", SECRET).result(WAIT).account
        assert current is not None and replace(current, last_login_utc=None) == admin


def test_accounts_persist_casefold_id_role_and_enabled_state(database: Path) -> None:
    assert Role.GUEST < Role.OPERATOR < Role.ENGINEER < Role.ADMINISTRATOR
    with opened(database) as owner:
        owner.login("Admin", SECRET).result(WAIT)
        account = owner.create_account(
            "Straße", Role.OPERATOR, "operator-secret"
        ).result(WAIT)
        with pytest.raises(AccountConflictError):
            owner.create_account("STRASSE", Role.ENGINEER, SECRET).result(WAIT)
        with pytest.raises(IdentityValidationError):
            owner.create_account("anonymous", Role.GUEST, SECRET).result(WAIT)
        changed = owner.set_role(account.account_id, Role.ENGINEER).result(WAIT)
        assert (
            changed.account_id == account.account_id
            and changed.username == account.username
        )
        owner.set_enabled(account.account_id, False).result(WAIT)
        assert len(owner.account_management().result(WAIT).accounts) == 2
    assert SECRET.encode() not in database.read_bytes()
    assert b"operator-secret" not in database.read_bytes()
    with opened(database) as owner:
        with pytest.raises(AuthenticationError, match="Invalid username or password"):
            owner.login("STRASSE", "operator-secret").result(WAIT)
        assert owner.snapshot().role is Role.GUEST
        owner.login("Admin", SECRET).result(WAIT)
        owner.set_enabled(account.account_id, True).result(WAIT)
        pending = owner.login("strasse", "operator-secret").result(WAIT)
        assert pending.rotation_required
        session = owner.rotate_password(
            "my permanent password", pending.revision
        ).result(WAIT)
        assert session.account is not None
        assert (
            session.account.account_id == account.account_id
            and session.role is Role.ENGINEER
        )


@pytest.mark.parametrize("kind", ["wrong-password", "unknown", "disabled"])
def test_failed_authentication_preserves_current_session(
    database: Path, kind: str
) -> None:
    with opened(database) as owner:
        owner.login("Admin", SECRET).result(WAIT)
        owner.create_account("Disabled", Role.OPERATOR, SECRET).result(WAIT)
        accounts = owner.account_management().result(WAIT).accounts
        disabled = next(a for a in accounts if a.username == "Disabled")
        owner.set_enabled(disabled.account_id, False).result(WAIT)
        before = owner.snapshot()
        name = (
            "Admin"
            if kind == "wrong-password"
            else "Disabled"
            if kind == "disabled"
            else "Unknown"
        )
        with pytest.raises(AuthenticationError, match="^Invalid username or password$"):
            owner.login(
                name, "incorrect" if kind == "wrong-password" else SECRET
            ).result(WAIT)
        assert owner.snapshot() == before


def test_management_boundary_and_self_password_change(database: Path) -> None:
    with opened(database) as owner:
        admin = owner.login("Admin", SECRET).result(WAIT).account
        assert admin is not None
        operator = owner.create_account("Worker", Role.OPERATOR, SECRET).result(WAIT)
        pending = owner.login("Worker", SECRET).result(WAIT)
        session = owner.rotate_password("first replacement", pending.revision).result(
            WAIT
        )
        for operation in (
            lambda: owner.account_management(),
            lambda: owner.create_account("Other", Role.OPERATOR, SECRET),
            lambda: owner.set_role(operator.account_id, Role.ADMINISTRATOR),
            lambda: owner.set_enabled(admin.account_id, False),
            lambda: owner.reset_password(operator.account_id, "new-secret"),
        ):
            with pytest.raises(AuthorizationError):
                operation()
        with pytest.raises(AuthenticationError):
            owner.change_password("wrong", "new-secret").result(WAIT)
        assert owner.snapshot() == session
        changed = owner.change_password("first replacement", "new-secret").result(WAIT)
        assert (
            changed.account == session.account and changed.revision > session.revision
        )
        with pytest.raises(AuthenticationError):
            owner.login("Worker", SECRET).result(WAIT)
        current = owner.login("Worker", "new-secret").result(WAIT).account
        assert (
            current is not None
            and replace(current, last_login_utc=None, must_change_password=True)
            == operator
        )


def test_administrators_cannot_modify_self_or_peers_and_last_admin_is_transactional(
    database: Path,
) -> None:
    with opened(database) as owner:
        session = owner.login("Admin", SECRET).result(WAIT)
        admin = session.account
        assert admin
        with pytest.raises(AuthorizationError):
            owner.create_account("Second", Role.ADMINISTRATOR, SECRET)
        second = owner.create_account(
            "Second", Role.ADMINISTRATOR, SECRET, confirm_administrator=True
        ).result(WAIT)
        for target in (admin, second):
            for work in (
                owner.set_enabled(target.account_id, False),
                owner.set_role(target.account_id, Role.ENGINEER),
                owner.reset_password(target.account_id, "replacement"),
            ):
                with pytest.raises(AuthorizationError):
                    work.result(WAIT)
        assert owner.snapshot() == session
        assert set(owner.account_management().result(WAIT).accounts) == {admin, second}
    store = AccountStore(database)
    try:
        store.update(second.account_id, enabled=False)
        for change in (
            lambda: store.update(admin.account_id, role=Role.OPERATOR),
            lambda: store.update(admin.account_id, enabled=False),
        ):
            with pytest.raises(LastAdministratorError):
                change()
            assert store.by_id(admin.account_id)[0] == admin
    finally:
        store.close()


def test_late_login_cannot_overwrite_logout_or_later_login(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = Event(), Event()
    original = passwords.verify_password

    def held(password: str, verifier: passwords.PasswordVerifier) -> bool:
        if not release.is_set():
            entered.set()
            assert release.wait(WAIT)
        return original(password, verifier)

    monkeypatch.setattr(identity_module, "verify_password", held)
    with opened(database) as owner:
        try:
            first = owner.login("Admin", SECRET)
            assert entered.wait(WAIT)
            logout = owner.logout()
            second = owner.login("Admin", SECRET)
            assert owner.snapshot() == logout
            release.set()
            with pytest.raises(StaleSessionError):
                first.result(WAIT)
            current = second.result(WAIT)
            assert current.role is Role.ADMINISTRATOR and owner.snapshot() == current
        finally:
            release.set()


def test_late_management_cannot_commit_after_logout(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = Event(), Event()
    original = passwords.hash_password

    def held(password: str) -> passwords.PasswordVerifier:
        entered.set()
        assert release.wait(WAIT)
        return original(password)

    with opened(database) as owner:
        owner.login("Admin", SECRET).result(WAIT)
        monkeypatch.setattr(identity_module, "hash_password", held)
        try:
            pending = owner.create_account("Must not commit", Role.OPERATOR, SECRET)
            assert entered.wait(WAIT)
            owner.logout()
            release.set()
            with pytest.raises(StaleSessionError):
                pending.result(WAIT)
            owner.login("Admin", SECRET).result(WAIT)
            assert [
                a.username for a in owner.account_management().result(WAIT).accounts
            ] == ["Admin"]
        finally:
            release.set()


def test_close_retains_actual_worker_and_rejects_admission(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = Event(), Event()
    original = passwords.verify_password

    def held(password: str, verifier: passwords.PasswordVerifier) -> bool:
        entered.set()
        assert release.wait(WAIT)
        return original(password, verifier)

    monkeypatch.setattr(identity_module, "verify_password", held)
    owner = LocalIdentity(database)
    try:
        owner.ready.result(WAIT)
        pending = owner.login("Admin", SECRET)
        assert entered.wait(WAIT)
        assert not pending.cancel()
        owner.request_stop()
        assert not owner.join(0)
        with pytest.raises(IdentityError):
            owner.login("Admin", SECRET)
        with pytest.raises(SecurityBusyError):
            recover_administrator(database, "Admin", SECRET)
        release.set()
        with pytest.raises(StaleSessionError):
            pending.result(WAIT)
    finally:
        release.set()
        owner.request_stop()
        assert owner.join(WAIT)
    assert owner.storage_snapshot().state is StorageState.CLOSED
    recover_administrator(database, "Admin", SECRET)


class HeldCloseApplication(ProductionApplication):
    def __init__(self, owner: LocalIdentity) -> None:
        self.cleanup_entered = Event()
        self.release_cleanup = Event()
        self.fail_cleanup = False
        super().__init__(lambda _prepared, _generation: FakeSession(), identity=owner)

    def _close_resources(self) -> None:
        self.cleanup_entered.set()
        assert self.release_cleanup.wait(WAIT)
        if self.fail_cleanup:
            raise ApplicationError("Controlled resource cleanup failure")
        super()._close_resources()


@contextmanager
def held_close(owner: LocalIdentity) -> Generator[HeldCloseApplication]:
    app = HeldCloseApplication(owner)
    try:
        yield app
    finally:
        app.fail_cleanup = False
        app.release_cleanup.set()
        close(app)


@pytest.mark.parametrize("recover", [False, True], ids=["success", "recover-and-retry"])
def test_application_close_fences_pending_login_and_queued_account_change(
    database: Path, monkeypatch: pytest.MonkeyPatch, recover: bool
) -> None:
    entered, release = Event(), Event()

    def held(password: str, verifier: passwords.PasswordVerifier) -> bool:
        entered.set()
        assert release.wait(WAIT)
        return passwords.verify_password(password, verifier)

    with opened(database) as owner, held_close(owner) as app:
        actor = owner.login("Admin", SECRET).result(WAIT)
        assert actor.account is not None
        actor_id = actor.account.account_id
        monkeypatch.setattr(identity_module, "verify_password", held)
        try:
            pending = owner.login("Admin", SECRET)
            assert entered.wait(WAIT)
            queued = owner.create_account(
                "Uncommitted", Role.OPERATOR, SECRET, revision=actor.revision
            )
            app.fail_cleanup = recover
            closed = app.close()
            revoked = owner.snapshot()
            assert revoked == SessionSnapshot(actor.revision + 1)
            assert app.cleanup_entered.wait(WAIT)
            assert not closed.done() and not closed.cancel()
            assert app.close() is closed and owner.snapshot() == revoked
            assert owner.storage_snapshot().state is StorageState.READY
            assert not owner.join(0)
            with pytest.raises(IdentityError, match="closing"):
                owner.login("Admin", SECRET)
            with pytest.raises(IdentityError, match="closing"):
                owner.create_account("Rejected", Role.OPERATOR, SECRET)

            fresh: Future[SessionSnapshot] | None = None
            if recover:
                offered: Future[Future[SessionSnapshot]] = Future()

                def reauthenticate(_closed: Future[None]) -> None:
                    try:
                        offered.set_result(owner.login("Admin", SECRET))
                    except IdentityError as exc:
                        offered.set_exception(exc)

                closed.add_done_callback(reauthenticate)
                app.release_cleanup.set()
                with pytest.raises(ApplicationError, match="Close failed"):
                    closed.result(WAIT)
                assert app.snapshot().state is ApplicationState.FAULT and not app.join(
                    0
                )
                assert owner.snapshot() == revoked
                assert owner.begin_startup_login() is None
                fresh = offered.result(WAIT)

            release.set()
            with pytest.raises(StaleSessionError):
                pending.result(WAIT)
            with pytest.raises(StaleSessionError):
                queued.result(WAIT)
            if fresh is not None:
                authenticated = fresh.result(WAIT)
                assert authenticated.revision == revoked.revision + 1
                assert authenticated.role is Role.ADMINISTRATOR
                assert [
                    a.username for a in owner.account_management().result(WAIT).accounts
                ] == ["Admin"]
                app.fail_cleanup = False
                retry = app.close()
                assert retry is not closed
                closed = retry
            else:
                assert not closed.done() and not owner.join(0)
                app.release_cleanup.set()
            closed.result(WAIT)
            assert app.close() is closed
            assert app.join(WAIT) and owner.join(0)
            assert owner.snapshot().role is Role.GUEST
        finally:
            release.set()

    with closing(sqlite3.connect(owner.audit.path)) as db:
        assert set(
            db.execute(
                "SELECT action, actor_id, username, outcome, detail "
                "FROM operations JOIN completions USING (id) WHERE outcome='stale'"
            )
        ) == {
            ("session.login", actor_id, "Admin", "stale", "StaleSessionError"),
            ("account.create", actor_id, "Admin", "stale", "StaleSessionError"),
        }


@pytest.mark.parametrize("operation", ["first-setup", "self-change", "rotation"])
def test_application_close_fences_password_work_before_its_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    path = tmp_path / "security.sqlite3"
    replacement = "uncommitted replacement credential"
    if operation != "first-setup":
        initialize_administrator(path, "Admin", SECRET)
    entered, release = Event(), Event()

    def held(password: str) -> passwords.PasswordVerifier:
        entered.set()
        assert release.wait(WAIT)
        return passwords.hash_password(password)

    with opened(path) as owner, held_close(owner) as app:
        if operation == "rotation":
            owner.login("Admin", SECRET).result(WAIT)
            owner.create_account("Worker", Role.OPERATOR, SECRET).result(WAIT)
            actor = owner.login("Worker", SECRET).result(WAIT)
            assert actor.rotation_required
        elif operation == "self-change":
            actor = owner.login("Admin", SECRET).result(WAIT)
        else:
            actor = owner.snapshot()
        monkeypatch.setattr(identity_module, "hash_password", held)
        try:
            if operation == "first-setup":
                pending = owner.setup_administrator(
                    "Admin", SECRET, revision=actor.revision
                )
            elif operation == "rotation":
                pending = owner.rotate_password(replacement, actor.revision)
            else:
                pending = owner.change_password(
                    SECRET, replacement, revision=actor.revision
                )
            assert entered.wait(WAIT)
            closed = app.close()
            assert app.cleanup_entered.wait(WAIT) and not closed.done()
            assert owner.snapshot() == SessionSnapshot(actor.revision + 1)
            with pytest.raises(IdentityError, match="closing"):
                if operation == "first-setup":
                    owner.setup_administrator("Admin", SECRET)
                elif operation == "rotation":
                    owner.rotate_password(replacement, actor.revision)
                else:
                    owner.change_password(SECRET, replacement)
            release.set()
            with pytest.raises(StaleSessionError):
                pending.result(WAIT)
            assert not closed.done()
        finally:
            release.set()

    if actor.account is None:
        assert not path.exists()
    else:
        with closing(AccountStore(path)) as store:
            account, verifier = store.by_id(actor.account.account_id)
            assert account.must_change_password == (operation == "rotation")
            assert passwords.verify_password(SECRET, verifier)
    with closing(sqlite3.connect(owner.audit.path)) as db:
        assert db.execute(
            "SELECT action, actor_id, outcome FROM operations "
            "JOIN completions USING (id) WHERE outcome='stale'"
        ).fetchall() == [
            (
                "account.create"
                if actor.account is None
                else "account.change_password",
                actor.account.account_id if actor.account is not None else "guest",
                "stale",
            )
        ]


def test_application_close_drains_a_committed_account_change_with_original_actor(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = Event(), Event()
    stopping = Event()
    with opened(database) as owner, held_close(owner) as app:
        actor = owner.login("Admin", SECRET).result(WAIT)
        assert actor.account is not None
        complete = owner.audit.complete
        request_stop = owner.request_stop

        def held(op: AuditOperation, outcome: str, detail: str = "") -> Future[None]:
            if op.action == "account.create":
                entered.set()
                assert release.wait(WAIT)
            return complete(op, outcome, detail)

        def stop_identity() -> None:
            request_stop()
            stopping.set()

        monkeypatch.setattr(owner.audit, "complete", held)
        monkeypatch.setattr(owner, "request_stop", stop_identity)
        try:
            accepted = owner.create_account("Committed", Role.OPERATOR, SECRET)
            assert entered.wait(WAIT)
            closed = app.close()
            assert app.cleanup_entered.wait(WAIT)
            assert owner.snapshot() == SessionSnapshot(actor.revision + 1)
            assert not stopping.is_set()
            app.release_cleanup.set()
            assert stopping.wait(WAIT)
            assert not accepted.done() and not closed.done() and not owner.join(0)
            release.set()
            account = accepted.result(WAIT)
            closed.result(WAIT)
        finally:
            release.set()

    with closing(AccountStore(database)) as store:
        assert store.by_id(account.account_id)[0] == account
    with closing(sqlite3.connect(owner.audit.path)) as db:
        assert db.execute(
            "SELECT actor_id, username, target, outcome FROM operations "
            "JOIN completions USING (id) WHERE action='account.create' AND target=?",
            (account.account_id,),
        ).fetchall() == [
            (actor.account.account_id, "Admin", account.account_id, "success")
        ]


def test_authentication_queue_is_bounded_and_logout_invalidates_all_pending(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = Event(), Event()
    original = passwords.verify_password

    def held(password: str, verifier: passwords.PasswordVerifier) -> bool:
        entered.set()
        assert release.wait(WAIT)
        return original(password, verifier)

    monkeypatch.setattr(identity_module, "verify_password", held)
    with opened(database) as owner:
        try:
            first = owner.login("Admin", SECRET)
            assert entered.wait(WAIT)
            queued = [owner.login("Admin", SECRET) for _ in range(16)]
            before = owner.snapshot()
            with pytest.raises(SecurityBusyError):
                owner.login("Admin", SECRET)
            assert owner.snapshot() == before
            logout = owner.logout()
            release.set()
            for pending in (first, *queued):
                with pytest.raises(StaleSessionError):
                    pending.result(WAIT)
            assert owner.snapshot() == logout
        finally:
            release.set()


def test_identity_import_does_not_import_qt() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from capsule_vision.application.identity import LocalIdentity; assert not any(x.startswith('PySide6') for x in sys.modules)",
        ],
        capture_output=True,
        text=True,
        timeout=WAIT,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "damage", ["bytes", "version", "verifier", "schema", "application-id"]
)
def test_corrupt_or_unsupported_store_is_visible_and_never_replaced(
    database: Path, damage: str
) -> None:
    if damage == "bytes":
        database.write_bytes(b"not a sqlite database")
    else:
        with sqlite3.connect(database) as connection:
            connection.execute(
                {
                    "version": "PRAGMA user_version=999",
                    "verifier": "UPDATE accounts SET password_version=999",
                    "schema": "CREATE TABLE unexpected(x TEXT)",
                    "application-id": "PRAGMA application_id=0",
                }[damage]
            )
    before = database.read_bytes()
    with opened(database) as owner:
        assert owner.storage_snapshot().state is StorageState.UNAVAILABLE
        assert owner.storage_snapshot().issue and owner.snapshot().role is Role.GUEST
        with pytest.raises(SecurityStorageError):
            owner.login("Admin", SECRET).result(WAIT)
    assert database.read_bytes() == before
    with pytest.raises((sqlite3.Error, SecurityStorageError)):
        initialize_administrator(database, "Replacement", SECRET)
    assert database.read_bytes() == before


def test_unwritable_store_failure_is_observable(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def denied(_path: Path) -> None:
        raise PermissionError("test-only denial")

    monkeypatch.setattr(identity_module, "AccountStore", denied)
    with opened(database) as owner:
        assert owner.storage_snapshot().state is StorageState.UNAVAILABLE
        assert "PermissionError" in owner.storage_snapshot().issue
        assert owner.snapshot().role is Role.GUEST


def test_offline_recovery_preserves_records_and_does_not_promote_operators(
    database: Path,
) -> None:
    with opened(database) as owner:
        admin = owner.login("Admin", SECRET).result(WAIT).account
        operator = owner.create_account("Worker", Role.OPERATOR, SECRET).result(WAIT)
    with pytest.raises(AccountConflictError):
        initialize_administrator(database, "Another", SECRET)
    with pytest.raises(AccountConflictError):
        recover_administrator(database, "Worker", "new-secret")
    recovered = recover_administrator(database, "ADMIN", "new-secret")
    assert admin is not None and recovered.account_id == admin.account_id
    with opened(database) as owner:
        current = owner.login("Admin", "new-secret").result(WAIT).account
        assert current is not None
        assert replace(current, last_login_utc=recovered.last_login_utc) == recovered
        rotated = owner.rotate_password(
            "rotated secret", owner.snapshot().revision
        ).result(WAIT)
        assert owner.account_management().result(WAIT).accounts == (
            rotated.account,
            operator,
        )


def test_maintenance_is_rejected_cross_process_even_when_database_missing(
    tmp_path: Path,
) -> None:
    path = tmp_path / "data/security.sqlite3"
    code = """
import sys
from pathlib import Path
from capsule_vision.application.account_maintenance import initialize_administrator
from capsule_vision.application.identity_models import SecurityBusyError
try:
    initialize_administrator(Path(sys.argv[1]), 'Admin', sys.stdin.read())
except SecurityBusyError:
    print('busy')
else:
    print('initialized')
"""
    with opened(path):
        with pytest.raises(SecurityBusyError):
            LocalIdentity(path)
        child = subprocess.run(
            [sys.executable, "-c", code, str(path)],
            input=SECRET,
            text=True,
            capture_output=True,
            timeout=WAIT,
        )
        assert child.returncode == 0 and child.stdout.strip() == "busy"
        assert not path.exists()
    child = subprocess.run(
        [sys.executable, "-c", code, str(path)],
        input=SECRET,
        text=True,
        capture_output=True,
        timeout=WAIT,
    )
    assert child.returncode == 0 and child.stdout.strip() == "initialized"


def test_offline_recovery_can_enable_an_existing_disabled_administrator(
    database: Path,
) -> None:
    with opened(database) as owner:
        admin = owner.login("Admin", SECRET).result(WAIT).account
        assert admin is not None
        second = owner.create_account(
            "Second", Role.ADMINISTRATOR, SECRET, confirm_administrator=True
        ).result(WAIT)
    # A disabled administrator can exist in customer databases from earlier versions.
    with sqlite3.connect(database) as db:
        db.execute(
            "UPDATE accounts SET enabled=0 WHERE account_id=?", (admin.account_id,)
        )
    restored = recover_administrator(database, "Admin", "recovered-secret")
    assert restored.account_id == admin.account_id and restored.enabled
    with opened(database) as owner:
        current = owner.login("Admin", "recovered-secret").result(WAIT).account
        assert current is not None
        assert replace(current, last_login_utc=restored.last_login_utc) == restored
        rotated = owner.rotate_password(
            "permanent credential", owner.snapshot().revision
        ).result(WAIT)
        assert owner.account_management().result(WAIT).accounts == (
            rotated.account,
            second,
        )
