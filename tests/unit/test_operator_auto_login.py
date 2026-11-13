"""Saved local authorization and startup session ordering on the real worker."""

import sqlite3
from collections.abc import Generator
from concurrent.futures import Future
from contextlib import closing, contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import Never

import pytest
from tests.support.application import WAIT
from tests.support.startup import TEST_PASSWORD, close, provision_account, saved_data

from capsule_vision.application import _basler_sdk
from capsule_vision.application._account_store import AccountStore
from capsule_vision.application.account_maintenance import initialize_administrator
from capsule_vision.application.basler_connection import ConnectionState
from capsule_vision.application.history import ProductionHistory
from capsule_vision.application.identity import LocalIdentity
from capsule_vision.application.identity_models import (
    Account,
    AuthorizationError,
    Role,
    SecurityStorageError,
    SessionSnapshot,
    StaleSessionError,
    StorageState,
)
from capsule_vision.application.models import ApplicationState, ProductionCounters
from capsule_vision.application.production_application import (
    SourceProductionApplication,
)
from capsule_vision.application.startup import ApplicationData, load_startup
from capsule_vision.configuration import load_camera_sources, save_camera_sources


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
def authorized(tmp_path: Path) -> tuple[ApplicationData, Account]:
    data = ApplicationData.at_entry(tmp_path)
    initialize_administrator(data.security, "TestAdmin", TEST_PASSWORD)
    with opened(data.security) as owner:
        owner.login("TestAdmin", TEST_PASSWORD).result(WAIT)
        operator = provision_account(
            owner, "Shared Shift", Role.OPERATOR, TEST_PASSWORD
        )
        owner.set_auto_login(operator.account_id, True).result(WAIT)
    return data, operator


def observe_startup(monkeypatch: pytest.MonkeyPatch) -> list[Future[SessionSnapshot]]:
    attempts: list[Future[SessionSnapshot]] = []
    begin = LocalIdentity.begin_startup_login

    def observed(owner: LocalIdentity) -> Future[SessionSnapshot] | None:
        attempt = begin(owner)
        if attempt is not None:
            attempts.append(attempt)
        return attempt

    monkeypatch.setattr(LocalIdentity, "begin_startup_login", observed)
    return attempts


def persisted_login(path: Path, account_id: str) -> str | None:
    with closing(sqlite3.connect(path)) as db:
        row: tuple[str | None] = db.execute(
            "SELECT last_login_utc FROM accounts WHERE account_id=?", (account_id,)
        ).fetchone()
        return row[0]


def test_normal_startup_publishes_operator_with_last_login_and_audit(
    authorized: tuple[ApplicationData, Account], monkeypatch: pytest.MonkeyPatch
) -> None:
    data, operator = authorized
    saved_data(data.root.parent)
    sources = load_camera_sources(data.sources)
    save_camera_sources(
        data.sources,
        replace(
            sources,
            mode="basler",
            cameras=tuple(
                replace(camera, basler_serial=str(100000 + index))
                for index, camera in enumerate(sources.cameras)
            ),
        ),
    )

    def forbidden(*_args: object, **_kwargs: object) -> Never:
        raise AssertionError("Automatic login must not access production hardware")

    monkeypatch.setattr(_basler_sdk, "PylonBackend", forbidden)
    monkeypatch.setattr(
        "capsule_vision.application.application.apply_process", forbidden
    )
    attempts = observe_startup(monkeypatch)
    earlier = persisted_login(data.security, operator.account_id)
    before = datetime.now(UTC)
    startup = load_startup(data, engine_factory=forbidden)
    try:
        application = startup.application
        assert isinstance(application, SourceProductionApplication)
        owner = application.identity
        assert owner is not None
        session = attempts[0].result(WAIT)
        assert session == owner.snapshot() and session.revision == 1
        assert session.role is Role.OPERATOR and not session.permits(Role.ENGINEER)
        assert (
            session.account is not None
            and session.account.account_id == operator.account_id
        )
        stamp = session.account.last_login_utc
        assert (
            stamp is not None
            and stamp.tzinfo is UTC
            and before <= stamp <= datetime.now(UTC)
        )
        committed = persisted_login(data.security, operator.account_id)
        assert (
            committed == stamp.isoformat(timespec="microseconds")
            and committed != earlier
        )
        assert application.snapshot().state is ApplicationState.READY
        assert application.snapshot().counters == ProductionCounters()
        assert application.source_snapshot().state is ConnectionState.DISCONNECTED
        assert application.reporting_snapshot() is None
        guest = owner.logout()
        assert owner.begin_startup_login() is None and owner.snapshot() == guest
        with pytest.raises(AuthorizationError):
            owner.set_auto_login(operator.account_id, False)
        owner.login("TestAdmin", TEST_PASSWORD).result(WAIT)
        owner.set_auto_login(operator.account_id, False).result(WAIT)
    finally:
        close(startup)

    reopened = load_startup(data, engine_factory=forbidden)
    try:
        assert attempts[1].result(WAIT) == SessionSnapshot()
    finally:
        close(reopened)
    assert persisted_login(data.security, operator.account_id) == committed
    with closing(sqlite3.connect(data.audit)) as db:
        assert db.execute(
            "SELECT actor_id, username, target, outcome FROM operations JOIN completions USING(id) WHERE action='session.auto_login'"
        ).fetchall() == [
            (operator.account_id, operator.username, operator.account_id, "success")
        ]
        assert db.execute(
            "SELECT username, target, action FROM operations JOIN completions USING(id) WHERE outcome='success' AND action IN ('account.set_auto_login','account.disable_auto_login') ORDER BY operations.utc"
        ).fetchall() == [
            ("TestAdmin", operator.account_id, "account.set_auto_login"),
            ("TestAdmin", operator.account_id, "account.disable_auto_login"),
        ]


