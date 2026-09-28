"""Compare serial x6 and batch=6 segmentation results on GPU 0, without timing.

OK/NG is an explicit comparison proxy: NG if any defect region is returned,
otherwise OK. It is not a production acceptance rule. No industrial tolerances
are applied. Detailed measurements are saved to outputs/vimo_consistency/.
"""

import argparse
import json
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
from benchmark_vimo import load_inputs, positive_int
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", type=Path, help="Source .vimosln model (read-only)")
    parser.add_argument(
        "image_dir", type=Path, help="Use the first six valid images in filename order"
    )
    parser.add_argument(
        "--warmup",
        type=positive_int,
        default=5,
        help="Six-image warmup rounds per mode (default: 5)",
    )
    args = parser.parse_args()
    phase = "load inputs"
    try:
        model, paths, images = load_inputs(args.model, args.image_dir)
        phase = "import ViMo and stage model"
        import smartmore.vimo.inference as vimo

        exported = read_export(model.with_name("index.html"))
        runtime_model = stage_model(model)
        requests = [vimo.Request(image) for image in images]
        versions = {}
        for package in ("smartmore.vimo.inference", "smartmore.vimo.inference.custom"):
            try:
                versions[package] = version(package)
            except PackageNotFoundError:
                versions[package] = None
        report = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "python_version": sys.version,
            "vimo_sdk_versions": versions,
            "model_path": str(model),
            "model_sha256": runtime_model.parent.name,
            "images": [
                {"name": p.name, "shape": list(im.shape), "dtype": str(im.dtype)}
                for p, im in zip(paths, images)
            ],
            "warmup_count": args.warmup,
            "warmup_unit": "six-image rounds per mode",
            "device_id": 0,
            "input_policy": "same six arrays and Request objects reused for both modes",
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
            report["module_parameters"] = {
                "serial": serial_params,
                "batch": batch_params,
                "exact_equal": True,
            }

            phase = "warm both modes"
            print(
                f"Warming each mode for {args.warmup} six-image rounds...", flush=True
            )
            for _ in range(args.warmup):
                for request in requests:
                    serial_module.run(request)
            for _ in range(args.warmup):
                batch_module.run(requests)
            phase = "collect six serial responses"
            print("Collecting serial results...", flush=True)
            serial_results = [
                snapshot(serial_module.run(request), vimo) for request in requests
            ]
            phase = "collect one batch=6 response list"
            print("Collecting batch results...", flush=True)
            responses = batch_module.run(requests)
            if not isinstance(responses, list) or len(responses) != 6:
                raise ValueError(
                    "Expected exactly six batch responses in request order"
                )
            batch_results = [snapshot(response, vimo) for response in responses]
            phase = "release modules and Solution"

        phase = "compare results and write JSON"
        report["per_image"] = [
            compare_image(path.name, a, b)
            for path, a, b in zip(paths, serial_results, batch_results)
        ]
        report["final_summary"] = summarize(report["per_image"])
        finished = datetime.now(timezone.utc)
        report["completed_at"] = finished.isoformat()
        output_dir = (
            Path(__file__).resolve().parents[1] / "outputs" / "vimo_consistency"
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        output = (
            output_dir / f"consistency_{finished.strftime('%Y%m%dT%H%M%S_%fZ')}.json"
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
        for index, image in enumerate(report["per_image"], 1):
            print(f"\nImage {index}: {image['image']}\n-------")
            print(json.dumps(image["summary"], indent=2, default=json_default))
            for mode in ("serial", "batch"):
                unmatched = image["regions"][f"unmatched_{mode}"]
                if unmatched:
                    print(
                        f"unmatched_{mode}: {json.dumps(unmatched, ensure_ascii=False, default=json_default)}"
                    )
        print("\nFinal batch summary\n-------------------")
        print(json.dumps(report["final_summary"], indent=2, default=json_default))
        print(report["assessment"])
        print(f"Detailed matches, class pixel counts and parameters: {output}")
        return 0
    except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 - CLI boundary
        print(f"\n[FAILED] {phase}", file=sys.stderr)
        traceback.print_exc()
        return 130 if isinstance(exc, KeyboardInterrupt) else 1


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="backslashreplace")
    sys.exit(main())
