"""Compare a full image folder using serial and batches of up to 6 on GPU 0.

OK/NG is an explicit comparison proxy: NG if any defect region is returned,
otherwise OK. It is not a production acceptance rule. No industrial tolerances
are applied. Detailed measurements are saved to outputs/vimo_consistency/.
"""

import argparse
import hashlib
import json
import platform
import sys
import traceback
from collections import Counter
from collections.abc import Callable
from contextlib import ExitStack
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import cast

import numpy as np
from benchmark_vimo import positive_int
from scan_vimo_folder import (
    EXTENSIONS,
    discover_images,
    exception_details,
    validate_paths,
)
from smoke_test_vimo import configure_regions, read_export, select_module, stage_model


def json_default(value):
    if isinstance(value, np.generic):
        return value.item()
    # Verified SDK parameter classes expose their fields as Python attributes.
    if hasattr(value, "__dict__"):
        return {
            key: item for key, item in vars(value).items() if not key.startswith("_")
        }
    raise TypeError(f"Cannot serialize {type(value)}")


def parameter_snapshot(params):
    return json.loads(json.dumps(params, default=json_default, allow_nan=False))


def relative_delta_percent(serial, batch):
    if serial == 0:
        return 0.0 if batch == 0 else None
    return 100.0 * abs(batch - serial) / abs(serial)


def snapshot(response, vimo):
    if not isinstance(response, vimo.SegmentationResponse):
        raise TypeError(f"Expected SegmentationResponse, got {type(response)}")
    regions = []
    for index, region in enumerate(response.region_infos):
        regions.append(
            {
                "index": index,
                "label": region.label,
                "area": int(region.area),
                "long_side": float(region.long_side),
                "short_side": float(region.short_side),
                "bounding_rect": list(region.bounding_rect),
                "min_area_rect": list(region.min_area_rect),
                "outer_contour_points": len(region.outer_contour),
                "inner_contour_point_counts": sorted(
                    len(c) for c in region.inner_contours
                ),
            }
        )
    return {
        "python_type": f"{type(response).__module__}.{type(response).__qualname__}",
        "type": int(response.type),
        "roi": list(response.roi),
        "rotated_roi": list(response.rotated_roi),
        "label_map": dict(response.label_map),
        # Own the mask before the next SDK call or module destruction.
        "mask": response.mask.copy(),
        "regions": regions,
    }


def compare_masks(serial, batch):
    a, b = serial["mask"], batch["mask"]
    # Normalize only the channel axis; preserve singleton spatial dimensions.
    if a.ndim == 3 and a.shape[-1] == 1:
        a = np.squeeze(a, axis=-1)
    if b.ndim == 3 and b.shape[-1] == 1:
        b = np.squeeze(b, axis=-1)
    compatible = a.shape == b.shape and a.dtype == b.dtype
    comparable = compatible and a.ndim == 2 and a.size > 0
    report = {
        "serial_shape": list(serial["mask"].shape),
        "batch_shape": list(batch["mask"].shape),
        "serial_dtype": str(a.dtype),
        "batch_dtype": str(b.dtype),
        "shape_equal": serial["mask"].shape == batch["mask"].shape,
        "dtype_equal": a.dtype == b.dtype,
        "comparable": comparable,
        "reason": None
        if comparable
        else "Masks differ in shape/dtype, are empty, or are not 2D",
        "total_pixels": int(a.size) if comparable else None,
        "identical_pixels": None,
        "differing_pixels": None,
        "mismatch_percent": None,
        "exact_equal": None,
    }
    if comparable:
        different = int(np.count_nonzero(a != b))
        report.update(
            identical_pixels=int(a.size) - different,
            differing_pixels=different,
            mismatch_percent=100.0 * different / a.size,
            exact_equal=different == 0,
        )

    # Each mode's own label_map resolves IDs: changed IDs must not silently
    # turn a raw mask mismatch into a change in defect classification.
    histograms = []
    for mask in (a, b):
        values, counts = np.unique(mask, return_counts=True)
        histograms.append(dict(zip(values.tolist(), counts.tolist())))
    classes = []
    labels = sorted(set(serial["label_map"]) | set(batch["label_map"]))
    for label in [None, *labels]:
        ids = [
            0 if label is None else rsp["label_map"].get(label)
            for rsp in (serial, batch)
        ]
        counts = [hist.get(code, 0) for hist, code in zip(histograms, ids)]
        classes.append(
            {
                "label": label,
                "role": "background" if label is None else "label",
                "serial_id": ids[0],
                "batch_id": ids[1],
                "serial_pixels": counts[0],
                "batch_pixels": counts[1],
                "absolute_delta": abs(counts[1] - counts[0]),
                "relative_delta_percent": relative_delta_percent(*counts),
            }
        )
    report["per_class"] = classes
    for mode, rsp, hist in zip(("serial", "batch"), (serial, batch), histograms):
        known_ids = {0, *rsp["label_map"].values()}
        report[f"{mode}_unmapped_pixel_ids"] = {
            str(k): v for k, v in hist.items() if k not in known_ids
        }
    return report


