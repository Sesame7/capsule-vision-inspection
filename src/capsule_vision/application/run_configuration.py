"""Immutable Run evidence; captured by preparation/lifecycle, stored by History."""

import json
import sqlite3
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING

from capsule_vision.communication.reporting import ReportingSelection
from capsule_vision.configuration import (
    ConfigurationError,
    ConfigurationIOError,
    load_camera_sources,
    load_machine_layout,
    load_recipe,
)
from capsule_vision.configuration.communication_preferences import (
    load_communication_preferences,
)

from .audit import AuditTrail, AuditUnavailable, RevisionEvidence

if TYPE_CHECKING:
    from .models import PreparedConfiguration


@dataclass(frozen=True, slots=True)
class RunResource:
    artifact: str
    resource: str
    sha256: str | None
    content: bytes | None = None  # Exact saved bytes, never Model binaries.
    origin: str = "unavailable"  # saved, effective, artifact, unavailable
    revision_id: str | None = None  # None explicitly means association unavailable.


@dataclass(frozen=True, slots=True)
class RunConfiguration:
    effective: bytes  # Canonical JSON of frozen values, independent of file formatting.
    resources: tuple[RunResource, ...]
    complete: bool
    revision_lookup: str  # completed, unavailable, or not_recorded


def capture_prepared(prepared: PreparedConfiguration) -> PreparedConfiguration:
    """Read once and parse those same bytes against the frozen prepared definitions.

    Called before candidate publication, never during Qt dispatch or after activation.
    The ordinary parsed-value freshness checks remain independent of file formatting.
    """
    evidence: list[RevisionEvidence] = []
    try:
        recipe = prepared.recipe_dependencies
        if recipe is not None:
            entry = recipe.entry
            content = entry.path.read_bytes()
            if (
                load_recipe(
                    entry.path, process_catalog=entry.process_catalog, content=content
                )
                != entry.recipe
            ):
                raise ConfigurationError("Recipe changed during configuration capture")
            evidence.append(RevisionEvidence("recipe", entry.recipe.recipe_id, content))
        vision = prepared.vision_dependencies
        if vision is not None:
            content = vision.machine_path.read_bytes()
            if (
                load_machine_layout(vision.machine_path, content=content)
                != vision.machine
            ):
                raise ConfigurationError(
                    "MachineLayout changed during configuration capture"
                )
            evidence.append(RevisionEvidence("machine", "machine.tsv", content))
            if vision.sources_path is not None and vision.sources is not None:
                content = vision.sources_path.read_bytes()
                if (
                    load_camera_sources(vision.sources_path, content=content)
                    != vision.sources
                ):
                    raise ConfigurationError(
                        "CameraSources changed during configuration capture"
                    )
                evidence.append(
                    RevisionEvidence("sources", "camera_sources.json", content)
                )
    except OSError as exc:
        raise ConfigurationIOError(
            f"Cannot capture prepared configuration: {exc}"
        ) from exc
    return replace(prepared, saved_content=tuple(evidence))


