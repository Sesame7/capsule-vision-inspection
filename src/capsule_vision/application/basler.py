"""Basler discovery, serial validation and immutable SDK observations."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

type FeatureValue = str | int | float | bool


def validate_serials(serials: Sequence[str]) -> tuple[str, ...]:
    values = tuple(serials)
    if (
        len(values) not in (5, 6)
        or any(not s or not s.isascii() or not s.isdecimal() for s in values)
        or len(set(values)) != len(values)
    ):
        raise ValueError("Supply five or six unique, explicit numeric serials")
    return values


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


def acquisition_issue(
    stage: str, serial: str | None, exc: BaseException
) -> AcquisitionIssue:
    return AcquisitionIssue(
        stage, serial, str(exc), f"{type(exc).__module__}.{type(exc).__qualname__}"
    )
