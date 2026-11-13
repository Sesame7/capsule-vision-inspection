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

from capsule_vision.diagnostics import RuntimeDiagnostics

from ._account_store import AccountStore
from ._security_lock import SecurityLease
from .audit import (
    AuditOperation,
    AuditQuery,
    AuditRecord,
    AuditTrail,
    AuditUnavailable,
    RevisionConfirmationError,
    RevisionEvidence,
    error_outcome,
)
from .identity_models import (
    Account,
    AccountConflictError,
    AccountManagementSnapshot,
    AuthenticationError,
    AuthorizationError,
    IdentityError,
    PasswordReuseError,
    Role,
    SecurityBusyError,
    SecurityStorageError,
    SecurityStorageSnapshot,
    SessionSnapshot,
    StaleSessionError,
    StorageState,
    username_key,
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

    def __init__(
        self, path: Path, *, diagnostics: RuntimeDiagnostics | None = None
    ) -> None:
        self.path = path.resolve()
        self._lease = SecurityLease(path)
        self._lock = Lock()
        self._queue: Queue[Callable[[], None] | None] = Queue(16)
        self._accepting = True
        self._session = SessionSnapshot()
        self._authentication: Future[SessionSnapshot] | None = None
        self._startup_login: Future[SessionSnapshot] | None = None
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
                self.path.with_name("audit.sqlite3"),
                self._review_revision,
                diagnostics=diagnostics,
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
        evidence: RevisionEvidence | None = None,
    ) -> Generator[None]:
        """Worker-only. Never wait for audit IO while holding the identity lock."""
        with self.admission(role, revision, action=action, target=target) as actor:
            op = AuditOperation.by(actor, action, target, before, after)
        self.audit.intent(op, evidence).result()
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
            completion = self.audit.complete(op, "success")
            if evidence is not None:
                try:
                    completion.result()
                except AuditUnavailable as exc:
                    raise RevisionConfirmationError(op) from exc

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
            if not self._session.permits(minimum):
                if action:
                    self.audit.record(
                        AuditOperation.by(
                            self._session, action, target or minimum.name
                        ),
                        "denied",
                    )
                if self._session.rotation_required:
                    raise AuthorizationError("Change the temporary password or log out")
                raise AuthorizationError(f"{minimum.name.title()} required")
            yield self._session

    def storage_snapshot(self) -> SecurityStorageSnapshot:
        with self._lock:
            return self._storage

    def _publish(self, account: Account | None) -> SessionSnapshot:
        self._authentication = None
        self._session = SessionSnapshot(self._session.revision + 1, account)
        return self._session

    def logout(self, *, revision: int | None = None) -> SessionSnapshot:
        with self._lock:
            if revision is not None and self._session.revision != revision:
                return self._session
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

    def _actor(
        self, revision: int, *, administrator: bool = False, rotation: bool = False
    ) -> Account:
        if not self._accepting or self._session.revision != revision:
            raise StaleSessionError("Session decision was superseded")
        account = self._session.account
        if (
            account is None
            or account.must_change_password != rotation
            or (administrator and account.role is not Role.ADMINISTRATOR)
        ):
            raise AuthorizationError(
                "Administrator required" if administrator else "Login required"
            )
        return account

    def login(self, username: str, password: str) -> Future[SessionSnapshot]:
        password_bytes(password)
        with self._lock:
            self._admission()
            if self._session.rotation_required:
                raise AuthorizationError("Change the temporary password or log out")
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
                        return self._publish_login(account, "session.login")
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

    def begin_startup_login(self) -> Future[SessionSnapshot] | None:
        """Observe one saved authorization on the worker during normal startup."""
        with self._lock:
            if (
                self._startup_login is not None
                or self._session.revision != 0
                or self._authentication is not None
                or self._storage.state is not StorageState.READY
                or not self._accepting
                or self._queue.full()
            ):
                return None
            revision = self._session.revision
            cancelled = Event()

            def authenticate() -> SessionSnapshot:
                target = None
                try:
                    with self._lock:
                        self._check_authentication(attempt, revision)
                    store = self._available()
                    target = store.auto_login_id()
                    with self._lock:
                        self._check_authentication(attempt, revision)
                        if target is not None:
                            account = store.record_auto_login(target, datetime.now(UTC))
                            if account is not None:
                                return self._publish_login(
                                    account, "session.auto_login"
                                )
                            self.audit.record(
                                AuditOperation(
                                    "system", "System", "session.auto_login", target
                                ),
                                "denied",
                            )
                        return self._session
                except Exception as exc:
                    if target is not None:
                        self.audit.record(
                            AuditOperation(
                                "system", "System", "session.auto_login", target
                            ),
                            "cancelled" if cancelled.is_set() else error_outcome(exc),
                            type(exc).__name__,
                        )
                    raise
                finally:
                    with self._lock:
                        if self._authentication is attempt:
                            self._authentication = None

            attempt = self._enqueue(authenticate)
            self._startup_login = self._authentication = attempt
            self._login_cancelled = cancelled
            return attempt

    def _publish_login(self, account: Account, action: str) -> SessionSnapshot:
        # Both paths commit Last Login while holding the session lock.
        session = self._publish(account)
        self.audit.record(
            AuditOperation.by(session, action, account.account_id), "success"
        )
        return session

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

    def cancel_startup_login(self) -> None:
        attempt = self._startup_login
        if attempt is not None:
            self.cancel_login(attempt)

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

    def account_management(self) -> Future[AccountManagementSnapshot]:
        def read(store: AccountStore) -> AccountManagementSnapshot:
            accounts = store.accounts()
            target = store.auto_login_id()
            authorized = next(
                (
                    account.account_id
                    for account in accounts
                    if account.account_id == target and account.auto_login_eligible
                ),
                None,
            )
            return AccountManagementSnapshot(accounts, authorized)

        return self._admin(read)

    def setup_administrator(
        self, username: str, password: str, *, revision: int | None = None
    ) -> Future[Account]:
        username_key(username)
        password_bytes(password)
        with self._lock:
            self._admission()
            actor = self._session
            account_id = uuid4().hex

            def check() -> None:
                if (
                    not self._accepting
                    or actor.revision != self._session.revision
                    or (revision is not None and revision != actor.revision)
                ):
                    raise StaleSessionError("Session decision was superseded")
                if not self._storage.uninitialized or actor.account is not None:
                    raise AccountConflictError(
                        "First Administrator setup is unavailable"
                    )

            check()

            def execute() -> Account:
                op = AuditOperation.by(
                    actor,
                    "account.create",
                    account_id,
                    after=f"{username} · ADMINISTRATOR · first setup",
                )
                self.audit.intent(op).result()
                try:
                    verifier = hash_password(password)
                    with self._lock:
                        check()
                        try:
                            if self._store is None:
                                self._store = AccountStore(self.path, initialize=True)
                            result = self._store.create(
                                username,
                                Role.ADMINISTRATOR,
                                verifier,
                                first=True,
                                account_id=account_id,
                            )
                        except (sqlite3.Error, OSError, SecurityStorageError) as exc:
                            self._storage = SecurityStorageSnapshot(
                                StorageState.UNAVAILABLE, _storage_issue(exc)
                            )
                            raise
                        self._storage = SecurityStorageSnapshot(StorageState.READY)
                except Exception as exc:
                    self.audit.complete(op, error_outcome(exc), type(exc).__name__)
                    raise
                self.audit.complete(op, "success", username)
                return result

            return self._enqueue(execute)

    def create_account(
        self,
        username: str,
        role: Role,
        password: str,
        *,
        confirm_administrator: bool = False,
        revision: int | None = None,
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
            revision=revision,
            confirmed=role is not Role.ADMINISTRATOR or confirm_administrator is True,
        )

    def set_enabled(
        self, account_id: str, enabled: bool, *, revision: int | None = None
    ) -> Future[Account]:
        return self._mutate(
            "account.enabled",
            account_id,
            str(enabled),
            None,
            lambda store, _: store.update(account_id, enabled=enabled),
            revision=revision,
        )

    def set_auto_login(
        self, account_id: str, enabled: bool, *, revision: int | None = None
    ) -> Future[Account]:
        return self._mutate(
            "account.set_auto_login" if enabled else "account.disable_auto_login",
            account_id,
            "",
            None,
            lambda store, _: store.set_auto_login(account_id, enabled),
            revision=revision,
        )

    def set_role(
        self,
        account_id: str,
        role: Role,
        *,
        confirm_administrator: bool = False,
        revision: int | None = None,
    ) -> Future[Account]:
        return self._mutate(
            "account.role",
            account_id,
            role.name,
            None,
            lambda store, _: store.update(account_id, role=role),
            revision=revision,
            confirmed=role is not Role.ADMINISTRATOR or confirm_administrator is True,
        )

    def reset_password(
        self, account_id: str, password: str, *, revision: int | None = None
    ) -> Future[Account]:
        return self._mutate(
            "account.reset_password",
            account_id,
            "",
            password,
            lambda store, verifier: store.update(
                account_id, verifier=self._verifier(verifier), temporary=True
            ),
            revision=revision,
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
        *,
        revision: int | None,
        confirmed: bool = True,
    ) -> Future[Account]:
        if password is not None:
            password_bytes(password)
        with self._lock:
            self._admission()
            ticket = self._session.revision if revision is None else revision
            try:
                self._actor(ticket, administrator=True)
                if not confirmed:
                    raise AuthorizationError("Confirm granting Administrator access")
            except IdentityError as exc:
                self.audit.record(
                    AuditOperation.by(self._session, action, target), error_outcome(exc)
                )
                raise
            initiator = self._session

            def execute() -> Account:
                try:
                    with self._lock:
                        self._actor(ticket, administrator=True)
                    before = ""
                    if action != "account.create":
                        previous, _ = self._available().by_id(target)
                        if previous.role is Role.ADMINISTRATOR:
                            raise AuthorizationError(
                                "Administrator accounts are protected"
                            )
                        if action == "account.role":
                            before = previous.role.name
                        elif action == "account.enabled":
                            before = str(previous.enabled)
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
                        self._actor(ticket, administrator=True)
                        result = operation(self._available(), verifier)
                except Exception as exc:
                    self.audit.complete(op, error_outcome(exc), type(exc).__name__)
                    raise
                self.audit.complete(op, "success", result.username)
                return result

            return self._enqueue(execute)

    def change_password(
        self, old_password: str, new_password: str, *, revision: int | None = None
    ) -> Future[SessionSnapshot]:
        password_bytes(old_password)
        return self._change_password(old_password, new_password, revision)

    def rotate_password(
        self, new_password: str, revision: int
    ) -> Future[SessionSnapshot]:
        if type(revision) is not int:
            raise StaleSessionError("A password-change session revision is required")
        return self._change_password(None, new_password, revision)

    def _change_password(
        self, old_password: str | None, new_password: str, revision: int | None
    ) -> Future[SessionSnapshot]:
        password_bytes(new_password)
        rotation = old_password is None
        with self._lock:
            self._admission()
            ticket = self._session.revision if revision is None else revision
            account = self._actor(ticket, rotation=rotation)
            actor = self._session

            def execute() -> SessionSnapshot:
                with self._lock:
                    self._actor(ticket, rotation=rotation)
                store = self._available()
                op = AuditOperation.by(
                    actor, "account.change_password", account.account_id
                )
                self.audit.intent(op).result()
                try:
                    current, previous = store.by_id(account.account_id)
                    if (
                        not current.enabled
                        or current.must_change_password != rotation
                        or (
                            old_password is not None
                            and not verify_password(old_password, previous)
                        )
                    ):
                        raise AuthenticationError("Invalid username or password")
                    if rotation and verify_password(new_password, previous):
                        raise PasswordReuseError("Choose a different password")
                    verifier = hash_password(new_password)
                    with self._lock:
                        self._actor(ticket, rotation=rotation)
                        updated = store.update(account.account_id, verifier=verifier)
                        result = self._publish(updated)
                except Exception as exc:
                    self.audit.complete(op, error_outcome(exc), type(exc).__name__)
                    raise
                self.audit.complete(op, "success")
                return result

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
                    state = SecurityStorageSnapshot(
                        StorageState.EMPTY
                        if self._store.empty()
                        else StorageState.READY
                    )
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