def capture_run(
    prepared: PreparedConfiguration,
    reporting: ReportingSelection | None,
    communication: bytes | None,
    audit: AuditTrail | None,
) -> RunConfiguration:
    """Lifecycle-only, before Run admission. No mutable file reads or audit writes."""
    config = prepared.configuration
    vision = prepared.vision_dependencies
    sources = vision.sources if vision is not None else None
    values = {
        "inspection": asdict(config),
        "sources": asdict(sources) if sources is not None else None,
        "source_base": str(vision.sources_path.resolve().parent)
        if vision is not None and vision.sources_path is not None
        else None,
        "process_catalog": asdict(prepared.recipe_dependencies.entry.process_catalog)
        if prepared.recipe_dependencies is not None
        else None,
        "reporting": {
            "enabled": reporting.enabled,
            "settings": {
                **asdict(reporting.settings),
                "protocol": reporting.settings.endpoint.protocol,
                "mode": reporting.settings.mode.value,
            },
        }
        if reporting is not None
        else None,
    }

    def encode(value: object) -> bytes:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")

    effective = encode(values)
    evidence = prepared.saved_content
    if (
        communication is not None
        and load_communication_preferences(
            Path("communication.json"), content=communication
        ).reporting
        == reporting
    ):
        evidence += (
            RevisionEvidence("communication", "communication.json", communication),
        )
    lookup = "unavailable"
    matches: tuple[str | None, ...] = (None,) * len(evidence)
    if audit is not None:
        try:
            matches = audit.match_run_revisions(evidence).result()
        except AuditUnavailable:
            pass  # Missing revision association never invents or confirms a Save.
        else:
            lookup = "completed"
    saved = {
        row.artifact: RunResource(
            row.artifact, row.resource, row.sha256, row.content, "saved", revision
        )
        for row, revision in zip(evidence, matches, strict=True)
    }
    resources: list[RunResource] = []
    for artifact, resource, value in (
        ("recipe", config.recipe_id, values["inspection"]),
        ("machine", "machine.tsv", asdict(config.layout)),
        ("sources", "camera_sources.json", values["sources"]),
        ("communication", "communication.json", values["reporting"]),
    ):
        content = encode(value) if value is not None else None
        resources.append(
            saved.get(
                artifact,
                RunResource(
                    artifact,
                    resource,
                    sha256(content).hexdigest() if content is not None else None,
                    content,
                    "effective" if content is not None else "unavailable",
                ),
            )
        )
    resources.append(
        RunResource(
            "model",
            config.model.model_id,
            config.model.expected_sha256,
            origin="artifact",
        )
    )
    return RunConfiguration(
        effective,
        tuple(sorted(resources, key=lambda row: row.artifact)),
        sources is not None and reporting is not None,
        lookup,
    )


SCHEMA = """
CREATE TABLE run_configurations (
    run TEXT PRIMARY KEY REFERENCES runs ON DELETE CASCADE,
    effective BLOB NOT NULL, complete INTEGER NOT NULL, revision_lookup TEXT NOT NULL
);
CREATE TABLE run_configuration_resources (
    run TEXT NOT NULL REFERENCES run_configurations ON DELETE CASCADE,
    artifact TEXT NOT NULL, resource TEXT NOT NULL, sha256 TEXT,
    content BLOB, origin TEXT NOT NULL, revision_id TEXT, PRIMARY KEY(run,artifact)
);
CREATE TRIGGER run_configuration_immutable BEFORE UPDATE ON run_configurations
    BEGIN SELECT RAISE(ABORT,'Immutable Run configuration'); END;
CREATE TRIGGER run_resource_immutable BEFORE UPDATE ON run_configuration_resources
    BEGIN SELECT RAISE(ABORT,'Immutable Run resource'); END;
"""


def store(db: sqlite3.Connection, run: str, manifest: RunConfiguration | None) -> None:
    # Trusted engineering producers can lack captured inputs. Record that absence,
    # rather than deriving evidence from current files or legacy Cycle rows.
    value = manifest or RunConfiguration(b"{}", (), False, "not_recorded")
    db.execute(
        "INSERT INTO run_configurations VALUES (?,?,?,?)",
        (run, value.effective, value.complete, value.revision_lookup),
    )
    db.executemany(
        "INSERT INTO run_configuration_resources VALUES (?,?,?,?,?,?,?)",
        (
            (run, r.artifact, r.resource, r.sha256, r.content, r.origin, r.revision_id)
            for r in value.resources
        ),
    )


def read(db: sqlite3.Connection, run: str) -> RunConfiguration | None:
    row = db.execute(
        "SELECT effective,complete,revision_lookup FROM run_configurations WHERE run=?",
        (run,),
    ).fetchone()
    if row is None:
        return None
    resources = tuple(
        RunResource(*r)
        for r in db.execute(
            "SELECT artifact,resource,sha256,content,origin,revision_id FROM run_configuration_resources WHERE run=? ORDER BY artifact",
            (run,),
        )
    )
    for resource in resources:
        if (
            resource.content is not None
            and sha256(resource.content).hexdigest() != resource.sha256
        ):
            raise ValueError("Run configuration content hash mismatch")
    return RunConfiguration(row[0], resources, bool(row[1]), row[2])
