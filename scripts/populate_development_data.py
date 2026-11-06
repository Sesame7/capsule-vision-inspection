"""Explicit offline commissioning samples for the development checkout's normal app.py."""

import argparse
import getpass
import sqlite3
import sys
import warnings
from pathlib import Path

from capsule_vision.application.identity import LocalIdentity
from capsule_vision.application.identity_models import Role, StorageState
from capsule_vision.application.passwords import password_bytes, weak_password
from capsule_vision.application.startup import ApplicationData


def checkout_data() -> ApplicationData:
    root = Path(__file__).resolve().parents[1]
    if (
        Path.cwd().resolve() != root
        or not (root / ".git").exists()
        or not (root / "app.py").is_file()
    ):
        raise ValueError(
            "Run this workflow explicitly from the development checkout containing app.py"
        )
    data = ApplicationData.at_entry(root)
    for path in (
        data.root,
        data.security.with_suffix(".lock"),
        *(
            db.with_name(db.name + suffix)
            for db in (data.security, data.history, data.audit)
            for suffix in ("", "-wal", "-shm", "-journal")
        ),
    ):
        if (
            path.is_symlink()
            or path.is_junction()
            or (path.is_file() and path.stat().st_nlink > 1)
        ):
            raise ValueError(f"Refusing redirected development data target: {path}")
    return data


def secret(username: str, prompt: str, *, new: bool = True) -> str:
    value = getpass.getpass(prompt + ": ")
    password_bytes(value)
    if new:
        if getpass.getpass("Confirm: ") != value:
            raise ValueError("Passwords do not match")
        if (
            weak_password(value, username)
            and input("Weak password. Use it anyway? [y/N] ").lower() != "y"
        ):
            raise ValueError("Password choice cancelled")
    return value


def provision_accounts(data: ApplicationData) -> None:
    if not sys.stdin.isatty():
        raise ValueError("Use a local interactive terminal for masked credential input")
    print(f"Development accounts for {data.root}. Close app.py before continuing.")
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        identity = LocalIdentity(data.security)
        try:
            storage = identity.ready.result()
            if not storage.uninitialized and storage.state is not StorageState.READY:
                raise RuntimeError(storage.issue or "Account storage unavailable")
            admin = (
                "DEV-Admin"
                if storage.uninitialized
                else input("Administrator username [DEV-Admin]: ").strip()
                or "DEV-Admin"
            )
            password = secret(
                admin, f"{admin} administrator password", new=storage.uninitialized
            )
            if storage.uninitialized:
                identity.setup_administrator(admin, password).result()
            actor = identity.login(admin, password).result()
            if not actor.permits(Role.ADMINISTRATOR):
                raise RuntimeError(
                    "An active Administrator is required. Complete any pending rotation in app.py."
                )
            accounts = {
                a.username.casefold(): a for a in identity.list_accounts().result()
            }
            for role in (Role.ENGINEER, Role.OPERATOR):
                name = "DEV-" + role.name.title()
                current = accounts.get(name.casefold())
                if current is not None:
                    if not current.enabled or current.role != role:
                        raise ValueError(
                            f"Existing {name} has a different role or is disabled; review it in Manage Users"
                        )
                    print(f"Keeping existing {name}.")
                    continue
                temporary = secret(name, f"{name} temporary password")
                identity.create_account(name, role, temporary).result()
                print(f"Created {name}. Change the temporary password at first login.")
        finally:
            identity.request_stop()
            identity.join()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("populate", "accounts"))
    args = parser.parse_args()
    try:
        data = checkout_data()
        if args.command == "accounts":
            provision_accounts(data)
        else:
            from development_records import populate

            populate(data)
    except (
        OSError,
        RuntimeError,
        ValueError,
        EOFError,
        sqlite3.Error,
        getpass.GetPassWarning,
    ) as exc:
        print(f"Development preparation stopped: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
