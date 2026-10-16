"""CPU-only identity/encoding and bounded localhost publication regressions."""

import json
import os
import shutil
import socket
import subprocess
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
from http.client import HTTPConnection
from http.server import HTTPServer
from ipaddress import IPv4Interface
from pathlib import Path
from threading import Event, get_ident
from threading import enumerate as threads
from time import monotonic

import cv2
import numpy as np
import pytest
from tests.business_support import mixed_result
from tests.production_support import source_frame

from vimo_demo.application import (
    ApplicationState,
    ProductionApplicationSnapshot,
    ProductionCounters,
)
from vimo_demo.inspection import CameraId, CycleId, Frame, FrameId
from vimo_demo.ui.display_frames import DisplayFrame, LatestDisplayFrameStore
from vimo_demo.web import ProductionSnapshotService
from vimo_demo.web.hosts import local_interfaces, validate_host
from vimo_demo.web.publisher import EncodedImage, SnapshotPublisher, encode_image

CAMERAS = tuple(CameraId(f"CAM{i}") for i in range(6, 0, -1))


class Source:
    def __init__(self) -> None:
        self.value = ProductionApplicationSnapshot(
            ApplicationState.RUNNING,
            1,
            None,
            None,
            None,
            ProductionCounters(),
            mixed_result(),
            False,
            None,
        )
        self.fail = False

    def snapshot(self) -> ProductionApplicationSnapshot:
        if self.fail:
            raise ValueError("test observation unavailable")
        return self.value


def inputs() -> tuple[Source, LatestDisplayFrameStore]:
    store = LatestDisplayFrameStore(CAMERAS)
    for camera in CAMERAS:
        store.publish(source_frame(camera), generation=1)
    return Source(), store


def wait(predicate: Callable[[], bool]) -> None:
    deadline = monotonic() + 3
    while not predicate():
        assert monotonic() < deadline, "Observation did not arrive"
        Event().wait(0.01)


def request(
    service: ProductionSnapshotService, path: str, method: str = "GET"
) -> tuple[int, bytes, dict[str, str]]:
    connection = HTTPConnection("127.0.0.1", service.port, timeout=2)
    try:
        connection.request(method, path)
        response = connection.getresponse()
        return response.status, response.read(), dict(response.getheaders())
    finally:
        connection.close()


def test_metadata_preserves_configured_order_identity_and_domain_status() -> None:
    source, store = inputs()
    publisher = SnapshotPublisher(source, store)
    publisher.publish()
    publication = publisher.latest()
    assert publication is not None
    metadata = json.loads(publication.metadata)
    assert metadata["configured_camera_ids"] == list(CAMERAS)
    assert metadata["application_state"] == "RUNNING"
    assert metadata["generation"] == metadata["latest_cycle_id"] == 1
    offset = datetime.fromisoformat(metadata["observed_at"]).utcoffset()
    assert offset is not None and offset.total_seconds() == 0
    rows = metadata["cameras"]
    result = source.value.latest_production_result
    assert result is not None
    for camera, row in zip(CAMERAS, rows, strict=True):
        assert row["camera_id"] == camera
        assert row["result_status"] == "matched"
        assert row["image_identity"]["camera_id"] == camera
        assert row["image_identity"]["sequence"] == 7
        expected = next(
            camera_result
            for camera_result in result.cameras
            if camera_result.camera_id == camera
        )
        assert row["channels"] == [
            {
                "channel_id": channel.channel_id,
                "business_status": channel.business_status.name,
                "technical_status": channel.assessment.technical_status.name,
            }
            for channel in expected.channels
        ]
    assert len(publication.images) == 6
    assert "mask" not in metadata and "counters" not in metadata


