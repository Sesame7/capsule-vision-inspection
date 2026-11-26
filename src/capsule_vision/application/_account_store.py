"""Private single-thread SQLite owner. Public authorization belongs to LocalIdentity."""

import sqlite3
from collections.abc import Generator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from shutil import copyfile
from tempfile import TemporaryDirectory
from uuid import UUID, uuid4

from .identity_models import (
    Account,
    AccountConflictError,
    AuthorizationError,
    IdentityValidationError,
    LastAdministratorError,
    Role,
    SecurityStorageError,
    account_role,
    username_key,
)
from .passwords import PasswordVerifier

APPLICATION_ID = 0x43564953
_SCHEMA_VERSION = 4
_COLUMNS = (
    "account_id, username, username_key, role, enabled, password_version, "
    "iterations, salt, digest, last_login_utc, must_change_password"
)
_SCHEMA = """
CREATE TABLE accounts (
    account_id TEXT PRIMARY KEY NOT NULL,
    username TEXT NOT NULL,
    username_key TEXT NOT NULL UNIQUE,
    role INTEGER NOT NULL CHECK(role IN (1,2,3)),
    enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
    password_version INTEGER NOT NULL,
    iterations INTEGER NOT NULL,
    salt BLOB NOT NULL,
    digest BLOB NOT NULL,
    last_login_utc TEXT,
    must_change_password INTEGER NOT NULL DEFAULT 0 CHECK(must_change_password IN (0,1))
);
"""
_AUTO_LOGIN_SCHEMA = """
CREATE TABLE auto_login (
    slot INTEGER PRIMARY KEY CHECK (slot = 1),
    account_id TEXT NOT NULL REFERENCES accounts(account_id)
);
"""


def _schema_text(sql: str) -> str:
    # Previously upgraded v4 files can have a space before a comma or parenthesis.
    return (
        " ".join(sql.strip().rstrip(";").split()).replace(" ,", ",").replace(" )", ")")
    )


def _require_current_version(connection: sqlite3.Connection) -> None:
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version != _SCHEMA_VERSION:
        raise SecurityStorageError(f"Unsupported account schema version {version}")


def _check_existing_version(path: Path) -> None:
    wal = path.with_name(path.name + "-wal")
    journal = path.with_name(path.name + "-journal")
    if journal.is_file():
        # The header can contain an uncommitted version. Let SQLite resolve the
        # journal on a private copy before admitting recovery of the original.
        with TemporaryDirectory(dir=path.parent) as directory:
            probe = Path(directory) / path.name
            copyfile(path, probe)
            copyfile(journal, probe.with_name(journal.name))
            if wal.is_file():
                copyfile(wal, probe.with_name(wal.name))
            with closing(sqlite3.connect(probe)) as db:
                _require_current_version(db)
        return

    has_wal = wal.is_file()
    mode = "rw" if has_wal else "ro&immutable=1"
    with closing(
        sqlite3.connect(f"{path.as_uri()}?mode={mode}", uri=True, timeout=0.2)
    ) as db:
        if has_wal:
            # Use a private WAL index under the security lease. Closing the
            # probe must not checkpoint or remove an unsupported file's WAL.
            db.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
            db.execute("PRAGMA locking_mode=EXCLUSIVE")
            db.execute("PRAGMA query_only=ON")
        _require_current_version(db)


