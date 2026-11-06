"""Local account observations and fixed roles; no Qt or production permissions."""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum, IntEnum


class Role(IntEnum):
    GUEST = 0
    OPERATOR = 1
    ENGINEER = 2
    ADMINISTRATOR = 3


class IdentityError(RuntimeError):
    pass


class IdentityValidationError(IdentityError):
    pass


class PasswordReuseError(IdentityValidationError):
    pass


class AuthenticationError(IdentityError):
    pass


class AuthorizationError(IdentityError):
    pass


class StaleSessionError(IdentityError):
    pass


class SecurityStorageError(IdentityError):
    pass


class SecurityBusyError(IdentityError):
    pass


class LastAdministratorError(IdentityError):
    pass


class AccountConflictError(IdentityError):
    pass


def username_key(username: str) -> str:
    if (
        type(username) is not str
        or not 1 <= len(username) <= 64
        or username != username.strip()
        or not username.isprintable()
    ):
        raise IdentityValidationError(
            "Username must be 1–64 printable characters without edge whitespace"
        )
    return username.casefold()


def account_role(role: Role) -> Role:
    if type(role) is not Role or role is Role.GUEST:
        raise IdentityValidationError(
            "An account requires Operator, Engineer or Administrator"
        )
    return role


@dataclass(frozen=True, slots=True)
class Account:
    account_id: str
    username: str
    role: Role
    enabled: bool
    last_login_utc: datetime | None = None
    must_change_password: bool = False


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    revision: int = 0
    account: Account | None = None

    @property
    def role(self) -> Role:
        if self.account is None or self.account.must_change_password:
            return Role.GUEST
        return self.account.role

    @property
    def rotation_required(self) -> bool:
        return self.account is not None and self.account.must_change_password

    def permits(self, minimum: Role) -> bool:
        return not self.rotation_required and self.role >= minimum


class StorageState(Enum):
    OPENING = "opening"
    MISSING = "missing"
    EMPTY = "empty"
    READY = "ready"
    UNAVAILABLE = "unavailable"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class SecurityStorageSnapshot:
    state: StorageState = StorageState.OPENING
    issue: str = ""

    @property
    def uninitialized(self) -> bool:
        return self.state in (StorageState.MISSING, StorageState.EMPTY)
