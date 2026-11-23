"""Join the frozen study tables and raw SDK receipts, keeping all coordinates native."""

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePath, PureWindowsPath
from typing import TypedDict, cast

from capsule_vision.application.archive_links import relocated_image_path
from capsule_vision.inference.models import BoundingRect, Contour

from .evidence import Channel, Frame, Group, Region, valid_sdk_area


class _Slot(TypedDict):
    channel_id: int
    rect: dict[str, int]


class _Camera(TypedDict):
    camera_id: str
    slots: list[_Slot]


class _Policy(TypedDict):
    enabled: bool
    disabled_labels: list[str]


class _Identity(TypedDict):
    database: str
    database_sha256: str
    model: str
    model_sha256: str
    frozen_model: dict[str, str]
    runs: list[dict[str, object]]
    layout: dict[str, list[_Camera]]
    vimo_policy: _Policy


class _Engine(TypedDict):
    model_sha256: str
    module_id: str
    sdk_distribution: str
    area_thresholds: dict[str, int]
    labels: list[str]


class _Source(TypedDict):
    run: str
    group_first_cycle: str
    slot: int
    cycle: str
    camera: str
    path: str
    sha256: str


class _Image(_Source):
    width: int
    height: int
    raw_result: str
    raw_regions: int
    occupancy_state: dict[str, str]
    recorded_business: dict[str, str]


class _Sides(TypedDict):
    region_id: int
    label: str
    sdk_area_px2: int
    long_side_px: float
    short_side_px: float


class _Row(_Source, _Sides):
    bbox_x: int
    bbox_y: int
    bbox_width: int
    bbox_height: int
    roi_channels: list[int]
    effective_channels: list[int]
    empty_gated_channels: list[int]
    recipe_label_enabled: bool
    comparison: str


class _Shape(_Sides):
    bbox_xywh: list[int]
    outer_contour: list[tuple[float, float]]
    inner_contours: list[list[tuple[float, float]]]


class _Receipt(TypedDict):
    source: _Source
    model_sha256: str
    module_id: str
    image_shape_hwc: list[int]
    regions: list[_Shape]


class _History(TypedDict):
    source: _Source
    stored_archive_link: str


class _CameraSlot(TypedDict):
    slot: int
    cycle: str
    camera: str
    image_state: str


class _Group(TypedDict):
    run: str
    group_first_cycle: str
    members: list[tuple[int, str]]
    recorded_final_channels: list[tuple[int, str]]
    camera_slots: list[_CameraSlot]


@dataclass(frozen=True)
class Study:
    directory: Path
    package: Path
    model: str
    model_sha: str
    database_sha: str
    analysis_sha: str
    sdk: str
    module: str
    native_minima: dict[str, int]
    channels: tuple[Channel, ...]
    groups: tuple[Group, ...]


def _require(valid: bool, message: str) -> None:
    if not valid:
        raise ValueError(message)


def _source(s: _Source) -> tuple[str, str, int, str, str, str, str]:
    return (
        s["run"],
        s["group_first_cycle"],
        s["slot"],
        s["cycle"],
        s["camera"],
        s["path"],
        s["sha256"],
    )


def _group(g: _Group, run: str) -> Group:
    members = dict(g["members"])
    _require(
        g["run"] == run
        and len(g["members"]) == 4
        and set(members) == {1, 2, 3, 4}
        and len(set(members.values())) == 4
        and members[1] == g["group_first_cycle"],
        "Invalid persisted group membership",
    )
    slots: dict[tuple[int, str], str] = {}
    for s in g["camera_slots"]:
        key = s["slot"], s["camera"]
        _require(
            key not in slots and members.get(s["slot"]) == s["cycle"],
            "Conflicting camera slot",
        )
        slots[key] = s["image_state"]
    return Group(
        run,
        g["group_first_cycle"],
        tuple(members[i] for i in range(1, 5)),
        dict(g["recorded_final_channels"]),
        {},
        slots,
    )


def _region(row: _Row, shape: _Shape, source: _Source, policy: _Policy) -> Region:
    box = [row["bbox_x"], row["bbox_y"], row["bbox_width"], row["bbox_height"]]
    _require(
        _source(row) == _source(source)
        and shape["bbox_xywh"] == box
        and row["region_id"] == shape["region_id"]
        and row["label"] == shape["label"]
        and row["sdk_area_px2"] == shape["sdk_area_px2"]
        and row["long_side_px"] == shape["long_side_px"]
        and row["short_side_px"] == shape["short_side_px"],
        f"Conflicting area/geometry for {source['path']} region {row['region_id']}",
    )
    enabled = policy["enabled"] and row["label"] not in policy["disabled_labels"]
    _require(
        enabled == row["recipe_label_enabled"], "Conflicting historical label filter"
    )
    contours: tuple[Contour, ...] = tuple(
        tuple((x, y) for x, y in points)
        for points in [shape["outer_contour"], *shape["inner_contours"]]
    )
    return Region(
        row["region_id"],
        row["label"],
        row["sdk_area_px2"],
        BoundingRect(*box),
        row["long_side_px"],
        row["short_side_px"],
        contours,
        tuple(row["roi_channels"]),
        tuple(row["effective_channels"]),
        tuple(row["empty_gated_channels"]),
        enabled,
        row["comparison"],
    )


