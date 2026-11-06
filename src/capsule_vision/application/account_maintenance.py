"""Explicit offline provisioning/recovery under the same lease as the application."""

import argparse
import getpass
import sqlite3
import sys
import warnings
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import NoReturn
from uuid import uuid4

from ._account_store import AccountStore
from ._security_lock import SecurityLease
from .audit import AuditOperation, AuditTrail, AuditUnavailable, error_outcome
from .identity_models import (
    Account,
    AccountConflictError,
    AuthorizationError,
    IdentityError,
    Role,
    username_key,
)
from .passwords import hash_password, weak_password


class MaintenanceParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        # Unknown arguments could be accidentally supplied secrets. Do not echo them.
        self.print_usage(sys.stderr)
        self.exit(2, "Invalid account maintenance arguments; use --help\n")


def _warn_audit() -> None:
    print("Offline maintenance audit evidence is incomplete", file=sys.stderr)


def _no_review(_revision: int | None) -> NoReturn:
    raise AuthorizationError("Offline maintenance cannot review Audit Trail")


@contextmanager
def _offline_audit(
    path: Path, action: str, target: str, username: str, warn: Callable[[], None]
) -> Generator[None]:
    op = AuditOperation(
        "offline-maintenance", "Offline maintenance", action, target, after=username
    )
    try:
        audit = AuditTrail(path.with_name("audit.sqlite3"), _no_review)
    except OSError, RuntimeError:
        warn()
        yield
        return
    try:
        try:
            audit.intent(op).result()
        except AuditUnavailable:
            warn()
            yield
            return
        outcome, detail = "success", ""
        try:
            yield
        except BaseException as exc:
            outcome, detail = error_outcome(exc), type(exc).__name__
            raise
        finally:
            try:
                audit.complete(op, outcome, detail).result()
            except AuditUnavailable:
                warn()
    finally:
        audit.close()


def initialize_administrator(
    path: Path,
    username: str,
    password: str,
    *,
    audit_warning: Callable[[], None] = _warn_audit,
) -> Account:
    """OS-trusted offline operation, never an in-application role exception."""
    with SecurityLease(path):
        username_key(username)
        verifier = hash_password(password)
        try:
            path.stat()
        except FileNotFoundError:
            initialize = True
        else:
            initialize = False
        account_id = uuid4().hex
        with _offline_audit(
            path, "account.create", account_id, username, audit_warning
        ):
            store = AccountStore(path, initialize=initialize)
            try:
                return store.create(
                    username,
                    Role.ADMINISTRATOR,
                    verifier,
                    first=True,
                    account_id=account_id,
                )
            finally:
                store.close()


def recover_administrator(
    path: Path,
    username: str,
    password: str,
    *,
    audit_warning: Callable[[], None] = _warn_audit,
) -> Account:
    with SecurityLease(path):
        verifier = hash_password(password)
        store = AccountStore(path)
        try:
            record = store.by_name(username)
            target = record[0].account_id if record else username
            with _offline_audit(
                path, "account.reset_password", target, username, audit_warning
            ):
                if record is None or record[0].role is not Role.ADMINISTRATOR:
                    raise AccountConflictError(
                        "Recovery requires an existing Administrator account"
                    )
                return store.update(
                    record[0].account_id,
                    enabled=True,
                    verifier=verifier,
                    temporary=True,
                )
        finally:
            store.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = MaintenanceParser(
        description="Offline local account initialization/recovery; stop the application first"
    )
    parser.add_argument(
        "--entry-directory",
        type=Path,
        required=True,
        help="Directory containing the normal app.py; uses its data/security.sqlite3",
    )
    parser.add_argument("command", choices=("initialize-admin", "recover-admin"))
    parser.add_argument("username")
    args = parser.parse_args(argv)
    if not sys.stdin.isatty():
        parser.error("Interactive secret input is required")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            password = getpass.getpass("Password: ")
            confirmation = getpass.getpass("Confirm password: ")
        if password != confirmation:
            raise IdentityError("Passwords do not match")
        if (
            weak_password(password, args.username)
            and input("Weak password. Use it anyway? [y/N] ").strip().lower() != "y"
        ):
            return 1
        path = Path(args.entry_directory).resolve() / "data" / "security.sqlite3"
        action = (
            initialize_administrator
            if args.command == "initialize-admin"
            else recover_administrator
        )
        action(path, args.username, password)
    except IdentityError, sqlite3.Error, OSError, EOFError, getpass.GetPassWarning:
        print(
            "Account maintenance failed; check storage, account identity and exclusive offline access",
            file=sys.stderr,
        )
        return 1
    print("Account maintenance completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
