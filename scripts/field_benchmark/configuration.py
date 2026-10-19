"""Freeze definitions into exclusive per-run files; saved endpoints are never read."""

import hashlib
import json
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from vimo_demo.communication.reporting import LocalReportingSettings
from vimo_demo.configuration import (
    ImageArchiveSettings,
    ModelRegistry,
    load_camera_sources,
    load_machine_layout,
    load_model_definition,
    load_recipe,
    resolve_configuration,
    save_camera_sources,
    save_machine_layout,
    save_model_definition,
    save_recipe,
    validate_model_artifact,
)


class Mode(StrEnum):
    B = "B"
    R = "R"
    A = "A"
    RA = "RA"


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


@dataclass(frozen=True, slots=True)
class RunFiles:
    run: str
    root: Path
    model: Path
    machine: Path
    recipe: Path
    sources: Path
    reporting: LocalReportingSettings | None


def prepare_run(
    root: Path,
    mode: Mode,
    *,
    model_file: Path,
    machine_file: Path,
    recipe_file: Path,
    sources_file: Path,
    simulator_port: int | None = None,
) -> RunFiles:
    reports, archives = mode in (Mode.R, Mode.RA), mode in (Mode.A, Mode.RA)
    if reports and (simulator_port is None or not 1 <= simulator_port <= 65535):
        raise ValueError("Reporting requires an explicit isolated UDP simulator port")
    model = load_model_definition(model_file)
    machine = load_machine_layout(machine_file)
    recipe = load_recipe(recipe_file)
    sources = load_camera_sources(sources_file)
    sources.basler_serials(machine.layout.camera_ids)
    resolved = resolve_configuration(
        machine, recipe, ModelRegistry((model,)), model.model_id
    )
    artifact = validate_model_artifact(
        resolved.model, base_directory=model_file.resolve().parent
    ).resolve()
    originals = (model_file, machine_file, recipe_file, sources_file)
    before = {str(p.resolve()): digest(p) for p in originals}
    run = uuid4().hex
    directory = root.resolve() / f"{mode}-{run}"
    directory.mkdir(parents=True, exist_ok=False)
    config = directory / "configuration"
    config.mkdir()
    archive_root = directory / "archive"
    effective_recipe = replace(
        recipe,
        image_archive=ImageArchiveSettings(
            str(archive_root),
            archives,
            archives,
            archives,
            archives,
        ),
    )
    effective_sources = replace(
        sources,
        cameras=tuple(
            replace(
                row,
                folder=str(row.path(sources_file.resolve().parent).resolve())
                if row.folder
                else "",
            )
            for row in sources.cameras
        ),
    )
    paths = tuple(
        config / name
        for name in (
            "model.json",
            "machine.json",
            "run.recipe.json",
            "camera_sources.json",
        )
    )
    save_model_definition(paths[0], replace(model, model_path=str(artifact)))
    save_machine_layout(paths[1], machine)
    save_recipe(paths[2], effective_recipe)
    save_camera_sources(paths[3], effective_sources)
    if before != {str(p.resolve()): digest(p) for p in originals}:
        raise RuntimeError(
            "Saved definitions changed while freezing run; do not execute"
        )
    reporting = (
        LocalReportingSettings("127.0.0.1", simulator_port)
        if reports and simulator_port
        else None
    )
    with (directory / "manifest.json").open("x", encoding="utf-8") as stream:
        json.dump(
            {
                "run": run,
                "mode": mode,
                "scope": "backend only; no Qt UI/Web",
                "source_hashes": before,
                "effective_hashes": {p.name: digest(p) for p in paths},
                "model_sha256": model.expected_sha256,
                "module": model.module_id,
                "camera_ids": machine.layout.camera_ids,
                "serials": sources.basler_serials(machine.layout.camera_ids),
                "reporting": {
                    "transport": "udp",
                    "host": "127.0.0.1",
                    "port": simulator_port,
                }
                if reports
                else None,
                "archive_root": str(archive_root) if archives else None,
                "limits": {
                    "early_cycles": 4,
                    "early_deadline_ms": 100,
                    "report_pending": 8,
                    "archive_pending": 12,
                    "archive_raw_bytes": 128 * 1024 * 1024,
                },
                "readiness": "not executed or qualified",
            },
            stream,
            ensure_ascii=False,
            indent=2,
        )
    return RunFiles(run, directory, paths[0], paths[1], paths[2], paths[3], reporting)