def test_only_administrator_can_assign_one_eligible_operator(
    authorized: tuple[ApplicationData, Account],
) -> None:
    data, first = authorized
    with opened(data.security) as owner:
        owner.login("TestAdmin", TEST_PASSWORD).result(WAIT)
        second = provision_account(owner, "Other Shift", Role.OPERATOR, TEST_PASSWORD)
        engineer = provision_account(owner, "Engineer", Role.ENGINEER, TEST_PASSWORD)
        pending = owner.create_account(
            "Temporary", Role.OPERATOR, TEST_PASSWORD
        ).result(WAIT)
        admin = owner.snapshot().account
        assert admin is not None
        owner.set_enabled(second.account_id, False).result(WAIT)
        for account in (second, engineer, pending, admin):
            with pytest.raises(AuthorizationError):
                owner.set_auto_login(account.account_id, True).result(WAIT)
            assert (
                owner.account_management().result(WAIT).auto_login_account_id
                == first.account_id
            )
        owner.set_enabled(second.account_id, True).result(WAIT)
        owner.set_auto_login(second.account_id, True).result(WAIT)
        owner.set_auto_login(first.account_id, False).result(WAIT)
        assert (
            owner.account_management().result(WAIT).auto_login_account_id
            == second.account_id
        )
        for username in (None, first.username, engineer.username):
            if username is None:
                owner.logout()
            else:
                owner.login(username, TEST_PASSWORD).result(WAIT)
            for enable in (True, False):
                with pytest.raises(AuthorizationError):
                    owner.set_auto_login(first.account_id, enable)
            with pytest.raises(AuthorizationError):
                owner.account_management()
    with closing(sqlite3.connect(data.security)) as db:
        assert db.execute("SELECT * FROM auto_login").fetchall() == [
            (1, second.account_id)
        ]


@pytest.mark.parametrize("change", ["disable", "role", "reset"])
def test_account_mutations_revoke_authorization(
    authorized: tuple[ApplicationData, Account], change: str
) -> None:
    data, operator = authorized
    account_id = operator.account_id
    with opened(data.security) as owner:
        owner.login("TestAdmin", TEST_PASSWORD).result(WAIT)
        if change == "disable":
            owner.set_enabled(account_id, False).result(WAIT)
            owner.set_enabled(account_id, True).result(WAIT)
        elif change == "role":
            owner.set_role(account_id, Role.ENGINEER).result(WAIT)
            owner.set_role(account_id, Role.OPERATOR).result(WAIT)
        else:
            updated = owner.reset_password(
                account_id, "temporary reset fixture"
            ).result(WAIT)
            assert updated.must_change_password
        assert owner.account_management().result(WAIT).auto_login_account_id is None
    with opened(data.security) as owner:
        attempt = owner.begin_startup_login()
        assert attempt is not None and attempt.result(WAIT) == SessionSnapshot()


