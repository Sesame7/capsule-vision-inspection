"""Versioned standard-library password verifiers. Never serialize a password."""

import hashlib
import hmac
import secrets
from dataclasses import dataclass

from .identity_models import IdentityValidationError, SecurityStorageError

WORK_FACTOR = 600_000
MIN_WORK_FACTOR = 100_000
MAX_WORK_FACTOR = 2_000_000


def password_bytes(password: str) -> bytes:
    if type(password) is not str:
        raise IdentityValidationError("Password must be text")
    try:
        value = password.encode("utf-8")
    except UnicodeError:
        raise IdentityValidationError("Password must be valid UTF-8 text") from None
    if not 1 <= len(value) <= 1024:
        raise IdentityValidationError("Password must contain 1–1024 UTF-8 bytes")
    return value


@dataclass(frozen=True, slots=True, repr=False)
class PasswordVerifier:
    version: int
    iterations: int
    salt: bytes
    digest: bytes

    def __post_init__(self) -> None:
        if (
            type(self.version) is not int
            or self.version != 1
            or type(self.iterations) is not int
            or not MIN_WORK_FACTOR <= self.iterations <= MAX_WORK_FACTOR
            or type(self.salt) is not bytes
            or len(self.salt) != 16
            or type(self.digest) is not bytes
            or len(self.digest) != 32
        ):
            raise SecurityStorageError("Unsupported or malformed password verifier")


def hash_password(password: str) -> PasswordVerifier:
    salt = secrets.token_bytes(16)
    return PasswordVerifier(
        1,
        WORK_FACTOR,
        salt,
        hashlib.pbkdf2_hmac(
            "sha256", password_bytes(password), salt, WORK_FACTOR, dklen=32
        ),
    )


def verify_password(password: str, verifier: PasswordVerifier) -> bool:
    digest = hashlib.pbkdf2_hmac(
        "sha256", password_bytes(password), verifier.salt, verifier.iterations, dklen=32
    )
    return hmac.compare_digest(digest, verifier.digest)
