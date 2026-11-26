"""Account schema admission and successful-login metadata on disposable stores."""

import hashlib
import sqlite3
from collections.abc import Generator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest
from tests.support.startup import close

from capsule_vision.application import identity as identity_module
from capsule_vision.application._account_store import AccountStore
from capsule_vision.application.account_maintenance import (
    initialize_administrator,
    recover_administrator,
)
from capsule_vision.application.identity import LocalIdentity
from capsule_vision.application.identity_models import (
    AccountConflictError,
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
from capsule_vision.application.startup import ApplicationData, load_startup

SECRET = "isolated account credential"
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


def write_account_fixture(path: Path, version: int) -> Path:
    """Use the original table definitions, including SQLite's ALTER formatting for v4."""
    salt = bytes(range(16))
    digest = hashlib.pbkdf2_hmac("sha256", SECRET.encode(), salt, 100_000)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(V1_SCHEMA)
        connection.execute("PRAGMA application_id=0x43564953")
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
        if version >= 2:
            connection.execute("ALTER TABLE accounts ADD COLUMN last_login_utc TEXT")
        if version >= 3:
            connection.execute(
                "ALTER TABLE accounts ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0 CHECK(must_change_password IN (0,1))"
            )
        if version == 4:
            connection.execute(
                """CREATE TABLE auto_login (
                    slot INTEGER PRIMARY KEY CHECK (slot = 1),
                    account_id TEXT NOT NULL REFERENCES accounts(account_id)
                )"""
            )
        connection.execute(f"PRAGMA user_version={version}")
    return path


@pytest.fixture
def account_db(tmp_path: Path) -> Path:
    return write_account_fixture(tmp_path / "security.sqlite3", 4)


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


@pytest.mark.parametrize("version", [1, 2, 3])
@pytest.mark.parametrize("empty", [False, True], ids=["populated", "empty"])
def test_unsupported_schema_blocks_startup_setup_and_recovery(
    tmp_path: Path, version: int, empty: bool
) -> None:
    data = ApplicationData.at_entry(tmp_path)
    data.root.mkdir()
    path = write_account_fixture(data.security, version)
    if empty:
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("DELETE FROM accounts")
    before = path.read_bytes()

    startup = load_startup(data)
    try:
        owner = startup.application.identity
        assert owner is not None and startup.security_error
        storage = owner.storage_snapshot()
        assert storage.state is StorageState.UNAVAILABLE and not storage.uninitialized
        assert owner.snapshot().role is Role.GUEST
        assert owner.begin_startup_login() is None
        with pytest.raises(AccountConflictError):
            owner.setup_administrator("Replacement", SECRET)
        with pytest.raises(SecurityStorageError):
            owner.login("Admin", SECRET).result(10)
    finally:
        close(startup)
    assert path.read_bytes() == before
    with pytest.raises(SecurityStorageError):
        initialize_administrator(path, "Replacement", SECRET)
    assert path.read_bytes() == before
    with pytest.raises(SecurityStorageError):
        recover_administrator(path, "Admin", SECRET)
    assert path.read_bytes() == before


@pytest.mark.parametrize(("on_disk", "committed"), [(4, 3), (3, 4)])
def test_schema_admission_uses_committed_wal_version(
    account_db: Path, on_disk: int, committed: int
) -> None:
    with closing(sqlite3.connect(account_db)) as db:
        db.execute(f"PRAGMA user_version={on_disk}")
        assert db.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        db.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
        db.execute(f"PRAGMA user_version={committed}")
    if committed == 4:
        with closing(AccountStore(account_db)) as store:
            assert len(store.accounts()) == 3
        return
    before = {p: p.read_bytes() for p in account_db.parent.glob("security.sqlite3*")}
    assert account_db.with_name("security.sqlite3-wal") in before
    with pytest.raises(SecurityStorageError):
        AccountStore(account_db).close()
    assert {
        p: p.read_bytes() for p in account_db.parent.glob("security.sqlite3*")
    } == before


@pytest.mark.parametrize("leftover_wal", [False, True])
@pytest.mark.parametrize(("committed", "interrupted"), [(3, 4), (4, 3)])
def test_interrupted_commit_checks_version_before_rollback(
    account_db: Path, committed: int, interrupted: int, leftover_wal: bool
) -> None:
    journal = account_db.with_name("security.sqlite3-journal")
    with closing(sqlite3.connect(account_db, isolation_level=None)) as db:
        db.execute(f"PRAGMA user_version={committed}")
        db.execute("PRAGMA cache_size=1")
        db.execute("BEGIN IMMEDIATE")
        db.execute(f"PRAGMA user_version={interrupted}")
        db.execute("CREATE TABLE uncommitted(x)")
        db.executemany("INSERT INTO uncommitted VALUES (zeroblob(12000))", [()] * 6)
        rollback = journal.read_bytes()
        db.commit()
    # Reproduce an interruption after SQLite wrote the main file but before it
    # removed the rollback journal. The journal contains the committed version.
    journal.write_bytes(rollback)
    if leftover_wal:
        account_db.with_name("security.sqlite3-wal").touch()
    if committed == 4:
        with closing(AccountStore(account_db)) as store:
            assert len(store.accounts()) == 3
        assert not journal.exists()
    else:
        before = {p: p.read_bytes() for p in account_db.parent.iterdir()}
        with pytest.raises(SecurityStorageError, match="schema version 3"):
            AccountStore(account_db).close()
        assert {p: p.read_bytes() for p in account_db.parent.iterdir()} == before
    assert all(p.is_file() for p in account_db.parent.iterdir())


def test_new_store_is_v4_without_a_login_or_guest_record(tmp_path: Path) -> None:
    path = tmp_path / "data/security.sqlite3"
    admin = initialize_administrator(path, "Admin", SECRET)
    assert admin.last_login_utc is None
    with closing(sqlite3.connect(path)) as connection, connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (4,)
        assert connection.execute("SELECT * FROM auto_login").fetchall() == []
        assert connection.execute(
            "SELECT username, last_login_utc FROM accounts"
        ).fetchall() == [("Admin", None)]
    with opened(path) as owner:
        assert owner.snapshot().role is Role.GUEST
    assert login_values(path) == {"Admin": None}


def test_login_persists_utc_before_success_and_account_operations_preserve_it(
    account_db: Path,
) -> None:
    with opened(account_db) as owner:
        before = datetime.now(UTC)
        first = owner.login("Admin", SECRET).result(10)
        assert first.account is not None
        stamp = first.account.last_login_utc
        assert stamp is not None and before <= stamp <= datetime.now(UTC)
        assert stamp.tzinfo is UTC
        assert login_values(account_db)["Admin"] == stamp.isoformat(
            timespec="microseconds"
        )
        worker = next(
            a
            for a in owner.account_management().result(10).accounts
            if a.username == "Worker"
        )
        backup = owner.create_account(
            "Backup", Role.ADMINISTRATOR, SECRET, confirm_administrator=True
        ).result(10)
        assert backup.last_login_utc is None
        owner.change_password(SECRET, "new credential").result(10)
        current = owner.snapshot().account
        assert current is not None and current.last_login_utc == stamp
        owner.logout()
        assert login_values(account_db)["Admin"] == stamp.isoformat(
            timespec="microseconds"
        )
        restricted = owner.login("Backup", SECRET).result(10)
        owner.rotate_password("replacement credential", restricted.revision).result(10)
        owner.reset_password(worker.account_id, SECRET).result(10)
        owner.set_enabled(worker.account_id, False).result(10)
        owner.set_enabled(worker.account_id, True).result(10)
        owner.set_role(worker.account_id, Role.ENGINEER).result(10)
        assert login_values(account_db)["Admin"] == stamp.isoformat(
            timespec="microseconds"
        )
        assert login_values(account_db)["Worker"] is None
    recover_administrator(account_db, "Backup", SECRET)
    assert login_values(account_db)["Admin"] == stamp.isoformat(timespec="microseconds")
    with opened(account_db) as owner:
        second = owner.login("Admin", "new credential").result(10)
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
    account_db: Path, name: str, password: str
) -> None:
    with opened(account_db) as owner:
        current = owner.login("Admin", SECRET).result(10)
        metadata = login_values(account_db)
        with pytest.raises(AuthenticationError):
            owner.login(name, password).result(10)
        assert owner.snapshot() == current
        assert login_values(account_db) == metadata


