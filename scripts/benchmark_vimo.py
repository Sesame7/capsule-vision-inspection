"""Compare steady-state serial x6 and batch=6 ViMo inference on GPU 0.

Warmup counts complete six-image rounds per mode. Run serial first, then batch.
Only timings and metadata are saved to outputs/vimo_benchmark/; model staging
reuses outputs/vimo_smoke/ and the verified smoke-test setup helpers.
"""

import argparse
import json
import sys
import time
import traceback
from collections.abc import Callable
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import cast

from smoke_test_vimo import (
    configure_regions,
    read_export,
    select_module,
    stage_model,
)


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def load_inputs(model, directory):
    """Validate paths and decode the first six valid images without loading ViMo."""
    model = model.resolve(strict=True)
    directory = directory.resolve(strict=True)
    if not model.is_file() or model.suffix.lower() != ".vimosln":
        raise ValueError(f"Expected a .vimosln file: {model}")
    if not directory.is_dir():
        raise NotADirectoryError(str(directory))

    import cv2
    import numpy as np

    candidates = sorted(
        (
            path
            for path in directory.iterdir()
            if path.is_file()
            and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".bmp"}
        ),
        key=lambda path: path.name.lower(),  # Same ordering as smoke_test_vimo.py.
    )
    paths, images = [], []
    for path in candidates:
        try:
            image = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
            if image is None:
                raise ValueError("OpenCV could not decode the image")
        except (OSError, ValueError, cv2.error) as exc:
            print(
                f"Skipping invalid image {path}: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            continue
        paths.append(path)
        images.append(image)
        if len(images) == 6:
            break
    if len(images) != 6:
        raise ValueError(
            f"Need exactly 6 valid images; found {len(images)} in {directory}"
        )
    if any(image.shape != images[0].shape for image in images):
        shapes = {path.name: image.shape for path, image in zip(paths, images)}
        raise ValueError(
            f"The first 6 valid images must have identical H/W/channels: {shapes}"
        )
    for path, image in zip(paths, images):
        print(f"Input: {path.name}; shape={image.shape}, dtype={image.dtype}, BGR")
    return model, paths, images


def measure_rounds(module, requests, warmup, rounds, batch):
    """Retain responses until the timer stops; release them between rounds."""
    print(f"Warmup: {warmup} complete six-image rounds", flush=True)
    for _ in range(warmup):
        if batch:
            responses = module.run(requests)
        else:
            responses = [module.run(request) for request in requests]
        del responses

    print(f"Measuring {rounds} rounds (no output inside timed loops)...", flush=True)
    timings_ms = []
    for _ in range(rounds):
        if batch:
            started = time.perf_counter()
            responses = module.run(requests)
        else:
            started = time.perf_counter()
            responses = [module.run(request) for request in requests]
        elapsed = time.perf_counter() - started
        timings_ms.append(elapsed * 1000)
        del responses
    return timings_ms


def statistics(timings_ms):
    import numpy as np

    values = np.asarray(timings_ms, dtype=np.float64)
    mean = float(np.mean(values))
    p50, p95, p99 = np.percentile(values, [50, 95, 99], method="linear")
    return {
        "rounds": len(timings_ms),
        "mean_ms": mean,
        "p50_ms": float(p50),
        "p95_ms": float(p95),
        "p99_ms": float(p99),
        "min_ms": float(np.min(values)),
        "max_ms": float(np.max(values)),
        "stddev_ms": float(np.std(values, ddof=0)),
        "batches_per_second": 1000 / mean,
        "images_per_second": 6000 / mean,
        "amortized_ms_per_image": mean / 6,
    }


def print_summary(title, stats):
    print(f"\n{title}\n{'-' * len(title)}")
    print(f"rounds: {stats['rounds']}")
    for name in ("mean", "p50", "p95", "p99", "min", "max", "stddev"):
        print(f"{name}: {stats[name + '_ms']:.3f} ms")
    print(f"batches per second: {stats['batches_per_second']:.3f}")
    print(f"images per second: {stats['images_per_second']:.3f}")
    print(f"amortized milliseconds per image: {stats['amortized_ms_per_image']:.3f}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", type=Path, help="Source .vimosln model (read-only)")
    parser.add_argument(
        "image_dir", type=Path, help="Directory containing at least 6 valid images"
    )
    parser.add_argument(
        "--rounds",
        type=positive_int,
        default=100,
        help="Measured rounds per mode (default: 100)",
    )
    parser.add_argument(
        "--warmup",
        type=positive_int,
        default=20,
        help="Warmup rounds per mode (default: 20)",
    )
    args = parser.parse_args()
    phase = "input validation"
    try:
        model, paths, images = load_inputs(args.model, args.image_dir)
        phase = "import ViMo SDK"
        import smartmore.vimo.inference as vimo

        sdk_versions = {}
        for package in ("smartmore.vimo.inference", "smartmore.vimo.inference.custom"):
            try:
                sdk_versions[package] = version(package)
            except PackageNotFoundError:
                sdk_versions[package] = None
        print(f"ViMo SDK versions: {sdk_versions}")
        phase = "read export metadata and stage model"
        exported = read_export(model.with_name("index.html"))
        runtime_model = stage_model(model)
        # Keep the images and requests alive and reuse them for both modes.
        phase = "construct requests"
        requests = [vimo.Request(image) for image in images]
        result = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "python_version": sys.version,
            "vimo_sdk_versions": sdk_versions,
            "model_path": str(model),
            "model_sha256": runtime_model.parent.name,
            "image_paths": [str(path) for path in paths],
            "image_shape": list(images[0].shape),
            "image_dtype": str(images[0].dtype),
            "device_id": 0,
            "use_gpu": True,
            "images_per_round": 6,
            "warmup_count": args.warmup,
            "round_count": args.rounds,
            "warmup_unit": "complete six-image rounds per mode",
            "execution_order": ["serial_x6", "batch_x6"],
            "timing_scope": "wall-clock module.run calls returning SDK responses; "
            "response release between rounds; all setup, warmup, "
            "image I/O, request construction and output excluded",
            "percentile_method": "numpy.percentile, linear",
            "stddev_ddof": 0,
            "throughput_definition": "1000 / mean_ms batches/s; 6 images per batch",
            "parameter_source": "SDK module defaults with smoke-test region output; "
            "same parameters for both modes; vimo_params.json not loaded",
        }
        phase = "load Solution"
        print("Loading Solution (excluded from benchmark timing)...", flush=True)
        with vimo.Solution(str(runtime_model)) as solution:
            phase = "select segmentation module"
            module_id = select_module(solution.module_info_list, exported, None, vimo)
            result["module_id"] = module_id
            parameters = None
            for mode, max_batch in (("serial_x6", 1), ("batch_x6", 6)):
                phase = f"{mode}: module initialization"
                print(
                    f"\nInitializing {mode}, GPU 0, requested max_batch={max_batch} "
                    "(excluded from timing)...",
                    flush=True,
                )
                with solution.create_module(module_id, True, 0, max_batch) as module:
                    actual_max_batch = module.max_batch_size
                    print(f"Actual max_batch_size: {actual_max_batch}")
                    if actual_max_batch < max_batch:
                        raise RuntimeError(
                            f"{mode} requires max_batch_size >= {max_batch}, "
                            f"but SDK reports {actual_max_batch}"
                        )
                    phase = f"{mode}: configure identical segmentation parameters"
                    if parameters is None:
                        configure_regions(module, vimo)
                        parameters = module.get_params()
                        if not isinstance(parameters, vimo.SegmentationParams):
                            raise TypeError(
                                f"Expected SegmentationParams, got {type(parameters)}"
                            )
                        result["region_output"] = {
                            "with_region_info": parameters.with_region_info,
                            "region_info_flag": int(parameters.region_info_flag),
                            "with_mask": parameters.with_mask,
                            "with_score_map": parameters.with_score_map,
                        }
                    else:
                        if not isinstance(parameters, vimo.SegmentationParams):
                            raise TypeError(
                                f"Expected SegmentationParams, got {type(parameters)}"
                            )
                        set_segmentation_params = cast(
                            Callable[[vimo.SegmentationParams], None], module.set_params
                        )
                        set_segmentation_params(parameters)
                    phase = f"{mode}: warmup and measured rounds"
                    timings = measure_rounds(
                        module,
                        requests,
                        args.warmup,
                        args.rounds,
                        batch=(max_batch == 6),
                    )
                    result[mode] = {
                        "requested_max_batch_size": max_batch,
                        "actual_max_batch_size": actual_max_batch,
                        "raw_round_timings_ms": timings,
                        "statistics": statistics(timings),
                    }
                    phase = f"{mode}: module cleanup"
            phase = "Solution cleanup"

        phase = "compute comparison and save JSON"
        serial = result["serial_x6"]["statistics"]
        batch = result["batch_x6"]["statistics"]
        difference = {
            "mean_percent": (batch["mean_ms"] / serial["mean_ms"] - 1) * 100,
            "p95_percent": (batch["p95_ms"] / serial["p95_ms"] - 1) * 100,
            "formula": "(batch / serial - 1) * 100; negative means shorter batch duration",
        }
        result["batch_vs_serial"] = difference
        finished = datetime.now(timezone.utc)
        result["completed_at"] = finished.isoformat()
        output_dir = Path(__file__).resolve().parents[1] / "outputs" / "vimo_benchmark"
        output_dir.mkdir(parents=True, exist_ok=True)
        output = output_dir / f"benchmark_{finished.strftime('%Y%m%dT%H%M%S_%fZ')}.json"
        with output.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")

        print_summary("Serial x6", serial)
        print_summary("Batch x6", batch)
        print("\nDifference\n----------")
        print(f"batch vs serial mean: {difference['mean_percent']:+.2f} %")
        print(f"batch vs serial P95: {difference['p95_percent']:+.2f} %")
        print(difference["formula"])
        print(
            "Amortized ms/image is not individual-image latency. "
            "Throughput covers measured inference time only."
        )
        print(f"\nSaved: {output}")
        return 0
    # Keep a final CLI boundary for SDK failures while reporting the active phase.
    except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001
        print(f"\n[FAILED] {phase}", file=sys.stderr)
        traceback.print_exc()
        return 130 if isinstance(exc, KeyboardInterrupt) else 1


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="backslashreplace")
    sys.exit(main())
