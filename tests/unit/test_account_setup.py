"""Online setup, temporary credentials and OS-trusted recovery on disposable databases."""

import sqlite3
from concurrent.futures import Future
from contextlib import closing
from pathlib import Path
from threading import Event

import pytest
from tests.unit.test_identity import SECRET, WAIT, opened

from capsule_vision.application import identity as service
from capsule_vision.application._account_store import AccountStore
from capsule_vision.application.account_maintenance import (
    initialize_administrator,
    recover_administrator,
)
from capsule_vision.application.audit import AuditUnavailable
from capsule_vision.application.identity_models import (
    AccountConflictError,
    AuthenticationError,
    AuthorizationError,
    PasswordReuseError,
    Role,
    SecurityStorageError,
    SessionSnapshot,
    StaleSessionError,
    StorageState,
)
from capsule_vision.application.passwords import PasswordVerifier, hash_password


@pytest.mark.parametrize("existing_empty", [False, True])
def test_online_first_administrator_is_exclusive_and_does_not_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing_empty: bool
) -> None:
    path = tmp_path / "security.sqlite3"
    if existing_empty:
        AccountStore(path, initialize=True).close()
    entered, release = Event(), Event()

    def held(password: str) -> PasswordVerifier:
        entered.set()
        assert release.wait(WAIT)
        return hash_password(password)

    monkeypatch.setattr(service, "hash_password", held)
    with opened(path) as owner:
        assert owner.storage_snapshot().uninitialized
        guest = owner.snapshot()
        try:
            first = owner.setup_administrator("First", SECRET)
            assert entered.wait(WAIT)
            second = owner.setup_administrator("Racing", SECRET)
            release.set()
            account = first.result(WAIT)
            with pytest.raises(AccountConflictError):
                second.result(WAIT)
        finally:
            release.set()
        assert not account.must_change_password and account.last_login_utc is None
        assert (
            owner.snapshot() == guest
            and owner.storage_snapshot().state is StorageState.READY
        )
        assert owner.login("First", SECRET).result(WAIT).role is Role.ADMINISTRATOR
        assert len(owner.list_accounts().result(WAIT)) == 1
    with closing(sqlite3.connect(path.with_name("audit.sqlite3"))) as db:
        assert db.execute(
            "SELECT actor_id,target,outcome FROM operations JOIN completions USING(id) WHERE action='account.create' AND outcome='success'"
        ).fetchall() == [("guest", account.account_id, "success")]


def test_invalid_or_unavailable_store_never_offers_setup(tmp_path: Path) -> None:
    path = tmp_path / "security.sqlite3"
    path.write_bytes(b"damaged")
    with opened(path) as owner:
        assert not owner.storage_snapshot().uninitialized
        with pytest.raises(AccountConflictError):
            owner.setup_administrator("First", SECRET)
    assert path.read_bytes() == b"damaged"


def test_dismissing_submitted_first_setup_before_commit_preserves_missing_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "security.sqlite3"
    entered, release = Event(), Event()

    def held(password: str) -> PasswordVerifier:
        entered.set()
        assert release.wait(WAIT)
        return hash_password(password)

    with opened(path) as owner:
        monkeypatch.setattr(service, "hash_password", held)
        revision = owner.snapshot().revision
        operation = owner.setup_administrator("First", SECRET, revision=revision)
        try:
            assert entered.wait(WAIT)
            owner.logout(revision=revision)
        finally:
            release.set()
        with pytest.raises(StaleSessionError):
            operation.result(WAIT)
        assert owner.snapshot().account is None
        assert owner.storage_snapshot().uninitialized and not path.exists()


def test_failed_rotation_transaction_retains_temporary_verifier_after_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "security.sqlite3"
    initialize_administrator(path, "Admin", SECRET)
    with opened(path) as owner:
        owner.login("Admin", SECRET).result(WAIT)
        owner.create_account("Tech", Role.ENGINEER, SECRET).result(WAIT)
        pending = owner.login("Tech", SECRET).result(WAIT)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute(
                "CREATE TRIGGER reject_change BEFORE UPDATE OF digest ON accounts BEGIN SELECT RAISE(ABORT,'isolated write failure'); END"
            )
        with pytest.raises(SecurityStorageError):
            owner.rotate_password("new local credential", pending.revision).result(WAIT)
        assert (
            owner.snapshot().account is None
            and owner.storage_snapshot().state is StorageState.UNAVAILABLE
        )
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("DROP TRIGGER reject_change")
    with opened(path) as owner:
        with pytest.raises(AuthenticationError):
            owner.login("Tech", "new local credential").result(WAIT)
        assert owner.login("Tech", SECRET).result(WAIT).rotation_required


