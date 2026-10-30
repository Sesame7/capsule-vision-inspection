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
from http.server import BaseHTTPRequestHandler, HTTPServer
from ipaddress import IPv4Interface
from pathlib import Path
from threading import Event, get_ident
from threading import enumerate as threads
from time import monotonic

import cv2
import numpy as np
import pytest
from tests.support.business import mixed_result
from tests.support.production import source_frame

from capsule_vision.application import (
    ApplicationState,
    ProductionApplicationSnapshot,
    ProductionCounters,
)
from capsule_vision.display import DisplayFrame, LatestDisplayFrameStore
from capsule_vision.inspection import CameraId, CycleId, Frame, FrameId
from capsule_vision.web import ProductionSnapshotService
from capsule_vision.web.hosts import local_interfaces, validate_host
from capsule_vision.web.publisher import EncodedImage, SnapshotPublisher, encode_image

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


def test_configured_camera_change_clears_pixels_and_updates_existing_publisher() -> (
    None
):
    source, store = inputs()
    publisher = SnapshotPublisher(source, store)
    publisher.publish()
    old = publisher.latest()
    assert old is not None and len(old.images) == 6
    five = tuple(c for c in CAMERAS if c != "CAM6")
    store.reconfigure(five)
    assert not store.snapshot().frames
    result = source.value.latest_production_result
    assert result is not None
    cameras = tuple(c for c in result.cameras if c.camera_id != "CAM6")
    source.value = replace(
        source.value,
        generation=2,
        latest_production_result=replace(
            result,
            layout=replace(result.layout, cameras=tuple(c.layout for c in cameras)),
            cameras=cameras,
        ),
    )
    for camera in five:
        store.publish(source_frame(camera), generation=2)
    publisher.publish()
    current = publisher.latest()
    assert current is not None and current.revision > old.revision
    assert current.publisher_id == old.publisher_id
    metadata = json.loads(current.metadata)
    assert metadata["configured_camera_ids"] == list(five)
    assert len(current.images) == len(metadata["cameras"]) == 5
    assert sum(len(row["channels"]) for row in metadata["cameras"]) == 30
    assert all(row["result_status"] == "matched" for row in metadata["cameras"])
    assert not {d.key for d in old.deliveries} & {d.key for d in current.deliveries}
    with pytest.raises(ValueError, match="Unknown display camera"):
        store.publish(source_frame(CameraId("CAM6")), generation=2)


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

    monkeypatch.setattr("capsule_vision.web.publisher.encode_image", failed_encode)
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
    assert not service.http_alive and not service.publisher_alive
    service.start()
    try:
        assert service.http_alive and service.publisher_alive
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
                url.replace("CAM6.jpg", "UNKNOWN.jpg"),
            )[0]
            == 404
        )
        assert (
            request(service, url.replace(value["publisher_id"], "previous-process"))[0]
            == 410
        )
        # Metadata-only refresh keeps the immutable URL and exactly the same bytes.
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(service.publisher.publish).result(2)
        assert request(service, url)[:2] == (200, jpeg)
        assert source.value.state is ApplicationState.RUNNING
    finally:
        service.request_stop()
        service.request_stop()
        assert service.join(3)
    assert not {thread.ident for thread in threads()} - original_threads
    assert not service.http_alive and not service.publisher_alive
    assert service.publisher.latest() is None
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

    monkeypatch.setattr("capsule_vision.web.publisher.encode_image", slow)
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

    monkeypatch.setattr("capsule_vision.web.publisher.encode_image", blocked)
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
            "from capsule_vision.web import ProductionSnapshotService\n"
            "assert not any(n.startswith(('PySide6', 'smartmore')) for n in sys.modules)\n"
            "import capsule_vision.ui\n"
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
        "capsule_vision.web.hosts.local_interfaces",
        lambda: (IPv4Interface("192.168.10.100/24"),),
    )
    with pytest.raises(ValueError):
        validate_host(host)


