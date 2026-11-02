"""Identity is optional Guest state, with application-lifetime exclusive ownership."""

from pathlib import Path

import pytest

from capsule_vision.application.account_maintenance import (
    initialize_administrator,
    recover_administrator,
)
from capsule_vision.application.identity_models import (
    Role,
    SecurityBusyError,
    StorageState,
)
from capsule_vision.application.startup import ApplicationData, load_startup


def test_missing_store_guest_and_maintenance_fence_follow_actual_application_close(
    tmp_path: Path,
) -> None:
    data = ApplicationData.at_entry(tmp_path)
    assert data.security == tmp_path / "data/security.sqlite3"
    startup = load_startup(data)
    identity = startup.application.identity
    assert identity is not None
    try:
        assert identity.snapshot().role is Role.GUEST
        assert identity.storage_snapshot().state is StorageState.MISSING
        assert not data.security.exists() and not startup.security_error
        with pytest.raises(SecurityBusyError):
            initialize_administrator(data.security, "Admin", "test credential")
        with pytest.raises(SecurityBusyError):
            load_startup(data)
    finally:
        startup.application.close().result(5)
        assert startup.application.join(5)
    initialize_administrator(data.security, "Admin", "test credential")
    restored = load_startup(data)
    try:
        owner = restored.application.identity
        assert owner is not None and owner.snapshot().role is Role.GUEST
        assert owner.storage_snapshot().state is StorageState.READY
        assert (
            owner.login("Admin", "test credential").result(5).role is Role.ADMINISTRATOR
        )
    finally:
        restored.application.close().result(5)
        assert restored.application.join(5)
    recover_administrator(data.security, "Admin", "recovery credential")


def test_corrupt_store_is_explicit_without_privilege_or_replacement(
    tmp_path: Path,
) -> None:
    data = ApplicationData.at_entry(tmp_path)
    data.root.mkdir()
    data.security.write_bytes(b"corrupt test database")
    startup = load_startup(data)
    try:
        owner = startup.application.identity
        assert owner is not None and owner.snapshot().role is Role.GUEST
        assert owner.storage_snapshot().state is StorageState.UNAVAILABLE
        assert startup.security_error
        assert data.security.read_bytes() == b"corrupt test database"
    finally:
        startup.application.close().result(5)
        assert startup.application.join(5)
