"""One stopped-candidate DM application; serial, bounded and never retried."""

from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Protocol

from capsule_vision.communication.endpoints import FinsTcpEndpoint
from capsule_vision.communication.fins import (
    MAX_WRITE_WORDS,
    FinsMemoryArea,
    FinsPlcError,
    validate_word_range,
)
from capsule_vision.communication.fins_tcp import FinsTcpClient
from capsule_vision.configuration import ConfigurationError
from capsule_vision.configuration.communication_preferences import (
    StatusPreferences,
)
from capsule_vision.configuration.process_parameters import DINT_MAX, DINT_MIN

from .audit import AuditOperation, error_outcome
from .identity import LocalIdentity
from .identity_models import Role
from .models import ApplicationError, PreparedConfiguration
from .process_preview import signed_dint


class ProcessClient(Protocol):
    def read_words(
        self, start: int, count: int = 1, *, area: FinsMemoryArea = FinsMemoryArea.DM
    ) -> tuple[int, ...]: ...

    def write_words(
        self,
        start: int,
        values: tuple[int, ...],
        *,
        area: FinsMemoryArea = FinsMemoryArea.DM,
    ) -> None: ...

    def close(self) -> None: ...


def open_process_client(endpoint: FinsTcpEndpoint) -> ProcessClient:
    return FinsTcpClient(endpoint.host, port=endpoint.port, timeout=endpoint.timeout)


def process_target(settings: StatusPreferences) -> tuple[FinsTcpEndpoint, int]:
    """Continuous Status enablement and output/alarm addresses do not bind DM application."""
    return settings.endpoint, settings.running_address


@dataclass(frozen=True, slots=True)
class ProcessWrite:
    parameter: str
    address: int
    value: int

    @property
    def words(self) -> tuple[int, int]:
        raw = self.value & 0xFFFFFFFF
        return raw & 65535, raw >> 16

    @property
    def description(self) -> str:
        return f"{self.parameter}: D{self.address}–D{self.address + 1} = {self.value}"


class ProcessApplicationError(ApplicationError):
    """Failure evidence never implies rollback or a PLC-atomic transaction."""

    def __init__(
        self,
        cause: BaseException,
        attempted: tuple[ProcessWrite, ...],
        verified: tuple[ProcessWrite, ...],
        rejected: tuple[ProcessWrite, ...] = (),
    ) -> None:
        self.attempted, self.verified = attempted, verified
        self.rejected = rejected
        uncertain = tuple(
            p for p in attempted if p not in verified and p not in rejected
        )
        self.uncertain = bool(uncertain)
        detail = "; ".join(p.description for p in uncertain)
        super().__init__(
            f"PLC process application failed: {cause}. "
            f"Verified {len(verified)}/{len(attempted)} attempted values."
            + (f" Not verified (may have been written): {detail}." if detail else "")
            + (
                f" PLC rejected: {'; '.join(p.description for p in rejected)}."
                if rejected
                else ""
            )
            + " Reselect the saved Recipe to try again."
        )


def process_plan(candidate: PreparedConfiguration) -> tuple[ProcessWrite, ...]:
    values = candidate.configuration.process_parameters
    if not values:
        return ()
    dependencies = candidate.recipe_dependencies
    if (
        dependencies is None
        or dependencies.entry.recipe.recipe_id != candidate.configuration.recipe_id
        or dependencies.entry.recipe.process_parameters != values
    ):
        raise ConfigurationError(
            "Process application requires matching saved Recipe evidence"
        )
    catalog = dependencies.entry.process_catalog
    catalog.validate(values)
    selected = dict(values)
    plan = tuple(
        ProcessWrite(p.id, p.address, selected[p.id])
        for p in catalog.parameters
        if p.id in selected
    )
    for item in plan:
        validate_word_range(item.address, 2, MAX_WRITE_WORDS, FinsMemoryArea.DM)
        if not DINT_MIN <= item.value <= DINT_MAX:
            raise ConfigurationError("Process value is outside signed DINT range")
    return plan


