"""Offline CLI secrets and normal startup ownership; all data is temporary."""

import getpass
import sys
import warnings
from pathlib import Path

import pytest

from capsule_vision.application import account_maintenance
from capsule_vision.application.identity import LocalIdentity
from capsule_vision.application.identity_models import AuthenticationError, Role


def test_interactive_initialization_and_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    secrets = iter(
        (
            "first test password",
            "first test password",
            "second test password",
            "second test password",
        )
    )

    def read_secret(_prompt: str) -> str:
        return next(secrets)

    monkeypatch.setattr(getpass, "getpass", read_secret)
    base = ["--entry-directory", str(tmp_path)]
    assert account_maintenance.main([*base, "initialize-admin", "Admin"]) == 0
    assert account_maintenance.main([*base, "recover-admin", "ADMIN"]) == 0
    captured = capsys.readouterr()
    assert (
        "password" not in captured.out.lower()
        and "password" not in captured.err.lower()
    )
    owner = LocalIdentity(tmp_path / "data/security.sqlite3")
    try:
        owner.ready.result(5)
        with pytest.raises(AuthenticationError):
            owner.login("Admin", "first test password").result(5)
        pending = owner.login("Admin", "second test password").result(5)
        assert pending.rotation_required and pending.role is Role.GUEST
        assert (
            owner.rotate_password("private replacement", pending.revision)
            .result(5)
            .role
            is Role.ADMINISTRATOR
        )
    finally:
        owner.request_stop()
        assert owner.join(5)


def test_cli_rejects_password_arguments_without_echoing_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    with pytest.raises(SystemExit) as error:
        account_maintenance.main(
            [
                "--entry-directory",
                str(tmp_path),
                "initialize-admin",
                "Admin",
                "--password",
                "must-never-echo",
            ]
        )
    assert error.value.code == 2
    captured = capsys.readouterr()
    assert "must-never-echo" not in captured.err + captured.out
    assert not (tmp_path / "data/security.sqlite3").exists()


@pytest.mark.parametrize("failure", ["not-terminal", "echo-warning", "mismatch"])
def test_cli_requires_secret_interactive_input_without_partial_initialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: failure != "not-terminal")

    def read(prompt: str) -> str:
        if failure == "not-terminal":
            raise AssertionError("Must not prompt without a terminal")
        if failure == "echo-warning":
            warnings.warn("test warning", getpass.GetPassWarning, stacklevel=2)
        return "first" if prompt.startswith("Password") else "different"

    monkeypatch.setattr(getpass, "getpass", read)
    args = ["--entry-directory", str(tmp_path), "initialize-admin", "Admin"]
    if failure == "not-terminal":
        with pytest.raises(SystemExit):
            account_maintenance.main(args)
    else:
        assert account_maintenance.main(args) == 1
    assert not (tmp_path / "data/security.sqlite3").exists()
