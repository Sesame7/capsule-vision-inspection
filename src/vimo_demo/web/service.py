"""Two independently owned, non-daemon threads and a single-request HTTP server."""

import json
import math
import socket
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib.resources import files
from threading import Event, Lock, Thread
from time import monotonic
from urllib.parse import parse_qs, unquote, urlsplit

from vimo_demo.inspection import CameraId
from vimo_demo.ui.display_frames import LatestDisplayFrameStore

from .hosts import validate_host
from .publisher import SnapshotPublisher, SnapshotSource

_ASSETS = {
    "/": ("observer.html", "text/html; charset=utf-8"),
    "/observer.css": ("observer.css", "text/css; charset=utf-8"),
    "/observer.js": ("observer.js", "text/javascript; charset=utf-8"),
}

# BaseServer's typed hooks include UDP requests; this listener is always TCP.
type _Request = socket.socket | tuple[bytes, socket.socket]


class _Server(HTTPServer):
    request_queue_size = 4
    # HTTPServer's SO_REUSEADDR permits competing listeners on Windows.
    allow_reuse_address = False

    def __init__(self, owner: ProductionSnapshotService, port: int) -> None:
        self.owner = owner
        self._active_lock = Lock()
        self._active: socket.socket | None = None
        super().__init__((owner.host, port), _Handler)
        self.timeout = 0.2

    def get_request(self) -> tuple[socket.socket, tuple[str, int]]:
        connection, address = super().get_request()
        connection.settimeout(1.0)
        with self._active_lock:
            self._active = connection
            if self.owner.stopping.is_set():
                self._interrupt(connection)
        return connection, address

    @staticmethod
    def _interrupt(connection: socket.socket) -> None:
        try:
            connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def stop_requests(self) -> None:
        with self._active_lock:
            if self._active is not None:
                self._interrupt(self._active)

    def close_request(self, request: _Request) -> None:
        with self._active_lock:
            if self._active is request:
                self._active = None
        super().close_request(request)

    def handle_error(self, request: _Request, client_address: tuple[str, int]) -> None:
        if not self.owner.stopping.is_set():
            super().handle_error(request, client_address)

    def verify_request(
        self, request: _Request, client_address: tuple[str, int]
    ) -> bool:
        return not self.owner.stopping.is_set()


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        server = self.server
        assert isinstance(server, _Server)
        owner = server.owner
        path = urlsplit(self.path)
        latest = owner.publisher.latest()
        if path.path in _ASSETS and not path.query:
            body, content_type = owner.assets[path.path]
            self._send(200, body, content_type)
        elif path.path == "/health" and not path.query:
            self._send(
                200,
                json.dumps(
                    {
                        "stopping": owner.stopping.is_set(),
                        "publisher_alive": owner.publisher_alive,
                        "snapshot_available": latest is not None,
                        "publication_error": owner.publisher.error(),
                    }
                ).encode(),
                "application/json",
            )
        elif path.path == "/api/snapshot" and not path.query:
            if latest is None:
                self._send(503, b"No observation published", "text/plain")
            else:
                self._send(200, latest.metadata, "application/json; charset=utf-8")
        elif path.path.startswith("/api/cameras/") and path.path.endswith(".jpg"):
            query = parse_qs(path.query, keep_blank_values=True)
            try:
                if set(query) != {"publisher", "revision"} or any(
                    len(values) != 1 for values in query.values()
                ):
                    raise ValueError("Missing exact publication revision")
                revision = int(query["revision"][0])
            except ValueError:
                self._send(400, b"Expected publication revision", "text/plain")
                return
            if (
                latest is None
                or latest.revision != revision
                or latest.publisher_id != query["publisher"][0]
            ):
                self._send(410, b"Observation unavailable or superseded", "text/plain")
                return
            camera_id = unquote(
                path.path.removeprefix("/api/cameras/").removesuffix(".jpg")
            )
            image = next(
                (image for image in latest.images if str(image.camera_id) == camera_id),
                None,
            )
            if image is None:
                self._send(404, b"Camera image unavailable", "text/plain")
            else:
                self._send(200, image.jpeg, "image/jpeg")
        else:
            self._send(404, b"Read-only snapshot API", "text/plain")

    def do_POST(self) -> None:
        self._send(405, b"Read-only snapshot API", "text/plain")


class ProductionSnapshotService:
    """Best-effort single-use observation service, independent of Production.

    request_stop() never waits. join() is for a non-GUI owner and confirms actual
    exit, including an in-progress encoder/request; timeout retains ownership.
    One serial HTTP handler and a four-connection listen backlog bound slow reads.
    """

    def __init__(
        self,
        source: SnapshotSource,
        frames: LatestDisplayFrameStore,
        *,
        cameras: tuple[CameraId, ...] | None = None,
        host: str = "127.0.0.1",
        port: int = 8765,
        interval: float = 0.5,
    ) -> None:
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("Invalid Web port")
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("Publication interval must be finite and positive")
        self.publisher = SnapshotPublisher(
            source, frames, cameras, refresh_interval_ms=interval * 1000
        )
        self.host = validate_host(host)
        self.assets = {
            route: (
                files(__package__).joinpath("static", name).read_bytes(),
                content_type,
            )
            for route, (name, content_type) in _ASSETS.items()
        }
        self.port, self.interval = port, interval
        self.stopping = Event()
        self._server: _Server | None = None
        self._threads: tuple[Thread, ...] = ()
        self._started = False

    def start(self) -> None:
        if self._started or self.stopping.is_set():
            raise RuntimeError("Snapshot service has one lifecycle")
        self._started = True
        server = self._server = _Server(self, self.port)
        self.port = server.server_address[1]
        self._threads = (
            Thread(
                target=self._publish_loop,
                name="ProductionSnapshotPublisher",
                daemon=False,
            ),
            Thread(target=self._http_loop, name="ProductionSnapshotHTTP", daemon=False),
        )
        try:
            for thread in self._threads:
                thread.start()
        except BaseException:
            self.request_stop()
            if not self._threads[-1].is_alive():
                server.server_close()
            raise

    def _publish_loop(self) -> None:
        while not self.stopping.is_set():
            try:
                self.publisher.publish()
            except Exception as exc:
                self.publisher.failed(exc)
                traceback.print_exception(exc)
            # No queued/catch-up observations, including when encoding is slow.
            self.stopping.wait(self.interval)

    def _http_loop(self) -> None:
        assert self._server is not None
        try:
            while not self.stopping.is_set():
                self._server.handle_request()
        except Exception as exc:
            self.publisher.failed(exc)
            traceback.print_exception(exc)
        finally:
            self._server.server_close()

    def request_stop(self) -> None:
        self.stopping.set()
        if self._server is not None:
            self._server.stop_requests()

    @property
    def publisher_alive(self) -> bool:
        return bool(self._threads and self._threads[0].is_alive())

    @property
    def http_alive(self) -> bool:
        """Local owner observation for the shell, not proof of remote reachability."""
        return len(self._threads) == 2 and self._threads[1].is_alive()

    def join(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else monotonic() + max(0, timeout)
        for thread in self._threads:
            if thread.ident is not None:
                thread.join(
                    None if deadline is None else max(0, deadline - monotonic())
                )
        return all(not thread.is_alive() for thread in self._threads)
