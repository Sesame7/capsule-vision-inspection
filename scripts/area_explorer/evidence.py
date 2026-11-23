"""Recorded channel, camera frame and SDK region evidence for offline replay."""

from collections.abc import Mapping
from dataclasses import dataclass

from capsule_vision.inference.models import BoundingRect, Contour


def valid_sdk_area(label: str, area: int, native_minima: Mapping[str, int]) -> bool:
    return (
        type(area) is int
        and area > 0
        and label in native_minima
        and area >= native_minima[label]
    )


@dataclass(frozen=True)
class Channel:
    number: int
    camera: str
    roi: BoundingRect
    slot: int = -1


@dataclass(frozen=True)
class Region:
    number: int
    label: str
    area: int
    box: BoundingRect
    long_side: float
    short_side: float
    contours: tuple[Contour, ...]
    channels: tuple[int, ...]
    effective: tuple[int, ...]
    empty: tuple[int, ...]
    label_enabled: bool
    comparison: str


@dataclass(frozen=True)
class Frame:
    path: str
    sha256: str
    raw_result: str
    width: int
    height: int
    regions: tuple[Region, ...] | None
    occupancy: dict[int, str]
    business: dict[int, str]

    def gate(self, channel: int) -> str:
        measured = self.occupancy.get(channel)
        recorded = self.business.get(channel)
        if measured == "EMPTY" and recorded == "EMPTY":
            return "EMPTY"
        if measured == "PRESENT" and recorded in ("OK", "NG"):
            return "PRESENT"
        return "Unknown"


@dataclass(frozen=True)
class Group:
    run: str
    first_cycle: str
    members: tuple[str, ...]
    recorded: dict[int, str]
    frames: dict[tuple[int, str], Frame]
    slot_states: dict[tuple[int, str], str]
