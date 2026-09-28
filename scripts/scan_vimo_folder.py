"""Survey a folder using one serial ViMo GPU module; this is not a benchmark.

Survey proxy only: no returned defect regions = OK; one or more = NG.
This is not a production acceptance rule. Source images are never changed.
"""

import argparse
import csv
import hashlib
import json
import platform
import sys
import time
import traceback
from collections import Counter
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, cast

from benchmark_vimo import positive_int
from smoke_test_vimo import configure_regions, read_export, select_module, stage_model

EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}
CSV_LABELS = {
    "black_spot_regions": "黑点",
    "dent_regions": "压痕",
    "damage_regions": "破损",
    "deformation_regions": "变形",
}
SURVEY_RULE = "Survey proxy only: OK = no returned defect regions; NG = one or more. Not a production acceptance rule."


def validate_paths(model, directory):
    model = model.resolve(strict=True)
    directory = directory.resolve(strict=True)
    if not model.is_file() or model.suffix.lower() != ".vimosln":
        raise ValueError(f"Expected a .vimosln file: {model}")
    if not directory.is_dir():
        raise NotADirectoryError(str(directory))
    return model, directory


def discover_images(directory, recursive):
    paths = directory.rglob("*") if recursive else directory.iterdir()
    return sorted(
        (p for p in paths if p.is_file() and p.suffix.lower() in EXTENSIONS),
        key=lambda p: (
            p.relative_to(directory).as_posix().lower(),
            p.relative_to(directory).as_posix(),
        ),
    )


def exception_details(exc, vimo=None):
    details: dict[str, str | int] = {
        "type": f"{type(exc).__module__}.{type(exc).__qualname__}",
        "message": str(exc),
        "traceback": "".join(traceback.format_exception(exc)),
    }
    if vimo is not None and isinstance(exc, vimo.VimoException):
        details["sdk_code"] = int(exc.code)
        details["sdk_message"] = exc.message
    return details


def record_failure(record, status, exc, vimo=None):
    record["status"] = status
    record["error"] = exception_details(exc, vimo)
    message = " ".join(str(exc).split())
    print(
        f"[WARN] {record['relative_path']} [{status}] {type(exc).__name__}: {message}",
        file=sys.stderr,
        flush=True,
    )


def scan_images(module, paths, records, warmup, vimo, cv2, np, report):
    warmed = warmup == 0
    for index, (path, record) in enumerate(zip(paths, records), 1):
        record["status"] = "processing"
        try:
            # Windows Unicode paths and 3-channel BGR, as in the verified scripts.
            image = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
            if image is None:
                raise ValueError("OpenCV could not decode the image")
        except (OSError, ValueError, cv2.error) as exc:
            record_failure(record, "unreadable", exc)
            continue
        record["decoded"] = True
        record["image_shape"] = list(image.shape)
        phase = "request"
        response = None
        try:
            request = vimo.Request(image)
            if not warmed:
                phase = "warmup"
                print(
                    f"Warmup on {record['relative_path']}: {warmup} calls (excluded)",
                    flush=True,
                )
                for _ in range(warmup):
                    module.run(request)
                    report["warmup_calls_completed"] += 1
                warmed = True
            phase = "inference"
            started = time.perf_counter()
            response = module.run(request)
            record["inference_ms"] = (time.perf_counter() - started) * 1000
            record["inference_succeeded"] = True
            phase = "result"
            if not isinstance(response, vimo.SegmentationResponse):
                raise TypeError(f"Expected SegmentationResponse, got {type(response)}")
            regions = [
                {
                    "label": region.label,
                    "area": int(region.area),
                    "bounding_rect": [int(value) for value in region.bounding_rect],
                }
                for region in response.region_infos
            ]
            counts = dict(
                sorted(Counter(region["label"] for region in regions).items())
            )
            record.update(
                status="success",
                ok_ng="NG" if regions else "OK",
                label_map={
                    label: int(code) for label, code in response.label_map.items()
                },
                total_regions=len(regions),
                regions_per_label=counts,
                regions=regions,
            )
        except Exception as exc:
            record_failure(record, f"{phase}_error", exc, vimo)
            # Result formatting errors do not affect the GPU module. SDK input
            # rejections can continue; unknown/runtime/license errors cannot be
            # assumed to leave the module usable. No reset/retry is attempted.
            input_error = isinstance(exc, vimo.VimoException) and getattr(
                exc, "code", None
            ) in (
                vimo.ErrorCode.INVALID_ARGUMENT,
                vimo.ErrorCode.INVALID_IMAGE_DATA,
            )
            request_error = phase == "request" and isinstance(
                exc, (ValueError, TypeError)
            )
            if phase != "result" and not input_error and not request_error:
                raise
        finally:
            response = None  # Do not retain masks or contours across images.
        if index % 100 == 0 or index == len(paths):
            print(f"Processed {index}/{len(paths)} selected files", flush=True)


