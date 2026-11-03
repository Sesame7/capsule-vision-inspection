"""Presentation hints for commands protected by the existing Application owner."""

from capsule_vision.application import ProductionApplication
from capsule_vision.application.identity_models import (
    AuthorizationError,
    Role,
    StaleSessionError,
)


def permits(application: ProductionApplication | None, minimum: Role) -> bool:
    identity = application.identity if application is not None else None
    return identity is None or identity.snapshot().role >= minimum


def failure_key(
    error: object, fallback: str, required: str = "permission_engineer"
) -> str:
    if isinstance(error, StaleSessionError):
        return "identity_stale"
    if isinstance(error, AuthorizationError):
        return required
    return fallback
