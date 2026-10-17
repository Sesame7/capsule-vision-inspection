"""Serialized Web ownership, observable close and real localhost rebinds."""

import json
import socket
from dataclasses import replace
from http.client import HTTPConnection
from ipaddress import IPv4Interface
from threading import Event, get_ident
from threading import enumerate as threads
from time import monotonic

import pytest

from vimo_demo.application import (
    ApplicationState,
    ProductionApplicationSnapshot,
    ProductionCounters,
)
from vimo_demo.application.communication_preferences import WebPreferences
from vimo_demo.inspection import CameraId
from vimo_demo.ui.display_frames import LatestDisplayFrameStore
from vimo_demo.web import ProductionSnapshotService
from vimo_demo.web.hosts import local_ipv4_addresses
from vimo_demo.web.runtime import WebRuntime, WebState


class Source:
    def snapshot(self) -> ProductionApplicationSnapshot:
        return ProductionApplicationSnapshot(
            ApplicationState.READY,
            0,
            None,
            None,
            None,
            ProductionCounters(),
            None,
            False,
            None,
        )


def service(settings: WebPreferences) -> ProductionSnapshotService:
    return ProductionSnapshotService(
        Source(),
        LatestDisplayFrameStore((CameraId("CAM1"),)),
        host=settings.host,
        port=settings.port,
        interval=settings.interval_ms / 1000,
    )


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def get(port: int, path: str) -> tuple[int, bytes]:
    connection = HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def publisher_id(port: int) -> str:
    deadline = monotonic() + 3
    while True:
        status, raw = get(port, "/api/snapshot")
        if status == 200:
            value: object = json.loads(raw)["publisher_id"]
            assert isinstance(value, str)
            return value
        assert status == 503 and monotonic() < deadline
        Event().wait(0.01)


class HeldService(ProductionSnapshotService):
    """No socket threads; barriers expose construction and actual release ordering."""

    def __init__(self, settings: WebPreferences) -> None:
        super().__init__(
            Source(), LatestDisplayFrameStore((CameraId("CAM1"),)), port=settings.port
        )
        self.entered, self.release_start = Event(), Event()
        self.joined, self.release_join = Event(), Event()
        self.release_start.set()
        self.release_join.set()
        self.active = False
        self.fail = False
        self.incomplete = False
        self.owners: list[int] = []

    def start(self) -> None:
        self.owners.append(get_ident())
        self.entered.set()
        assert self.release_start.wait(5)
        self.active = True
        if self.fail:
            raise OSError("controlled bind failure")

    @property
    def http_alive(self) -> bool:
        return self.active

    @property
    def publisher_alive(self) -> bool:
        return self.active

    def request_stop(self) -> None:
        self.owners.append(get_ident())
        self.stopping.set()

    def join(self, timeout: float | None = None) -> bool:
        self.owners.append(get_ident())
        self.joined.set()
        assert self.release_join.wait(5)
        if self.incomplete:
            return False
        self.active = False
        return True


def test_actual_restart_changes_identity_releases_socket_and_restores_old_bind() -> (
    None
):
    before = {t.ident for t in threads()}
    first = WebPreferences(port=free_port())
    second = replace(first, port=free_port(), interval_ms=1000)
    runtime = WebRuntime(service)
    try:
        assert runtime.start(first).result(5).state is WebState.RUNNING
        old_id = publisher_id(first.port)
        assert runtime.apply(second).result(5).configuration == second
        with socket.socket() as probe:
            probe.bind((first.host, first.port))  # Old listener really released.
        assert publisher_id(second.port) != old_id
        assert (
            get(second.port, f"/api/cameras/CAM1.jpg?publisher={old_id}&revision=1")[0]
            == 410
        )
        with socket.socket() as occupied:
            occupied.bind((first.host, first.port))
            occupied.listen()
            result = runtime.apply(first).result(5)
            assert result.state is WebState.RUNNING and result.restored
            assert result.configuration == second and "OSError" in result.error
            assert get(second.port, "/health")[0] == 200
        assert (
            runtime.apply(second).result(5).configuration == second
        )  # Same settings restart.
        assert runtime.stop().result(5).state is WebState.STOPPED
        assert runtime.stop().result(5).state is WebState.STOPPED
        assert runtime.start(second).result(5).state is WebState.RUNNING
        assert (
            runtime.apply(replace(second, enabled=False)).result(5).state
            is WebState.STOPPED
        )
        with pytest.raises(ValueError):
            runtime.start(replace(first, enabled=False))
    finally:
        runtime.request_close()
        assert runtime.join(5)
    assert not {t.ident for t in threads()} - before


