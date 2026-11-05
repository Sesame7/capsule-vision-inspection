"""One bounded account/credential worker and an immediately observable local session."""

import secrets
import sqlite3
from collections.abc import Callable, Generator
from concurrent.futures import Future
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from queue import Full, Queue
from threading import Event, Lock, Thread, current_thread
from uuid import uuid4

from ._account_store import AccountStore
from ._security_lock import SecurityLease
from .audit import AuditOperation, AuditQuery, AuditRecord, AuditTrail, error_outcome
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


def admit_engineering(
    identity: LocalIdentity | None, revision: int | None = None
) -> int | None:
    """Admit one operation. Its IO or native work runs after releasing the session lock."""
    if identity is None:
        return None
    with identity.admission(Role.ENGINEER, revision) as actor:
        return actor.revision


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
        self._authentication: Future[SessionSnapshot] | None = None
        self._login_cancelled = Event()
        self._storage = SecurityStorageSnapshot()
        self._store: AccountStore | None = None
        self._dummy = PasswordVerifier(
            1, WORK_FACTOR, secrets.token_bytes(16), bytes(32)
        )
        self.ready: Future[SecurityStorageSnapshot] = Future()
        self.ready.set_running_or_notify_cancel()
        self._thread = Thread(target=self._run, name="LocalIdentity", daemon=False)
        try:
            self.audit = AuditTrail(
                self.path.with_name("audit.sqlite3"), self._review_revision
            )
        except BaseException:
            self._lease.close()
            raise
        try:
            self._thread.start()
        except BaseException:
            self.audit.close()
            self._lease.close()
            raise

    def snapshot(self) -> SessionSnapshot:
        with self._lock:
            return self._session

    def audit_page(
        self, query: AuditQuery, revision: int | None = None
    ) -> Future[tuple[AuditRecord, ...]]:
        return self.audit.page(query, revision)

    def _review_revision(self, revision: int | None) -> int:
        with self.admission(Role.ENGINEER, revision):
            return self._session.revision

    @contextmanager
    def change(
        self,
        action: str,
        target: str,
        revision: int | None,
        before: str = "",
        after: str = "",
        *,
        role: Role = Role.ENGINEER,
        uncertain: Callable[[BaseException], bool] = lambda _: False,
    ) -> Generator[None]:
        """Worker-only. Never wait for audit IO while holding the identity lock."""
        with self.admission(role, revision, action=action, target=target) as actor:
            op = AuditOperation.by(actor, action, target, before, after)
        self.audit.intent(op).result()
        try:
            with self.admission(role, actor.revision, action=""):
                pass
            yield
        except BaseException as exc:
            self.audit.complete(
                op,
                "uncertain" if uncertain(exc) else error_outcome(exc),
                type(exc).__name__,
            )
            raise
        else:
            self.audit.complete(op, "success")

    @contextmanager
    def admission(
        self,
        minimum: Role,
        revision: int | None = None,
        *,
        action: str = "security.admission",
        target: str = "",
    ) -> Generator[SessionSnapshot]:
        """Linearize a command with logout, login and committed account changes."""
        with self._lock:
            if not self._accepting or (
                revision is not None and self._session.revision != revision
            ):
                if action:
                    # A revision alone cannot identify a superseded caller. The
                    # present user did not initiate that request.
                    self.audit.record(
                        AuditOperation(
                            "system", "System", action, target or minimum.name
                        ),
                        "stale",
                        f"Requested revision {revision}; current revision {self._session.revision}",
                    )
                raise StaleSessionError("Session decision was superseded")
            if self._session.role < minimum:
                if action:
                    self.audit.record(
                        AuditOperation.by(
                            self._session, action, target or minimum.name
                        ),
                        "denied",
                    )
                raise AuthorizationError(f"{minimum.name.title()} required")
            yield self._session

    def storage_snapshot(self) -> SecurityStorageSnapshot:
        with self._lock:
            return self._storage

    def _publish(self, account: Account | None) -> SessionSnapshot:
        self._authentication = None
        self._session = SessionSnapshot(self._session.revision + 1, account)
        return self._session

    def logout(self) -> SessionSnapshot:
        with self._lock:
            actor = self._session
            session = self._publish(None)
            if actor.account:
                self.audit.record(
                    AuditOperation.by(
                        actor, "session.logout", actor.account.account_id
                    ),
                    "success",
                )
            return session

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
                    self.audit.record(
                        AuditOperation.by(
                            self._session, "security.storage", "security.sqlite3"
                        ),
                        "failed",
                        type(exc).__name__,
                    )
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
            revision = self._session.revision
            initiator = self._session
            cancelled = Event()

            def authenticate() -> SessionSnapshot:
                try:
                    with self._lock:
                        self._check_authentication(attempt, revision)
                    record = self._available().by_name(username)
                    # Unknown names still perform the ordinary expensive verifier work.
                    verifier = record[1] if record is not None else self._dummy
                    valid = verify_password(password, verifier)
                    with self._lock:
                        self._check_authentication(attempt, revision)
                        if record is None or not record[0].enabled or not valid:
                            raise AuthenticationError("Invalid username or password")
                        account = self._available().record_login(
                            record[0].account_id, datetime.now(UTC)
                        )
                        session = self._publish(account)
                        self.audit.record(
                            AuditOperation.by(
                                session, "session.login", account.account_id
                            ),
                            "success",
                        )
                        return session
                except Exception as exc:
                    self.audit.record(
                        AuditOperation.by(initiator, "session.login", username),
                        "cancelled" if cancelled.is_set() else error_outcome(exc),
                        type(exc).__name__,
                    )
                    raise
                finally:
                    with self._lock:
                        if self._authentication is attempt:
                            self._authentication = None

            attempt = self._enqueue(authenticate)
            self._authentication = attempt
            self._login_cancelled = cancelled
            return attempt

    def _check_authentication(
        self, attempt: Future[SessionSnapshot], revision: int
    ) -> None:
        # Caller holds the same lock used for cancellation and session publication.
        if (
            not self._accepting
            or self._session.revision != revision
            or self._authentication is not attempt
        ):
            raise StaleSessionError("Authentication was superseded")

    def cancel_login(self, attempt: Future[SessionSnapshot]) -> bool:
        """Invalidate only this pending attempt, without undoing a committed login.

        Worker ownership is retained; its Future still delivers a terminal result.
        Cancellation and the Last Login/session commit share the session lock.
        """
        with self._lock:
            if self._authentication is not attempt:
                return False
            self._login_cancelled.set()
            self._authentication = None
            return True

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
        account_id = uuid4().hex
        return self._mutate(
            "account.create",
            account_id,
            f"{username} · {role.name} · enabled",
            password,
            lambda store, verifier: store.create(
                username, role, self._verifier(verifier), account_id=account_id
            ),
        )

    def _changed(self, account: Account, *, reset: bool = False) -> Account:
        current = self._session.account
        if current is not None and current.account_id == account.account_id:
            self._publish(account if account.enabled and not reset else None)
        return account

    def set_enabled(self, account_id: str, enabled: bool) -> Future[Account]:
        return self._mutate(
            "account.enabled",
            account_id,
            str(enabled),
            None,
            lambda store, _: self._changed(store.update(account_id, enabled=enabled)),
        )

    def set_role(self, account_id: str, role: Role) -> Future[Account]:
        return self._mutate(
            "account.role",
            account_id,
            role.name,
            None,
            lambda store, _: self._changed(store.update(account_id, role=role)),
        )

    def reset_password(self, account_id: str, password: str) -> Future[Account]:
        return self._mutate(
            "account.reset_password",
            account_id,
            "",
            password,
            lambda store, verifier: self._changed(
                store.update(account_id, verifier=self._verifier(verifier)), reset=True
            ),
        )

    @staticmethod
    def _verifier(value: PasswordVerifier | None) -> PasswordVerifier:
        assert value is not None
        return value

    def _mutate(
        self,
        action: str,
        target: str,
        after: str,
        password: str | None,
        operation: Callable[[AccountStore, PasswordVerifier | None], Account],
    ) -> Future[Account]:
        if password is not None:
            password_bytes(password)
        with self._lock:
            self._admission()
            revision = self._session.revision
            try:
                self._actor(revision, administrator=True)
            except IdentityError as exc:
                self.audit.record(
                    AuditOperation.by(self._session, action, target), error_outcome(exc)
                )
                raise
            initiator = self._session

            def execute() -> Account:
                try:
                    with self._lock:
                        self._actor(revision, administrator=True)
                    before = ""
                    if action in ("account.role", "account.enabled"):
                        previous, _ = self._available().by_id(target)
                        before = (
                            previous.role.name
                            if action == "account.role"
                            else str(previous.enabled)
                        )
                except Exception as exc:
                    self.audit.record(
                        AuditOperation.by(initiator, action, target),
                        error_outcome(exc),
                        type(exc).__name__,
                    )
                    raise
                op = AuditOperation.by(initiator, action, target, before, after)
                self.audit.intent(op).result()
                try:
                    verifier = hash_password(password) if password is not None else None
                    with self._lock:
                        self._actor(revision, administrator=True)
                        result = operation(self._available(), verifier)
                except Exception as exc:
                    self.audit.complete(op, error_outcome(exc), type(exc).__name__)
                    raise
                self.audit.complete(op, "success", result.username)
                return result

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
                with self.change(
                    "account.change_password",
                    account.account_id,
                    revision,
                    role=Role.OPERATOR,
                ):
                    if not current.enabled or not verify_password(
                        old_password, previous
                    ):
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
                self.audit.record(
                    AuditOperation(
                        "system", "System", "security.storage", "security.sqlite3"
                    ),
                    "failed",
                    type(exc).__name__,
                )
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
                self.audit.close()
                self._lease.close()
                with self._lock:
                    self._storage = SecurityStorageSnapshot(
                        StorageState.CLOSED, self._storage.issue
                    )
