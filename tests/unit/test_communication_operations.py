"""Manual dispatch policy is Qt independent; no remote network access."""

from dataclasses import replace

import pytest

from vimo_demo.communication import FinsAddress, FinsUdpClient
from vimo_demo.communication.operations import (
    CommunicationRequest,
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
def test_write_permission_is_enforced_at_execution_boundary(
    host: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_socket(*args: object, **kwargs: object) -> None:
        pytest.fail("Forbidden write reached socket construction")

    monkeypatch.setattr("vimo_demo.communication.operations.FinsUdpClient", no_socket)
    with pytest.raises(PermissionError, match="127.0.0.1"):
        execute_request(
            replace(request(), kind=RequestKind.WRITE, host=host, values=(4321,))
        )


def test_nonloopback_read_uses_configured_address_without_substitution(
    monkeypatch: pytest.MonkeyPatch,
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

    def read(self: FinsUdpClient, start: int, count: int = 1) -> tuple[int, ...]:
        observed.append(("read", (start, count)))
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
        ("read", (100, 2)),
        ("close", None),
    ]
