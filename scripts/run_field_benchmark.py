"""Explicit finite field diagnostic; no UI/Web, no Production PLC trigger API."""

import argparse
import json
from pathlib import Path

from field_benchmark.live import STAGES, execute


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--stage", choices=[s.name for s in STAGES], required=True)
    for name in ("model", "machine", "recipe", "sources"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--live-field-photo", action="store_true", required=True)
    args = parser.parse_args()
    directory = execute(
        args.campaign.resolve(),
        args.stage,
        model=args.model,
        machine=args.machine,
        recipe=args.recipe,
        sources=args.sources,
    )
    return (
        0 if json.loads((directory / "stage-result.json").read_text())["passed"] else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