@pytest.mark.parametrize("mismatch", ["generation", "cycle", "sequence", "dimensions"])
def test_mismatched_evidence_is_not_published_as_current_judgment(
    mismatch: str,
) -> None:
    source, store = inputs()
    if mismatch == "generation":
        source.value = replace(source.value, generation=2)
    elif mismatch == "cycle":
        store.publish(source_frame(CAMERAS[0], cycle=2), generation=1)
    elif mismatch == "sequence":
        frame = source_frame(CAMERAS[0])
        store.publish(
            Frame(replace(frame.id, sequence=8), 0, frame.image.copy()), generation=1
        )
    else:
        result = source.value.latest_production_result
        assert result is not None
        source.value = replace(
            source.value,
            latest_production_result=replace(
                result,
                cameras=tuple(
                    replace(camera, image_shape=(20, 61, 3))
                    for camera in result.cameras
                ),
            ),
        )
    publisher = SnapshotPublisher(source, store)
    publisher.publish()
    publication = publisher.latest()
    assert publication is not None
    row = json.loads(publication.metadata)["cameras"][0]
    assert row["channels"] == []
    assert row["result_status"] == ("stale" if mismatch == "generation" else "pending")
    assert (row["image_url"] is None) == (mismatch == "generation")


def test_reference_missing_stopped_and_new_generation_observations() -> None:
    source, store = inputs()
    empty = LatestDisplayFrameStore(CAMERAS)
    empty.publish(source_frame(CAMERAS[0]), generation=0)
    publisher = SnapshotPublisher(source, empty)
    publisher.publish()
    value = publisher.latest()
    assert value is not None and not value.images
    rows = json.loads(value.metadata)["cameras"]
    assert rows[0]["image_status"] == "reference"
    assert rows[1]["image_status"] == rows[1]["result_status"] == "unavailable"
    assert all(row["image_url"] is None and not row["channels"] for row in rows)
    source.value = replace(source.value, state=ApplicationState.READY)
    publisher = SnapshotPublisher(source, store)
    publisher.publish()
    value = publisher.latest()
    assert value is not None
    assert all(
        row["image_status"] == "retained" and row["result_status"] == "matched"
        for row in json.loads(value.metadata)["cameras"]
    )
    source.value = replace(source.value, state=ApplicationState.STARTING, generation=2)
    publisher.publish()
    value = publisher.latest()
    assert value is not None and not value.images
    assert all(
        row["image_status"] == "stale" for row in json.loads(value.metadata)["cameras"]
    )
    # Failed startup/stopped generation cannot relabel old pixels or judgments.
    source.value = replace(source.value, state=ApplicationState.READY)
    publisher.publish()
    value = publisher.latest()
    assert value is not None
    assert all(
        row["image_identity"]["generation"] == 1
        and row["result_status"] == "stale"
        and not row["channels"]
        for row in json.loads(value.metadata)["cameras"]
    )


