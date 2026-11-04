"""Real local credentials/transactions, isolated roots and controlled worker races."""

import hashlib
import hmac
import sqlite3
import subprocess
import sys
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from threading import Event

import pytest

from capsule_vision.application import identity as identity_module
from capsule_vision.application import passwords
from capsule_vision.application.account_maintenance import (
    initialize_administrator,
    recover_administrator,
)
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
        assert len(owner.list_accounts().result(WAIT)) == 2
    assert SECRET.encode() not in database.read_bytes()
    assert b"operator-secret" not in database.read_bytes()
    with opened(database) as owner:
        with pytest.raises(AuthenticationError, match="Invalid username or password"):
            owner.login("STRASSE", "operator-secret").result(WAIT)
        assert owner.snapshot().role is Role.GUEST
        owner.login("Admin", SECRET).result(WAIT)
        owner.set_enabled(account.account_id, True).result(WAIT)
        session = owner.login("strasse", "operator-secret").result(WAIT)
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
        accounts = owner.list_accounts().result(WAIT)
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
        session = owner.login("Worker", SECRET).result(WAIT)
        for operation in (
            lambda: owner.list_accounts(),
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
        changed = owner.change_password(SECRET, "new-secret").result(WAIT)
        assert (
            changed.account == session.account and changed.revision > session.revision
        )
        with pytest.raises(AuthenticationError):
            owner.login("Worker", SECRET).result(WAIT)
        current = owner.login("Worker", "new-secret").result(WAIT).account
        assert current is not None and replace(current, last_login_utc=None) == operator


def test_last_admin_rollback_and_immediate_session_reconciliation(
    database: Path,
) -> None:
    with opened(database) as owner:
        session = owner.login("Admin", SECRET).result(WAIT)
        admin = session.account
        assert admin is not None
        for operation in (
            lambda: owner.set_enabled(admin.account_id, False),
            lambda: owner.set_role(admin.account_id, Role.ENGINEER),
        ):
            with pytest.raises(LastAdministratorError):
                operation().result(WAIT)
            assert owner.snapshot() == session
            assert owner.list_accounts().result(WAIT) == (admin,)
        owner.create_account("Second", Role.ADMINISTRATOR, SECRET).result(WAIT)
        changed = owner.set_role(admin.account_id, Role.ENGINEER).result(WAIT)
        assert (
            owner.snapshot().account == changed
            and owner.snapshot().revision > session.revision
        )
        assert session.role is Role.ADMINISTRATOR  # Old observations never mutate.
        with pytest.raises(AuthorizationError):
            owner.list_accounts()
        owner.login("Second", SECRET).result(WAIT)
        second = owner.snapshot().account
        assert second is not None
        owner.set_role(admin.account_id, Role.ADMINISTRATOR).result(WAIT)
        owner.set_enabled(second.account_id, False).result(WAIT)
        assert owner.snapshot().role is Role.GUEST
        owner.login("Admin", SECRET).result(WAIT)
        owner.reset_password(admin.account_id, "reset-secret").result(WAIT)
        assert owner.snapshot().role is Role.GUEST
        with pytest.raises(AuthenticationError):
            owner.login("Admin", SECRET).result(WAIT)
        assert (
            owner.login("Admin", "reset-secret").result(WAIT).role is Role.ADMINISTRATOR
        )


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
            assert [a.username for a in owner.list_accounts().result(WAIT)] == ["Admin"]
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
        assert owner.list_accounts().result(WAIT) == (current, operator)


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
        second = owner.create_account("Second", Role.ADMINISTRATOR, SECRET).result(WAIT)
        owner.set_enabled(admin.account_id, False).result(WAIT)
        assert owner.snapshot().role is Role.GUEST
    restored = recover_administrator(database, "Admin", "recovered-secret")
    assert restored.account_id == admin.account_id and restored.enabled
    with opened(database) as owner:
        current = owner.login("Admin", "recovered-secret").result(WAIT).account
        assert current is not None
        assert replace(current, last_login_utc=restored.last_login_utc) == restored
        assert owner.list_accounts().result(WAIT) == (current, second)
