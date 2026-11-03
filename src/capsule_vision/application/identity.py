"""One bounded account/credential worker and an immediately observable local session."""

import secrets
import sqlite3
from collections.abc import Callable
from concurrent.futures import Future
from datetime import UTC, datetime
from pathlib import Path
from queue import Full, Queue
from threading import Lock, Thread, current_thread

from ._account_store import AccountStore
from ._security_lock import SecurityLease
from .identity_models import (
    Account,
    AuthenticationError,
    AuthorizationError,
    IdentityError,
    Role,
    SecurityBusyError,
    SecurityStorageError,
    SecurityStorageSnapshot,
    SessionSnapshot,
    StaleSessionError,
    StorageState,
)
from .passwords import (
    WORK_FACTOR,
    PasswordVerifier,
    hash_password,
    password_bytes,
    verify_password,
)


def _storage_issue(error: BaseException) -> str:
    detail = (
        str(error) if isinstance(error, SecurityStorageError) else type(error).__name__
    )
    return f"Security storage unavailable: {detail}"


class LocalIdentity:
    """Qt-independent service. Accepted Futures are observations, not cancellation.

    SQLite and PBKDF2 execute on one retained non-daemon worker. Passwords never
    enter SQLite or observations. A short lock linearizes session decisions with
    authorized transactions; expensive password work stays outside that lock.
    """

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self._lease = SecurityLease(path)
        self._lock = Lock()
        self._queue: Queue[Callable[[], None] | None] = Queue(16)
        self._accepting = True
        self._session = SessionSnapshot()
        self._storage = SecurityStorageSnapshot()
        self._store: AccountStore | None = None
        self._dummy = PasswordVerifier(
            1, WORK_FACTOR, secrets.token_bytes(16), bytes(32)
        )
        self.ready: Future[SecurityStorageSnapshot] = Future()
        self.ready.set_running_or_notify_cancel()
        self._thread = Thread(target=self._run, name="LocalIdentity", daemon=False)
        try:
            self._thread.start()
        except BaseException:
            self._lease.close()
            raise

    def snapshot(self) -> SessionSnapshot:
        with self._lock:
            return self._session

    def storage_snapshot(self) -> SecurityStorageSnapshot:
        with self._lock:
            return self._storage

    def _publish(self, account: Account | None) -> SessionSnapshot:
        self._session = SessionSnapshot(self._session.revision + 1, account)
        return self._session

    def logout(self) -> SessionSnapshot:
        with self._lock:
            return self._publish(None)

    def _admission(self) -> None:
        if not self._accepting:
            raise IdentityError("Local identity is closing")
        if self._queue.full():
            raise SecurityBusyError("Local identity request capacity reached")

    def _enqueue[T](self, work: Callable[[], T]) -> Future[T]:
        """Caller holds the session/admission lock; no unbounded executor queue."""
        future: Future[T] = Future()
        future.set_running_or_notify_cancel()

        def run() -> None:
            try:
                value = work()
            except (sqlite3.Error, OSError, SecurityStorageError) as exc:
                with self._lock:
                    issue = self._storage.issue or _storage_issue(exc)
                    if self._storage.state is StorageState.READY:
                        self._storage = SecurityStorageSnapshot(
                            StorageState.UNAVAILABLE, issue
                        )
                    self._publish(None)
                future.set_exception(SecurityStorageError(issue))
            except BaseException as exc:
                future.set_exception(exc)
            else:
                future.set_result(value)

        self._queue.put_nowait(run)
        return future

    def _available(self) -> AccountStore:
        if self._store is None or self._storage.state is not StorageState.READY:
            raise SecurityStorageError(
                self._storage.issue or "Security storage has not been initialized"
            )
        return self._store

    def _actor(self, revision: int, *, administrator: bool = False) -> Account:
        if not self._accepting or self._session.revision != revision:
            raise StaleSessionError("Session decision was superseded")
        account = self._session.account
        if account is None or (
            administrator and account.role is not Role.ADMINISTRATOR
        ):
            raise AuthorizationError(
                "Administrator required" if administrator else "Login required"
            )
        return account

    def login(self, username: str, password: str) -> Future[SessionSnapshot]:
        password_bytes(password)
        with self._lock:
            self._admission()
            revision = self._publish(None).revision

            def authenticate() -> SessionSnapshot:
                with self._lock:
                    if not self._accepting or self._session.revision != revision:
                        raise StaleSessionError("Authentication was superseded")
                record = self._available().by_name(username)
                # Unknown names still perform the ordinary expensive verifier work.
                verifier = record[1] if record is not None else self._dummy
                valid = verify_password(password, verifier)
                with self._lock:
                    if not self._accepting or self._session.revision != revision:
                        raise StaleSessionError("Authentication was superseded")
                    if record is None or not record[0].enabled or not valid:
                        raise AuthenticationError("Invalid username or password")
                    account = self._available().record_login(
                        record[0].account_id, datetime.now(UTC)
                    )
                    return self._publish(account)

            return self._enqueue(authenticate)

    def _admin[T](self, operation: Callable[[AccountStore], T]) -> Future[T]:
        with self._lock:
            self._admission()
            revision = self._session.revision
            self._actor(revision, administrator=True)

            def execute() -> T:
                with self._lock:
                    self._actor(revision, administrator=True)
                    return operation(self._available())

            return self._enqueue(execute)

    def list_accounts(self) -> Future[tuple[Account, ...]]:
        return self._admin(lambda store: store.accounts())

    def create_account(
        self, username: str, role: Role, password: str
    ) -> Future[Account]:
        return self._password_admin(
            password, lambda store, verifier: store.create(username, role, verifier)
        )

    def _changed(self, account: Account, *, reset: bool = False) -> Account:
        current = self._session.account
        if current is not None and current.account_id == account.account_id:
            self._publish(account if account.enabled and not reset else None)
        return account

    def set_enabled(self, account_id: str, enabled: bool) -> Future[Account]:
        return self._admin(
            lambda store: self._changed(store.update(account_id, enabled=enabled))
        )

    def set_role(self, account_id: str, role: Role) -> Future[Account]:
        return self._admin(
            lambda store: self._changed(store.update(account_id, role=role))
        )

    def reset_password(self, account_id: str, password: str) -> Future[Account]:
        return self._password_admin(
            password,
            lambda store, verifier: self._changed(
                store.update(account_id, verifier=verifier), reset=True
            ),
        )

    def _password_admin[T](
        self, password: str, operation: Callable[[AccountStore, PasswordVerifier], T]
    ) -> Future[T]:
        password_bytes(password)
        with self._lock:
            self._admission()
            revision = self._session.revision
            self._actor(revision, administrator=True)

            def execute() -> T:
                with self._lock:
                    self._actor(revision, administrator=True)
                verifier = hash_password(password)
                with self._lock:
                    self._actor(revision, administrator=True)
                    return operation(self._available(), verifier)

            return self._enqueue(execute)

    def change_password(
        self, old_password: str, new_password: str
    ) -> Future[SessionSnapshot]:
        password_bytes(old_password)
        password_bytes(new_password)
        with self._lock:
            self._admission()
            revision = self._session.revision
            account = self._actor(revision)

            def execute() -> SessionSnapshot:
                with self._lock:
                    self._actor(revision)
                store = self._available()
                current, previous = store.by_id(account.account_id)
                if not current.enabled or not verify_password(old_password, previous):
                    raise AuthenticationError("Invalid username or password")
                verifier = hash_password(new_password)
                with self._lock:
                    self._actor(revision)
                    updated = store.update(account.account_id, verifier=verifier)
                    return self._publish(updated)

            return self._enqueue(execute)

    def request_stop(self) -> None:
        with self._lock:
            if not self._accepting:
                return
            self._accepting = False
            self._publish(None)
            try:
                self._queue.put_nowait(None)
            except Full:
                pass

    def join(self, timeout: float | None = None) -> bool:
        if current_thread() is self._thread:
            raise IdentityError("Identity worker cannot join itself")
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def _run(self) -> None:
        try:
            try:
                try:
                    self.path.stat()
                except FileNotFoundError:
                    exists = False
                else:
                    exists = True
                if exists:
                    self._store = AccountStore(self.path)
                    state = SecurityStorageSnapshot(StorageState.READY)
                else:
                    state = SecurityStorageSnapshot(StorageState.MISSING)
            except (sqlite3.Error, OSError, SecurityStorageError) as exc:
                state = SecurityStorageSnapshot(
                    StorageState.UNAVAILABLE,
                    _storage_issue(exc),
                )
            with self._lock:
                self._storage = state
            self.ready.set_result(state)
            while True:
                work = self._queue.get()
                if work is not None:
                    work()
                with self._lock:
                    if not self._accepting and self._queue.empty():
                        break
        finally:
            try:
                if self._store is not None:
                    self._store.close()
            finally:
                self._lease.close()
                with self._lock:
                    self._storage = SecurityStorageSnapshot(
                        StorageState.CLOSED, self._storage.issue
                    )
