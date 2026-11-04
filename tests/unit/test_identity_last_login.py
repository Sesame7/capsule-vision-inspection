"""Schema migration and successful-login metadata, using disposable stores only."""

import hashlib
import sqlite3
from collections.abc import Generator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest

from capsule_vision.application import identity as identity_module
from capsule_vision.application._account_store import AccountStore
from capsule_vision.application.account_maintenance import (
    initialize_administrator,
    recover_administrator,
)
from capsule_vision.application.identity import LocalIdentity
from capsule_vision.application.identity_models import (
    AuthenticationError,
    Role,
    SecurityStorageError,
    SessionSnapshot,
    StaleSessionError,
    StorageState,
)
from capsule_vision.application.passwords import (
    PasswordVerifier,
    hash_password,
    verify_password,
)

SECRET = "isolated migration credential"
V1_SCHEMA = """
CREATE TABLE accounts (
    account_id TEXT PRIMARY KEY NOT NULL,
    username TEXT NOT NULL,
    username_key TEXT NOT NULL UNIQUE,
    role INTEGER NOT NULL CHECK(role IN (1,2,3)),
    enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
    password_version INTEGER NOT NULL,
    iterations INTEGER NOT NULL,
    salt BLOB NOT NULL,
    digest BLOB NOT NULL
);
"""


@pytest.fixture
def v1_store(tmp_path: Path) -> Path:
    path = tmp_path / "security.sqlite3"
    salt = bytes(range(16))
    digest = hashlib.pbkdf2_hmac("sha256", SECRET.encode(), salt, 100_000)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(V1_SCHEMA)
        connection.execute("PRAGMA application_id=0x43564953")
        connection.execute("PRAGMA user_version=1")
        for name, role, enabled in (
            ("Admin", 3, 1),
            ("Worker", 1, 1),
            ("Straße", 2, 0),
        ):
            connection.execute(
                "INSERT INTO accounts VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    uuid4().hex,
                    name,
                    name.casefold(),
                    role,
                    enabled,
                    1,
                    100_000,
                    salt,
                    digest,
                ),
            )
    return path


@contextmanager
def opened(path: Path) -> Generator[LocalIdentity]:
    owner = LocalIdentity(path)
    try:
        owner.ready.result(10)
        yield owner
    finally:
        owner.request_stop()
        assert owner.join(10)


def login_values(path: Path) -> dict[str, str | None]:
    with closing(sqlite3.connect(path)) as connection, connection:
        return dict(connection.execute("SELECT username, last_login_utc FROM accounts"))


def test_valid_v1_migrates_once_preserving_every_existing_field(v1_store: Path) -> None:
    with closing(sqlite3.connect(v1_store)) as connection, connection:
        before = connection.execute(
            "SELECT * FROM accounts ORDER BY username_key"
        ).fetchall()
    for _ in range(2):
        store = AccountStore(v1_store)
        try:
            assert store.connection.execute("PRAGMA user_version").fetchone() == (2,)
            after = store.connection.execute(
                "SELECT * FROM accounts ORDER BY username_key"
            ).fetchall()
            assert [row[:-1] for row in after] == before
            assert all(row[-1] is None for row in after)
            for name in ("ADMIN", "worker", "STRASSE"):
                record = store.by_name(name)
                assert record is not None and record[0].last_login_utc is None
                assert verify_password(SECRET, record[1])
            assert [a.enabled for a in store.accounts()] == [True, False, True]
        finally:
            store.close()


def test_new_store_is_v2_without_a_login_or_guest_record(tmp_path: Path) -> None:
    path = tmp_path / "data/security.sqlite3"
    admin = initialize_administrator(path, "Admin", SECRET)
    assert admin.last_login_utc is None
    with closing(sqlite3.connect(path)) as connection, connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (2,)
        assert connection.execute(
            "SELECT username, last_login_utc FROM accounts"
        ).fetchall() == [("Admin", None)]
    with opened(path) as owner:
        assert owner.snapshot().role is Role.GUEST
    assert login_values(path) == {"Admin": None}


