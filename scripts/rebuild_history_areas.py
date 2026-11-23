"""Explicit offline backfill of the development checkout's legacy ViMo evidence."""

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import zlib
from collections import Counter
from contextlib import ExitStack, closing
from itertools import batched, islice
from pathlib import Path
from tempfile import NamedTemporaryFile

from scripts.area_explorer.replay import analysis_owner, atomic_json, file_sha
from scripts.area_explorer.study import load_study
from scripts.history_area_plan import Replay, array, plan, source_path

from capsule_vision.application import ng_capsules
from capsule_vision.application._security_lock import SecurityLease
from capsule_vision.application.history_findings import lower_view
from capsule_vision.application.startup import ApplicationData
from capsule_vision.inference import BoundingRect


def checkout_data() -> ApplicationData:
    root = Path(__file__).resolve().parents[1]
    if (
        Path.cwd().resolve() != root
        or not (root / ".git").exists()
        or not (root / "app.py").is_file()
    ):
        raise ValueError(
            "Run this workflow explicitly from the development checkout containing app.py"
        )
    data = ApplicationData.at_entry(root)
    for path in (
        data.root,
        data.security.with_suffix(".lock"),
        *(
            db.with_name(db.name + suffix)
            for db in (data.security, data.history, data.audit)
            for suffix in ("", "-wal", "-shm", "-journal")
        ),
    ):
        if (
            path.is_symlink()
            or path.is_junction()
            or (path.is_file() and path.stat().st_nlink > 1)
        ):
            raise ValueError(f"Refusing redirected development data target: {path}")
    return data


def pending_journal(path: Path) -> bool:
    files = (path.with_name(path.name + suffix) for suffix in ("-wal", "-journal"))
    return any(file.is_file() and file.stat().st_size > 0 for file in files)


def readonly(path: Path) -> sqlite3.Connection:
    # An offline, checkpointed WAL database needs no shared-memory sidecar to verify.
    options = "?mode=ro" if pending_journal(path) else "?mode=ro&immutable=1"
    return sqlite3.connect(
        path.resolve(strict=True).as_uri() + options, uri=True, timeout=0
    )


def stage_identity(db: sqlite3.Connection, identity: dict[str, object]) -> None:
    db.execute("CREATE TABLE IF NOT EXISTS identity(value TEXT PRIMARY KEY)")
    db.execute("""CREATE TABLE IF NOT EXISTS plans(id INTEGER PRIMARY KEY,run TEXT,cycle TEXT,
        original_sha TEXT,rebuilt BLOB,images TEXT,retry INTEGER,first_cycle TEXT)""")
    text = json.dumps(identity, sort_keys=True)
    previous = db.execute("SELECT value FROM identity").fetchall()
    if previous and previous != [(text,)]:
        raise ValueError(
            "Plan belongs to different History, model, study, paths or rebuild rules"
        )
    with db:
        db.execute("INSERT OR IGNORE INTO identity VALUES(?)", (text,))


def integrity(db: sqlite3.Connection) -> None:
    if (
        db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]
        or db.execute("PRAGMA foreign_key_check").fetchone()
    ):
        raise ValueError("SQLite integrity or foreign-key check failed")


def protected_payload(blob: bytes) -> list[object]:
    data = array(json.loads(zlib.decompress(blob)))
    if len(data) != 3:
        raise ValueError("Unsupported Cycle detail format")
    for cam in array(data[1]):
        row = array(cam)
        if len(row) == 6:
            area = array(row[5])
            area[1] = []  # Only the effective finding rows may differ.
    return data


def accounting(db: sqlite3.Connection) -> dict[str, str]:
    """Fingerprint every schema object and every scalar table, plus all protected Cycle detail."""
    result: dict[str, str] = {}
    schema = db.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name"
    ).fetchall()
    result["schema"] = hashlib.sha256(repr(schema).encode()).hexdigest()
    for _, name, _, _ in (r for r in schema if r[0] == "table"):
        quoted = '"' + name.replace('"', '""') + '"'
        columns = db.execute(
            "SELECT name,pk FROM pragma_table_info(?) ORDER BY cid", (name,)
        ).fetchall()
        digest = hashlib.sha256()
        primary = sorted((p, n) for n, p in columns if p)
        order = (
            ",".join('"' + n.replace('"', '""') + '"' for _, n in primary) or "rowid"
        )
        for row in db.execute(f"SELECT * FROM {quoted} ORDER BY {order}"):
            if name == "cycles":
                i = next(k for k, (n, _) in enumerate(columns) if n == "detail")
                row = (*row[:i], protected_payload(row[i]), *row[i + 1 :])
            digest.update(repr(row).encode() + b"\n")
        result[name] = digest.hexdigest()
    return result


