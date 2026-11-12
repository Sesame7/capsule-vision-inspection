"""Independent synthetic expectations and atomic offline development replacement."""

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from dataclasses import replace
from datetime import UTC, date, timedelta, timezone
from pathlib import Path
from shutil import copytree
from typing import cast
from zipfile import ZipFile

import development_replacement as replacement
import pytest
from development_records import active_configuration
from development_showcase import (
    APPLICATION_ID,
    build,
    digest,
    group_plan,
    model_labels,
    object_map,
    schedule,
    verify,
)
from tests.support.startup import saved_data

from capsule_vision.application._security_lock import SecurityLease
from capsule_vision.application.audit import (
    AuditOperation,
    AuditQuery,
    AuditTrail,
    RevisionEvidence,
)
from capsule_vision.application.history import ProductionHistory
from capsule_vision.application.identity_models import SecurityBusyError
from capsule_vision.application.startup import ApplicationData
from capsule_vision.configuration import (
    load_model_definition,
    load_recipe,
    save_model_definition,
    save_recipe,
)
from capsule_vision.inspection.production.capsule import (
    CapsuleSettings,
    ColorSettings,
    SizeSettings,
)


def source(root: Path, cameras: int = 6) -> ApplicationData:
    data = saved_data(root, camera_count=cameras)
    model_path = data.models / "general/model.json"
    definition = load_model_definition(model_path)
    artifact = model_path.parent / "model.vimosln"
    with ZipFile(artifact, "w") as archive:
        archive.writestr(
            "manifest",
            json.dumps(
                {
                    "product": {
                        "vimo_cloud": {
                            "module_infos": [
                                {
                                    "id": definition.module_id,
                                    "features": [
                                        "Spot",
                                        "Dent",
                                        "Crack",
                                        "Deformation",
                                    ],
                                }
                            ]
                        }
                    }
                }
            ),
        )
    save_model_definition(
        model_path, replace(definition, expected_sha256=digest(artifact))
    )
    recipe_path = data.recipes / "local.json"
    save_recipe(
        recipe_path,
        replace(
            load_recipe(recipe_path),
            display_name="Yellow",
            capsule=CapsuleSettings(
                SizeSettings(True, 10, 1), ColorSettings(True, 14, 1)
            ),
        ),
    )
    return data


def compact_plan():
    plan = schedule(date.today() - timedelta(days=1), timezone(timedelta(hours=8)))
    return replace(
        plan, runs=tuple(replace(r, groups=min(20, r.groups)) for r in plan.runs)
    )