def bbox_iou(a, b):
    # ViMo Rect is (x, y, width, height); no inclusive-coordinate +1 adjustment.
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    intersection = max(0, min(ax + aw, bx + bw) - max(ax, bx)) * max(
        0, min(ay + ah, by + bh) - max(ay, by)
    )
    union = max(0, aw) * max(0, ah) + max(0, bw) * max(0, bh) - intersection
    return intersection / union if union > 0 else 0.0


def region_key(region):
    return (
        region["label"],
        *region["bounding_rect"],
        region["area"],
        region["long_side"],
        region["short_side"],
        *region["min_area_rect"],
        region["outer_contour_points"],
        tuple(region["inner_contour_point_counts"]),
    )


def match_regions(serial, batch):
    # Greedy highest-IoU matching within a label, geometry breaks ties before
    # source indices. This is deterministic, not a global optimal assignment.
    candidates = []
    for i, a in enumerate(serial):
        for j, b in enumerate(batch):
            if a["label"] == b["label"]:
                iou = bbox_iou(a["bounding_rect"], b["bounding_rect"])
                if iou > 0:  # Disjoint/degenerate boxes stay explicitly unmatched.
                    candidates.append((-iou, region_key(a), region_key(b), i, j))
    used_serial, used_batch, matched = set(), set(), []
    for negative_iou, _, _, i, j in sorted(candidates):
        if i in used_serial or j in used_batch:
            continue
        used_serial.add(i)
        used_batch.add(j)
        a, b = serial[i], batch[j]
        matched.append(
            {
                "label": a["label"],
                "serial": a,
                "batch": b,
                "absolute_area_delta": abs(b["area"] - a["area"]),
                "relative_area_delta_percent": relative_delta_percent(
                    a["area"], b["area"]
                ),
                "bbox_iou": -negative_iou,
                "long_side_delta": b["long_side"] - a["long_side"],
                "short_side_delta": b["short_side"] - a["short_side"],
                "bounding_rect_delta": [
                    y - x for x, y in zip(a["bounding_rect"], b["bounding_rect"])
                ],
                "min_area_rect_delta": [
                    y - x for x, y in zip(a["min_area_rect"], b["min_area_rect"])
                ],
                "outer_contour_point_count_delta": b["outer_contour_points"]
                - a["outer_contour_points"],
                "reported_region_metrics_equal": region_key(a) == region_key(b),
            }
        )
    return {
        "matched": matched,
        "unmatched_serial": [r for i, r in enumerate(serial) if i not in used_serial],
        "unmatched_batch": [r for i, r in enumerate(batch) if i not in used_batch],
    }


def extrema(values, operation):
    return operation([value for value in values if value is not None], default=None)