@pytest.mark.parametrize(
    "later", ["cancel", "logout", "login", "failed-login", "close"]
)
def test_stale_verified_login_does_not_write_timestamp(
    account_db: Path, monkeypatch: pytest.MonkeyPatch, later: str
) -> None:
    entered, release = Event(), Event()

    def held(password: str, verifier: PasswordVerifier) -> bool:
        entered.set()
        assert release.wait(10)
        return verify_password(password, verifier)

    with opened(account_db) as owner:
        current = owner.login("Admin", SECRET).result(10)
        metadata = login_values(account_db)
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
                assert login_values(account_db) == metadata
            assert login_values(account_db)["Worker"] is None
        finally:
            release.set()


def test_timestamp_write_failure_never_publishes_success(account_db: Path) -> None:
    with opened(account_db) as owner:
        current = owner.login("Admin", SECRET).result(10)
        metadata = login_values(account_db)
        # Installed after schema validation: this is a deterministic storage-failure fixture.
        with closing(sqlite3.connect(account_db)) as connection, connection:
            connection.execute(
                "CREATE TRIGGER deny_login BEFORE UPDATE OF last_login_utc ON accounts BEGIN SELECT RAISE(ABORT, 'isolated write failure'); END"
            )
        with pytest.raises(SecurityStorageError):
            owner.login("Worker", SECRET).result(10)
        assert owner.snapshot().role is Role.GUEST
        assert owner.snapshot().revision == current.revision + 1
        assert owner.storage_snapshot().state is StorageState.UNAVAILABLE
    assert login_values(account_db) == metadata


