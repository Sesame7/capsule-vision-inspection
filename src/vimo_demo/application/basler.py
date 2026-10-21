"""One serial, externally controlled camera-group attempt, separate from Production."""

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import CancelledError
from dataclasses import dataclass
from math import isfinite
from threading import Event
from time import perf_counter
from typing import Protocol

import numpy as np
from numpy.typing import NDArray

type FeatureValue = str | int | float | bool


@dataclass(frozen=True, slots=True)
class BaslerDevice:
    """Enumeration metadata only; no camera handle or physical-position claim."""

    serial: str
    model: str


def discover_basler_devices() -> tuple[BaslerDevice, ...]:
    """Blocking, read-only discovery; callers own background execution.

    Optional SDK loading occurs only on this explicit request, never at import.
    """
    from ._basler_sdk import enumerate_devices

    return enumerate_devices()


@dataclass(frozen=True, slots=True)
class CameraSettings:
    serial: str
    features: Mapping[str, FeatureValue]
    triggers: Mapping[str, Mapping[str, FeatureValue]]
    model: str = ""  # Read-only device identity, never a feature to restore.


@dataclass(frozen=True, slots=True)
class CameraReadiness:
    serial: str
    supported: bool
    ready: bool | None
    observed_at: float


@dataclass(frozen=True, slots=True)
class AcquisitionIssue:
    stage: str
    serial: str | None
    message: str
    error_type: str | None = None


@dataclass(frozen=True, slots=True)
class CameraCleanup:
    serial: str
    stopped: bool
    settings_restored: bool
    closed: bool
    detached: bool
    issues: tuple[AcquisitionIssue, ...] = ()

    @property
    def safe(self) -> bool:
        return self.stopped and self.settings_restored and self.closed and self.detached


@dataclass(frozen=True, slots=True)
class AcquisitionMetadata:
    serial: str
    received_at: float  # Host perf_counter observation, never an exposure clock.
    succeeded: bool
    error_code: int
    error_description: str
    width: int
    height: int
    pixel_type: int
    payload_bytes: int
    block_id: int | None
    image_number: int | None
    camera_timestamp: int | None  # Raw SDK value; units/synchronization not inferred.
    skipped_images: int | None


@dataclass(frozen=True, slots=True, eq=False)
class AcquiredImage:
    serial: str
    pixels: NDArray[np.uint8]
    metadata: AcquisitionMetadata


@dataclass(frozen=True, slots=True)
class ArmedAcquisition:
    serials: tuple[str, ...]
    armed_at: float
    deadline: float
    readiness: tuple[CameraReadiness, ...]


@dataclass(frozen=True, slots=True)
class AcquisitionReport:
    serials: tuple[str, ...]
    started_at: float
    armed: ArmedAcquisition | None
    finished_at: float
    settings: tuple[CameraSettings, ...]
    observations: tuple[AcquisitionMetadata, ...]
    issues: tuple[AcquisitionIssue, ...]
    cleanup: tuple[CameraCleanup, ...]

    @property
    def missing_serials(self) -> tuple[str, ...]:
        seen = {row.serial for row in self.observations}
        return tuple(serial for serial in self.serials if serial not in seen)

    @property
    def extra_serials(self) -> tuple[str, ...]:
        counts = Counter(row.serial for row in self.observations)
        return tuple(serial for serial in self.serials if counts[serial] > 1)

    @property
    def cleanup_safe(self) -> bool:
        return not any(i.stage == "cleanup" for i in self.issues) and all(
            row.safe for row in self.cleanup
        )


@dataclass(frozen=True, slots=True, eq=False)
class AcquisitionGroup:
    """Complete ordered images; NOT proof of one electrical pulse/shared CycleId.

    Pixels are independently owned and read-only. Keep every alias immutable
    while retained by a consumer. No SDK buffers/objects escape this boundary.
    """

    images: tuple[AcquiredImage, ...]
    report: AcquisitionReport


class BaslerAcquisitionError(RuntimeError):
    def __init__(self, report: AcquisitionReport) -> None:
        self.report = report
        super().__init__(
            "; ".join(f"{issue.stage}: {issue.message}" for issue in report.issues)
        )