def compare_image(name, serial, batch):
    a_counts = dict(sorted(Counter(r["label"] for r in serial["regions"]).items()))
    b_counts = dict(sorted(Counter(r["label"] for r in batch["regions"]).items()))
    high_level = {
        "serial": {k: v for k, v in serial.items() if k not in ("mask", "regions")},
        "batch": {k: v for k, v in batch.items() if k not in ("mask", "regions")},
        "serial_total_regions": len(serial["regions"]),
        "batch_total_regions": len(batch["regions"]),
        "serial_regions_per_label": a_counts,
        "batch_regions_per_label": b_counts,
        "response_type_equal": serial["python_type"] == batch["python_type"]
        and serial["type"] == batch["type"],
        "roi_equal": serial["roi"] == batch["roi"],
        "rotated_roi_equal": serial["rotated_roi"] == batch["rotated_roi"],
        "label_map_equal": serial["label_map"] == batch["label_map"],
        "total_regions_equal": len(serial["regions"]) == len(batch["regions"]),
        "region_counts_equal": a_counts == b_counts,
        "labels_equal": set(a_counts) == set(b_counts),
        "serial_ok_ng_proxy": "NG" if a_counts else "OK",
        "batch_ok_ng_proxy": "NG" if b_counts else "OK",
        "ok_ng_equal": bool(a_counts) == bool(b_counts),
    }
    masks = compare_masks(serial, batch)
    regions = match_regions(serial["regions"], batch["regions"])
    pairs = regions["matched"]
    unmatched = len(regions["unmatched_serial"]) + len(regions["unmatched_batch"])
    summary = {
        key: value for key, value in high_level.items() if key.endswith("_equal")
    }
    summary.update(
        {
            "mask_comparable": masks["comparable"],
            "mask_exact_equal": masks["exact_equal"],
            "mask_mismatch_percent": masks["mismatch_percent"],
            "matched_regions": len(pairs),
            "unmatched_regions": unmatched,
            "max_area_delta": extrema((p["absolute_area_delta"] for p in pairs), max),
            "max_relative_area_delta_percent": extrema(
                (p["relative_area_delta_percent"] for p in pairs), max
            ),
            "undefined_relative_area_deltas": sum(
                p["relative_area_delta_percent"] is None for p in pairs
            ),
            "minimum_bbox_iou": extrema((p["bbox_iou"] for p in pairs), min),
            "reported_region_metrics_equal": unmatched == 0
            and all(p["reported_region_metrics_equal"] for p in pairs),
            "semantic_result_equal": high_level["labels_equal"]
            and high_level["ok_ng_equal"],
        }
    )
    summary["high_level_exact_equal"] = all(
        value for key, value in high_level.items() if key.endswith("_equal")
    )
    differences = []
    if not summary["semantic_result_equal"]:
        differences.append("defect_label_or_ok_ng_difference")
    if not summary["high_level_exact_equal"]:
        differences.append("high_level_field_difference")
    if not masks["comparable"]:
        differences.append("mask_comparison_unavailable")
    elif not masks["exact_equal"]:
        differences.append("pixel_difference")
    if not summary["reported_region_metrics_equal"]:
        differences.append("region_geometry_or_matching_difference")
    summary["difference_categories"] = differences
    return {
        "image": name,
        "high_level": high_level,
        "mask": masks,
        "regions": regions,
        "summary": summary,
    }


def summarize(images):
    summaries = [image["summary"] for image in images]
    return {
        "images_compared": len(images),
        "images_with_identical_label_map": sum(s["label_map_equal"] for s in summaries),
        "images_with_identical_per_label_region_counts": sum(
            s["region_counts_equal"] for s in summaries
        ),
        "images_with_same_ok_ng_proxy": sum(s["ok_ng_equal"] for s in summaries),
        "images_with_same_semantic_result": sum(
            s["semantic_result_equal"] for s in summaries
        ),
        "images_with_exact_high_level_fields": sum(
            s["high_level_exact_equal"] for s in summaries
        ),
        "total_unmatched_regions": sum(s["unmatched_regions"] for s in summaries),
        "images_with_comparable_masks": sum(s["mask_comparable"] for s in summaries),
        "images_with_exact_masks": sum(
            s["mask_exact_equal"] is True for s in summaries
        ),
        "maximum_mask_mismatch_percent": extrema(
            (s["mask_mismatch_percent"] for s in summaries), max
        ),
        "maximum_relative_region_area_difference_percent": extrema(
            (s["max_relative_area_delta_percent"] for s in summaries), max
        ),
        "undefined_relative_region_area_differences": sum(
            s["undefined_relative_area_deltas"] for s in summaries
        ),
        "minimum_matched_bbox_iou": extrema(
            (s["minimum_bbox_iou"] for s in summaries), min
        ),
    }


def record_failure(report, records, phase, exc, batch_id=None, vimo=None):
    failure = {
        "phase": phase, "batch_id": batch_id,
        "filenames": [r["filename"] for r in records],
        "relative_paths": [r["relative_path"] for r in records],
        "exception": exception_details(exc, vimo),
    }
    failure_id = len(report["failures"])
    report["failures"].append(failure)
    for record in records:
        record["status"] = "failed"
        record["failure_ids"].append(failure_id)
    print(f"[WARN] {phase}: {', '.join(failure['relative_paths'])}: "
          f"{type(exc).__name__}: {' '.join(str(exc).split())}", file=sys.stderr, flush=True)