def validate_start(reference: sqlite3.Connection, snapshot: sqlite3.Connection) -> None:
    """Accept earlier finding enrichment only for the same historical evidence groups."""
    integrity(snapshot)
    if accounting(reference) != accounting(snapshot):
        raise ValueError(
            "Development History differs from the reference in protected fields"
        )
    geometry: dict[str, dict[int, ng_capsules.FrozenROI]] = {}
    sql = "SELECT run_id,detail FROM cycles ORDER BY id"
    for (run, original), (_, current) in zip(
        reference.execute(sql), snapshot.execute(sql), strict=True
    ):
        if original == current:
            continue
        if run not in geometry:
            geometry[run] = ng_capsules.frozen_rois(reference, run)
        scopes: list[set[tuple[str, int, str, bool | None]]] = []
        for blob in (original, current):
            keys: set[tuple[str, int, str, bool | None]] = set()
            for cam in json.loads(zlib.decompress(blob))[1]:
                if len(cam) != 6:
                    continue
                for label, box, channels, *_ in cam[5][1]:
                    for ch in channels:
                        roi = geometry[run].get(ch)
                        view = lower_view(
                            BoundingRect(*box),
                            roi.rect if roi and roi.camera == cam[0] else None,
                        )
                        keys.add((cam[0], ch, label, view))
            scopes.append(keys)
        if scopes[0] != scopes[1]:
            raise ValueError(
                "Development History finding associations differ from the reference"
            )


def capture_snapshot(
    history: Path, backup: Path, reference: sqlite3.Connection
) -> None:
    if pending_journal(history):
        raise ValueError(
            "Checkpoint development History and close other owners before planning"
        )
    original_sha = file_sha(history)
    with NamedTemporaryFile(dir=backup.parent, delete=False) as stream:
        temp = Path(stream.name)
    try:
        shutil.copyfile(history, temp)
        if (
            file_sha(temp) != original_sha
            or file_sha(history) != original_sha
            or pending_journal(history)
        ):
            raise ValueError("Development History changed during its offline backup")
        with closing(readonly(temp)) as snapshot:
            validate_start(reference, snapshot)
        os.replace(temp, backup)
    finally:
        temp.unlink(missing_ok=True)


def validate_proposal(original: bytes, proposed: bytes, receipt: str) -> None:
    if protected_payload(original) != protected_payload(proposed):
        raise ValueError("Proposal changes protected Cycle fields")
    images = {im["camera"]: im for im in json.loads(receipt)}
    old, new = (
        array(array(json.loads(zlib.decompress(blob)))[1])
        for blob in (original, proposed)
    )
    for before, after in zip(old, new, strict=True):
        if before == after:
            continue
        a, b = array(before), array(after)
        im = images.get(a[0])
        if (
            len(a) != 6
            or len(b) != 6
            or im is None
            or ("error" in im and im.get("source") != "legacy")
            or im.get("before") != array(a[5])[1]
            or im.get("after") != array(b[5])[1]
        ):
            raise ValueError(
                "Proposed finding detail differs from its camera comparison"
            )
        if im.get("source") == "legacy" and any(
            not any(
                new[:2] == old[:2] and new[3:] == old[3:] and set(new[2]) <= set(old[2])
                for old in im["before"]
            )
            for new in im["after"]
        ):
            raise ValueError(
                "Legacy fallback introduced a rectangle, association or measurement"
            )
        if {(r[0], ch) for r in im["before"] for ch in r[2]} != {
            (r[0], ch) for r in im["after"] for ch in r[2]
        }:
            raise ValueError("Proposal changed effective historical channel or label")


def compare(
    reference: sqlite3.Connection, target: sqlite3.Connection, stage: sqlite3.Connection
) -> Counter[str]:
    integrity(target)
    if accounting(reference) != accounting(target):
        raise ValueError(
            "Database schema, accounting, identities, links, frozen manifests or protected Cycle fields differ"
        )
    proposals = {
        r[0]: r[1:]
        for r in stage.execute(
            "SELECT id,run,cycle,original_sha,rebuilt,images FROM plans"
        )
    }
    counts: Counter[str] = Counter()
    sql = "SELECT id,run_id,cycle_id,detail FROM cycles ORDER BY id"
    for old, current in zip(reference.execute(sql), target.execute(sql), strict=True):
        record, run, cycle, blob = old
        candidate = proposals.pop(record, None)
        if current[:3] != old[:3]:
            raise ValueError("Cycle identities differ from the original")
        after: bytes | None = None
        receipt = "[]"
        if candidate is not None:
            if candidate[:3] != (run, cycle, hashlib.sha256(blob).hexdigest()):
                raise ValueError(f"Plan has a different source Cycle {record}")
            after = candidate[3]
            receipt = candidate[4]
            if after is not None:
                validate_proposal(blob, after, receipt)
        if current[3] == blob:
            counts["pending" if after is not None else "unchanged"] += 1
        elif after is not None and current[3] == after:
            counts["applied"] += 1
        elif after is not None and _partly_applied(blob, current[3], after, receipt):
            # Retrying a missing photograph can extend a previously published Cycle.
            counts["pending"] += 1
        else:
            raise ValueError(f"Development Cycle {record} changed outside this rebuild")
    if proposals:
        raise ValueError("Plan refers to unknown Cycle identities")
    return counts