@pytest.mark.parametrize("shape", [(960, 1056), (901, 1377), (120, 80)])
def test_jpeg_preserves_bgr_orientation_aspect_and_limit(
    shape: tuple[int, int],
) -> None:
    source, store = inputs()
    image = np.zeros((*shape, 3), dtype=np.uint8)
    image[: shape[0] // 2] = (10, 30, 230)  # Red upper half, blue lower half.
    image[shape[0] // 2 :] = (220, 25, 15)
    store.publish(Frame(FrameId(CycleId(2), CAMERAS[0], 7), 0, image), generation=1)
    publisher = SnapshotPublisher(source, store)
    publisher.publish()
    publication = publisher.latest()
    assert publication is not None
    encoded = publication.images[0]
    decoded = cv2.imdecode(
        np.frombuffer(encoded.jpeg, dtype=np.uint8), cv2.IMREAD_COLOR
    )
    assert decoded is not None
    h, w = decoded.shape[:2]
    assert max(h, w) <= 640 and (w, h) == (encoded.width, encoded.height)
    assert abs(w / h - shape[1] / shape[0]) < 0.005
    assert np.max(np.abs(decoded[h // 4, w // 2].astype(int) - (10, 30, 230))) < 8
    assert np.max(np.abs(decoded[3 * h // 4, w // 2].astype(int) - (220, 25, 15))) < 8
    assert image.shape == (*shape, 3) and not image.flags.writeable


def test_replacement_and_encoding_failure_do_not_substitute_wrong_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, store = inputs()
    publisher = SnapshotPublisher(source, store)
    publisher.publish()
    previous = publisher.latest()
    assert previous is not None
    publisher.publish()
    latest = publisher.latest()
    assert latest is not None and latest.images[0] is previous.images[0]
    for cycle in range(2, 15):
        store.publish(source_frame(CAMERAS[0], cycle=cycle), generation=1)
        publisher.publish()
        latest = publisher.latest()
        assert latest is not None and len(latest.images) == 6

    def failed_encode(_frame: DisplayFrame) -> EncodedImage:
        raise ValueError("test JPEG failure")

    monkeypatch.setattr("vimo_demo.web.publisher.encode_image", failed_encode)
    store.publish(source_frame(CAMERAS[0], cycle=15), generation=1)
    publisher.publish()
    latest = publisher.latest()
    assert latest is not None and len(latest.images) == 5
    row = json.loads(latest.metadata)["cameras"][0]
    assert row["image_url"] is None and "ValueError" in row["encoding_error"]
    assert row["image_identity"]["cycle_id"] == 15
    assert source.value.counters == ProductionCounters()


def test_local_read_only_http_revision_and_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bound_addresses: list[
        tuple[str | bytes | bytearray, int]
        | tuple[str | bytes | bytearray, int, int, int]
    ] = []
    bind = HTTPServer.server_bind

    def observed_bind(server: HTTPServer) -> None:
        bind(server)
        bound_addresses.append(server.server_address)

    monkeypatch.setattr(HTTPServer, "server_bind", observed_bind)
    source, store = inputs()
    service = ProductionSnapshotService(source, store, port=0, interval=60)
    original_threads = {thread.ident for thread in threads()}
    service.start()
    try:
        assert bound_addresses == [("127.0.0.1", service.port)]
        wait(lambda: service.publisher.latest() is not None)
        status, body, headers = request(service, "/api/snapshot")
        assert status == 200 and "no-store" in headers["Cache-Control"]
        value = json.loads(body)
        url = value["cameras"][0]["image_url"]
        status, jpeg, _ = request(service, url)
        assert status == 200 and jpeg.startswith(b"\xff\xd8")
        assert request(service, "/health")[0] == 200
        assert request(service, "/api/snapshot", "POST")[0] == 405
        assert request(service, "/api/snapshot", "PUT")[0] == 501
        assert request(service, "/")[0] == 200
        assert request(service, "/unknown")[0] == 404
        assert request(service, "/api/cameras/CAM6.jpg")[0] == 400
        assert (
            request(
                service,
                f"/api/cameras/UNKNOWN.jpg?publisher={value['publisher_id']}&revision={value['revision']}",
            )[0]
            == 404
        )
        assert (
            request(service, url.replace(value["publisher_id"], "previous-process"))[0]
            == 410
        )
        # Refresh on a separate owner context: the stale URL cannot return new bytes.
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(service.publisher.publish).result(2)
        assert request(service, url)[0] == 410
        assert source.value.state is ApplicationState.RUNNING
    finally:
        service.request_stop()
        service.request_stop()
        assert service.join(3)
    assert not {thread.ident for thread in threads()} - original_threads
    with socket.socket() as client:
        assert client.connect_ex(("127.0.0.1", service.port)) != 0
    with pytest.raises(RuntimeError):
        service.start()


def test_slow_publication_does_not_queue_or_block_reads_or_display(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, store = inputs()
    service = ProductionSnapshotService(source, store, port=0, interval=0.02)
    entered, release = Event(), Event()
    owners: set[int] = set()

    def slow(frame: DisplayFrame) -> EncodedImage:
        owners.add(get_ident())
        entered.set()
        assert release.wait(3)
        return encode_image(frame)

    monkeypatch.setattr("vimo_demo.web.publisher.encode_image", slow)
    service.start()
    try:
        assert entered.wait(2)
        assert request(service, "/api/snapshot")[0] == 503
        assert request(service, "/health")[0] == 200
        for cycle in range(2, 50):
            store.publish(source_frame(CAMERAS[0], cycle=cycle), generation=1)
        assert len(store.snapshot().frames) == 6
        release.set()

        def newest() -> bool:
            value = service.publisher.latest()
            return (
                value is not None
                and json.loads(value.metadata)["cameras"][0]["image_identity"][
                    "cycle_id"
                ]
                == 49
            )

        wait(newest)

        def read_snapshot(_index: int) -> int:
            return request(service, "/api/snapshot")[0]

        with ThreadPoolExecutor(max_workers=4) as executor:
            statuses = list(executor.map(read_snapshot, range(20)))
        assert statuses == [200] * 20
        assert get_ident() not in owners and len(owners) == 1
        assert source.value.counters == ProductionCounters()
    finally:
        release.set()
        service.request_stop()
        assert service.join(3)


def test_failures_and_blocked_encoder_keep_shutdown_observable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, store = inputs()
    service = ProductionSnapshotService(source, store, port=0, interval=0.02)
    source.fail = True
    service.start()
    try:
        wait(lambda: service.publisher.error() is not None)
        assert request(service, "/api/snapshot")[0] == 503
        assert (
            "ValueError"
            in json.loads(request(service, "/health")[1])["publication_error"]
        )
        source.fail = False
        wait(lambda: service.publisher.latest() is not None)
    finally:
        service.request_stop()
        assert service.join(3)
    entered, release = Event(), Event()

    def blocked(_frame: DisplayFrame) -> EncodedImage:
        entered.set()
        assert release.wait(3)
        raise ValueError("test released encoder")

    monkeypatch.setattr("vimo_demo.web.publisher.encode_image", blocked)
    service = ProductionSnapshotService(source, store, port=0)
    service.start()
    try:
        assert entered.wait(2)
        service.request_stop()
        assert not service.join(0)
        assert service.publisher_alive
    finally:
        release.set()
        assert service.join(3)


def test_port_conflict_and_incomplete_http_request_cleanup() -> None:
    source, store = inputs()
    service = ProductionSnapshotService(source, store, port=0)
    service.start()
    try:
        conflicting = ProductionSnapshotService(source, store, port=service.port)
        try:
            with pytest.raises(OSError):
                conflicting.start()
        finally:
            conflicting.request_stop()
            assert conflicting.join(3)
        with socket.create_connection(("127.0.0.1", service.port), timeout=2) as client:
            client.sendall(b"GET /api/snapshot HTTP/1.1\r\nHost: localhost\r\n")
            service.request_stop()
            assert service.join(3)
    finally:
        service.request_stop()
        assert service.join(3)


def test_web_and_ui_package_imports_do_not_load_qt_or_vendor_sdk() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys\n"
            "from vimo_demo.web import ProductionSnapshotService\n"
            "assert not any(n.startswith(('PySide6', 'smartmore')) for n in sys.modules)\n"
            "import vimo_demo.ui\n"
            "assert not any(n.startswith(('PySide6', 'smartmore')) for n in sys.modules)\n",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        env={**os.environ, "PYTHONPATH": str(Path("src").resolve())},
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "host",
    [
        "",
        "localhost",
        "::1",
        "0.0.0.0",
        "0.1.2.3",
        "255.255.255.255",
        "224.0.0.1",
        "240.0.0.1",
        "127.0.0.2",
        "192.168.10.255",
        "192.168.10.0",
        "192.0.2.99",
    ],
)
def test_unsafe_invalid_or_unassigned_host_is_rejected(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    monkeypatch.setattr(
        "vimo_demo.web.hosts.local_interfaces",
        lambda: (IPv4Interface("192.168.10.100/24"),),
    )
    with pytest.raises(ValueError):
        validate_host(host)


def test_assigned_host_uses_actual_subnet_and_preserves_interface_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # .255 is a valid host on this /23; do not guess broadcast from the suffix.
    monkeypatch.setattr(
        "vimo_demo.web.hosts.local_interfaces",
        lambda: (
            IPv4Interface("192.168.10.255/23"),
            IPv4Interface("192.168.10.100/24"),
        ),
    )
    assert validate_host("192.168.10.255") == "192.168.10.255"
    monkeypatch.setattr(
        "vimo_demo.web.hosts.local_interfaces",
        lambda: (IPv4Interface("192.168.10.255/24"),),
    )
    with pytest.raises(ValueError, match="broadcast address"):
        validate_host("192.168.10.255")

    def failed() -> tuple[IPv4Interface, ...]:
        raise OSError("interface observation failed")

    monkeypatch.setattr("vimo_demo.web.hosts.local_interfaces", failed)
    assert validate_host("127.0.0.1") == "127.0.0.1"
    with pytest.raises(OSError, match="interface observation failed"):
        validate_host("192.168.10.100")


def test_explicit_assigned_ipv4_listener_and_port_release() -> None:
    if sys.platform != "win32":
        pytest.skip("Windows local-interface verification")
    interfaces = local_interfaces()
    hosts = [
        str(interface.ip) for interface in interfaces if not interface.ip.is_loopback
    ]
    if not hosts:
        pytest.skip("No assigned non-loopback IPv4; physical LAN remains manual")
    source, store = inputs()
    service = ProductionSnapshotService(source, store, host=hosts[0], port=0)
    service.start()
    try:
        connection = HTTPConnection(service.host, service.port, timeout=2)
        try:
            connection.request("GET", "/")
            response = connection.getresponse()
            assert response.status == 200
            assert b"ViMo Observer" in response.read()
        finally:
            connection.close()
    finally:
        service.request_stop()
        assert service.join(3)
    with socket.socket() as client:
        assert client.connect_ex((service.host, service.port)) != 0


def test_static_observer_and_three_clients_reuse_cached_jpegs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, store = inputs()
    encoded: list[int] = []

    def counted(frame: DisplayFrame) -> EncodedImage:
        encoded.append(get_ident())
        return encode_image(frame)

    monkeypatch.setattr("vimo_demo.web.publisher.encode_image", counted)
    service = ProductionSnapshotService(source, store, port=0, interval=60)
    original_threads = {thread.ident for thread in threads()}
    service.start()
    try:
        wait(lambda: service.publisher.latest() is not None)
        for route, content_type in (
            ("/", "text/html"),
            ("/observer.js", "text/javascript"),
            ("/observer.css", "text/css"),
        ):
            status, body, headers = request(service, route)
            assert status == 200 and body
            assert headers["Content-Type"].startswith(content_type)
            assert "Access-Control-Allow-Origin" not in headers
            assert request(service, route, "POST")[0] == 405
        assert request(service, "/static/../service.py")[0] == 404

        def observe(_client: int) -> int:
            calls = 0
            for _ in range(20):
                status, body, _ = request(service, "/api/snapshot")
                assert status == 200
                calls += 1
                for row in json.loads(body)["cameras"]:
                    status, image, _ = request(service, row["image_url"])
                    assert status == 200 and image.startswith(b"\xff\xd8")
                    calls += 1
            return calls

        with ThreadPoolExecutor(max_workers=3) as clients:
            assert list(clients.map(observe, range(3))) == [140, 140, 140]
        assert len(encoded) == 6 and len(set(encoded)) == 1
        latest = service.publisher.latest()
        assert latest is not None and len(latest.images) == 6
    finally:
        service.request_stop()
        assert service.join(3)
    assert not {thread.ident for thread in threads()} - original_threads


def test_browser_polling_identity_and_recovery() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip(
            "JavaScript checks need an existing Node.js; no dependencies installed"
        )
    result = subprocess.run(
        [node, "--test", "tests/web_observer_checks.cjs"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