def recoverable_inference_error(exc, vimo):
    # Same conservative policy as the folder survey: input rejections are
    # recoverable; license/runtime/unknown SDK failures stop further GPU calls.
    return (
        isinstance(exc, vimo.VimoException)
        and exc.code in (vimo.ErrorCode.INVALID_ARGUMENT, vimo.ErrorCode.INVALID_IMAGE_DATA)
    ) or isinstance(exc, (ValueError, TypeError))


def decoded_chunks(paths, records, report, vimo, cv2):
    chunk = []
    for path, record in zip(paths, records):
        try:
            image = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
            if image is None:
                raise ValueError("OpenCV could not decode image")
        except (OSError, ValueError, cv2.error) as exc:
            record_failure(report, [record], "decode", exc)
            continue
        record.update(decoded=True, shape=list(image.shape), dtype=str(image.dtype), status="decoded")
        # Keep discovery order and avoid unverified mixed-shape batches.
        if chunk and chunk[0][1].image.shape != image.shape:
            yield chunk
            chunk = []
        try:
            request = vimo.Request(image)
        except Exception as exc:
            record_failure(report, [record], "request", exc, vimo=vimo)
            if not recoverable_inference_error(exc, vimo):
                raise
            continue
        chunk.append((record, request))
        if len(chunk) == 6:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def compare_folder(serial_module, batch_module, paths, records, report, warmup, vimo, cv2):
    warmed = False
    for batch_id, chunk in enumerate(decoded_chunks(paths, records, report, vimo, cv2), 1):
        batch_records = [item[0] for item in chunk]
        requests = [item[1] for item in chunk]
        batch_record = {
            "batch_id": batch_id, "size": len(requests),
            "relative_paths": [r["relative_path"] for r in batch_records],
            "status": "pending", "inference_attempted": False,
        }
        report["batches"].append(batch_record)
        for record in batch_records:
            record["batch_id"] = batch_id
        if not warmed:
            phase = "serial warmup"
            affected = batch_records
            try:
                print(f"Warming both modules: {warmup} rounds, {len(requests)} images", flush=True)
                for _ in range(warmup):
                    for record, request in chunk:
                        affected = [record]
                        serial_module.run(request)
                phase = "batch warmup"
                affected = batch_records
                for _ in range(warmup):
                    batch_module.run(requests)
                warmed = True
                report["warmup_images"] = batch_record["relative_paths"]
            except Exception as exc:
                record_failure(report, affected, phase, exc, batch_id, vimo)
                batch_record["status"] = "warmup_failed"
                for record in batch_records:
                    if record["status"] != "failed":
                        record["status"] = "not_compared_warmup_failure"
                if not recoverable_inference_error(exc, vimo):
                    raise
                requests = chunk = request = None
                continue

        serial_results = []
        for record, request in chunk:
            phase = "serial inference"
            try:
                response = serial_module.run(request)
                record["serial_inferred"] = True
                phase = "serial result processing"
                serial_results.append(snapshot(response, vimo))
            except Exception as exc:
                serial_results.append(None)
                record_failure(report, [record], phase, exc, batch_id, vimo)
                if isinstance(exc, MemoryError) or (
                    phase == "serial inference" and not recoverable_inference_error(exc, vimo)
                ):
                    raise
            finally:
                response = None
        batch_record["inference_attempted"] = True
        try:
            # Keep all requests, including any with serial failures; never pad.
            responses = batch_module.run(requests)
        except Exception as exc:
            batch_record["status"] = "inference_failed"
            record_failure(report, batch_records, "batch inference", exc, batch_id, vimo)
            if not recoverable_inference_error(exc, vimo):
                raise
        else:
            if not isinstance(responses, list) or len(responses) != len(requests):
                batch_record["status"] = "invalid_response"
                record_failure(report, batch_records, "batch result processing",
                               ValueError("Batch response count/type does not match submitted requests"), batch_id)
            else:
                batch_record["status"] = "inferred"
                for record, serial, response in zip(batch_records, serial_results, responses):
                    record["batch_inferred"] = True
                    if serial is None:
                        continue
                    try:
                        result = compare_image(record["filename"], serial, snapshot(response, vimo))
                        result["relative_path"] = record["relative_path"]
                        result["image_shape"] = record["shape"]
                        result["batch_id"] = batch_id
                        report["per_image"].append(result)
                        record["status"] = "compared"
                    except Exception as exc:
                        record_failure(report, [record], "comparison/result processing", exc, batch_id, vimo)
                        if isinstance(exc, MemoryError):
                            raise
        # The report holds measurements only, not raw masks or input arrays.
        serial_results = responses = response = serial = None
        requests = chunk = request = None
        if batch_id % 10 == 0:
            print(f"Progress: {batch_id} chunks; {len(report['per_image'])} images compared; "
                  f"{len(report['failures'])} failure events", flush=True)