def apply_process(
    candidate: PreparedConfiguration,
    settings: StatusPreferences,
    guard: Callable[[], None],
    *,
    identity: LocalIdentity | None,
    revision: int | None,
    connect: Callable[[FinsTcpEndpoint], ProcessClient] = open_process_client,
) -> None:
    """Worker-only. Guard every independently admitted operation; release all locks for IO.

    Each pair has a durable intent/result, linked to the enclosing attempt. Thus
    large catalogs do not truncate address/value evidence into one audit summary.
    """
    plan = process_plan(candidate)
    if not plan:
        return
    attempted: list[ProcessWrite] = []
    verified: list[ProcessWrite] = []
    rejected: list[ProcessWrite] = []

    def admit() -> None:
        guard()
        if identity:
            with identity.admission(Role.OPERATOR, revision, action=""):
                pass

    admit()
    recipe_hash = next(
        (r.sha256 for r in candidate.saved_content if r.artifact == "recipe"),
        "unavailable",
    )
    target = candidate.configuration.recipe_id
    actor = None
    if identity:
        with identity.admission(Role.OPERATOR, revision, action="") as admitted:
            actor = admitted
    attempt = (
        AuditOperation.by(
            actor,
            "recipe.process_apply",
            target,
            after=f"Recipe SHA256 {recipe_hash}; "
            f"{settings.endpoint.host}:{settings.endpoint.port}; W{settings.running_address}.1; "
            f"{len(plan)} values; {candidate.configuration.display_name}",
        )
        if actor
        else None
    )
    if identity and attempt:
        identity.audit.intent(attempt).result()

    @contextmanager
    def parameter_audit(item: ProcessWrite) -> Generator[None]:
        operation = (
            AuditOperation.by(
                actor,
                "recipe.process_value",
                target,
                before=f"Application {attempt.id}",
                after=item.description,
            )
            if actor and attempt
            else None
        )
        if identity and operation:
            identity.audit.intent(operation).result()
        try:
            yield
        except BaseException as exc:
            if identity and operation:
                identity.audit.complete(
                    operation,
                    "uncertain"
                    if item in attempted
                    and item not in verified
                    and item not in rejected
                    else error_outcome(exc),
                    f"{type(exc).__name__}: {exc}",
                ).result()
            raise
        else:
            if identity and operation:
                identity.audit.complete(
                    operation, "success", "Register readback matched"
                ).result()

    try:
        admit()
        client = connect(settings.endpoint)
        try:
            admit()
            running = client.read_words(
                settings.running_address, 1, area=FinsMemoryArea.W
            )
            if len(running) != 1:
                raise ApplicationError("Missing Running observation")
            if running[0] & 2:
                raise ApplicationError(
                    f"Machine running: W{settings.running_address}.1 = 1"
                )
            for item in plan:
                admit()
                with parameter_audit(item):
                    admit()
                    attempted.append(item)
                    try:
                        client.write_words(item.address, item.words)
                    except FinsPlcError:
                        rejected.append(item)
                        raise
                    admit()
                    readback = client.read_words(item.address, 2)
                    if len(readback) != 2 or signed_dint(*readback) != item.value:
                        raise ApplicationError(
                            f"Readback mismatch at D{item.address}: expected {item.value}, words {readback}"
                        )
                    verified.append(item)
            admit()
        finally:
            client.close()
    except BaseException as exc:
        failure = ProcessApplicationError(
            exc, tuple(attempted), tuple(verified), tuple(rejected)
        )
        if identity and attempt:
            identity.audit.complete(
                attempt,
                "uncertain" if failure.uncertain else error_outcome(exc),
                str(failure),
            ).result()
        raise failure from exc
    else:
        if identity and attempt:
            identity.audit.complete(
                attempt,
                "success",
                f"{len(verified)} register values verified; no ladder-consumption confirmation",
            ).result()
