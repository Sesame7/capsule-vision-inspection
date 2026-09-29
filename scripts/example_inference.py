"""Infer one local image through the supported API; no acceptance policy or UI."""

import argparse
import json
from pathlib import Path

from _tool_helpers import decode, result_metadata, run_cli

from vimo_demo.inference import VimoSegmentationEngine


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", type=Path)
    parser.add_argument("image", type=Path)
    args = parser.parse_args()
    image_bgr = decode(args.image)
    with VimoSegmentationEngine(args.model) as engine:
        result = engine.infer(image_bgr)
    print(json.dumps(result_metadata(result), ensure_ascii=False, indent=2))
    print("has_defects means returned regions exist; it is not production NG.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli(main))