@pytest.mark.parametrize(
    "invalid", ["missing", "disabled", "engineer", "admin", "rotation"]
)
def test_invalid_saved_target_leaves_guest(
    authorized: tuple[ApplicationData, Account], invalid: str
) -> None:
    data, operator = authorized
    earlier = persisted_login(data.security, operator.account_id)
    # Simulate an inconsistent authorization in an isolated, closed store.
    with closing(sqlite3.connect(data.security)) as db, db:
        db.execute(
            {
                "missing": "UPDATE auto_login SET account_id='missing' WHERE account_id=?",
                "disabled": "UPDATE accounts SET enabled=0 WHERE account_id=?",
                "engineer": "UPDATE accounts SET role=2 WHERE account_id=?",
                "admin": "UPDATE accounts SET role=3 WHERE account_id=?",
                "rotation": "UPDATE accounts SET must_change_password=1 WHERE account_id=?",
            }[invalid],
            (operator.account_id,),
        )
    with opened(data.security) as owner:
        attempt = owner.begin_startup_login()
        assert attempt is not None and attempt.result(WAIT) == SessionSnapshot()
        assert owner.snapshot() == SessionSnapshot()
    assert persisted_login(data.security, operator.account_id) == earlier


@pytest.mark.parametrize("decision", ["manual", "logout", "close", "role_edit"])
def test_pending_startup_checks_newer_session_or_storage_decision_before_commit(
    authorized: tuple[ApplicationData, Account],
    decision: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data, operator = authorized
    before = persisted_login(data.security, operator.account_id)
    waiting, release_read = Event(), Event()
    teardown, release_teardown = Event(), Event()
    read = AccountStore.auto_login_id
    stop_history = ProductionHistory.request_stop

    def paused_read(store: AccountStore) -> str | None:
        target = read(store)
        if not waiting.is_set():
            waiting.set()
            assert release_read.wait(WAIT)
        return target

    def paused_close(history: ProductionHistory) -> None:
        teardown.set()
        assert release_teardown.wait(WAIT)
        stop_history(history)

    monkeypatch.setattr(AccountStore, "auto_login_id", paused_read)
    if decision == "close":
        monkeypatch.setattr(ProductionHistory, "request_stop", paused_close)
    attempts = observe_startup(monkeypatch)
    startup = load_startup(data)
    owner = startup.application.identity
    assert owner is not None
    try:
        assert waiting.wait(WAIT)
        attempt = attempts[0]
        manual: Future[SessionSnapshot] | None = None
        guest: SessionSnapshot | None = None
        if decision == "manual":
            manual = owner.login("TestAdmin", TEST_PASSWORD)
        elif decision == "logout":
            guest = owner.logout()
        elif decision == "close":
            startup.application.close()
            assert teardown.wait(WAIT)  # Identity shutdown has not been reached.
        else:
            with closing(sqlite3.connect(data.security)) as db, db:
                db.execute(
                    "UPDATE accounts SET role=3 WHERE account_id=?",
                    (operator.account_id,),
                )
        release_read.set()
        if decision == "role_edit":
            assert attempt.result(WAIT) == SessionSnapshot()
        else:
            with pytest.raises(StaleSessionError):
                attempt.result(WAIT)
        if manual is not None:
            assert manual.result(WAIT).role is Role.ADMINISTRATOR
        elif guest is not None:
            assert owner.snapshot() == guest
        else:
            assert owner.snapshot().role is Role.GUEST
        assert persisted_login(data.security, operator.account_id) == before
    finally:
        release_read.set()
        release_teardown.set()
        close(startup)


def test_last_login_write_failure_does_not_create_operator_session(
    authorized: tuple[ApplicationData, Account], monkeypatch: pytest.MonkeyPatch
) -> None:
    data, operator = authorized
    before = persisted_login(data.security, operator.account_id)

    def unavailable(_store: AccountStore, _account: str, _timestamp: datetime) -> Never:
        raise sqlite3.OperationalError("isolated storage failure")

    monkeypatch.setattr(AccountStore, "record_auto_login", unavailable)
    with opened(data.security) as owner:
        attempt = owner.begin_startup_login()
        assert attempt is not None
        with pytest.raises(SecurityStorageError):
            attempt.result(WAIT)
        assert owner.snapshot().role is Role.GUEST
        assert owner.storage_snapshot().state is StorageState.UNAVAILABLE
    assert persisted_login(data.security, operator.account_id) == before