def summarize(records, known_labels):
    successful = [r for r in records if r["status"] == "success"]
    states = Counter(r["status"] for r in records)
    ok = sum(r["ok_ng"] == "OK" for r in successful)
    ng = len(successful) - ok
    image_counts, region_counts = Counter(), Counter()
    samples = {"OK": [], "NG": [], "per_detected_label": {}}
    for record in successful:
        known_labels.update(record["label_map"])
        image_counts.update(record["regions_per_label"].keys())
        region_counts.update(record["regions_per_label"])
        bucket = samples[record["ok_ng"]]
        if len(bucket) < 10:
            bucket.append(record["filename"])
        for label in record["regions_per_label"]:
            bucket = samples["per_detected_label"].setdefault(label, [])
            if len(bucket) < 5:
                bucket.append(record["filename"])
    timings = [r["inference_ms"] for r in successful]
    timing_stats = None
    if timings:
        import numpy as np

        timing_stats = {
            "count": len(timings),
            "mean_ms": float(np.mean(timings)),
            "p50_ms": float(np.percentile(timings, 50, method="linear")),
            "p95_ms": float(np.percentile(timings, 95, method="linear")),
            "min_ms": min(timings),
            "max_ms": max(timings),
        }
    return {
        "total_discovered_image_files": len(records),
        "selected_images": len(records) - states["skipped_limit"],
        "attempted_images": len(records)
        - states["skipped_limit"]
        - states["not_processed"],
        "successfully_decoded_images": sum(r["decoded"] for r in records),
        "successfully_inferred_images": sum(r["inference_succeeded"] for r in records),
        "successfully_classified_images": len(successful),
        "unreadable_images": states["unreadable"],
        "inference_failures": states["inference_error"]
        + states["warmup_error"]
        + states["request_error"],
        "result_processing_failures": states["result_error"],
        "status_counts": dict(sorted(states.items())),
        "ok_count": ok,
        "ng_count": ng,
        "ok_percent": 100 * ok / len(successful) if successful else None,
        "ng_percent": 100 * ng / len(successful) if successful else None,
        "percentage_denominator": "successfully_classified_images; null when zero",
        "per_label": {
            label: {
                "images_containing_label": image_counts[label],
                "total_regions": region_counts[label],
            }
            for label in sorted(known_labels | set(region_counts))
        },
        "descriptive_inference_timing": timing_stats,
        "timing_note": "Descriptive only, not a benchmark. Successful classified images only; "
        "module.run wall time excludes loading, decoding, request construction, warmup and serialization. "
        "New input shapes may trigger lazy initialization during the scan.",
        "sample_filenames": samples,
    }


