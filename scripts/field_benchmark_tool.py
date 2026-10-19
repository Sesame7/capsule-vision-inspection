"""Offline run-file preparation and bounded ledger analysis. No live run command."""

import argparse
import json
from pathlib import Path
from typing import cast

from field_benchmark.analyze import load_rows, reconcile
from field_benchmark.archive_check import verify_archive
from field_benchmark.configuration import Mode, prepare_run
from field_benchmark.measurements import live_measurements


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser(
        "prepare", help="Freeze configuration only; no runtime/SDK/network"
    )
    prepare.add_argument("--mode", choices=list(Mode), required=True)
    for name in ("model", "machine", "recipe", "sources"):
        prepare.add_argument(f"--{name}", type=Path, required=True)
    prepare.add_argument("--root", type=Path, default=Path("outputs/s1-p0b-1/prepared"))
    prepare.add_argument("--simulator-port", type=int)
    analyze = commands.add_parser("analyze")
    analyze.add_argument("directory", type=Path)
    args = parser.parse_args()
    if args.command == "prepare":
        files = prepare_run(
            args.root,
            Mode(args.mode),
            model_file=args.model,
            machine_file=args.machine,
            recipe_file=args.recipe,
            sources_file=args.sources,
            simulator_port=args.simulator_port,
        )
        print(files.root)
        return 0
    directory = cast(Path, args.directory)
    manifest: dict[str, object] = json.loads(
        (directory / "manifest.json").read_text(encoding="utf-8")
    )
    mode = Mode(str(manifest["mode"]))
    owners = (
        (
            "session",
            "source",
            "early_runtime",
            "photo_socket",
            "supervisor_process",
            "target",
        )
        if mode in (Mode.R, Mode.RA)
        else (
            "session",
            "source",
            "early_runtime",
            "photo_socket",
            "supervisor_process",
        )
    )
    if mode in (Mode.R, Mode.RA):
        owners += ("reporter",)
    if mode in (Mode.A, Mode.RA):
        owners += ("archive",)
    if manifest.get("live_field") is True:
        owners += ("application", "resource_sampler")
    paths = sorted(directory.glob("*.jsonl"))
    rows = load_rows(paths)
    summary = reconcile(
        rows,
        str(manifest["run"]),
        reports=mode in (Mode.R, Mode.RA),
        archives=mode in (Mode.A, Mode.RA),
        required_owners=owners,
    )
    issues = cast(list[str], summary["issues"])
    if manifest.get("live_field") is True:
        live_issues, measurements = live_measurements(rows, manifest)
        issues.extend(live_issues)
        summary["live_measurements"] = measurements
    logging_cost: list[dict[str, object]] = []
    if mode in (Mode.A, Mode.RA):
        issues.extend(verify_archive(rows, directory / "archive"))
    for path in paths:
        footer_path = path.with_suffix(".closed.json")
        if not footer_path.exists():
            issues.append(f"ledger termination/footer missing: {path.name}")
            continue
        footer: dict[str, object] = json.loads(footer_path.read_text(encoding="utf-8"))
        logging_cost.append(footer)
        count = sum(row.get("producer") == footer.get("producer") for row in rows)
        if (
            footer.get("run") != manifest["run"]
            or footer.get("terminated") is not True
            or footer.get("error") is not None
            or footer.get("events") != count
        ):
            issues.append(f"ledger incomplete/overflow: {path.name}")
    supervisor_path = directory / "supervisor-final.json"
    if supervisor_path.exists():
        final: dict[str, object] = json.loads(
            supervisor_path.read_text(encoding="utf-8")
        )
        if (
            final.get("run") != manifest["run"]
            or final.get("release_verified") is not True
            or final.get("ledger_ok") is not True
        ):
            issues.append("independent supervisor release/ledger unresolved")
    else:
        issues.append("independent supervisor final release evidence missing")
    summary["evidence_complete"] = not issues
    summary["logging_cost"] = logging_cost
    # Exclusive analysis output preserves previous/partial evidence.
    with (directory / "reconciliation.json").open("x", encoding="utf-8") as stream:
        json.dump(summary, stream, ensure_ascii=False, indent=2)
    print(
        f"Evidence complete: {summary['evidence_complete']}; planned={summary['planned']}, attempted={summary['attempted']}, verified releases={summary['pulse_releases']}"
    )
    for issue in cast(list[str], summary["issues"]):
        print(f"- {issue}")
    return 0 if summary["evidence_complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
