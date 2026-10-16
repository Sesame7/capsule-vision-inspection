"""Manual dispatch policy is Qt independent; no remote network access."""

from dataclasses import replace
from typing import cast

import pytest

from vimo_demo.communication import (
    FinsAddress,
    FinsMemoryArea,
    FinsTcpClient,
    FinsTransport,
    FinsUdpClient,
)
from vimo_demo.communication.operations import (
    CommunicationRequest,
    RegisterWatchGroup,
    RequestKind,
    execute_request,
)


def request() -> CommunicationRequest:
    return CommunicationRequest(
        RequestKind.READ,
        "127.0.0.1",
        9600,
        1,
        FinsAddress(0, 2, 0),
        FinsAddress(0, 1, 0),
        100,
    )


@pytest.mark.parametrize(
    "invalid",
    [
        replace(request(), transport=cast(FinsTransport, "tcp")),
        replace(request(), host="localhost"),
        replace(request(), host="224.0.0.1"),
        replace(request(), host="0.0.0.0"),
        replace(request(), host="255.255.255.255"),
        replace(request(), port=0),
        replace(request(), timeout=0),
        replace(request(), timeout=31),
        replace(request(), timeout=True),
        replace(request(), start=32767, count=2),
        replace(request(), count=0),
        replace(request(), count=True),
        replace(request(), count=1000),
        replace(request(), kind=RequestKind.TEST, count=2),
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

    monkeypatch.setattr("vimo_demo.communication.operations.FinsUdpClient", no_socket)
    with pytest.raises(ValueError):
        execute_request(invalid)


@pytest.mark.parametrize("host", ["192.0.2.10", "127.0.0.2", "127.1.1.1"])
@pytest.mark.parametrize("area", list(FinsMemoryArea))
@pytest.mark.parametrize("transport", list(FinsTransport))
def test_remote_write_uses_configured_endpoint(
    host: str,
    area: FinsMemoryArea,
    transport: FinsTransport,
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
        host=host,
        values=(4321,),
        area=area,
        transport=transport,
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
        host="192.0.2.10",
        port=19600,
        count=2,
        destination=FinsAddress(7, 19, 0),
        source=FinsAddress(3, 41, 0),
        area=area,
    )
    response = execute_request(target)
    assert response.request == target and response.values == (0, 65535)
    assert response.elapsed_ms >= 0 and response.completed_at.tzinfo is not None
    assert observed == [
        (
            "construct",
            (
                target.host,
                target.port,
                target.timeout,
                target.destination,
                target.source,
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
