"""Build a resumable evidence plan exclusively from the original portable History and PNGs."""

import hashlib
import json
import sqlite3
import struct
import zlib
from collections import Counter
from contextlib import closing
from dataclasses import asdict, dataclass, replace
from itertools import batched, islice
from pathlib import Path, PureWindowsPath
from typing import cast

from scripts.area_explorer.batch import ReplayClient
from scripts.area_explorer.evidence import Channel, Frame
from scripts.area_explorer.replay import CameraImage, Profile, ReplaySetup, cached_frame
from scripts.area_explorer.study import Study
from scripts.history_area_matching import encode, reconcile

from capsule_vision.application import ng_capsules, run_configuration
from capsule_vision.application.archive_links import relocated_image_path
from capsule_vision.application.history import HistoryFinding
from capsule_vision.application.history_findings import (
    display_winners,
    selected_findings,
)
from capsule_vision.configuration import (
    ModelDefinition,
    ModelRegistry,
    load_machine_layout,
    load_recipe,
    resolve_configuration,
)
from capsule_vision.inference import BoundingRect
from capsule_vision.inspection.production import CameraLayout
from capsule_vision.inspection.production.layout import valid_rect


def array(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ValueError("Unsupported compact History array")
    return cast(list[object], value)


def finding(row: object, allowed: set[int], width: int, height: int) -> HistoryFinding:
    fields = array(row)
    if len(fields) not in (3, 4):
        raise ValueError("Unsupported History finding")
    label, box, channels = fields[:3]
    rect = BoundingRect(*cast(tuple[int, int, int, int], tuple(array(box))))
    valid_rect(rect)
    ids = array(channels)
    area = fields[3] if len(fields) == 4 else None
    if (
        not isinstance(label, str)
        or not label.strip()
        or not ids
        or len(set(ids)) != len(ids)
        or any(type(n) is not int or n not in allowed for n in ids)
        or rect.x + rect.width > width
        or rect.y + rect.height > height
        or (len(fields) == 4 and (type(area) is not int or area < 0))
    ):
        raise ValueError("Invalid historical finding or effective channel association")
    return HistoryFinding(
        label, rect, tuple(cast(list[int], ids)), cast(int | None, area)
    )


@dataclass
class FrozenRun:
    recipe: str
    model: str
    profile: Profile
    cameras: dict[str, CameraLayout]
    minima: dict[str, int]


def frozen_run(
    db: sqlite3.Connection, run: str, recipe_identity: str, study: Study
) -> FrozenRun:
    manifest = run_configuration.read(db, run)
    if manifest is None or not manifest.complete:
        raise ValueError("Missing complete frozen Run configuration")
    doc = json.loads(manifest.effective)["inspection"]
    resources = {r.artifact: r for r in manifest.resources}
    machine, recipe = resources["machine"], resources["recipe"]
    if (
        machine.content is None
        or recipe.content is None
        or (machine.origin, recipe.origin) != ("saved", "saved")
    ):
        raise ValueError("Missing frozen MachineLayout or Recipe source")
    model = ModelDefinition(**doc["model"])
    config = resolve_configuration(
        load_machine_layout(Path("machine.tsv"), content=machine.content),
        load_recipe(Path(recipe.resource + ".json"), content=recipe.content),
        ModelRegistry((model,)),
    )
    vimo = config.checks.vimo
    policy = doc["checks"]["vimo"]
    if (
        json.loads(recipe_identity)
        != dict(
            recipe_id=config.recipe_id,
            model_id=model.model_id,
            model_sha256=study.model_sha,
        )
        or model.expected_sha256 != study.model_sha
        or model.module_id != study.module
        or (resources["model"].resource, resources["model"].sha256)
        != (model.model_id, study.model_sha)
        or config.recipe_id != doc["recipe_id"]
        or json.loads(json.dumps(asdict(config.layout))) != doc["layout"]
        or policy["enabled"] is not True
        or not vimo.enabled
        or policy["required"] != vimo.required
        or set(policy["disabled_labels"]) != set(vimo.disabled_labels)
        or policy.get("minimum_areas", {}) != dict(vimo.minimum_areas)
    ):
        raise ValueError(
            "Run identity, geometry or policy disagrees with frozen resources"
        )
    cameras = {str(c.camera_id): c for c in config.layout.cameras}
    channels = tuple(
        Channel(s.channel_id, c.camera_id, s.rect, s.slot)
        for c in cameras.values()
        for s in c.slots
    )
    return FrozenRun(
        config.recipe_id,
        model.model_id,
        Profile(
            channels,
            model.expected_sha256,
            True,
            frozenset(vimo.disabled_labels),
        ),
        cameras,
        dict(vimo.minimum_areas),
    )


def source_path(root: Path, stored: str, camera: str) -> Path:
    mapped = relocated_image_path(PureWindowsPath(stored), camera, root)
    if mapped is None:
        raise ValueError("Invalid archived image link")
    path = mapped.resolve(strict=True)
    if path != mapped or not path.is_relative_to(root):
        raise ValueError("Redirected archived image link")
    return path


def previous_photos(
    plan_file: Path, study: Study
) -> dict[tuple[str, str, str], tuple[str, str]]:
    """Pin known originals even if a different same-name PNG has no cache entry."""
    if not plan_file.is_file():
        return {}
    with closing(
        sqlite3.connect(plan_file.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    ) as db:
        metadata = json.loads(db.execute("SELECT value FROM identity").fetchone()[0])
        if (
            metadata["database_sha"],
            metadata["model_sha"],
            metadata["receipts_sha"],
        ) != (study.database_sha, study.model_sha, study.analysis_sha):
            return {}
        return {
            (run, cycle, im["camera"]): (im["path"], im["sha256"])
            for run, cycle, images in db.execute("SELECT run,cycle,images FROM plans")
            for im in json.loads(images)
            if "sha256" in im
        }


class Replay:
    def __init__(
        self,
        db: sqlite3.Connection,
        study: Study,
        model: Path,
        cache: Path,
        work: Path,
        previous_cache: Path | None = None,
    ):
        self.runs: dict[str, FrozenRun] = {}
        self.errors: dict[str, str] = {}
        for run, recipe in db.execute("SELECT uuid,recipe FROM runs"):
            try:
                self.runs[run] = frozen_run(db, run, recipe, study)
            except (ValueError, KeyError, TypeError) as exc:
                self.errors[run] = str(exc)
        self.setup = ReplaySetup(
            study,
            study.database_sha,
            cache,
            {k: r.profile for k, r in self.runs.items()},
        )
        self.caches = [("explorer", self.setup)]
        self.known_photos: dict[tuple[str, str, str], tuple[str, str]] = {}
        if previous_cache is not None:
            self.known_photos = previous_photos(
                previous_cache.parent / "plan.sqlite3", study
            )
            self.caches.append(
                ("previous_rebuild", replace(self.setup, cache=previous_cache))
            )
        self.pilot = {
            (g.run, g.first_cycle, slot, g.members[slot - 1], cam): f
            for g in study.groups
            for (slot, cam), f in g.frames.items()
            if f.regions is not None
        }
        self.client = ReplayClient(
            replace(self.setup, cache=work / "replay-cache"), model
        )
        linked = Counter(
            relocated_image_path(PureWindowsPath(path), camera, study.package)
            for camera, path in db.execute("SELECT camera_id,path FROM archive_images")
        )
        self.duplicates = {path for path, count in linked.items() if count > 1}

    def read(self, image: CameraImage) -> tuple[Frame, str]:
        expected = self.known_photos.get((image.run, image.cycle, image.camera))
        if expected is not None and expected != (image.path, image.sha):
            raise ValueError("Linked PNG differs from the previously verified original")
        pilot = self.pilot.get(
            (image.run, image.group, image.slot, image.cycle, image.camera)
        )
        if pilot:
            if (pilot.path, pilot.sha256, pilot.width, pilot.height) != (
                image.path,
                image.sha,
                image.width,
                image.height,
            ):
                raise ValueError("Linked PNG differs from the verified pilot original")
            return replace(pilot, business=image.business), "pilot"
        for source, setup in self.caches:
            try:
                frame = cached_frame(setup, image)
            except ValueError, KeyError, TypeError, AttributeError, OSError:
                continue  # Never modify another analysis's receipt.
            if frame is not None:
                return frame, source
        answer = self.client.request(image)
        if answer.frame is None:
            raise ValueError(answer.problem or "No replay result")
        return answer.frame, "local_cache" if answer.reused else "inference"


def camera_plan(
    replay: Replay,
    frozen: FrozenRun,
    run: str,
    cycle: str,
    group: str,
    slot: int,
    camera_row: list[object],
    channels: list[list[object]],
    stored: str,
    audit: dict[str, object],
) -> bool:
    cam = str(camera_row[0])
    layout = frozen.cameras[cam]
    if (
        len(camera_row) not in (5, 6)
        or camera_row[1] != "SUCCESS"
        or camera_row[3] != []
    ):
        raise ValueError("Unsupported historical camera evidence")
    members = [r for r in channels if r[0] == cam]
    if [(r[1], r[2]) for r in members] != [
        (s.slot, s.channel_id) for s in layout.slots
    ] or any(
        len(r) != 7
        or type(r[1]) is not int
        or type(r[2]) is not int
        or r[4] != "SUCCESS"
        for r in members
    ):
        raise ValueError(
            "Recorded channels or slots disagree with frozen camera layout"
        )
    if not stored:
        raise ValueError("Missing Run/Cycle/Camera image link")
    root = replay.setup.study.package
    path = source_path(root, stored, cam)
    if path in replay.duplicates:
        raise ValueError("Several Run/Cycle/Camera identities link to this PNG")
    with path.open("rb") as stream:
        header = stream.read(26)
        if (
            header[:16] != b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
            or header[24:26] != b"\x08\x02"
        ):
            raise ValueError("Expected an original 8-bit full-color PNG")
        width, height = struct.unpack_from(">II", header, 16)
        stream.seek(0)
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    size = [width, height]
    business = {cast(int, r[2]): str(r[3]) for r in members}
    allowed = {k for k, v in business.items() if v == "NG"}
    original: list[HistoryFinding] = []
    if len(camera_row) == 6:
        dims, rows = array(camera_row[5])
        if dims != size:
            raise ValueError("Recorded dimensions disagree with the linked PNG")
        original = [finding(r, allowed, width, height) for r in array(rows)]
    image = CameraImage(
        run,
        group,
        slot,
        cycle,
        cam,
        path.relative_to(root).as_posix(),
        digest,
        width,
        height,
        business,
    )
    frame, origin = replay.read(image)
    result = reconcile(
        original,
        frame,
        int(cycle),
        layout,
        replay.setup.study.module,
        frozen.profile.disabled,
        frozen.minima,
    )
    after = [encode(f) for f in result.findings]
    before = [encode(f) for f in original]
    audit.update(
        source=origin,
        path=image.path,
        sha256=digest,
        size=size,
        receipt=frame.raw_result,
        before=before,
        after=after,
        counts=dict(result.counts),
        issues=result.issues,
        raw_regions=len(frame.regions or ()),
    )
    if before == after:
        return False
    camera_row[5] = [size, after]
    return True


def legacy_plan(
    geometry: dict[int, ng_capsules.FrozenROI],
    camera: list[object],
    channels: list[list[object]],
    audit: dict[str, object],
) -> bool:
    """Reduce recorded rows without measuring or relocating a finding."""
    if len(camera) != 6:
        return False
    size, rows = array(camera[5])
    width, height = array(size)
    if type(width) is not int or type(height) is not int or min(width, height) <= 0:
        raise ValueError("No recorded image dimensions")
    slots = {
        cast(int, r[2]): r[1] for r in channels if r[0] == camera[0] and r[3] == "NG"
    }
    old = [finding(row, set(slots), width, height) for row in array(rows)]
    rois: dict[int, BoundingRect] = {}
    for ch in {ch for f in old for ch in f.channels}:
        roi = geometry.get(ch)
        if (
            roi is None
            or (roi.camera, roi.slot) != (camera[0], slots[ch])
            or roi.rect.x + roi.rect.width > width
            or roi.rect.y + roi.rect.height > height
        ):
            raise ValueError("Recorded channel/view geometry unavailable")
        rois[ch] = roi.rect
    kept = selected_findings(old, display_winners(old, rois))
    after = [encode(f) for f in kept]
    audit.update(
        source="legacy",
        before=rows,
        after=after,
        size=size,
        counts={
            "legacy_assignments": sum(len(f.channels) for f in kept),
            "subordinate_assignments": sum(len(f.channels) for f in old)
            - sum(len(f.channels) for f in kept),
        },
    )
    if rows == after:
        return False
    camera[5] = [size, after]
    return True


def plan(
    db: sqlite3.Connection,
    stage: sqlite3.Connection,
    replay: Replay,
    groups: list[str],
    limit: int | None,
    batch_size: int,
    retry: bool,
) -> Counter[str]:
    done = {
        r[0]
        for r in stage.execute(
            "SELECT id FROM plans WHERE retry=0 OR ?=0", (int(retry),)
        )
    }
    sql = """SELECT c.id,c.run_id,c.cycle_id,c.recipe_id,c.model_id,c.model_sha,c.detail,
        m.first_cycle,m.slot FROM cycles c LEFT JOIN group_members m ON (m.run,m.cycle)=(c.run_id,c.cycle_id)"""
    if groups:
        sql += " WHERE m.first_cycle IN (" + ",".join("?" for _ in groups) + ")"
    stream = (r for r in db.execute(sql + " ORDER BY c.id", groups) if r[0] not in done)
    counts: Counter[str] = Counter()
    geometry: dict[str, dict[int, ng_capsules.FrozenROI]] = {}
    for batch in batched(islice(stream, limit), batch_size, strict=False):
        with stage:
            for (
                record,
                run,
                cycle,
                recipe,
                model,
                model_sha,
                blob,
                group,
                slot,
            ) in batch:
                payload = array(json.loads(zlib.decompress(blob)))
                channels = [array(r) for r in array(payload[0])]
                links = dict(
                    db.execute(
                        "SELECT camera_id,path FROM archive_images WHERE run_id=? AND cycle_id=?",
                        (run, cycle),
                    )
                )
                images: list[dict[str, object]] = []
                previous = (
                    stage.execute(
                        "SELECT images FROM plans WHERE id=?", (record,)
                    ).fetchone()
                    if retry
                    else None
                )
                prior = (
                    {im["camera"]: im for im in json.loads(previous[0])}
                    if previous
                    else {}
                )
                changed = incomplete = False
                for item in array(payload[1]):
                    camera = array(item)
                    cam = str(camera[0])
                    if cam not in links and len(camera) == 5:
                        continue
                    audit: dict[str, object] = dict(camera=cam, slot=slot)
                    saved = prior.get(cam)
                    if saved is not None and "error" not in saved:
                        images.append(saved)
                        if saved["before"] != saved["after"]:
                            camera[5] = [saved["size"], saved["after"]]
                            changed = True
                        continue
                    try:
                        frozen = replay.runs.get(run)
                        if frozen is None:
                            raise ValueError(replay.errors[run])
                        if (recipe, model, model_sha) != (
                            frozen.recipe,
                            frozen.model,
                            frozen.profile.model_sha,
                        ):
                            raise ValueError(
                                "Cycle Recipe or model differs from its Run"
                            )
                        changed |= camera_plan(
                            replay,
                            frozen,
                            run,
                            cycle,
                            group or "",
                            slot or 0,
                            camera,
                            channels,
                            links.get(cam, ""),
                            audit,
                        )
                    except (
                        ValueError,
                        OSError,
                        KeyError,
                        TypeError,
                        IndexError,
                    ) as exc:
                        incomplete = True
                        audit["error"] = str(exc)
                        try:
                            if run not in geometry:
                                geometry[run] = ng_capsules.frozen_rois(db, run)
                            changed |= legacy_plan(
                                geometry[run], camera, channels, audit
                            )
                        except (
                            ValueError,
                            TypeError,
                            IndexError,
                            KeyError,
                        ) as fallback_error:
                            audit["fallback_error"] = str(fallback_error)
                        if "before" not in audit:
                            audit["original_evidence"] = camera[5:]
                    if (
                        saved is not None
                        and saved.get("source") == "legacy"
                        and saved["before"] != saved["after"]
                    ):
                        audit["previous_fallback"] = saved["after"]
                    images.append(audit)
                    counts[str(audit.get("source", "unavailable"))] += 1
                after = (
                    zlib.compress(
                        json.dumps(
                            payload, ensure_ascii=False, separators=(",", ":")
                        ).encode()
                    )
                    if changed
                    else None
                )
                stage.execute(
                    "INSERT OR REPLACE INTO plans VALUES(?,?,?,?,?,?,?,?)",
                    (
                        record,
                        run,
                        cycle,
                        hashlib.sha256(blob).hexdigest(),
                        after,
                        json.dumps(images, ensure_ascii=False, separators=(",", ":")),
                        int(incomplete),
                        group,
                    ),
                )
                counts["cycles"] += 1
                counts["changed"] += changed
        if counts["cycles"] % (batch_size * 8) == 0:
            print(dict(counts), flush=True)
    return counts