def test_late_file_cannot_be_adopted_by_a_pending_first_setup(tmp_path: Path) -> None:
    path = tmp_path / "security.sqlite3"
    with opened(path) as owner:
        path.write_bytes(b"unexpected occupant")
        with pytest.raises(SecurityStorageError):
            owner.setup_administrator("First", SECRET).result(WAIT)
        assert owner.storage_snapshot().state is StorageState.UNAVAILABLE
        assert owner.snapshot().account is None
    assert path.read_bytes() == b"unexpected occupant"


def test_audit_loss_blocks_online_setup_but_offline_recovery_preserves_identity(
    tmp_path: Path,
) -> None:
    path = tmp_path / "security.sqlite3"
    path.with_name("audit.sqlite3").mkdir()
    with opened(path) as owner:
        with pytest.raises(AuditUnavailable):
            owner.setup_administrator("First", SECRET).result(WAIT)
        assert not path.exists() and owner.storage_snapshot().uninitialized
    losses: list[bool] = []
    admin = initialize_administrator(
        path, "First", SECRET, audit_warning=lambda: losses.append(True)
    )
    recovered = recover_administrator(
        path, "First", "replacement", audit_warning=lambda: losses.append(True)
    )
    assert recovered.account_id == admin.account_id and recovered.must_change_password
    assert losses == [True, True]
    with opened(path) as owner:
        pending = owner.login("First", "replacement").result(WAIT)
        with pytest.raises(AuditUnavailable):
            owner.rotate_password("user-selected replacement", pending.revision).result(
                WAIT
            )
        assert owner.snapshot() == pending


def test_rotation_is_durable_restricted_and_revision_fenced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "security.sqlite3"
    initialize_administrator(path, "Admin", SECRET)
    with opened(path) as owner:
        owner.login("Admin", SECRET).result(WAIT)
        account = owner.create_account(
            "Line", Role.OPERATOR, "temporary credential"
        ).result(WAIT)
        assert account.must_change_password
        pending = owner.login("Line", "temporary credential").result(WAIT)
        assert pending.account and pending.account.account_id == account.account_id
        for role in Role:
            with pytest.raises(AuthorizationError), owner.admission(role):
                pytest.fail("Unrotated credential admitted a command")
        with pytest.raises(AuthorizationError):
            owner.login("Admin", SECRET)
        with pytest.raises(AuthorizationError):
            owner.list_accounts()
        with pytest.raises(AuthorizationError):
            owner.change_password("temporary credential", SECRET)
        with pytest.raises(PasswordReuseError):
            owner.rotate_password("temporary credential", pending.revision).result(WAIT)
        assert owner.snapshot() == pending
        entered, release = Event(), Event()

        def held(password: str) -> PasswordVerifier:
            entered.set()
            assert release.wait(WAIT)
            return hash_password(password)

        with monkeypatch.context() as context:
            context.setattr(service, "hash_password", held)
            try:
                attempt = owner.rotate_password("superseded password", pending.revision)
                assert entered.wait(WAIT)
                guest = owner.logout()
                release.set()
                with pytest.raises(StaleSessionError):
                    attempt.result(WAIT)
                assert owner.snapshot() == guest
            finally:
                release.set()
    with opened(path) as owner:
        with pytest.raises(AuthenticationError):
            owner.login("Line", "superseded password").result(WAIT)
        pending = owner.login("Line", "temporary credential").result(WAIT)
        assert pending.account and pending.rotation_required
        calls: list[Future[SessionSnapshot]] = []
        for value in ("private replacement", "stale replacement"):
            try:
                calls.append(owner.rotate_password(value, pending.revision))
            except StaleSessionError:
                pass  # First commit already won admission.
        active = calls[0].result(WAIT)
        for later in calls[1:]:
            with pytest.raises(StaleSessionError):
                later.result(WAIT)
        assert owner.logout(revision=pending.revision) == active
        assert active.role is Role.OPERATOR and active.revision > pending.revision
        assert (
            active.account
            and active.account.last_login_utc == pending.account.last_login_utc
        )
    with opened(path) as owner:
        assert (
            owner.login("Line", "private replacement").result(WAIT).role
            is Role.OPERATOR
        )
        with pytest.raises(AuthenticationError):
            owner.login("Line", "temporary credential").result(WAIT)


def test_offline_recovery_audit_distinguishes_os_actor_and_stable_target(
    tmp_path: Path,
) -> None:
    path = tmp_path / "security.sqlite3"
    admin = initialize_administrator(path, "First", SECRET)
    recover_administrator(path, "FIRST", "temporary for recovery")
    with closing(sqlite3.connect(path.with_name("audit.sqlite3"))) as db:
        assert db.execute(
            "SELECT actor_id, action, target, outcome FROM operations JOIN completions USING(id) ORDER BY operations.rowid"
        ).fetchall() == [
            ("offline-maintenance", "account.create", admin.account_id, "success"),
            (
                "offline-maintenance",
                "account.reset_password",
                admin.account_id,
                "success",
            ),
        ]