def test_assigned_host_uses_actual_subnet_and_preserves_interface_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # .255 is a valid host on this /23; do not guess broadcast from the suffix.
    monkeypatch.setattr(
        "capsule_vision.web.hosts.local_interfaces",
        lambda: (
            IPv4Interface("192.168.10.255/23"),
            IPv4Interface("192.168.10.100/24"),
        ),
    )
    assert validate_host("192.168.10.255") == "192.168.10.255"
    monkeypatch.setattr(
        "capsule_vision.web.hosts.local_interfaces",
        lambda: (IPv4Interface("192.168.10.255/24"),),
    )
    with pytest.raises(ValueError, match="broadcast address"):
        validate_host("192.168.10.255")

    def failed() -> tuple[IPv4Interface, ...]:
        raise OSError("interface observation failed")

    monkeypatch.setattr("capsule_vision.web.hosts.local_interfaces", failed)
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
            assert b"Capsule Vision Inspection Observer" in response.read()
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

    monkeypatch.setattr("capsule_vision.web.publisher.encode_image", counted)
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
        [node, "--test", "tests/web/observer.test.cjs"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_stable_urls_and_two_encoded_versions_without_extra_encoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, store = inputs()
    calls: list[int] = []

    def counted(frame: DisplayFrame) -> EncodedImage:
        calls.append(frame.revision)
        return encode_image(frame)

    monkeypatch.setattr("capsule_vision.web.publisher.encode_image", counted)
    publisher = SnapshotPublisher(source, store)
    publisher.publish()
    first = publisher.latest()
    assert first is not None
    first_urls = [row["image_url"] for row in json.loads(first.metadata)["cameras"]]
    for _ in range(12):
        publisher.publish()
        current = publisher.latest()
        assert current is not None and current.revision > first.revision
        assert current.deliveries == first.deliveries
        assert all(a is b for a, b in zip(first.images, current.images, strict=True))
        assert [
            row["image_url"] for row in json.loads(current.metadata)["cameras"]
        ] == first_urls
    assert len(calls) == 6
    for cycle in range(2, 8):
        previous = publisher.latest()
        assert previous is not None
        for camera in CAMERAS:
            frame = source_frame(camera, cycle=cycle)
            store.publish(frame, generation=1)
        publisher.publish()
        current = publisher.latest()
        assert (
            current is not None
            and len(current.images) == 6
            and len(current.deliveries) == 12
        )
        for camera in CAMERAS:
            retained = [d for d in current.deliveries if d.image.camera_id == camera]
            assert len(retained) == 2
            assert retained[0].image is next(
                i for i in current.images if i.camera_id == camera
            )
            assert retained[1].image is next(
                i for i in previous.images if i.camera_id == camera
            )
            assert retained[0].key != retained[1].key
            assert retained[0].epoch == retained[1].epoch
    assert len(calls) == 42
    assert source.value.counters == ProductionCounters()


def test_http_current_previous_and_evicted_image_bytes() -> None:
    source, store = inputs()
    service = ProductionSnapshotService(source, store, port=0, interval=0.01)
    service.start()
    try:
        wait(lambda: service.publisher.latest() is not None)
        first = json.loads(request(service, "/api/snapshot")[1])["cameras"][0]
        first_bytes = request(service, first["image_url"])[1]
        urls = [first["image_url"]]
        pixels = [first_bytes]
        for cycle in (2, 3):
            frame = Frame(
                FrameId(CycleId(cycle), CAMERAS[0], 7),
                0,
                np.full((20, 60, 3), cycle * 60, dtype=np.uint8),
            )
            store.publish(frame, generation=1)

            def ready(expected: int = cycle) -> bool:
                value = service.publisher.latest()
                return (
                    value is not None
                    and json.loads(value.metadata)["cameras"][0]["image_identity"][
                        "cycle_id"
                    ]
                    == expected
                )

            wait(ready)
            row = json.loads(request(service, "/api/snapshot")[1])["cameras"][0]
            status, jpeg, _ = request(service, row["image_url"])
            assert status == 200 and jpeg != pixels[-1]
            assert request(service, urls[-1])[:2] == (200, pixels[-1])
            urls.append(row["image_url"])
            pixels.append(jpeg)
        assert request(service, urls[0])[0] == 410
        assert request(service, urls[1])[:2] == (200, pixels[1])
        assert request(service, urls[2])[:2] == (200, pixels[2])
        for invalid in (
            urls[-1] + "&image=1-1-1-7-1-20-60",
            urls[-1].replace("&image=", "&revision="),
            urls[-1].rsplit("=", 1)[0] + "=bad",
        ):
            assert request(service, invalid)[0] == 400
    finally:
        service.request_stop()
        assert service.join(3)
    assert service.publisher.latest() is None


@pytest.mark.parametrize(
    "boundary",
    [
        "generation",
        "shape",
        "source",
        "unavailable",
        "encoding",
        "observation_failure",
        "removal",
    ],
)
def test_hard_delivery_invalidation(
    boundary: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, store = inputs()
    service = ProductionSnapshotService(source, store, port=0, interval=0.01)
    service.start()
    try:
        wait(lambda: service.publisher.latest() is not None)
        row = json.loads(request(service, "/api/snapshot")[1])["cameras"][0]
        url = row["image_url"]
        if boundary == "observation_failure":
            source.fail = True
            wait(lambda: service.publisher.error() is not None)
        elif boundary == "removal":
            store.reconfigure(CAMERAS[1:])
            wait(
                lambda: (
                    (value := service.publisher.latest()) is not None
                    and value.camera_ids == CAMERAS[1:]
                )
            )
        else:
            if boundary == "generation":
                source.value = replace(source.value, generation=2)
            elif boundary == "unavailable":
                store.reconfigure(CAMERAS[1:])
                store.reconfigure(CAMERAS)
            else:
                if boundary == "encoding":

                    def fail(_frame: DisplayFrame) -> EncodedImage:
                        raise ValueError("injected encoding invalidation")

                    monkeypatch.setattr(
                        "capsule_vision.web.publisher.encode_image", fail
                    )
                shape = (25, 61, 3) if boundary == "shape" else (20, 60, 3)
                store.publish_pixels(
                    FrameId(CycleId(2), CAMERAS[0], 7),
                    0,
                    np.full(shape, 100, dtype=np.uint8),
                    generation=1,
                    source_key="replacement" if boundary == "source" else None,
                )
            wait(
                lambda: (
                    (value := service.publisher.latest()) is not None
                    and not any(
                        d.key == url.rsplit("=", 1)[1] for d in value.deliveries
                    )
                )
            )
        assert request(service, url)[0] == (404 if boundary == "removal" else 410)
        source.fail = False
        source.value = replace(source.value, state=ApplicationState.READY)
        monkeypatch.setattr("capsule_vision.web.publisher.encode_image", encode_image)
        # A recovered source may expose the same frame, but never revive the retired URL.
        if boundary not in ("removal", "unavailable"):
            wait(
                lambda: (
                    (value := service.publisher.latest()) is not None
                    and any(image.camera_id == CAMERAS[0] for image in value.images)
                )
            )
            assert request(service, url)[0] == 410
    finally:
        source.fail = False
        service.request_stop()
        assert service.join(3)


def test_inflight_old_body_stays_immutable_across_generation_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, store = inputs()
    service = ProductionSnapshotService(source, store, port=0, interval=0.01)
    service.start()
    entered, release = Event(), Event()
    try:
        wait(lambda: service.publisher.latest() is not None)
        url = json.loads(request(service, "/api/snapshot")[1])["cameras"][0][
            "image_url"
        ]
        original_bytes = request(service, url)[1]
        original_headers = BaseHTTPRequestHandler.end_headers

        def delayed_headers(handler: BaseHTTPRequestHandler) -> None:
            original_headers(handler)
            if handler.path == url:
                entered.set()
                assert release.wait(3)

        monkeypatch.setattr(BaseHTTPRequestHandler, "end_headers", delayed_headers)
        with ThreadPoolExecutor(max_workers=1) as client:
            response = client.submit(request, service, url)
            try:
                assert entered.wait(2)
                source.value = replace(source.value, generation=2)
                store.publish(
                    Frame(
                        FrameId(CycleId(2), CAMERAS[0], 7),
                        0,
                        np.full((20, 60, 3), 201, dtype=np.uint8),
                    ),
                    generation=2,
                )
                wait(
                    lambda: (
                        (value := service.publisher.latest()) is not None
                        and not any(
                            d.key == url.rsplit("=", 1)[1] for d in value.deliveries
                        )
                    )
                )
                value = service.publisher.latest()
                assert value is not None and value.images[0].jpeg != original_bytes
            finally:
                release.set()
            assert response.result(2)[:2] == (200, original_bytes)
        assert request(service, url)[0] == 410
    finally:
        release.set()
        service.request_stop()
        assert service.join(3)
    assert service.publisher.latest() is None


def test_incomplete_join_keeps_cache_until_actual_encoder_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, store = inputs()
    service = ProductionSnapshotService(source, store, port=0, interval=0.01)
    entered, release = Event(), Event()
    service.start()
    try:
        wait(lambda: service.publisher.latest() is not None)

        def blocked(frame: DisplayFrame) -> EncodedImage:
            entered.set()
            assert release.wait(3)
            return encode_image(frame)

        monkeypatch.setattr("capsule_vision.web.publisher.encode_image", blocked)
        store.publish(source_frame(CAMERAS[0], cycle=2), generation=1)
        assert entered.wait(2)
        service.request_stop()
        assert not service.join(0)
        value = service.publisher.latest()
        assert value is not None and len(value.deliveries) == 6
        assert service.publisher_alive
    finally:
        release.set()
        service.request_stop()
        assert service.join(3)
    assert service.publisher.latest() is None