def _partly_applied(
    original: bytes, current: bytes, planned: bytes, receipt: str
) -> bool:
    if protected_payload(original) != protected_payload(current):
        return False
    versions = [
        array(json.loads(zlib.decompress(b)))[1] for b in (original, current, planned)
    ]
    fallbacks = {
        im["camera"]: im["previous_fallback"]
        for im in json.loads(receipt)
        if "previous_fallback" in im
    }
    for before, now, after in zip(*(array(v) for v in versions), strict=True):
        if now == before or now == after:
            continue
        camera = array(now)
        if (
            len(camera) != 6
            or camera[0] not in fallbacks
            or array(camera[5])[1] != fallbacks[camera[0]]
        ):
            return False
    return True


def publish(
    reference: sqlite3.Connection,
    target: sqlite3.Connection,
    stage: sqlite3.Connection,
    images: Path,
    batch_size: int,
    limit: int | None,
) -> int:
    before = compare(reference, target, stage)
    if not before["pending"]:
        return 0
    sql = "SELECT id,run,cycle,rebuilt,images FROM plans WHERE rebuilt IS NOT NULL ORDER BY id"
    pending = (
        r
        for r in stage.execute(sql)
        if target.execute("SELECT detail FROM cycles WHERE id=?", (r[0],)).fetchone()
        != (r[3],)
    )
    written = 0
    for batch in batched(islice(pending, limit), batch_size, strict=False):
        for _record, run, cycle, _planned, evidence in batch:
            for image in json.loads(evidence):
                if (
                    image.get("before") != image.get("after")
                    and image.get("source") != "legacy"
                ):
                    stored = reference.execute(
                        "SELECT path FROM archive_images WHERE (run_id,cycle_id,camera_id)=(?,?,?)",
                        (run, cycle, image["camera"]),
                    ).fetchone()
                    if (
                        stored is None
                        or file_sha(source_path(images, stored[0], image["camera"]))
                        != image["sha256"]
                    ):
                        raise ValueError("Original linked PNG changed since planning")
        with target:
            target.execute("BEGIN EXCLUSIVE")
            for record, run, cycle, after, audit in batch:
                current = target.execute(
                    "SELECT detail FROM cycles WHERE id=?", (record,)
                ).fetchone()[0]
                original = reference.execute(
                    "SELECT detail FROM cycles WHERE id=?", (record,)
                ).fetchone()[0]
                if current == after:
                    continue
                if current != original and not _partly_applied(
                    original, current, after, audit
                ):
                    raise ValueError(
                        f"Development Cycle {record} changed before publication"
                    )
                cursor = target.execute(
                    "UPDATE cycles SET detail=? WHERE id=? AND run_id=? AND cycle_id=? AND detail=?",
                    (after, record, run, cycle, current),
                )
                if cursor.rowcount != 1:
                    raise ValueError(
                        f"Development Cycle {record} changed before publication"
                    )
                written += 1
    return written


