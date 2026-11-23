"""Verify original camera inputs and persist measured regions for History backfill."""

import hashlib
import json
import math
import msvcrt
import os
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from importlib.metadata import version
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TypedDict, cast

import cv2
import numpy as np
from numpy.typing import NDArray

from capsule_vision.inference import BoundingRect, VimoSegmentationEngine
from capsule_vision.inference.models import Contour
from capsule_vision.inspection.production.layout import OverlapPolicy
from capsule_vision.inspection.production.occupancy import measure_occupancy

from .evidence import Channel, Frame, Region, valid_sdk_area
from .study import Study


def file_sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp: Path | None = None
    try:
        with NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            tmp = Path(stream.name)
            json.dump(
                value,
                stream,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
        os.replace(tmp, path)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


@dataclass(frozen=True)
class Profile:
    channels: tuple[Channel, ...]
    model_sha: str
    enabled: bool
    disabled: frozenset[str]


@dataclass(frozen=True)
class ReplaySetup:
    study: Study
    database_sha: str
    cache: Path
    profiles: dict[str, Profile]


@dataclass(frozen=True)
class CameraImage:
    run: str
    group: str
    slot: int
    cycle: str
    camera: str
    path: str
    sha: str
    width: int
    height: int
    business: dict[int, str]


def image_identity(a: ReplaySetup, s: CameraImage) -> dict[str, object]:
    return dict(
        format=1,
        database_sha256=a.database_sha,
        model_sha256=a.study.model_sha,
        module=a.study.module,
        sdk=a.study.sdk,
        native_minima=a.study.native_minima,
        run=s.run,
        group=s.group,
        slot=s.slot,
        cycle=s.cycle,
        camera=s.camera,
        path=s.path,
        png_sha256=s.sha,
        width=s.width,
        height=s.height,
    )


class _StoredRegion(TypedDict):
    number: int
    label: str
    area: int
    box: dict[str, int]
    long_side: float
    short_side: float
    contours: list[list[tuple[float, float]]]
    channels: list[int]
    effective: list[int]
    empty: list[int]
    label_enabled: bool
    comparison: str


class _Data(TypedDict):
    identity: dict[str, object]
    regions: list[_StoredRegion]
    occupancy: dict[str, str]


def _unpack(r: _StoredRegion) -> Region:
    contours: tuple[Contour, ...] = tuple(
        tuple((x, y) for x, y in ring) for ring in r["contours"]
    )
    return Region(
        r["number"],
        r["label"],
        r["area"],
        BoundingRect(**r["box"]),
        r["long_side"],
        r["short_side"],
        contours,
        tuple(r["channels"]),
        tuple(r["effective"]),
        tuple(r["empty"]),
        r["label_enabled"],
        r["comparison"],
    )


def cached_frame(a: ReplaySetup, s: CameraImage) -> Frame | None:
    ident = image_identity(a, s)
    name = _digest(ident) + ".json"
    relative = "frames/" + name
    if not (a.cache / relative).is_file():
        return None
    saved = json.loads((a.cache / relative).read_bytes())
    data = cast(_Data, saved["data"])
    if data["identity"] != ident or saved["sha256"] != _digest(data):
        raise ValueError("Replay cache identity or checksum mismatch")
    regions = tuple(_unpack(r) for r in data["regions"])
    if any(
        r.number != i or not valid_sdk_area(r.label, r.area, a.study.native_minima)
        for i, r in enumerate(regions, 1)
    ):
        raise ValueError("Cached SDK region identity, label or area is invalid")
    occupancy = {int(k): v for k, v in data["occupancy"].items()}
    return Frame(
        s.path, s.sha, relative, s.width, s.height, regions, occupancy, s.business
    )


@contextmanager
def analysis_owner(cache: Path) -> Generator[None]:
    cache.mkdir(parents=True, exist_ok=True)
    with (cache / "analysis.lock").open("a+b") as handle:
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise ValueError("Another archive analysis is active") from exc
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


class ImageUnavailable(ValueError):
    pass


def verified_png(a: ReplaySetup, s: CameraImage) -> bytes:
    try:
        path = (a.study.package / s.path).resolve(strict=True)
        if not path.is_relative_to(a.study.package):
            raise ImageUnavailable("Image outside portable archive")
        data = path.read_bytes()
    except OSError as exc:
        raise ImageUnavailable(f"PNG unavailable: {s.path}") from exc
    if hashlib.sha256(data).hexdigest() != s.sha:
        raise ImageUnavailable(f"PNG changed since discovery: {s.path}")
    return data


def capture(
    a: ReplaySetup, s: CameraImage, engine: VimoSegmentationEngine, data: bytes
) -> Frame:
    try:
        image = cv2.imdecode(
            np.frombuffer(data, np.uint8),
            cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION,
        )
    except cv2.error as exc:
        raise ImageUnavailable(f"Unreadable PNG: {s.path}") from exc
    if (
        image is None
        or image.dtype != np.uint8
        or image.shape != (s.height, s.width, 3)
    ):
        raise ImageUnavailable(f"Unexpected input geometry: {s.path}")
    image = cast(NDArray[np.uint8], image)
    policy = a.profiles[s.run]
    channels = [c for c in policy.channels if c.camera == s.camera]
    occupancy = {
        c.number: measure_occupancy(image, c.roi).state.name
        for c in channels
        if c.roi.x + c.roi.width <= s.width and c.roi.y + c.roi.height <= s.height
    }
    result = engine.infer(image)
    if (
        result.image_shape != image.shape
        or result.module_id != a.study.module
        or result.roi != BoundingRect(0, 0, s.width, s.height)
    ):
        raise ValueError("Response identity or native image geometry mismatch")
    overlap = OverlapPolicy()
    regions: list[Region] = []
    for n, r in enumerate(result.regions, 1):
        b = r.bounding_rect
        if (
            not 0 <= b.x < b.x + b.width <= s.width
            or not 0 <= b.y < b.y + b.height <= s.height
            or not valid_sdk_area(r.label, r.area, a.study.native_minima)
            or not math.isfinite(r.long_side)
            or not 0 <= r.short_side <= r.long_side
        ):
            raise ValueError("Invalid region measurement in SDK response")
        assigned = tuple(
            c.number
            for c in channels
            if overlap.intersection(b, c.roi) >= overlap.minimum_intersection_area
        )
        empty = tuple(n for n in assigned if occupancy.get(n) == "EMPTY")
        enabled = policy.enabled and r.label not in policy.disabled
        effective = tuple(n for n in assigned if enabled and n not in empty)
        regions.append(
            Region(
                n,
                r.label,
                r.area,
                b,
                r.long_side,
                r.short_side,
                (r.outer_contour, *r.inner_contours),
                assigned,
                effective,
                empty,
                enabled,
                "replayed",
            )
        )
    ident = image_identity(a, s)
    relative = "frames/" + _digest(ident) + ".json"
    payload = dict(
        identity=ident,
        regions=[asdict(r) for r in regions],
        occupancy={str(k): v for k, v in occupancy.items()},
    )
    atomic_json(a.cache / relative, dict(data=payload, sha256=_digest(payload)))
    return Frame(
        s.path,
        s.sha,
        relative,
        s.width,
        s.height,
        tuple(regions),
        occupancy,
        s.business,
    )


@contextmanager
def verified_engine(a: ReplaySetup, model: Path) -> Generator[VimoSegmentationEngine]:
    if (
        file_sha(model) != a.study.model_sha
        or version("smartmore-vimo-inference") != a.study.sdk
    ):
        raise ValueError(
            "Supplied model or installed SDK differs from the verified study"
        )
    with VimoSegmentationEngine(
        model, module_id=a.study.module, cache_dir=a.cache / "sdk-work"
    ) as engine:
        if (
            engine.info.model_sha256 != a.study.model_sha
            or engine.info.module_id != a.study.module
            or dict(engine.info.area_thresholds) != a.study.native_minima
            or set(engine.info.labels) != set(a.study.native_minima)
        ):
            raise ValueError(
                "Active model, module, labels or native cutoffs differ from the study"
            )
        yield engine
