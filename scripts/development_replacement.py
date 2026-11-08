"""Explicit, Windows-checkout-only replacement of known synthetic record stores.

History and Audit are copied in one SQLite multi-database rollback-journal
transaction. No credential/configuration files participate in that transaction.
"""

import ctypes
import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from shutil import copyfile
from time import time
from typing import cast
from uuid import uuid4

from development_records import example_id
from development_showcase import APPLICATION_ID, digest, object_map, schedule, verify

from capsule_vision.application._security_lock import SecurityLease
from capsule_vision.application.startup import ApplicationData


def closed_file(path: Path) -> None:
    """Detect even an idle SQLite owner before taking database locks on Windows."""
    if os.name != "nt":
        raise ValueError(
            "Showcase replacement is restricted to the Windows development workflow"
        )
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateFileW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_void_p,
    ]
    api.CreateFileW.restype = ctypes.c_void_p
    api.CloseHandle.argtypes = [ctypes.c_void_p]
    api.CloseHandle.restype = ctypes.c_int
    handle = api.CreateFileW(str(path), 0x80000000, 0, None, 3, 0, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ValueError(
            f"Close the application and all storage owners first: {path.name}"
        )
    api.CloseHandle(handle)


def local_files(data: ApplicationData) -> None:
    if data.root.drive.startswith("\\\\"):
        raise ValueError("Replacement requires a local development volume")
    for path in (
        data.root,
        *(
            p.with_name(p.name + suffix)
            for p in (data.history, data.audit, data.security)
            for suffix in ("", "-wal", "-shm", "-journal")
        ),
        data.security.with_suffix(".lock"),
    ):
        if (
            path.is_symlink()
            or path.is_junction()
            or (path.is_file() and path.stat().st_nlink > 1)
        ):
            raise ValueError(f"Refusing redirected data path: {path}")
    for path in (data.history, data.audit):
        if not path.is_file():
            raise ValueError(
                "Replacement requires an existing, recognized development pair"
            )
        closed_file(path)


def known_development(data: ApplicationData) -> dict[str, object]:
    """Fail closed on customer/unknown records, rather than using a --force option."""
    result: dict[str, object] = {}
    ids: list[int] = []
    for path, version in ((data.history, 3), (data.audit, 1)):
        with closing(sqlite3.connect(path, timeout=0)) as db:
            if db.execute("PRAGMA user_version").fetchone() != (version,) or db.execute(
                "PRAGMA integrity_check"
            ).fetchone() != ("ok",):
                raise ValueError("Unsupported or damaged development database")
            ids.append(db.execute("PRAGMA application_id").fetchone()[0])
            if path == data.history:
                labels = [
                    r[0] for r in db.execute("SELECT label FROM batches ORDER BY label")
                ]
                result["batches"] = labels
                result["runs"] = db.execute("SELECT count(*) FROM runs").fetchone()[0]
                result["cycles"] = db.execute("SELECT count(*) FROM cycles").fetchone()[
                    0
                ]
                if db.execute(
                    "SELECT 1 FROM runs WHERE outcome='open' LIMIT 1"
                ).fetchone():
                    raise ValueError(
                        "Open Run found; close/recover it in the application first"
                    )
                if ids[0] != APPLICATION_ID:
                    if not labels or not all(
                        re.fullmatch(r"DEV-LOT-[A-F]", label) for label in labels
                    ):
                        raise ValueError(
                            "Target is not recognized commissioning/showcase data; customer records are never replaced"
                        )
                    unknown = {
                        r[0]
                        for r in db.execute("SELECT uuid FROM runs WHERE batch IS NULL")
                    } - {example_id(f"run/{i}") for i in range(12)}
                    if unknown:
                        raise ValueError(
                            "Unknown unbatched activity; refusing replacement"
                        )
                else:
                    first = next(
                        (
                            label
                            for label in labels
                            if re.fullmatch(r"GC\d{6}-01", label)
                        ),
                        "",
                    )
                    if not first:
                        raise ValueError("Unknown showcase Batch provenance")
                    anchor = datetime.strptime(first[2:8], "%y%m%d").date() + timedelta(
                        days=13
                    )
                    expected = schedule(anchor, UTC)
                    if set(db.execute("SELECT uuid,label FROM batches")) != {
                        (b.uuid, b.label) for b in expected.batches
                    } or set(db.execute("SELECT uuid,batch FROM runs")) != {
                        (r.uuid, r.batch) for r in expected.runs
                    }:
                        raise ValueError(
                            "Additional/unknown production records found; refusing replacement"
                        )
            else:
                result["audit_events"] = db.execute(
                    "SELECT count(*) FROM operations"
                ).fetchone()[0]
    if ids not in ([0, 0], [APPLICATION_ID, APPLICATION_ID]):
        raise ValueError("Mixed or unknown database provenance")
    return result


def stage_files(directory: Path, data: ApplicationData) -> dict[str, str]:
    directory = directory.resolve()
    if not directory.is_relative_to(data.root.parent / "outputs"):
        raise ValueError(
            "Use an isolated showcase staged under this checkout's outputs directory"
        )
    staged = ApplicationData.at_entry(directory)
    local_files(staged)
    verify(directory)
    manifest = object_map(
        json.loads((directory / "expected.json").read_text(encoding="utf-8"))
    )
    if manifest["profile"] != "production":
        raise ValueError(
            "The 100-Run stress profile is isolated and cannot be installed"
        )
    runs = [
        object_map(r)
        for r in cast(list[object], object_map(manifest["expected"])["runs"])
    ]
    if (
        min(cast(float, r["started"]) for r in runs) < time() - 29 * 86400
        or max(cast(float, r["ended"]) for r in runs) > time()
    ):
        raise ValueError(
            "Choose an anchor within retention and with no future observations"
        )
    return {
        p.name: digest(p)
        for p in (staged.history, staged.audit, directory / "expected.json")
    }


def preview(data: ApplicationData, directory: Path) -> Path:
    """Bind the exact target and both old/new byte hashes into a reviewable plan."""
    local_files(data)
    with SecurityLease(data.security):
        local_files(data)
        scope = known_development(data)
        staged = stage_files(directory, data)
        plan = {
            "development_only": True,
            "target": str(data.root.resolve()),
            "stage": str(directory.resolve()),
            "old": {p.name: digest(p) for p in (data.history, data.audit)},
            "new": staged,
            "replacing": scope,
            "installing": verify(directory),
            "preserved": "security.sqlite3, credentials, all configuration, models, licenses, images and source files",
        }
        path = directory / "replacement-plan.json"
        path.write_text(
            json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path


def quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def copy_schema(db: sqlite3.Connection, source: str, target: str) -> None:
    """Copy a validated schema inside the caller's transaction (no executescript)."""
    objects = db.execute(
        f"SELECT type,name,sql FROM {source}.sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY type='table' DESC"
    ).fetchall()
    if any(kind not in ("table", "index") for kind, _, _ in objects):
        raise ValueError("Unexpected staged schema object")
    old = db.execute(
        f"SELECT name FROM {target}.sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    for (name,) in old:
        db.execute(f"DROP TABLE {target}.{quoted(name)}")
    for kind, name, sql in objects:
        # SQLite schema-qualified CREATE belongs before the object's name. Our
        # maintained History/Audit DDL uses a plain CREATE TABLE/INDEX prefix.
        qualified, count = re.subn(
            r"^(CREATE\s+(?:UNIQUE\s+)?(?:TABLE|INDEX)\s+(?:IF NOT EXISTS\s+)?)([^\s(]+)",
            lambda m, name=name: m[1] + target + "." + quoted(name),
            sql,
            count=1,
            flags=re.I,
        )
        if count != 1:
            raise ValueError("Unsupported staged DDL")
        db.execute(qualified)
        if kind == "table":
            db.execute(
                f"INSERT INTO {target}.{quoted(name)} SELECT * FROM {source}.{quoted(name)}"
            )
            if (
                db.execute(f"SELECT count(*) FROM {target}.{quoted(name)}").fetchone()
                != db.execute(
                    f"SELECT count(*) FROM {source}.{quoted(name)}"
                ).fetchone()
            ):
                raise ValueError("Incomplete staged table copy")
    version = db.execute(f"PRAGMA {source}.user_version").fetchone()[0]
    db.execute(f"PRAGMA {target}.user_version={int(version)}")
    db.execute(f"PRAGMA {target}.application_id={APPLICATION_ID}")
    if db.execute(f"PRAGMA {target}.foreign_key_check").fetchall() or db.execute(
        f"PRAGMA {target}.integrity_check"
    ).fetchone() != ("ok",):
        raise ValueError("Replacement integrity check failed")


def install(data: ApplicationData, directory: Path, confirmation: str) -> Path:
    plan_path = directory / "replacement-plan.json"
    if digest(plan_path) != confirmation:
        raise ValueError(
            "Explicit confirmation must equal the reviewed replacement-plan SHA-256"
        )
    plan = object_map(json.loads(plan_path.read_text(encoding="utf-8")))
    if (
        plan.get("development_only") is not True
        or plan["target"] != str(data.root.resolve())
        or plan["stage"] != str(directory.resolve())
    ):
        raise ValueError("Preview belongs to a different checkout or staging directory")
    local_files(data)
    with SecurityLease(data.security):
        local_files(data)
        known_development(data)
        if (
            stage_files(directory, data) != plan["new"]
            or {p.name: digest(p) for p in (data.history, data.audit)} != plan["old"]
        ):
            raise ValueError("Databases changed since preview; review a new plan")
        backup = directory / ("before-" + uuid4().hex)
        backup.mkdir()
        for path in (data.history, data.audit):
            copyfile(path, backup / path.name)
        staged = ApplicationData.at_entry(directory)
        # Both writable stores must be on the same local volume and in rollback
        # journal mode: SQLite's super-journal makes the pair atomic after a crash.
        with closing(sqlite3.connect(data.history.as_uri(), timeout=0, uri=True)) as db:
            db.execute("ATTACH DATABASE ? AS audit", (str(data.audit),))
            db.execute(
                "ATTACH DATABASE ? AS staged_history",
                (staged.history.as_uri() + "?mode=ro",),
            )
            db.execute(
                "ATTACH DATABASE ? AS staged_audit",
                (staged.audit.as_uri() + "?mode=ro",),
            )
            for schema in ("main", "audit"):
                if db.execute(f"PRAGMA {schema}.journal_mode=DELETE").fetchone() != (
                    "delete",
                ):
                    raise ValueError("Cannot establish atomic offline replacement")
                db.execute(f"PRAGMA {schema}.synchronous=FULL")
            db.execute("BEGIN EXCLUSIVE")
            try:
                copy_schema(db, "staged_history", "main")
                copy_schema(db, "staged_audit", "audit")
                db.commit()
            except BaseException:
                db.rollback()
                raise
        verify(directory, data)
        return backup