def test_replacement_and_same_account_login_each_publish_one_revision(
    account_db: Path,
) -> None:
    with opened(account_db) as owner:
        current = owner.login("Admin", SECRET).result(10)
        for username in ("Worker", "Worker"):
            previous = current
            current = owner.login(username, SECRET).result(10)
            assert current.revision == previous.revision + 1
            assert owner.snapshot() == current
            assert current.account is not None and current.account.username == username
            assert current.account.last_login_utc is not None
            assert login_values(account_db)[
                username
            ] == current.account.last_login_utc.isoformat(timespec="microseconds")


def test_cancellation_after_commit_before_future_delivery_cannot_undo_login(
    account_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    verifying, verify_release, committed, deliver_release = (Event() for _ in range(4))

    def held(password: str, verifier: PasswordVerifier) -> bool:
        verifying.set()
        assert verify_release.wait(10)
        return verify_password(password, verifier)

    with opened(account_db) as owner:
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
            assert login_values(account_db)["Worker"] is not None
            deliver_release.set()
            assert attempt.result(10) == replacement
        finally:
            verify_release.set()
            deliver_release.set()


def test_committed_current_account_change_invalidates_pending_authentication(
    account_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = Event(), Event()

    def held(password: str) -> PasswordVerifier:
        entered.set()
        assert release.wait(10)
        return hash_password(password)

    with opened(account_db) as owner:
        current = owner.login("Admin", SECRET).result(10)
        assert current.account is not None
        metadata = login_values(account_db)
        monkeypatch.setattr(identity_module, "hash_password", held)
        try:
            # Hold the serial worker outside its session lock so both the account
            # change and authentication are admitted under the original revision.
            backup = owner.create_account(
                "Backup", Role.ADMINISTRATOR, SECRET, confirm_administrator=True
            )
            assert entered.wait(10)
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
            assert changed.role is Role.ADMINISTRATOR
            assert login_values(account_db) == {**metadata, "Backup": None}
        finally:
            release.set()


@pytest.mark.parametrize(
    "stamp", ["2026-11-01", "2026-11-01T12:00:00.000000+08:00", "broken"]
)
def test_invalid_timestamp_is_visible_and_never_replaced(
    account_db: Path, stamp: str
) -> None:
    with closing(sqlite3.connect(account_db)) as connection, connection:
        connection.execute("UPDATE accounts SET last_login_utc=?", (stamp,))
    before = account_db.read_bytes()
    with opened(account_db) as owner:
        assert owner.storage_snapshot().state is StorageState.UNAVAILABLE
    assert account_db.read_bytes() == before


def test_readonly_storage_fails_visibly_without_replacement(
    account_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = account_db.read_bytes()
    connect = sqlite3.connect

    def readonly(
        database: str, *, uri: bool, timeout: float, isolation_level: None = None
    ) -> sqlite3.Connection:
        return connect(
            database.replace("?mode=rw", "?mode=ro"),
            uri=uri,
            timeout=timeout,
            isolation_level=isolation_level,
        )

    monkeypatch.setattr(sqlite3, "connect", readonly)
    with opened(account_db) as owner:
        assert owner.storage_snapshot().state is StorageState.UNAVAILABLE
        assert owner.snapshot().role is Role.GUEST
    assert account_db.read_bytes() == before
