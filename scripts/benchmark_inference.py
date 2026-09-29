"""Measure serial engine.infer(), including owned response normalization/copies.

One round visits the entire fixed image pool. These timings have a different
scope from historical raw SDK timings and must not be treated as equivalent.
"""

import argparse
import json
import time
from pathlib import Path

import numpy as np
from _tool_helpers import (
    engine_metadata,
    load_pool,
    nonnegative_int,
    output_directory,
    positive_int,
    run_cli,
    statistics,
    write_json,
)

from vimo_demo.inference import VimoSegmentationEngine


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", type=Path)
    parser.add_argument("images", type=Path)
    parser.add_argument("--pool-size", type=positive_int, default=6)
    parser.add_argument(
        "--warmup", type=nonnegative_int, default=20, help="Individual calls, excluded"
    )
    parser.add_argument(
        "--rounds", type=positive_int, default=100, help="Visits to the entire pool"
    )
    args = parser.parse_args()
    pool = load_pool(args.images, args.pool_size)
    timings = np.empty(args.rounds * len(pool), dtype=np.float64)
    with VimoSegmentationEngine(args.model) as engine:
        metadata = engine_metadata(engine.info)
        for index in range(args.warmup):
            engine.infer(pool[index % len(pool)][1])
        for index in range(timings.size):
            image = pool[index % len(pool)][1]
            started = time.perf_counter()
            result = engine.infer(image)
            timings[index] = (time.perf_counter() - started) * 1000
            del result  # Release outside timing, before the next call.
    stats = statistics(timings)
    report = {
        **metadata,
        "warmup_calls": args.warmup,
        "rounds": args.rounds,
        "pool": [path.name for path, _ in pool],
        "statistics": stats,
        "timings_ms": timings.tolist(),
        "scope": __doc__,
    }
    destination = output_directory("benchmark") / "benchmark.json"
    write_json(destination, report)
    print(json.dumps(stats, indent=2))
    print(f"Wrapper timings include normalization/copies. JSON: {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli(main))
