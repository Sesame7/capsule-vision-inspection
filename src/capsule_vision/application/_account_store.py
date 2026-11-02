"""Private single-thread SQLite owner. Public authorization belongs to LocalIdentity."""

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

from .identity_models import (
    Account,
    AccountConflictError,
    IdentityValidationError,
    LastAdministratorError,
    Role,
    SecurityStorageError,
    account_role,
    username_key,
)
from .passwords import PasswordVerifier

APPLICATION_ID = 0x43564953
_COLUMNS = "account_id, username, username_key, role, enabled, password_version, iterations, salt, digest"
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
    digest BLOB NOT NULL
);
"""


class AccountStore:
    def __init__(self, path: Path, *, initialize: bool = False) -> None:
        # mode=rw never repairs or creates a missing database during normal use.
        if path.is_symlink():
            raise SecurityStorageError("Security database must not be a symlink")
        if initialize:
            # Never initialize a file that appeared after the missing-file check.
            with path.open("xb"):
                pass
        self.connection = sqlite3.connect(
            path.resolve().as_uri() + ("?mode=rwc" if initialize else "?mode=rw"),
            uri=True,
            timeout=0.2,
            isolation_level=None,
        )
        try:
            if initialize:
                with self.transaction():
                    self.connection.execute(_SCHEMA)
                    self.connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
                    self.connection.execute("PRAGMA user_version=1")
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
        if (
            self.connection.execute("PRAGMA application_id").fetchone()[0]
            != APPLICATION_ID
            or self.connection.execute("PRAGMA user_version").fetchone()[0] != 1
            or self.connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]
        ):
            raise SecurityStorageError("Corrupt or unsupported security database")
        columns: list[tuple[object, ...]] = self.connection.execute(
            "PRAGMA table_info(accounts)"
        ).fetchall()
        if [row[1] for row in columns] != _COLUMNS.split(", "):
            raise SecurityStorageError("Unsupported account schema")
        schema: list[tuple[object, ...]] = self.connection.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' AND type IN ('table','view','trigger')"
        ).fetchall()
        if (
            len(schema) != 1
            or schema[0][:2] != ("table", "accounts")
            or not isinstance(schema[0][2], str)
            or " ".join(schema[0][2].rstrip(";").split())
            != " ".join(_SCHEMA.strip().rstrip(";").split())
        ):
            raise SecurityStorageError("Unsupported account schema")
        rows: list[tuple[object, ...]] = self.connection.execute(
            f"SELECT {_COLUMNS} FROM accounts"
        ).fetchall()
        for row in rows:
            self.decode(row)
        with self.transaction():
            pass  # Detect a read-only database without modifying any account.

    @staticmethod
    def decode(row: tuple[object, ...]) -> tuple[Account, PasswordVerifier]:
        if len(row) != 9:
            raise SecurityStorageError("Malformed account record")
        identity, name, key, role, enabled, version, iterations, salt, digest = row
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
            or type(version) is not int
            or type(iterations) is not int
            or not isinstance(salt, bytes)
            or not isinstance(digest, bytes)
        ):
            raise SecurityStorageError("Malformed account record")
        return Account(identity, name, Role(role), bool(enabled)), PasswordVerifier(
            version, iterations, salt, digest
        )

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

    def create(
        self,
        username: str,
        role: Role,
        verifier: PasswordVerifier,
        *,
        first: bool = False,
    ) -> Account:
        role, key = account_role(role), username_key(username)
        account = Account(uuid4().hex, username, role, True)
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
                "INSERT INTO accounts VALUES (?,?,?,?,?,?,?,?,?)",
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
            )
            password = verifier or password
            self.connection.execute(
                "UPDATE accounts SET role=?, enabled=?, password_version=?, iterations=?, salt=?, digest=? WHERE account_id=?",
                (
                    int(account.role),
                    int(account.enabled),
                    password.version,
                    password.iterations,
                    password.salt,
                    password.digest,
                    account_id,
                ),
            )
            if not self.connection.execute(
                "SELECT count(*) FROM accounts WHERE role=3 AND enabled=1"
            ).fetchone()[0]:
                raise LastAdministratorError(
                    "At least one enabled Administrator must remain"
                )
        return account