class BaslerAcquisitionCancelled(BaslerAcquisitionError):
    """Owner Stop observed; no successful group, even if all frames had arrived."""


class _Backend(Protocol):
    """Private test seam, not an application camera/source abstraction."""

    def prepare(self, serial: str) -> CameraSettings: ...
    def inventory(self, serials: tuple[str, ...]) -> None: ...
    def arm(self) -> tuple[CameraReadiness, ...]: ...
    def available(self, serial: str, timeout_ms: int) -> bool: ...
    def retrieve(
        self, serial: str
    ) -> tuple[AcquisitionMetadata, NDArray[np.uint8] | None]: ...
    def cleanup(self) -> tuple[CameraCleanup, ...]: ...


def _load_backend() -> _Backend:
    # Optional SDK import happens only when an actual attempt is requested.
    from ._basler_sdk import PylonBackend

    return PylonBackend()


class BaslerAcquisitionSource:
    """Synchronous; one non-GUI serial owner, no threads/retries/trigger writes.

    acquire() reopens and closes the six devices for one bounded observation
    window. The caller maintains the single-trigger condition externally.
    on_armed must return promptly; its time consumes the window. It is notification,
    not an independent observation of the operator action/electrical trigger.
    A blocked native SDK call cannot be forcibly cancelled by this interface.
    """

    def __init__(self, serials: Sequence[str], *, timeout: float = 5.0) -> None:
        values = tuple(serials)
        if (
            len(values) not in (5, 6)
            or any(not s or not s.isascii() or not s.isdecimal() for s in values)
            or len(set(values)) != len(values)
        ):
            raise ValueError("Supply five or six unique, explicit numeric serials")
        if isinstance(timeout, bool) or not isfinite(timeout) or not 0 < timeout <= 120:
            raise ValueError("timeout must be finite and in (0, 120] seconds")
        self.serials = values
        self.timeout = timeout
        self._backend: _Backend | None = None
        self._active = False

    def close(self) -> tuple[CameraCleanup, ...]:
        """Retry uncertain cleanup only; never force termination of a native call."""
        if self._active:
            raise RuntimeError("An acquisition is active; close on its serial owner")
        if self._backend is None:
            return ()
        rows = self._backend.cleanup()
        if all(row.safe for row in rows):
            self._backend = None
        return rows

    @property
    def cleanup_safe(self) -> bool:
        """Inspect after the owning call/thread ends; not a native cancellation API."""
        return not self._active and self._backend is None

    def acquire(
        self,
        *,
        on_armed: Callable[[ArmedAcquisition], None] | None = None,
        cancel: Event | None = None,
    ) -> AcquisitionGroup:
        if self._active or self._backend is not None:
            raise RuntimeError("Acquisition active or prior SDK cleanup unresolved")
        self._active = True
        started = perf_counter()
        armed: ArmedAcquisition | None = None
        settings: list[CameraSettings] = []
        observations: list[AcquisitionMetadata] = []
        images: dict[str, AcquiredImage] = {}
        issues: list[AcquisitionIssue] = []
        cleanup: tuple[CameraCleanup, ...] = ()
        cause: BaseException | None = None
        stage, serial = "initialization", None
        try:
            _check_cancel(cancel)
            backend = self._backend = _load_backend()
            stage = "inventory"
            backend.inventory(self.serials)
            for serial in self.serials:
                _check_cancel(cancel)
                stage = "prepare"
                settings.append(backend.prepare(serial))
            serial = None
            stage = "arming"
            _check_cancel(cancel)
            readiness = backend.arm()
            _check_cancel(cancel)
            if tuple(row.serial for row in readiness) != self.serials or any(
                row.supported and row.ready is not True for row in readiness
            ):
                raise RuntimeError("Incomplete or unsuccessful readiness observation")
            for serial in self.serials:
                _check_cancel(cancel)
                if backend.available(serial, 0):
                    raise RuntimeError(
                        f"Pre-ARMED frame on {serial}; association ambiguous"
                    )
            serial = None
            now = perf_counter()
            armed = ArmedAcquisition(self.serials, now, now + self.timeout, readiness)
            stage = "armed_notification"
            if on_armed is not None:
                on_armed(armed)
            _check_cancel(cancel)
            stage = "acquisition"
            # Observe the entire window, including after the first complete set.
            while perf_counter() < armed.deadline:
                for serial in self.serials:
                    _check_cancel(cancel)
                    remaining = armed.deadline - perf_counter()
                    if remaining <= 0:
                        break
                    if backend.available(
                        serial, min(25, max(1, int(remaining * 1000)))
                    ):
                        row, pixels = backend.retrieve(serial)
                        observations.append(row)
                        if pixels is None:
                            raise RuntimeError(f"Failed/incomplete frame: {row}")
                        image = AcquiredImage(serial, pixels, row)
                        self._accept(image, serial, images)
            # One bounded nonblocking sweep catches queued boundary observations.
            for serial in self.serials:
                _check_cancel(cancel)
                if backend.available(serial, 0):
                    row, _ = backend.retrieve(serial)
                    observations.append(row)
                    raise RuntimeError(
                        f"Pending frame at deadline on {serial}; ambiguous"
                    )
            serial = None
            if len(images) != len(self.serials):
                raise TimeoutError("Window expired without all configured frames")
        except BaseException as exc:
            cause = exc
            issues.append(acquisition_issue(stage, serial, exc))
        finally:
            try:
                if self._backend is not None:
                    cleanup = self._backend.cleanup()
                    for row in cleanup:
                        issues.extend(row.issues)
                    if all(row.safe for row in cleanup):
                        self._backend = None
                    else:
                        issues.append(
                            AcquisitionIssue(
                                "cleanup", None, "Unsafe SDK ownership retained"
                            )
                        )
            except BaseException as exc:
                issues.append(acquisition_issue("cleanup", None, exc))
                cause = cause or exc
            finally:
                self._active = False
        if cause is None and cancel is not None and cancel.is_set():
            cause = CancelledError("Basler acquisition cancelled during cleanup")
            issues.append(acquisition_issue("cancelled", None, cause))
        report = AcquisitionReport(
            self.serials,
            started,
            armed,
            perf_counter(),
            tuple(settings),
            tuple(observations),
            tuple(issues),
            cleanup,
        )
        if cause is not None and not isinstance(cause, Exception):
            cause.add_note(
                f"Basler cleanup_safe={report.cleanup_safe}; issues={report.issues}"
            )
            raise cause
        if issues:
            if isinstance(cause, CancelledError):
                raise BaslerAcquisitionCancelled(report) from cause
            raise BaslerAcquisitionError(report) from cause
        return AcquisitionGroup(tuple(images[s] for s in self.serials), report)

    @staticmethod
    def _accept(
        image: AcquiredImage, serial: str, images: dict[str, AcquiredImage]
    ) -> None:
        row, pixels = image.metadata, image.pixels
        if image.serial != serial or row.serial != serial:
            raise RuntimeError("Retrieved device identity mismatch")
        if serial in images:
            raise RuntimeError(
                f"Unexpected extra frame on {serial}; association ambiguous"
            )
        if not row.succeeded or row.skipped_images not in (0, None):
            raise RuntimeError(f"Failed/incomplete/skipped frame: {row}")
        if row.block_id == (1 << 64) - 1:
            raise RuntimeError("Invalid SDK BlockID")
        if (
            pixels.dtype != np.uint8
            or pixels.shape != (row.height, row.width, 3)
            or min(row.height, row.width) <= 0
            or not pixels.flags.owndata
            or not pixels.flags.c_contiguous
            or pixels.flags.writeable
        ):
            raise RuntimeError("Invalid independently owned readonly BGR image")
        images[serial] = image


def acquisition_issue(
    stage: str, serial: str | None, exc: BaseException
) -> AcquisitionIssue:
    return AcquisitionIssue(
        stage, serial, str(exc), f"{type(exc).__module__}.{type(exc).__qualname__}"
    )


def _check_cancel(cancel: Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise CancelledError("Basler acquisition cancelled by owner")