@pytest.fixture(scope="module")
def staged(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("showcase")
    build(source(root / "source"), root / "stage", compact_plan())
    return root / "stage"


def manifest(directory: Path) -> dict[str, object]:
    return object_map(
        json.loads((directory / "expected.json").read_text(encoding="utf-8"))
    )


@pytest.mark.parametrize("cameras", [5, 6])
def test_scenario_repeats_with_independent_totals(tmp_path: Path, cameras: int) -> None:
    data = source(tmp_path / "source", cameras)
    before = {
        str(p.relative_to(data.root)): digest(p)
        for p in data.root.rglob("*")
        if p.is_file()
    }
    for name in ("first", "second"):
        build(data, tmp_path / name, compact_plan())
        assert verify(tmp_path / name) == {
            "batches": 10,
            "runs": 25,
            "groups": 492,
            "cycles": 1968,
            "audit": 147,
            "alarms": 8,
        }
    assert (tmp_path / "first/expected.json").read_bytes() == (
        tmp_path / "second/expected.json"
    ).read_bytes()
    assert before == {
        str(p.relative_to(data.root)): digest(p)
        for p in data.root.rglob("*")
        if p.is_file()
    }
    expected = object_map(manifest(tmp_path / "first")["expected"])
    batches = [object_map(b) for b in cast(list[object], expected["batches"])]
    runs = [object_map(r) for r in cast(list[object], expected["runs"])]
    assert len({r["username"] for r in runs}) == 3
    assert sum(r["outcome"] == "interrupted" for r in runs) == 1
    assert sum(r["batch"] is None for r in runs) == 1
    categories = {
        key for b in batches for key in object_map(object_map(b["totals"])["positions"])
    }
    assert categories >= {
        "vimo/Spot",
        "vimo/Dent",
        "vimo/Crack",
        "vimo/Deformation",
        "traditional/capsule.size.upper.length",
        "traditional/capsule.color.lower.tolerance",
    }
    assert (
        sum(
            cast(int, object_map(b["totals"])["multi_vimo_label_positions"])
            for b in batches
        )
        > 0
    )
    for batch in batches:
        for key in ("ok", "ng", "inspected", "empty", "groups"):
            assert object_map(batch["totals"])[key] == sum(
                cast(int, object_map(r["totals"])[key])
                for r in runs
                if r["batch"] == batch["uuid"]
            )


def test_labels_and_optional_checks_are_grounded_in_configuration(
    tmp_path: Path,
) -> None:
    data = source(tmp_path)
    config, _ = active_configuration(data)
    labels = model_labels(config, data.models)
    plan = compact_plan()
    hits = [
        h
        for i in range(100)
        for h in group_plan(plan.runs[7], i, 36, labels, size=False, color=True).hits
    ]
    assert hits and all(".size." not in h.category for h in hits)
    with pytest.raises(ValueError, match="two enabled"):
        model_labels(
            replace(
                config,
                checks=replace(
                    config.checks, vimo=replace(config.checks.vimo, enabled=False)
                ),
            ),
            data.models,
        )
    artifact = data.models / config.model.model_path
    with ZipFile(artifact, "w") as archive:
        archive.writestr("manifest", '{"product":{"vimo_cloud":{"module_infos":[]}}}')
    with pytest.raises(ValueError, match="offline label catalog"):
        model_labels(config, data.models)


def test_oracle_detects_bad_projection_and_preserves_compact_boundary_fixtures(
    staged: Path, tmp_path: Path
) -> None:
    copytree(staged, tmp_path / "copy")
    with closing(sqlite3.connect(tmp_path / "copy/data/history.sqlite3")) as db, db:
        db.execute(
            "UPDATE run_positions SET positions=json_set(positions,'$.channel_ng',0)"
        )
    with pytest.raises(ValueError, match="independent manifest"):
        verify(tmp_path / "copy")


def test_alarm_observations_support_each_transition(staged: Path) -> None:
    expected = object_map(manifest(staged)["expected"])
    reads = [object_map(r) for r in cast(list[object], expected["alarm_reads"])]
    for value in cast(list[object], expected["alarms"]):
        event = object_map(value)
        reading = next(r for r in reads if r["observed"] == event["observed"])
        assert (reading["batch"], reading["label"]) == (
            event["batch_id"],
            event["batch_label"],
        )
        # These template signals are active-high; no inferred transition during the gap.
        active = bool(cast(int, reading["word"]) & (1 << cast(int, event["bit"])))
        assert active == (event["state"] in ("activated", "observed_active"))
        if event["state"] == "observed_recovered":
            assert any(
                r["word"] is None
                and cast(float, r["observed"]) < cast(float, event["observed"])
                for r in reads
            )


def test_replacement_rejects_new_production_in_a_showcase_store(
    staged: Path, tmp_path: Path
) -> None:
    target, directory = install_fixture(tmp_path, staged)
    with closing(sqlite3.connect(target.history)) as db, db:
        db.execute("UPDATE runs SET uuid='later-production' WHERE rowid=1")
    with pytest.raises(ValueError, match="Additional/unknown"):
        replacement.preview(target, directory)


def test_installation_requires_recent_complete_observations(
    staged: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, directory = install_fixture(tmp_path, staged)
    runs = cast(
        list[dict[str, object]], object_map(manifest(staged)["expected"])["runs"]
    )
    for now in (
        cast(float, runs[0]["started"]) - 1,
        cast(float, runs[-1]["ended"]) + 31 * 86400,
    ):
        monkeypatch.setattr(replacement, "time", lambda now=now: now)
        with pytest.raises(ValueError, match="anchor"):
            replacement.preview(target, directory)


def test_stress_is_separate_and_has_100_real_run_summaries(tmp_path: Path) -> None:
    plan = schedule(date.today() - timedelta(days=1), UTC, stress=True)
    build(source(tmp_path / "source"), tmp_path / "stress", plan)
    counts = verify(tmp_path / "stress")
    assert (counts["batches"], counts["runs"], counts["groups"], counts["cycles"]) == (
        1,
        100,
        200,
        800,
    )
    assert counts["alarms"] == 0
    with closing(sqlite3.connect(tmp_path / "stress/data/history.sqlite3")) as db:
        assert db.execute(
            "SELECT count(*) FROM run_positions WHERE positions IS NOT NULL"
        ).fetchone() == (100,)


def install_fixture(tmp_path: Path, staged: Path) -> tuple[ApplicationData, Path]:
    target = ApplicationData.at_entry(tmp_path)
    copytree(staged / "data", target.root)
    target.security.write_bytes(b"protected credential evidence")
    # Previous recognized synthetic store, distinct from the incoming dataset.
    with closing(sqlite3.connect(target.audit)) as db, db:
        db.execute("UPDATE completions SET detail='previous synthetic records'")
    directory = tmp_path / "outputs/new"
    copytree(staged, directory)
    return target, directory


def logical_pair(data: ApplicationData) -> tuple[tuple[str, ...], ...]:
    result: list[tuple[str, ...]] = []
    for path in (data.history, data.audit):
        with closing(sqlite3.connect(path)) as db:
            result.append(tuple(db.iterdump()))
    return tuple(result)


def test_install_requires_exact_preview_and_preserves_credentials(
    staged: Path, tmp_path: Path
) -> None:
    target, directory = install_fixture(tmp_path, staged)
    before = logical_pair(target)
    plan = replacement.preview(target, directory)
    with pytest.raises(ValueError, match="confirmation"):
        replacement.install(target, directory, "incorrect")
    backup = replacement.install(target, directory, digest(plan))
    assert target.security.read_bytes() == b"protected credential evidence"
    assert verify(directory, target) == verify(staged)
    for name, old in zip(("history.sqlite3", "audit.sqlite3"), before, strict=True):
        with closing(sqlite3.connect(backup / name)) as db:
            assert tuple(db.iterdump()) == old
    with closing(sqlite3.connect(target.audit)) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (2,)
        assert set(
            db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        ) == {("revision_immutable_update",), ("revision_immutable_delete",)}


def test_development_replacement_cannot_remove_configuration_revisions(
    staged: Path, tmp_path: Path
) -> None:
    target, directory = install_fixture(tmp_path, staged)
    audit = AuditTrail(target.audit, lambda _: 1)
    operation = AuditOperation("engineer", "Engineer", "recipe.save", "recipe")
    try:
        audit.intent(
            operation, RevisionEvidence("recipe", "recipe", b"retained definition")
        ).result()
        audit.complete(operation, "success").result()
    finally:
        audit.close()
    before = target.audit.read_bytes()
    with pytest.raises(ValueError, match="Configuration revisions present"):
        replacement.preview(target, directory)
    assert target.audit.read_bytes() == before


@pytest.mark.parametrize("change", ["target", "stage", "root", "customer", "redirect"])
def test_replacement_refuses_changed_or_unknown_targets(
    staged: Path, tmp_path: Path, change: str
) -> None:
    target, directory = install_fixture(tmp_path, staged)
    plan = replacement.preview(target, directory)
    confirmation = digest(plan)
    if change == "target":
        with closing(sqlite3.connect(target.audit)) as db, db:
            db.execute("UPDATE completions SET detail='changed after preview'")
    elif change == "stage":
        (directory / "expected.json").write_text(
            (directory / "expected.json").read_text(encoding="utf-8") + "\n",
            encoding="utf-8",
        )
    elif change == "root":
        target = ApplicationData.at_entry(tmp_path / "other")
    elif change == "customer":
        with closing(sqlite3.connect(target.history)) as db, db:
            db.execute("PRAGMA application_id=0")
    else:
        os.link(target.audit, tmp_path / "alias.sqlite3")
    with pytest.raises(ValueError):
        replacement.install(target, directory, confirmation)


@pytest.mark.parametrize("owner", ["lease", "history", "audit"])
def test_replacement_refuses_even_idle_owners(
    staged: Path, tmp_path: Path, owner: str
) -> None:
    target, directory = install_fixture(tmp_path, staged)
    if owner == "lease":
        with SecurityLease(target.security), pytest.raises(SecurityBusyError):
            replacement.preview(target, directory)
    elif owner == "history":
        history = ProductionHistory(target.history)
        try:
            history.initial_selection.result()
            with pytest.raises(ValueError, match="storage owners"):
                replacement.preview(target, directory)
        finally:
            history.request_stop()
            history.join()
    else:
        audit = AuditTrail(target.audit, lambda _: 1)
        try:
            audit.page(AuditQuery()).result()
            with pytest.raises(ValueError, match="storage owners"):
                replacement.preview(target, directory)
        finally:
            audit.close()


@pytest.mark.parametrize("crash", [False, True])
def test_failed_or_interrupted_copy_never_installs_half_a_pair(
    staged: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crash: bool
) -> None:
    target, directory = install_fixture(tmp_path, staged)
    plan = replacement.preview(target, directory)
    before = logical_pair(target)
    confirmation = digest(plan)
    if crash:
        program = """
import os,sys
from pathlib import Path
sys.path.insert(0,'scripts')
import development_replacement as r
from capsule_vision.application.startup import ApplicationData
original=r.copy_schema
def crash_copy(db,source,target):
    original(db,source,target)
    os._exit(77)
r.copy_schema=crash_copy
r.install(ApplicationData.at_entry(Path(sys.argv[1])),Path(sys.argv[2]),sys.argv[3])
"""
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                program,
                str(tmp_path),
                str(directory),
                confirmation,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert completed.returncode == 77, completed.stderr
    else:
        original = replacement.copy_schema

        def failing_copy(db: sqlite3.Connection, source: str, target: str) -> None:
            original(db, source, target)
            raise OSError("simulated interruption before Audit copy")

        monkeypatch.setattr(replacement, "copy_schema", failing_copy)
        with pytest.raises(OSError, match="simulated"):
            replacement.install(target, directory, confirmation)
    assert logical_pair(target) == before
    assert target.security.read_bytes() == b"protected credential evidence"
    with closing(sqlite3.connect(target.history)) as db:
        assert db.execute("PRAGMA application_id").fetchone() == (APPLICATION_ID,)