def summarize_folder(report):
    images, records = report["per_image"], report["images"]
    result = summarize(images)  # Retain the original aggregate fields.
    summaries = [image["summary"] for image in images]
    high_levels = [image["high_level"] for image in images]
    masks = [s["mask_mismatch_percent"] for s in summaries if s["mask_comparable"]]
    result.update({
        "total_discovered_images": len(records),
        "selected_images": sum(r["status"] != "skipped_limit" for r in records),
        "decoded_images": sum(r["decoded"] for r in records),
        "successfully_compared_images": len(images),
        "failed_images": sum(bool(r["failure_ids"]) for r in records),
        "failure_events": len(report["failures"]),
        "fatal_error": report["fatal_error"] is not None,
        "unreadable_images": sum(f["phase"] == "decode" for f in report["failures"]),
        "image_status_counts": dict(Counter(r["status"] for r in records)),
        "aggregation_scope": "Semantic, region and mask statistics use successfully compared image pairs only. "
                             "Failures and unprocessed images are never counted as OK or equal.",
        "total_matched_regions": sum(s["matched_regions"] for s in summaries),
        "total_unmatched_serial_regions": sum(len(i["regions"]["unmatched_serial"]) for i in images),
        "total_unmatched_batch_regions": sum(len(i["regions"]["unmatched_batch"]) for i in images),
        "images_containing_unmatched_regions": sum(s["unmatched_regions"] > 0 for s in summaries),
        "maximum_absolute_area_delta": extrema((s["max_area_delta"] for s in summaries), max),
        "masks_not_comparable": sum(not s["mask_comparable"] for s in summaries),
        "masks_different": sum(s["mask_exact_equal"] is False for s in summaries),
        "mean_mask_mismatch_percent": float(np.mean(masks)) if masks else None,
        "p95_mask_mismatch_percent": float(np.percentile(masks, 95, method="linear")) if len(masks) >= 2 else None,
        "mask_statistics_note": "Unweighted across comparable images; P95 uses linear interpolation with >=2 samples, else null.",
        "batch_size_counts": dict(sorted(Counter(b["size"] for b in report["batches"] if b["inference_attempted"]).items())),
    })
    for mode in ("serial", "batch"):
        for decision in ("OK", "NG"):
            result[f"{mode}_{decision.lower()}_count"] = sum(h[f"{mode}_ok_ng_proxy"] == decision for h in high_levels)
    for name, key in (("ok_ng", "ok_ng_equal"), ("label_sets", "labels_equal"),
                      ("label_map", "label_map_equal"), ("per_label_region_counts", "region_counts_equal")):
        count = sum(s[key] for s in summaries)
        result[f"images_with_equal_{name}"] = count
        result[f"images_with_different_{name}"] = len(images) - count
    labels = sorted({label for h in high_levels for mode in ("serial", "batch")
                     for label in h[f"{mode}_regions_per_label"]})
    result["per_label"] = {}
    for label in labels:
        counts = {mode: [h[f"{mode}_regions_per_label"].get(label, 0) for h in high_levels]
                  for mode in ("serial", "batch")}
        result["per_label"][label] = {
            "serial_images": sum(n > 0 for n in counts["serial"]),
            "batch_images": sum(n > 0 for n in counts["batch"]),
            "serial_total_regions": sum(counts["serial"]), "batch_total_regions": sum(counts["batch"]),
            "images_with_different_region_count": sum(a != b for a, b in zip(counts["serial"], counts["batch"])),
        }
    examples = {
        "semantic_differences": [i["relative_path"] for i in images if not i["summary"]["semantic_result_equal"]][:20],
        "label_map_differences": [i["relative_path"] for i in images if not i["summary"]["label_map_equal"]][:20],
        "unmatched_regions": [i["relative_path"] for i in images if i["summary"]["unmatched_regions"]][:20],
    }
    for name, key in (("largest_mask_mismatch", "mask_mismatch_percent"),
                      ("largest_relative_region_area_difference", "max_relative_area_delta_percent")):
        ranked = sorted((i for i in images if i["summary"][key] is not None and i["summary"][key] > 0),
                        key=lambda i: (-i["summary"][key], i["relative_path"]))
        examples[name] = [i["relative_path"] for i in ranked[:20]]
    result["difference_examples"] = examples
    result["difference_example_note"] = "Relative filenames disambiguate recursive folders; ranked lists include nonzero defined differences only."
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", type=Path, help="Source .vimosln model (read-only)")
    parser.add_argument(
        "image_dir", type=Path, help="Compare the entire image folder in deterministic filename order"
    )
    parser.add_argument(
        "--warmup",
        type=positive_int,
        default=5,
        help="Warmup rounds per mode on the first usable chunk (default: 5)",
    )
    parser.add_argument("--recursive", action="store_true", help="Include subfolders")
    parser.add_argument("--limit", type=positive_int, help="Select the first N discovered files (default: all)")
    args = parser.parse_args()
    phase = "load inputs"
    report = None
    interrupted = False
    try:
        model, directory = validate_paths(args.model, args.image_dir)
        paths = discover_images(directory, args.recursive)
        selected = paths if args.limit is None else paths[:args.limit]
        versions = {}
        for package in ("smartmore.vimo.inference", "smartmore.vimo.inference.custom"):
            try:
                versions[package] = version(package)
            except PackageNotFoundError:
                versions[package] = None
        with model.open("rb") as stream:
            model_hash = hashlib.file_digest(stream, "sha256").hexdigest()
        report = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "python_version": sys.version,
            "vimo_sdk_versions": versions,
            "model_path": str(model),
            "platform": platform.platform(),
            "numpy_version": np.__version__,
            "model_sha256": model_hash,
            "images": [
                {"filename": p.name, "relative_path": p.relative_to(directory).as_posix(),
                 "status": "not_processed" if index < len(selected) else "skipped_limit",
                 "decoded": False, "serial_inferred": False, "batch_inferred": False,
                 "shape": None, "dtype": None, "failure_ids": []}
                for index, p in enumerate(paths)
            ],
            "settings": {
                "image_directory": str(directory), "recursive": args.recursive, "limit": args.limit,
                "extensions": sorted(EXTENSIONS), "warmup": args.warmup,
                "ordering": "case-insensitive relative filename, original-case tie break",
                "limit_policy": "first N discovered files, including unreadable files",
                "batch_policy": "up to 6 decoded requests in discovery order; flush at shape changes; no padding or duplicates",
            },
            "per_image": [], "failures": [], "batches": [], "fatal_error": None,
            "warmup_count": args.warmup,
            "warmup_unit": "first usable chunk per mode; warmup outputs discarded",
            "device_id": 0,
            "input_policy": "same decoded arrays and Request objects supplied to both modes in each chunk",
            "parameter_source": "SDK module parameters; mask and region outputs enabled in memory; vimo_params.json not loaded",
            "semantic_rule": "NG if any returned defect region, else OK (comparison proxy, not production acceptance). "
            "semantic_result_equal means equal detected defect-label sets and equal OK/NG proxy; "
            "region counts and label_map equality are reported separately.",
            "matching_rule": "Same-label greedy highest positive bounding-box IoU; geometry-based tie breaks, "
            "then original indices; disjoint/degenerate boxes unmatched; not a global optimum.",
            "delta_conventions": "Signed deltas are batch minus serial; relative deltas are absolute difference "
            "/ abs(serial) * 100; zero/zero is 0, nonzero/zero is null. Unmatched regions "
            "are excluded from matched-region extrema; no matches means null extrema.",
            "equality_scope": "Exact flags cover named fields only; region contour coordinates are not compared. "
            "Raw mask mismatch compares IDs; per-label pixel counts use each mode's label_map.",
            "assessment": "Measurements only. No tolerance or good-enough conclusion is applied.",
        }
        if not selected:
            raise ValueError("No supported image files discovered")
        phase = "import ViMo and stage model"
        import cv2
        import smartmore.vimo.inference as vimo

        report["opencv_version"] = cv2.__version__
        exported = read_export(model.with_name("index.html"))
        runtime_model = stage_model(model)
        report["model_sha256"] = runtime_model.parent.name
        print(f"Discovered {len(paths)} images; selected {len(selected)}", flush=True)
        with ExitStack() as stack:
            phase = "load Solution"
            print("Loading Solution...", flush=True)
            solution = stack.enter_context(vimo.Solution(str(runtime_model)))
            module_id = select_module(solution.module_info_list, exported, None, vimo)
            report["module_id"] = module_id
            phase = "create independent serial and batch GPU modules"
            serial_module = stack.enter_context(
                solution.create_module(module_id, True, 0, 1)
            )
            batch_module = stack.enter_context(
                solution.create_module(module_id, True, 0, 6)
            )
            if serial_module.max_batch_size != 1 or batch_module.max_batch_size < 6:
                raise RuntimeError(
                    f"Unexpected capacities: serial={serial_module.max_batch_size}, "
                    f"batch={batch_module.max_batch_size}"
                )
            report["module_capacities"] = {
                "serial": serial_module.max_batch_size,
                "batch": batch_module.max_batch_size,
            }
            phase = "configure and verify identical module parameters"
            configure_regions(serial_module, vimo)
            parameters = serial_module.get_params()
            if not isinstance(parameters, vimo.SegmentationParams):
                raise TypeError(f"Expected SegmentationParams, got {type(parameters)}")
            parameters.with_mask = True
            set_serial_params = cast(
                Callable[[vimo.SegmentationParams], None], serial_module.set_params
            )
            set_batch_params = cast(
                Callable[[vimo.SegmentationParams], None], batch_module.set_params
            )
            set_serial_params(parameters)
            set_batch_params(parameters)
            serial_params = parameter_snapshot(serial_module.get_params())
            batch_params = parameter_snapshot(batch_module.get_params())
            report["module_parameters"] = {
                "serial": serial_params,
                "batch": batch_params,
                "exact_equal": serial_params == batch_params,
            }
            if serial_params != batch_params:
                raise ValueError(
                    "SDK read-back segmentation parameters differ between modules"
                )
            if not serial_params["with_mask"] or not serial_params["with_region_info"]:
                raise ValueError("SDK did not enable the requested mask/region outputs")
            if serial_params["region_info_flag"] & int(
                parameters.region_info_flag
            ) != int(parameters.region_info_flag):
                raise ValueError(
                    "SDK did not enable the requested region information flags"
                )
            phase = "folder decoding, warmup, inference and comparison"
            compare_folder(serial_module, batch_module, selected, report["images"],
                           report, args.warmup, vimo, cv2)
            phase = "release modules and Solution"
    except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 - preserve partial results
        interrupted = isinstance(exc, KeyboardInterrupt)
        print(f"\n[FAILED] {phase}", file=sys.stderr)
        traceback.print_exc()
        if report is None:
            return 130 if interrupted else 1
        report["fatal_error"] = {"phase": phase, **exception_details(exc)}

    try:
        report["status"] = "aborted" if report["fatal_error"] else "completed_with_errors" if report["failures"] else "completed"
        report["final_summary"] = summarize_folder(report)
        finished = datetime.now(timezone.utc)
        report["completed_at"] = finished.isoformat()
        output_dir = (
            Path(__file__).resolve().parents[1] / "outputs" / "vimo_consistency"
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        output = (
            output_dir / f"consistency_full_{finished.strftime('%Y%m%dT%H%M%S_%fZ')}.json"
        )
        with output.open("x", encoding="utf-8") as stream:
            json.dump(
                report,
                stream,
                ensure_ascii=False,
                indent=2,
                default=json_default,
                allow_nan=False,
            )
            stream.write("\n")
        print(f"\nSemantic rule: {report['semantic_rule']}")
        print(f"\nFull-folder summary ({report['status']})\n-------------------")
        print(json.dumps(report["final_summary"], ensure_ascii=False, indent=2, default=json_default))
        print(report["assessment"])
        print(f"Detailed matches, class pixel counts and parameters: {output}")
        if interrupted:
            return 130
        return 1 if report["fatal_error"] or report["failures"] else 0
    except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 - CLI boundary
        print("\n[FAILED] aggregate/write comparison report", file=sys.stderr)
        traceback.print_exc()
        return 130 if isinstance(exc, KeyboardInterrupt) else 1


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="backslashreplace")
    sys.exit(main())