def _login_time(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            stamp = datetime.fromisoformat(value)
            if (
                stamp.tzinfo is UTC
                and stamp.isoformat(timespec="microseconds") == value
            ):
                return stamp
        except ValueError:
            pass
    raise SecurityStorageError("Malformed account login timestamp")


class AccountStore:
    def __init__(self, path: Path, *, initialize: bool = False) -> None:
        if path.is_symlink():
            raise SecurityStorageError("Security database must not be a symlink")
        path = path.resolve()
        if initialize:
            # Never initialize a file that appeared after the missing-file check.
            with path.open("xb"):
                pass
        else:
            _check_existing_version(path)
        self.connection = sqlite3.connect(
            path.as_uri() + ("?mode=rwc" if initialize else "?mode=rw"),
            uri=True,
            timeout=0.2,
            isolation_level=None,
        )
        try:
            if initialize:
                with self.transaction():
                    self.connection.execute(_SCHEMA)
                    self.connection.execute(_AUTO_LOGIN_SCHEMA)
                    self.connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
                    self.connection.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
            self.validate()
        except BaseException:
            self.connection.close()
            raise

    def close(self) -> None:
        self.connection.close()

    @contextmanager
    def transaction(self) -> Generator[None]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.connection.execute("COMMIT")
        except BaseException:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise

    def validate(self) -> None:
        with self.transaction():
            _require_current_version(self.connection)
            self._validate_schema()
            # BEGIN IMMEDIATE alone can succeed on a read-only connection.
            # This requires write access while changing no rows or metadata.
            self.connection.execute(
                "UPDATE accounts SET last_login_utc=last_login_utc WHERE 0"
            )

    def _validate_schema(self) -> None:
        if self.connection.execute("PRAGMA application_id").fetchone()[
            0
        ] != APPLICATION_ID or self.connection.execute(
            "PRAGMA quick_check"
        ).fetchall() != [("ok",)]:
            raise SecurityStorageError("Corrupt or unsupported security database")
        columns: list[tuple[object, ...]] = self.connection.execute(
            "PRAGMA table_info(accounts)"
        ).fetchall()
        if [row[1] for row in columns] != _COLUMNS.split(", "):
            raise SecurityStorageError("Unsupported account schema")
        schema: list[tuple[object, ...]] = self.connection.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' AND type IN ('table','view','trigger')"
        ).fetchall()
        expected = {
            ("table", "accounts", _schema_text(_SCHEMA)),
            ("table", "auto_login", _schema_text(_AUTO_LOGIN_SCHEMA)),
        }
        actual = {
            (kind, name, _schema_text(sql))
            for kind, name, sql in schema
            if isinstance(sql, str)
        }
        if len(schema) != len(expected) or actual != expected:
            raise SecurityStorageError("Unsupported account schema")
        rows: list[tuple[object, ...]] = self.connection.execute(
            f"SELECT {_COLUMNS} FROM accounts"
        ).fetchall()
        for row in rows:
            self.decode(row)

    @staticmethod
    def decode(row: tuple[object, ...]) -> tuple[Account, PasswordVerifier]:
        if len(row) != 11:
            raise SecurityStorageError("Malformed account record")
        (
            identity,
            name,
            key,
            role,
            enabled,
            version,
            iterations,
            salt,
            digest,
            login,
            rotate,
        ) = row
        if not isinstance(identity, str) or not isinstance(name, str):
            raise SecurityStorageError("Malformed account identity")
        try:
            valid_identity = UUID(identity).hex == identity
            valid_key = username_key(name) == key
        except ValueError, IdentityValidationError:
            raise SecurityStorageError("Malformed account identity") from None
        if (
            not valid_identity
            or not valid_key
            or type(role) is not int
            or role not in (1, 2, 3)
            or type(enabled) is not int
            or enabled not in (0, 1)
            or type(rotate) is not int
            or rotate not in (0, 1)
            or type(version) is not int
            or type(iterations) is not int
            or not isinstance(salt, bytes)
            or not isinstance(digest, bytes)
        ):
            raise SecurityStorageError("Malformed account record")
        return Account(
            identity, name, Role(role), bool(enabled), _login_time(login), bool(rotate)
        ), PasswordVerifier(version, iterations, salt, digest)

    def by_name(self, username: str) -> tuple[Account, PasswordVerifier] | None:
        row: tuple[object, ...] | None = self.connection.execute(
            f"SELECT {_COLUMNS} FROM accounts WHERE username_key=?",
            (username_key(username),),
        ).fetchone()
        return self.decode(row) if row is not None else None

    def by_id(self, account_id: str) -> tuple[Account, PasswordVerifier]:
        row: tuple[object, ...] | None = self.connection.execute(
            f"SELECT {_COLUMNS} FROM accounts WHERE account_id=?", (account_id,)
        ).fetchone()
        if row is None:
            raise AccountConflictError("Account is unavailable")
        return self.decode(row)

    def accounts(self) -> tuple[Account, ...]:
        rows: list[tuple[object, ...]] = self.connection.execute(
            f"SELECT {_COLUMNS} FROM accounts ORDER BY username_key"
        ).fetchall()
        return tuple(self.decode(row)[0] for row in rows)

    def auto_login_id(self) -> str | None:
        row: tuple[object, ...] | None = self.connection.execute(
            "SELECT account_id FROM auto_login WHERE slot=1"
        ).fetchone()
        return row[0] if row is not None and isinstance(row[0], str) else None

    def set_auto_login(self, account_id: str, enabled: bool) -> Account:
        if type(enabled) is not bool:
            raise IdentityValidationError("Enabled must be boolean")
        with self.transaction():
            account, _ = self.by_id(account_id)
            if enabled:
                if not account.auto_login_eligible:
                    raise AuthorizationError("Auto login requires an eligible Operator")
                self.connection.execute(
                    "INSERT INTO auto_login VALUES (1,?) ON CONFLICT(slot) DO UPDATE SET account_id=excluded.account_id",
                    (account_id,),
                )
            else:
                self._revoke_auto_login(account_id)
        return account

    def _revoke_auto_login(self, account_id: str) -> None:
        self.connection.execute(
            "DELETE FROM auto_login WHERE account_id=?", (account_id,)
        )

    def empty(self) -> bool:
        return (
            self.connection.execute("SELECT 1 FROM accounts LIMIT 1").fetchone() is None
        )

    def create(
        self,
        username: str,
        role: Role,
        verifier: PasswordVerifier,
        *,
        first: bool = False,
        account_id: str | None = None,
    ) -> Account:
        role, key = account_role(role), username_key(username)
        account = Account(
            account_id or uuid4().hex,
            username,
            role,
            True,
            must_change_password=not first,
        )
        with self.transaction():
            if (
                first
                and self.connection.execute("SELECT count(*) FROM accounts").fetchone()[
                    0
                ]
            ):
                raise AccountConflictError(
                    "First Administrator initialization requires an empty account store"
                )
            if self.by_name(username) is not None:
                raise AccountConflictError("Username already exists")
            self.connection.execute(
                f"INSERT INTO accounts ({_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    account.account_id,
                    username,
                    key,
                    int(role),
                    1,
                    verifier.version,
                    verifier.iterations,
                    verifier.salt,
                    verifier.digest,
                    None,
                    int(account.must_change_password),
                ),
            )
        return account

    def update(
        self,
        account_id: str,
        *,
        role: Role | None = None,
        enabled: bool | None = None,
        verifier: PasswordVerifier | None = None,
        temporary: bool = False,
    ) -> Account:
        if role is not None:
            account_role(role)
        if enabled is not None and type(enabled) is not bool:
            raise IdentityValidationError("Enabled must be boolean")
        with self.transaction():
            previous, password = self.by_id(account_id)
            account = Account(
                account_id,
                previous.username,
                role if role is not None else previous.role,
                enabled if enabled is not None else previous.enabled,
                previous.last_login_utc,
                previous.must_change_password if verifier is None else temporary,
            )
            password = verifier or password
            self.connection.execute(
                "UPDATE accounts SET role=?, enabled=?, password_version=?, iterations=?, salt=?, digest=?, must_change_password=? WHERE account_id=?",
                (
                    int(account.role),
                    int(account.enabled),
                    password.version,
                    password.iterations,
                    password.salt,
                    password.digest,
                    int(account.must_change_password),
                    account_id,
                ),
            )
            if (
                account.role is not previous.role
                or enabled is False
                or account.enabled != previous.enabled
                or (verifier is not None and temporary)
            ):
                self._revoke_auto_login(account_id)
            if not self.connection.execute(
                "SELECT count(*) FROM accounts WHERE role=3 AND enabled=1"
            ).fetchone()[0]:
                raise LastAdministratorError(
                    "At least one enabled Administrator must remain"
                )
        return account

    def record_login(self, account_id: str, timestamp: datetime) -> Account:
        with self.transaction():
            return self._record_login(account_id, timestamp)

    def record_auto_login(self, account_id: str, timestamp: datetime) -> Account | None:
        with self.transaction():
            if self.auto_login_id() != account_id:
                return None
            try:
                account, _ = self.by_id(account_id)
            except AccountConflictError:
                return None  # A dangling authorization has no account privileges.
            if not account.auto_login_eligible:
                return None
            return self._record_login(account_id, timestamp)

    def _record_login(self, account_id: str, timestamp: datetime) -> Account:
        value = timestamp.astimezone(UTC).isoformat(timespec="microseconds")
        self.connection.execute(
            "UPDATE accounts SET last_login_utc=? WHERE account_id=?",
            (value, account_id),
        )
        account, _ = self.by_id(account_id)
        return account
