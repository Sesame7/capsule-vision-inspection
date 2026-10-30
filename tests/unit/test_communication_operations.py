"""Manual dispatch policy is Qt independent; no remote network access."""

from dataclasses import FrozenInstanceError, fields, replace
from typing import cast

import pytest

from capsule_vision.communication import (
    Endpoint,
    FinsAddress,
    FinsMemoryArea,
    FinsTcpClient,
    FinsTcpEndpoint,
    FinsUdpClient,
    FinsUdpEndpoint,
    ModbusSettings,
)
from capsule_vision.communication.operations import (
    CommunicationRequest,
    RegisterWatchGroup,
    RequestKind,
    execute_request,
)
from capsule_vision.communication.reporting import ReportingSettings


def request() -> CommunicationRequest:
    return CommunicationRequest(
        kind=RequestKind.READ,
        start=100,
        endpoint=FinsUdpEndpoint(
            host="127.0.0.1",
            port=9600,
            timeout=1,
            destination=FinsAddress(0, 2, 0),
            source=FinsAddress(0, 1, 0),
        ),
    )


@pytest.mark.parametrize(
    "invalid",
    [
        replace(request(), endpoint=cast(Endpoint, "tcp")),
        replace(request(), start=32767, count=2),
        replace(request(), count=0),
        replace(request(), count=True),
        replace(request(), count=1000),
        replace(request(), values=(1,)),
        replace(request(), kind=RequestKind.WRITE, count=0),
        replace(request(), kind=RequestKind.WRITE, values=(-1,)),
        replace(request(), kind=RequestKind.WRITE, values=(65536,)),
        replace(request(), kind=RequestKind.WRITE, values=(True,)),
        replace(request(), kind=RequestKind.WRITE, count=998, values=(0,) * 998),
        replace(request(), kind=RequestKind.WRITE, count=2, values=(1,)),
        replace(request(), area=FinsMemoryArea.W, start=512),
        replace(request(), area=FinsMemoryArea.W, start=511, count=2),
        replace(
            request(),
            area=FinsMemoryArea.W,
            kind=RequestKind.WRITE,
            start=511,
            count=2,
            values=(1, 2),
        ),
    ],
)
def test_invalid_request_rejected_before_socket_creation(
    invalid: CommunicationRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_socket(*args: object, **kwargs: object) -> None:
        pytest.fail("Invalid input reached socket construction")

    monkeypatch.setattr(
        "capsule_vision.communication.endpoints.FinsUdpClient", no_socket
    )
    with pytest.raises(ValueError):
        execute_request(invalid)


@pytest.mark.parametrize("host", ["192.0.2.10", "127.0.0.2", "127.1.1.1"])
@pytest.mark.parametrize("area", list(FinsMemoryArea))
@pytest.mark.parametrize("endpoint_type", [FinsUdpEndpoint, FinsTcpEndpoint])
def test_remote_write_uses_configured_endpoint(
    host: str,
    area: FinsMemoryArea,
    endpoint_type: type[FinsUdpEndpoint] | type[FinsTcpEndpoint],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[object] = []

    def init(
        self: FinsUdpClient | FinsTcpClient, address: str, **kwargs: object
    ) -> None:
        observed.append((address, kwargs))

    def enter(self: FinsUdpClient | FinsTcpClient) -> FinsUdpClient | FinsTcpClient:
        return self

    def write(
        self: FinsUdpClient | FinsTcpClient,
        start: int,
        values: object,
        *,
        area: FinsMemoryArea,
    ) -> None:
        observed.append((start, values, area))

    def close(self: FinsUdpClient | FinsTcpClient) -> None:
        observed.append("closed")

    for cls in (FinsUdpClient, FinsTcpClient):
        monkeypatch.setattr(cls, "__init__", init)
        monkeypatch.setattr(cls, "__enter__", enter)
        monkeypatch.setattr(cls, "write_words", write)
        monkeypatch.setattr(cls, "close", close)
    target = replace(
        request(),
        kind=RequestKind.WRITE,
        values=(4321,),
        area=area,
        endpoint=endpoint_type(
            host=host, port=request().endpoint.port, timeout=request().endpoint.timeout
        ),
    )
    response = execute_request(target)
    assert response.request == target and response.values is None
    assert isinstance(observed[0], tuple) and observed[0][0] == host
    assert observed[1:] == [(100, (4321,), area), "closed"]


@pytest.mark.parametrize("area", list(FinsMemoryArea))
def test_nonloopback_read_uses_configured_address_without_substitution(
    monkeypatch: pytest.MonkeyPatch,
    area: FinsMemoryArea,
) -> None:
    observed: list[tuple[str, object]] = []

    def init(
        self: FinsUdpClient,
        host: str,
        *,
        destination: FinsAddress,
        source: FinsAddress,
        port: int,
        timeout: float,
    ) -> None:
        observed.append(("construct", (host, port, timeout, destination, source)))

    def read(
        self: FinsUdpClient,
        start: int,
        count: int = 1,
        *,
        area: FinsMemoryArea = FinsMemoryArea.DM,
    ) -> tuple[int, ...]:
        observed.append(("read", (start, count, area)))
        return (0, 65535)

    def close(self: FinsUdpClient) -> None:
        observed.append(("close", None))

    def enter(self: FinsUdpClient) -> FinsUdpClient:
        return self

    monkeypatch.setattr(FinsUdpClient, "__init__", init)
    monkeypatch.setattr(FinsUdpClient, "__enter__", enter)
    monkeypatch.setattr(FinsUdpClient, "read_words", read)
    monkeypatch.setattr(FinsUdpClient, "close", close)
    target = replace(
        request(),
        count=2,
        area=area,
        endpoint=replace(
            request().endpoint,
            host="192.0.2.10",
            port=19600,
            destination=FinsAddress(7, 19, 0),
            source=FinsAddress(3, 41, 0),
        ),
    )
    response = execute_request(target)
    assert response.request == target and response.values == (0, 65535)
    assert response.elapsed_ms >= 0 and response.completed_at.tzinfo is not None
    assert isinstance(target.endpoint, FinsUdpEndpoint)
    assert observed == [
        (
            "construct",
            (
                target.endpoint.host,
                target.endpoint.port,
                target.endpoint.timeout,
                target.endpoint.destination,
                target.endpoint.source,
            ),
        ),
        ("read", (100, 2, area)),
        ("close", None),
    ]


@pytest.mark.parametrize(
    "area,start,count",
    [
        (FinsMemoryArea.DM, 32767, 2),
        (FinsMemoryArea.W, 511, 2),
        (FinsMemoryArea.W, 512, 1),
        (FinsMemoryArea.DM, 0, 1000),
        (FinsMemoryArea.DM, -1, 1),
        (FinsMemoryArea.W, 0, 0),
        (FinsMemoryArea.DM, True, 1),
    ],
)
def test_watch_group_range_validation(
    area: FinsMemoryArea, start: int, count: int
) -> None:
    with pytest.raises(ValueError):
        RegisterWatchGroup(area, start, count)


def test_watch_group_boundary_and_name() -> None:
    assert RegisterWatchGroup(FinsMemoryArea.W, 0, 512, "work area").count == 512
    assert RegisterWatchGroup(FinsMemoryArea.DM, 0, 999).count == 999
    with pytest.raises(ValueError, match="64"):
        RegisterWatchGroup(FinsMemoryArea.W, 49, 4, "x" * 65)


@pytest.mark.parametrize(
    "endpoint_type", [FinsUdpEndpoint, FinsTcpEndpoint, ModbusSettings]
)
@pytest.mark.parametrize(
    "field,value",
    [
        ("host", "localhost"),
        ("host", "224.0.0.1"),
        ("host", "0.0.0.0"),
        ("host", "255.255.255.255"),
        ("port", 0),
        ("port", True),
        ("timeout", 0),
        ("timeout", 31),
        ("timeout", True),
        ("timeout", float("nan")),
        ("timeout", float("inf")),
    ],
)
def test_invalid_endpoint_rejected_before_network_access(
    endpoint_type: type[FinsUdpEndpoint] | type[FinsTcpEndpoint] | type[ModbusSettings],
    field: str,
    value: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Invalid endpoint reached socket construction")

    monkeypatch.setattr("socket.socket", forbidden)
    with pytest.raises(ValueError):
        replace(endpoint_type(), **{field: value})


@pytest.mark.parametrize(
    "endpoint", [FinsUdpEndpoint(), FinsTcpEndpoint(), ModbusSettings()]
)
def test_endpoint_has_only_its_protocol_fields_and_is_immutable(
    endpoint: Endpoint,
) -> None:
    names = {field.name for field in fields(endpoint)}
    common = {"host", "port", "timeout"}
    expected = common | (
        {"destination", "source"}
        if isinstance(endpoint, FinsUdpEndpoint)
        else {"unit_id"}
        if isinstance(endpoint, ModbusSettings)
        else set[str]()
    )
    assert names == expected
    assert endpoint.protocol in ("fins_udp", "fins_tcp", "modbus_tcp")
    assert endpoint.port == (502 if isinstance(endpoint, ModbusSettings) else 9600)
    settings = ReportingSettings(endpoint=endpoint)
    assert settings.endpoint is endpoint
    assert {field.name for field in fields(settings)} == {
        "endpoint",
        "base_address",
        "max_pending",
    }
    request = CommunicationRequest(RequestKind.READ, endpoint, 0)
    assert request.endpoint is endpoint
    assert {field.name for field in fields(request)} == {
        "kind",
        "endpoint",
        "start",
        "count",
        "values",
        "area",
    }
    with pytest.raises(FrozenInstanceError):
        field_name = "host"
        setattr(endpoint, field_name, "192.0.2.1")