def summary(stage: sqlite3.Connection, directory: Path) -> dict[str, int]:
    counts: Counter[str] = Counter()
    with (directory / "comparison.jsonl").open("w", encoding="utf-8") as stream:
        for record, run, cycle, group, changed, evidence in stage.execute(
            "SELECT id,run,cycle,first_cycle,rebuilt IS NOT NULL,images FROM plans ORDER BY id"
        ):
            counts["planned_cycles"] += 1
            counts["changed_cycles"] += changed
            for im in json.loads(evidence):
                counts[str(im.get("source", "unavailable"))] += 1
                before, after = im.get("before", []), im.get("after", [])
                counts["changed_camera_records"] += before != after
                counts["historical_findings"] += len(before)
                counts["measured_findings"] += sum(len(f) == 4 for f in after)
                counts["remaining_unmeasured"] += sum(len(f) == 3 for f in after)
                if "error" in im and im.get("original_evidence"):
                    original = im["original_evidence"][0][1]
                    counts["historical_findings"] += len(original)
                    counts["remaining_unmeasured"] += sum(len(f) == 3 for f in original)
                counts.update(im.get("counts", {}))
                stream.write(
                    json.dumps(
                        dict(id=record, run=run, cycle=cycle, group=group, **im),
                        ensure_ascii=False,
                    )
                    + "\n"
                )
    atomic_json(directory / "summary.json", dict(counts))
    return dict(counts)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "apply", "verify"))
    parser.add_argument("--images", type=Path, default=Path("D:/Ly/Images"))
    parser.add_argument(
        "--model",
        type=Path,
        default=Path("C:/Users/Sesame/Projects/Ly/model261116.vimosln"),
    )
    parser.add_argument(
        "--work", type=Path, default=Path("outputs/dev-history-area-cleanup-1/full")
    )
    parser.add_argument(
        "--group",
        action="append",
        default=[],
        help="Plan a stored group first Cycle (repeatable)",
    )
    parser.add_argument(
        "--limit", type=int, help="Maximum pending Cycles for this invocation"
    )
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--retry-unavailable", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.batch_size <= 128 or (args.limit is not None and args.limit < 1):
        parser.error("batch size must be 1–128 and limit must be positive")
    if args.action != "plan" and (args.group or args.retry_unavailable):
        parser.error("--group and --retry-unavailable select planning work")
    data = checkout_data()
    images = Path(args.images).resolve(strict=True)
    work = Path(args.work).resolve()
    outputs = (data.root.parent / "outputs").resolve()
    if (
        not work.is_relative_to(outputs)
        or work == outputs
        or not data.history.is_file()
    ):
        raise ValueError(
            "Expected a dedicated checkout outputs directory and existing development History"
        )
    work.mkdir(parents=True, exist_ok=True)
    source = images / "history.sqlite3"
    if source.samefile(data.history):
        raise ValueError(
            "Reference package and writable development History must be distinct files"
        )
    study = load_study(outputs / "area-study-1", images)
    model = Path(args.model).resolve(strict=True)
    if (
        file_sha(source) != study.database_sha
        or file_sha(model) != study.model_sha
        or any(source.with_name(source.name + s).exists() for s in ("-wal", "-journal"))
    ):
        raise ValueError(
            "Reference package or model differs from the verified AREA-STUDY snapshot"
        )
    identity: dict[str, object] = dict(
        version=2,
        source=str(source),
        history=str(data.history),
        database_sha=study.database_sha,
        model_sha=study.model_sha,
        receipts_sha=study.analysis_sha,
        sdk=study.sdk,
        module=study.module,
    )
    with analysis_owner(work), ExitStack() as owners:
        pristine = owners.enter_context(closing(readonly(source)))
        backup = work / "before-history.sqlite3"
        plan_file = work / "plan.sqlite3"
        if args.action != "plan" and not (plan_file.is_file() and backup.is_file()):
            raise ValueError(
                "Plan and review this cleanup before verifying or applying it"
            )
        if not backup.exists():
            if plan_file.exists():
                raise ValueError(
                    "Existing plan is missing its original development backup"
                )
            with SecurityLease(data.security):
                capture_snapshot(data.history, backup, pristine)
        elif not plan_file.exists():
            with closing(readonly(backup)) as orphan:
                validate_start(pristine, orphan)
        ref = owners.enter_context(closing(readonly(backup)))
        identity["snapshot_sha"] = file_sha(backup)
        stage = owners.enter_context(closing(sqlite3.connect(plan_file)))
        stage_identity(stage, identity)
        if args.action == "plan":
            replay = Replay(
                ref,
                study,
                model,
                outputs / "area-explorer-ux-2" / "cache",
                work,
                outputs / "dev-history-rebuild-1" / "full" / "replay-cache",
            )
            try:
                print(
                    plan(
                        ref,
                        stage,
                        replay,
                        args.group,
                        args.limit,
                        args.batch_size,
                        args.retry_unavailable,
                    )
                )
            finally:
                replay.client.close()
            print(summary(stage, work))
            return
        with SecurityLease(data.security):
            if args.action == "verify":
                with closing(readonly(data.history)) as target:
                    result = compare(ref, target, stage)
                atomic_json(work / "verification.json", dict(result))
                print(dict(result))
                return
            with closing(
                sqlite3.connect(data.history.as_uri() + "?mode=rw", uri=True, timeout=0)
            ) as target:
                target.execute("PRAGMA locking_mode=EXCLUSIVE")
                target.execute("BEGIN EXCLUSIVE")
                target.commit()
                written = publish(
                    ref, target, stage, images, args.batch_size, args.limit
                )
                result = compare(ref, target, stage)
            print({"written": written, **result})


if __name__ == "__main__":
    main()
