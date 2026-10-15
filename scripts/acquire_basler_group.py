"""One controlled external-trigger Basler attempt; never operates HMI/PLC outputs."""

import argparse
import hashlib
import json
import traceback
from dataclasses import asdict

from vimo_demo.application.basler import (
    AcquisitionReport,
    ArmedAcquisition,
    BaslerAcquisitionError,
    BaslerAcquisitionSource,
)


def armed_notice(armed: ArmedAcquisition) -> None:
    print(
        json.dumps({"event": "ARMED", **asdict(armed)}, ensure_ascii=False), flush=True
    )
    print(
        "Perform ONE manual flash+photo press-and-release now; do not press again.",
        flush=True,
    )


def print_report(report: AcquisitionReport) -> None:
    print(
        json.dumps(
            {
                "event": "report",
                "serials": report.serials,
                "started_at": report.started_at,
                "finished_at": report.finished_at,
                "armed": asdict(report.armed) if report.armed is not None else None,
                "settings": [
                    {
                        "serial": row.serial,
                        "features": dict(row.features),
                        "triggers": {k: dict(v) for k, v in row.triggers.items()},
                    }
                    for row in report.settings
                ],
                "observations": [asdict(row) for row in report.observations],
                "missing_serials": report.missing_serials,
                "extra_serials": report.extra_serials,
                "issues": [asdict(row) for row in report.issues],
                "cleanup": [asdict(row) for row in report.cleanup],
                "cleanup_safe": report.cleanup_safe,
                "association": "Requires independent single-trigger evidence; not a Production CycleId",
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--serials", nargs=6, required=True, help="Six explicit serials in output order"
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="Observation window after ARMED, seconds (0,120]",
    )
    parser.add_argument(
        "--operator-confirmed",
        action="store_true",
        help="Operator ready for one manual action after ARMED; not proof of pulse count",
    )
    args = parser.parse_args()
    if not args.operator_confirmed:
        parser.error(
            "Operator confirmation required before acquisition; no automatic triggers"
        )
    try:
        source = BaslerAcquisitionSource(args.serials, timeout=args.timeout)
        group = source.acquire(on_armed=armed_notice)
    except BaslerAcquisitionError as exc:
        print_report(exc.report)
        traceback.print_exc()
        return 1
    except ValueError as exc:
        parser.error(str(exc))
    print_report(group.report)
    for image in group.images:
        print(
            json.dumps(
                {
                    "event": "owned_image",
                    "serial": image.serial,
                    "shape": image.pixels.shape,
                    "dtype": str(image.pixels.dtype),
                    "c_contiguous": image.pixels.flags.c_contiguous,
                    "owndata": image.pixels.flags.owndata,
                    "writeable": image.pixels.flags.writeable,
                    "sha256": hashlib.sha256(
                        memoryview(image.pixels).cast("B")
                    ).hexdigest(),
                }
            ),
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