@pytest.mark.parametrize("failure", ["alter", "version", "commit"])
def test_migration_failure_rolls_back_schema_version_and_accounts(
    v1_store: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    connect = sqlite3.connect
    before = v1_store.read_bytes()

    def denied(
        database: str, *, uri: bool, timeout: float, isolation_level: None
    ) -> sqlite3.Connection:
        connection = connect(
            database, uri=uri, timeout=timeout, isolation_level=isolation_level
        )

        def authorize(
            action: int,
            arg1: str | None,
            arg2: str | None,
            _db: str | None,
            _trigger: str | None,
        ) -> int:
            deny = (
                (failure == "alter" and action == sqlite3.SQLITE_ALTER_TABLE)
                or (
                    failure == "version"
                    and action == sqlite3.SQLITE_PRAGMA
                    and arg1 == "user_version"
                    and arg2 == "2"
                )
                or (
                    failure == "commit"
                    and action == sqlite3.SQLITE_TRANSACTION
                    and arg1 == "COMMIT"
                )
            )
            return sqlite3.SQLITE_DENY if deny else sqlite3.SQLITE_OK

        connection.set_authorizer(authorize)
        return connection

    monkeypatch.setattr(sqlite3, "connect", denied)
    with pytest.raises(sqlite3.DatabaseError):
        AccountStore(v1_store)
    assert v1_store.read_bytes() == before
    with closing(connect(v1_store)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)
        assert len(connection.execute("PRAGMA table_info(accounts)").fetchall()) == 9


@pytest.mark.parametrize("damage", ["version", "schema", "verifier", "bytes"])
def test_invalid_v1_is_rejected_without_migration(v1_store: Path, damage: str) -> None:
    if damage == "bytes":
        v1_store.write_bytes(b"invalid isolated security storage")
    else:
        with closing(sqlite3.connect(v1_store)) as connection, connection:
            connection.execute(
                {
                    "version": "PRAGMA user_version=3",
                    "schema": "CREATE TABLE extra(x)",
                    "verifier": "UPDATE accounts SET iterations=1",
                }[damage]
            )
    before = v1_store.read_bytes()
    with opened(v1_store) as owner:
        assert owner.storage_snapshot().state is StorageState.UNAVAILABLE
        assert owner.snapshot().role is Role.GUEST
    assert v1_store.read_bytes() == before


def test_login_persists_utc_before_success_and_account_operations_preserve_it(
    v1_store: Path,
) -> None:
    with opened(v1_store) as owner:
        before = datetime.now(UTC)
        first = owner.login("Admin", SECRET).result(10)
        assert first.account is not None
        stamp = first.account.last_login_utc
        assert stamp is not None and before <= stamp <= datetime.now(UTC)
        assert stamp.tzinfo is UTC
        assert login_values(v1_store)["Admin"] == stamp.isoformat(
            timespec="microseconds"
        )
        worker = next(
            a for a in owner.list_accounts().result(10) if a.username == "Worker"
        )
        backup = owner.create_account("Backup", Role.ADMINISTRATOR, SECRET).result(10)
        assert backup.last_login_utc is None
        owner.change_password(SECRET, "new credential").result(10)
        owner.set_role(first.account.account_id, Role.ENGINEER).result(10)
        current = owner.snapshot().account
        assert current is not None and current.last_login_utc == stamp
        owner.logout()
        assert login_values(v1_store)["Admin"] == stamp.isoformat(
            timespec="microseconds"
        )
        owner.login("Backup", SECRET).result(10)
        owner.reset_password(first.account.account_id, SECRET).result(10)
        owner.set_enabled(first.account.account_id, False).result(10)
        owner.set_enabled(first.account.account_id, True).result(10)
        owner.set_role(worker.account_id, Role.ENGINEER).result(10)
        assert login_values(v1_store)["Admin"] == stamp.isoformat(
            timespec="microseconds"
        )
        assert login_values(v1_store)["Worker"] is None
    recover_administrator(v1_store, "Backup", SECRET)
    assert login_values(v1_store)["Admin"] == stamp.isoformat(timespec="microseconds")
    with opened(v1_store) as owner:
        second = owner.login("Admin", SECRET).result(10)
        assert second.account is not None and second.account.last_login_utc is not None
        assert second.account.last_login_utc > stamp
        assert (
            first.account.last_login_utc == stamp
        )  # Earlier observations remain immutable.


@pytest.mark.parametrize(
    "name,password",
    [("Admin", "wrong"), ("unknown", SECRET), ("STRASSE", SECRET)],
    ids=["wrong-password", "unknown-account", "disabled-account"],
)
def test_failed_or_disabled_login_leaves_metadata_untouched(
    v1_store: Path, name: str, password: str
) -> None:
    with opened(v1_store) as owner:
        current = owner.login("Admin", SECRET).result(10)
        metadata = login_values(v1_store)
        with pytest.raises(AuthenticationError):
            owner.login(name, password).result(10)
        assert owner.snapshot() == current
        assert login_values(v1_store) == metadata


@pytest.mark.parametrize(
    "later", ["cancel", "logout", "login", "failed-login", "close"]
)
def test_stale_verified_login_does_not_write_timestamp(
    v1_store: Path, monkeypatch: pytest.MonkeyPatch, later: str
) -> None:
    entered, release = Event(), Event()

    def held(password: str, verifier: PasswordVerifier) -> bool:
        entered.set()
        assert release.wait(10)
        return verify_password(password, verifier)

    with opened(v1_store) as owner:
        current = owner.login("Admin", SECRET).result(10)
        metadata = login_values(v1_store)
        monkeypatch.setattr(identity_module, "verify_password", held)
        try:
            first = owner.login("Worker", SECRET)
            assert entered.wait(10)
            assert owner.snapshot() == current
            second = (
                owner.login("Admin" if later == "login" else "Unknown", SECRET)
                if later in ("login", "failed-login")
                else None
            )
            if second is not None:
                assert owner.snapshot() == current
                assert not owner.cancel_login(first)  # Does not cancel its successor.
            if later == "cancel":
                assert owner.cancel_login(first)
                assert not owner.cancel_login(first)
                assert owner.snapshot() == current
            if later == "logout":
                owner.logout()
            if later == "close":
                owner.request_stop()
            release.set()
            with pytest.raises(StaleSessionError):
                first.result(10)
            if second is not None:
                if later == "login":
                    assert second.result(10).revision == current.revision + 1
                else:
                    with pytest.raises(AuthenticationError):
                        second.result(10)
            if later in ("cancel", "failed-login"):
                assert owner.snapshot() == current
            if later != "login":
                assert login_values(v1_store) == metadata
            assert login_values(v1_store)["Worker"] is None
        finally:
            release.set()


def test_timestamp_write_failure_never_publishes_success(v1_store: Path) -> None:
    with opened(v1_store) as owner:
        current = owner.login("Admin", SECRET).result(10)
        metadata = login_values(v1_store)
        # Installed after schema validation: this is a deterministic storage-failure fixture.
        with closing(sqlite3.connect(v1_store)) as connection, connection:
            connection.execute(
                "CREATE TRIGGER deny_login BEFORE UPDATE OF last_login_utc ON accounts BEGIN SELECT RAISE(ABORT, 'isolated write failure'); END"
            )
        with pytest.raises(SecurityStorageError):
            owner.login("Worker", SECRET).result(10)
        assert owner.snapshot().role is Role.GUEST
        assert owner.snapshot().revision == current.revision + 1
        assert owner.storage_snapshot().state is StorageState.UNAVAILABLE
    assert login_values(v1_store) == metadata


def test_replacement_and_same_account_login_each_publish_one_revision(
    v1_store: Path,
) -> None:
    with opened(v1_store) as owner:
        current = owner.login("Admin", SECRET).result(10)
        for username in ("Worker", "Worker"):
            previous = current
            current = owner.login(username, SECRET).result(10)
            assert current.revision == previous.revision + 1
            assert owner.snapshot() == current
            assert current.account is not None and current.account.username == username
            assert current.account.last_login_utc is not None
            assert login_values(v1_store)[
                username
            ] == current.account.last_login_utc.isoformat(timespec="microseconds")


def test_cancellation_after_commit_before_future_delivery_cannot_undo_login(
    v1_store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    verifying, verify_release, committed, deliver_release = (Event() for _ in range(4))

    def held(password: str, verifier: PasswordVerifier) -> bool:
        verifying.set()
        assert verify_release.wait(10)
        return verify_password(password, verifier)

    with opened(v1_store) as owner:
        current = owner.login("Admin", SECRET).result(10)
        monkeypatch.setattr(identity_module, "verify_password", held)
        try:
            attempt = owner.login("Worker", SECRET)
            assert verifying.wait(10)
            deliver = attempt.set_result

            def held_delivery(value: SessionSnapshot) -> None:
                committed.set()
                assert deliver_release.wait(10)
                deliver(value)

            monkeypatch.setattr(attempt, "set_result", held_delivery)
            verify_release.set()
            assert committed.wait(10)
            assert not attempt.done()
            assert not owner.cancel_login(attempt)
            replacement = owner.snapshot()
            assert replacement.role is Role.OPERATOR
            assert replacement.revision == current.revision + 1
            assert login_values(v1_store)["Worker"] is not None
            deliver_release.set()
            assert attempt.result(10) == replacement
        finally:
            verify_release.set()
            deliver_release.set()


@pytest.mark.parametrize("change", ["role", "disable", "reset", "self-password"])
def test_committed_current_account_change_invalidates_pending_authentication(
    v1_store: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    entered, release = Event(), Event()

    def held(password: str) -> PasswordVerifier:
        entered.set()
        assert release.wait(10)
        return hash_password(password)

    with opened(v1_store) as owner:
        current = owner.login("Admin", SECRET).result(10)
        assert current.account is not None
        account_id = current.account.account_id
        metadata = login_values(v1_store)
        monkeypatch.setattr(identity_module, "hash_password", held)
        try:
            # Hold the serial worker outside its session lock so both the account
            # change and authentication are admitted under the original revision.
            backup = owner.create_account("Backup", Role.ADMINISTRATOR, SECRET)
            assert entered.wait(10)
            match change:
                case "role":
                    update = owner.set_role(account_id, Role.ENGINEER)
                case "disable":
                    update = owner.set_enabled(account_id, False)
                case "reset":
                    update = owner.reset_password(account_id, "replacement credential")
                case _:
                    update = owner.change_password(SECRET, "replacement credential")
            attempt = owner.login("Worker", SECRET)
            assert owner.snapshot() == current
            release.set()
            backup.result(10)
            update.result(10)
            changed = owner.snapshot()
            with pytest.raises(StaleSessionError):
                attempt.result(10)
            assert owner.snapshot() == changed
            assert changed.revision == current.revision + 1
            assert changed.role is (
                Role.ENGINEER
                if change == "role"
                else Role.ADMINISTRATOR
                if change == "self-password"
                else Role.GUEST
            )
            assert login_values(v1_store) == {**metadata, "Backup": None}
        finally:
            release.set()


@pytest.mark.parametrize(
    "stamp", ["2026-11-01", "2026-11-01T12:00:00.000000+08:00", "broken"]
)
def test_invalid_v2_timestamp_is_visible_and_never_replaced(
    v1_store: Path, stamp: str
) -> None:
    store = AccountStore(v1_store)
    store.close()
    with closing(sqlite3.connect(v1_store)) as connection, connection:
        connection.execute("UPDATE accounts SET last_login_utc=?", (stamp,))
    before = v1_store.read_bytes()
    with opened(v1_store) as owner:
        assert owner.storage_snapshot().state is StorageState.UNAVAILABLE
    assert v1_store.read_bytes() == before


@pytest.mark.parametrize("version", [1, 2])
def test_readonly_storage_fails_visibly_without_replacement(
    v1_store: Path, monkeypatch: pytest.MonkeyPatch, version: int
) -> None:
    if version == 2:
        store = AccountStore(v1_store)
        store.close()
    before = v1_store.read_bytes()
    connect = sqlite3.connect

    def readonly(
        database: str, *, uri: bool, timeout: float, isolation_level: None
    ) -> sqlite3.Connection:
        return connect(
            database.replace("?mode=rw", "?mode=ro"),
            uri=uri,
            timeout=timeout,
            isolation_level=isolation_level,
        )

    monkeypatch.setattr(sqlite3, "connect", readonly)
    with opened(v1_store) as owner:
        assert owner.storage_snapshot().state is StorageState.UNAVAILABLE
        assert owner.snapshot().role is Role.GUEST
    assert v1_store.read_bytes() == before
