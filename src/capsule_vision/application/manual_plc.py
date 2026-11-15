"""Admission of one diagnostic request on its existing serial transport worker."""

from collections.abc import Callable

from capsule_vision.communication.endpoints import FinsUdpEndpoint
from capsule_vision.communication.fins import FinsError, FinsPlcError
from capsule_vision.communication.modbus_tcp import (
    ModbusError,
    ModbusExceptionResponse,
    ModbusSettings,
)
from capsule_vision.communication.operations import (
    CommunicationRequest,
    CommunicationResponse,
    RequestKind,
)

from .identity import LocalIdentity, admit_engineering


def manual_target(request: CommunicationRequest) -> str:
    endpoint = request.endpoint
    route = ""
    if isinstance(endpoint, ModbusSettings):
        route = f" unit {endpoint.unit_id}"
    elif isinstance(endpoint, FinsUdpEndpoint):
        route = f" {endpoint.destination}"
    return f"{request.protocol_name} {endpoint.host}:{endpoint.port}{route} {request.area.value} {request.start} × {request.count}"


def _write_execution_uncertain(error: BaseException) -> bool:
    if isinstance(error, (FinsPlcError, ModbusExceptionResponse)):
        return False
    return (
        not isinstance(error, (FinsError, ModbusError))
        or error.write_may_have_executed is not False
    )


def execute_manual(
    request: CommunicationRequest,
    execute: Callable[[CommunicationRequest], CommunicationResponse],
    guard: Callable[[CommunicationRequest], None] | None,
    identity: LocalIdentity | None,
    revision: int | None,
) -> CommunicationResponse:
    request.validate()
    if identity is None or request.kind is not RequestKind.WRITE:
        admit_engineering(identity, revision)
        if guard:
            guard(request)
        return execute(request)
    attempted = False
    with identity.change(
        "plc.write",
        manual_target(request),
        revision,
        after=f"[{', '.join(map(str, request.values))}]",
        uncertain=lambda exc: attempted and _write_execution_uncertain(exc),
    ):
        if guard:
            guard(request)
        attempted = True
        result = execute(request)
        if result.request != request or result.values is not None:
            raise RuntimeError("Missing matching write acknowledgement")
        return result