def save_report(report):
    finished = datetime.now(timezone.utc)
    report["completed_at"] = finished.isoformat()
    output_dir = Path(__file__).resolve().parents[1] / "outputs" / "vimo_folder_scan"
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"scan_{finished.strftime('%Y%m%dT%H%M%S_%fZ')}"
    json_path, csv_path = output_dir / f"{stem}.json", output_dir / f"{stem}.csv"
    with json_path.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    columns = [
        "filename",
        "relative_path",
        "status",
        "ok_ng",
        "image_shape",
        "inference_ms",
        "labels",
        "total_regions",
        *CSV_LABELS,
        "regions_per_label",
        "error",
    ]
    # BOM lets Windows Excel display Chinese labels without manual encoding setup.
    with csv_path.open("x", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for record in report["per_image"]:
            row = {key: record[key] for key in columns if key in record}
            row["image_shape"] = (
                json.dumps(record["image_shape"]) if record["image_shape"] else ""
            )
            row["labels"] = json.dumps(
                sorted(record["regions_per_label"]), ensure_ascii=False
            )
            row["regions_per_label"] = json.dumps(
                record["regions_per_label"], ensure_ascii=False
            )
            for column, label in CSV_LABELS.items():
                row[column] = (
                    record["regions_per_label"].get(label, 0)
                    if record["status"] == "success"
                    else ""
                )
            error = record["error"]
            row["error"] = f"{error['type']}: {error['message']}" if error else ""
            writer.writerow(row)
    print(f"\nJSON: {json_path}\nCSV:  {csv_path}")


def run_survey(args, model, directory, paths):
    selected = paths if args.limit is None else paths[: args.limit]
    records = [
        {
            "filename": path.name,
            "relative_path": path.relative_to(directory).as_posix(),
            "status": "not_processed" if index < len(selected) else "skipped_limit",
            "decoded": False,
            "inference_succeeded": False,
            "image_shape": None,
            "inference_ms": None,
            "ok_ng": None,
            "label_map": None,
            "total_regions": None,
            "regions_per_label": {},
            "regions": [],
            "error": None,
        }
        for index, path in enumerate(paths)
    ]
    versions = {}
    for package in (
        "smartmore.vimo.inference",
        "smartmore.vimo.inference.custom",
        "numpy",
    ):
        try:
            versions[package] = version(package)
        except PackageNotFoundError:
            versions[package] = None
    with model.open("rb") as stream:
        model_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "environment": {
            "python_version": sys.version,
            "platform": platform.platform(),
            "versions": versions,
        },
        "model_path": str(model),
        "model_sha256": model_hash,
        "settings": {
            "image_directory": str(directory),
            "recursive": args.recursive,
            "limit": args.limit,
            "limit_unit": "first N discovered files attempted, including unreadable files; remaining rows are skipped_limit",
            "warmup": args.warmup,
            "warmup_policy": "once on the first usable request before collecting results",
            "extensions": sorted(EXTENSIONS),
            "ordering": "case-insensitive relative filename, original-case tie break",
            "device_id": 0,
            "max_batch_size": 1,
            "use_gpu": True,
        },
        "survey_rule": SURVEY_RULE,
        "warmup_calls_completed": 0,
        "per_image": records,
        "fatal_error": None,
    }
    known_labels = set(CSV_LABELS.values())
    phase = "SDK import"
    interrupted = False
    try:
        if selected:
            import cv2
            import numpy as np
            import smartmore.vimo.inference as vimo

            report["environment"]["opencv_version"] = cv2.__version__
            phase = "read export metadata and stage model"
            exported = read_export(model.with_name("index.html"))
            runtime_model = stage_model(model)
            phase = "load Solution"
            print("Loading Solution...", flush=True)
            with vimo.Solution(str(runtime_model)) as solution:
                phase = "select segmentation module"
                module_id = select_module(
                    solution.module_info_list, exported, None, vimo
                )
                report["module_id"] = module_id
                phase = "initialize serial GPU module"
                with solution.create_module(module_id, True, 0, 1) as module:
                    if module.max_batch_size != 1:
                        raise RuntimeError(
                            f"Expected max_batch_size=1, got {module.max_batch_size}"
                        )
                    phase = "configure segmentation outputs"
                    configure_regions(module, vimo)
                    parameters = cast(Any, module.get_params())
                    report["segmentation_outputs"] = {
                        "with_mask": parameters.with_mask,
                        "with_region_info": parameters.with_region_info,
                        "region_info_flag": int(parameters.region_info_flag),
                        "parameter_source": "verified smoke-test helper; vimo_params.json not loaded or modified",
                    }
                    known_labels.update(module.info.features)
                    phase = "serial folder scan"
                    print(SURVEY_RULE, flush=True)
                    print(
                        f"Discovered {len(paths)} images; selected {len(selected)}",
                        flush=True,
                    )
                    scan_images(
                        module, selected, records, args.warmup, vimo, cv2, np, report
                    )
                    phase = "release module"
                phase = "release Solution"
        else:
            print("No supported image files found; saving an empty survey.")
    except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 - preserve partial survey
        interrupted = isinstance(exc, KeyboardInterrupt)
        report["fatal_error"] = {"phase": phase, **exception_details(exc)}
        for record in records:
            if record["status"] == "processing":
                record_failure(record, "interrupted" if interrupted else "aborted", exc)
        print(f"[STOPPED] {phase}; saving partial results", file=sys.stderr)
        traceback.print_exc()
    report["failures"] = [
        {
            "filename": r["filename"],
            "relative_path": r["relative_path"],
            "status": r["status"],
            "exception": r["error"],
        }
        for r in records
        if r["error"] is not None
    ]
    report["status"] = (
        "aborted"
        if report["fatal_error"]
        else "completed_with_errors"
        if report["failures"]
        else "completed"
    )
    report["summary"] = summarize(records, known_labels)
    save_report(report)
    print(f"\nSurvey status: {report['status']}\n{SURVEY_RULE}")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    if interrupted:
        return 130
    return 1 if report["fatal_error"] or report["failures"] else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", type=Path, help="Source .vimosln model (read-only)")
    parser.add_argument("image_dir", type=Path, help="Folder containing test images")
    parser.add_argument(
        "--warmup",
        type=int,
        default=5,
        help="Initial warmup calls (default: 5; 0 disables)",
    )
    parser.add_argument(
        "--recursive", action="store_true", help="Include subfolders (default: false)"
    )
    parser.add_argument(
        "--limit",
        type=positive_int,
        help="Attempt only the first N discovered files (default: all)",
    )
    args = parser.parse_args()
    if args.warmup < 0:
        parser.error("--warmup must be >= 0")
    try:
        model, directory = validate_paths(args.model, args.image_dir)
        paths = discover_images(directory, args.recursive)
        return run_survey(args, model, directory, paths)
    except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 - CLI boundary
        traceback.print_exc()
        return 130 if isinstance(exc, KeyboardInterrupt) else 1


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="backslashreplace")
    sys.exit(main())