@pytest.mark.parametrize("stage", ["start", "stop"])
def test_close_fences_commands_and_waits_for_actual_owned_work(stage: str) -> None:
    settings = WebPreferences()
    owned = HeldService(settings)
    if stage == "start":
        owned.release_start.clear()
    else:
        owned.release_join.clear()
    runtime = WebRuntime(lambda _: owned)
    try:
        started = runtime.start(settings)
        assert owned.entered.wait(3)
        if stage == "stop":
            assert started.result(3).state is WebState.RUNNING
            pending = runtime.stop()
            assert owned.joined.wait(3)
        else:
            pending = started
        assert runtime.snapshot().state is (
            WebState.STARTING if stage == "start" else WebState.STOPPING
        )
        with pytest.raises(RuntimeError, match="in progress"):
            runtime.apply(settings)
        assert not pending.cancel()
        runtime.request_close()
        runtime.request_close()
        with pytest.raises(RuntimeError, match="closing"):
            runtime.start(settings)
        assert not runtime.join(0)
        assert not runtime.closed and runtime.snapshot().owned
        owned.release_start.set()
        owned.release_join.set()
        pending.result(3)
        assert runtime.join(3)
        assert not runtime.snapshot().owned
        assert runtime.snapshot().state is WebState.STOPPED
        assert len(set(owned.owners)) == 1 and owned.owners[0] != get_ident()
    finally:
        owned.release_start.set()
        owned.release_join.set()
        runtime.request_close()
        assert runtime.join(5)


def test_failed_cleanup_retains_owner_and_prevents_replacement() -> None:
    first = HeldService(WebPreferences())
    first.incomplete = True
    created: list[HeldService] = []

    def factory(_settings: WebPreferences) -> HeldService:
        created.append(first)
        return first

    runtime = WebRuntime(factory)
    try:
        runtime.start(WebPreferences()).result(3)
        result = runtime.apply(WebPreferences(port=8766)).result(3)
        assert result.state is WebState.FAILED and result.owned
        assert "incomplete" in result.error and len(created) == 1
        runtime.request_close()
        assert not runtime.join(0)
        first.incomplete = False
    finally:
        first.incomplete = False
        runtime.request_close()
        assert runtime.join(5)


def test_failed_candidate_and_failed_restoration_are_explicit() -> None:
    created: list[HeldService] = []

    def factory(settings: WebPreferences) -> HeldService:
        value = HeldService(settings)
        value.fail = bool(created)
        created.append(value)
        return value

    runtime = WebRuntime(factory)
    try:
        runtime.start(WebPreferences()).result(3)
        result = runtime.apply(WebPreferences(port=8766)).result(3)
        assert result.state is WebState.FAILED and not result.owned
        assert "restoration" in result.error and not result.restored
        assert len(created) == 3 and all(not item.active for item in created)
    finally:
        runtime.request_close()
        assert runtime.join(5)


def test_enumeration_is_local_deterministic_and_always_offers_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "vimo_demo.web.hosts.local_interfaces",
        lambda: (
            IPv4Interface("192.168.2.8/24"),
            IPv4Interface("10.0.0.8/24"),
            IPv4Interface("192.168.2.8/24"),
            IPv4Interface("127.0.0.2/8"),
            IPv4Interface("0.0.0.0/0"),
            IPv4Interface("224.0.0.1/4"),
            IPv4Interface("10.0.0.0/24"),
            IPv4Interface("10.0.0.255/24"),
        ),
    )
    assert local_ipv4_addresses() == ("127.0.0.1", "10.0.0.8", "192.168.2.8")
    monkeypatch.setattr("vimo_demo.web.hosts.local_interfaces", lambda: ())
    assert local_ipv4_addresses() == ("127.0.0.1",)