def load_study(directory: Path, package: Path | None = None) -> Study:
    directory = directory.resolve()
    receipts: dict[str, str] = {}

    def read(name: str) -> object:
        data = (directory / name).read_bytes()
        receipts[name] = hashlib.sha256(data).hexdigest()
        return json.loads(data)

    identity = cast(_Identity, read("identity-coverage.json"))
    engine = cast(_Engine, read("engine-pilot.json"))
    _require(
        engine == read("engine-remaining.json"),
        "Pilot and remaining SDK settings differ",
    )
    _require(
        engine["model_sha256"]
        == identity["model_sha256"]
        == identity["frozen_model"]["expected_sha256"]
        and engine["module_id"] == identity["frozen_model"]["module_id"]
        and len(identity["runs"]) == 1,
        "Replay and frozen Run model identities differ or the frozen layout has no unique Run",
    )
    run = str(identity["runs"][0]["run_id"])
    package = (package or Path(identity["database"]).parent).resolve()
    channels = tuple(
        Channel(s["channel_id"], camera["camera_id"], BoundingRect(**s["rect"]))
        for camera in identity["layout"]["cameras"]
        for s in camera["slots"]
    )
    _require(
        bool(channels)
        and len({ch.number for ch in channels}) == len(channels)
        and all(ch.roi.width > 0 and ch.roi.height > 0 for ch in channels),
        "Missing, duplicate or invalid frozen channel ROI",
    )
    groups = tuple(
        _group(g, run) for g in cast(list[_Group], read("selected-groups.json"))
    )
    by_group = {g.first_cycle: g for g in groups}
    _require(
        bool(groups) and len(by_group) == len(groups),
        "Missing or duplicate study group",
    )
    history = cast(dict[str, _History], read("history-evidence.json"))
    rows: defaultdict[tuple[str, str, str], dict[int, _Row]] = defaultdict(dict)
    for row in cast(list[_Row], read("regions.json")):
        indexed = rows[row["run"], row["cycle"], row["camera"]]
        _require(row["region_id"] not in indexed, "Duplicate region identity")
        indexed[row["region_id"]] = row

    for image in cast(list[_Image], read("images.json")):
        group = by_group[image["group_first_cycle"]]
        slot, camera, cycle = image["slot"], image["camera"], image["cycle"]
        key = slot, camera
        evidence_id = f"{run}_{cycle}_{camera}"
        raw_name = f"raw/{evidence_id}.json"
        _require(
            image["run"] == run
            and group.slot_states.get(key) == "available"
            and group.members[slot - 1] == cycle
            and key not in group.frames
            and image["raw_result"] == raw_name
            and PurePath(raw_name).parts == ("raw", evidence_id + ".json"),
            f"Image does not match its persisted group slot or raw receipt: {evidence_id}",
        )
        hist = history[evidence_id]
        linked = relocated_image_path(
            PureWindowsPath(hist["stored_archive_link"]), camera, package
        )
        _require(
            linked is not None
            and linked.relative_to(package).as_posix() == image["path"]
            and _source(image) == _source(hist["source"]),
            f"Inconsistent archive link: {evidence_id}",
        )
        indexed = rows.pop((run, cycle, camera), {})
        regions = None
        if (directory / raw_name).is_file():
            raw = cast(_Receipt, read(raw_name))
            shapes = raw["regions"]
            _require(
                _source(raw["source"]) == _source(image)
                and raw["model_sha256"] == engine["model_sha256"]
                and raw["module_id"] == engine["module_id"]
                and raw["image_shape_hwc"] == [image["height"], image["width"], 3]
                and len(shapes) == image["raw_regions"] == len(indexed)
                and {r["region_id"] for r in shapes} == set(indexed),
                f"Replay receipt identity, dimensions or region count differ: {evidence_id}",
            )
            regions = tuple(
                _region(indexed[s["region_id"]], s, image, identity["vimo_policy"])
                for s in shapes
            )
            _require(
                all(
                    valid_sdk_area(r.label, r.area, engine["area_thresholds"])
                    for r in regions
                ),
                "Replay label or area contradicts native model settings",
            )
        group.frames[key] = Frame(
            image["path"],
            image["sha256"],
            raw_name,
            image["width"],
            image["height"],
            regions,
            {int(k): v for k, v in image["occupancy_state"].items()},
            {int(k): v for k, v in image["recorded_business"].items()},
        )
    _require(not rows, "Region table references an image outside the selected replay")
    analysis_sha = hashlib.sha256(
        json.dumps(sorted(receipts.items())).encode()
    ).hexdigest()
    return Study(
        directory,
        package,
        Path(identity["model"]).name,
        identity["model_sha256"],
        identity["database_sha256"],
        analysis_sha,
        engine["sdk_distribution"],
        engine["module_id"],
        {s: engine["area_thresholds"][s] for s in engine["labels"]},
        channels,
        groups,
    )
