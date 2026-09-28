"""Save serial ViMo overlays for manual visual inspection, without a UI.

Each selected image is inferred exactly once (no warmup). The original decoded
image occupies (0, 0, width, height) in the overlay; a legend is appended below.
Mask PNGs preserve the SDK's uint8 label IDs, so they may look nearly black in
ordinary viewers. The overlay provides the class-colored visualization.
"""

import argparse
import colorsys
import hashlib
import json
import os
import re
import sys
import traceback
from collections import Counter
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from math import ceil
from pathlib import Path

from benchmark_vimo import positive_int
from compare_vimo_results import parameter_snapshot, recoverable_inference_error
from scan_vimo_folder import discover_images, exception_details, validate_paths
from smoke_test_vimo import configure_regions, read_export, select_module, stage_model

# Named display-color fallback supplied by the user, never a mask-ID mapping.
KNOWN_COLORS = {
    "黑点": "#D40005",
    "压痕": "#CCC900",
    "破损": "#69CC00",
    "变形": "#00CC85",
}
ALPHA = 0.4


def class_colors(exported, module_id, labels):
    exported_colors = {}
    for module in exported or []:
        if str(module["ID"]) == module_id:
            for label in module.get("Labels", []):
                color = label.get("Color", "")
                if isinstance(color, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", color):
                    exported_colors[label["Name"]] = color.upper()
    colors = {}
    for label in sorted(labels):
        if label in exported_colors:
            color, source = exported_colors[label], "export index.html Labels.Color"
        elif label in KNOWN_COLORS:
            color, source = KNOWN_COLORS[label], "user-supplied display color fallback"
        else:
            hue = (
                int.from_bytes(
                    hashlib.sha256(label.encode("utf-8")).digest()[:2], "big"
                )
                / 65536
            )
            rgb = [
                round(channel * 255) for channel in colorsys.hsv_to_rgb(hue, 0.75, 0.9)
            ]
            color = "#" + "".join(f"{channel:02X}" for channel in rgb)
            source = "generated display color; model color unavailable"
        colors[label] = {"hex_rgb": color, "source": source}
    return colors


def chinese_font():
    """Use existing Pillow and a Windows CJK font; never install anything."""
    try:
        from PIL import ImageFont
    except ImportError:
        return (
            None,
            "Pillow unavailable; ASCII legend tokens map to Chinese labels in JSON",
        )
    fonts = Path(os.environ.get("SystemRoot", "C:/Windows")) / "Fonts"
    for name in ("msyh.ttc", "simhei.ttf", "simsun.ttc"):
        path = fonts / name
        if path.is_file():
            try:
                return ImageFont.truetype(str(path), 20), str(path)
            except OSError:
                pass
    return (
        None,
        "No usable Chinese font; ASCII legend tokens map to Chinese labels in JSON",
    )


def add_legend(overlay, lines, font, cv2, np):
    """Append a footer without translating/resizing the image coordinate plane."""
    height, width = overlay.shape[:2]
    if font is not None:
        from PIL import Image, ImageDraw

        # OpenCV stores BGR; Pillow receives RGB and is converted back on return.
        probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        text_width = max(
            ceil(probe.textbbox((0, 0), text, font=font)[2]) for text, _ in lines
        )
        canvas = Image.new(
            "RGB",
            (max(width, text_width + 24), height + 16 + 28 * len(lines)),
            (32, 32, 32),
        )
        canvas.paste(Image.fromarray(cv2.cvtColor(overlay, cv2.COLOR_BGR2RGB)), (0, 0))
        draw = ImageDraw.Draw(canvas)
        for index, (text, rgb) in enumerate(lines):
            draw.text((12, height + 8 + index * 28), text, font=font, fill=rgb)
        return cv2.cvtColor(np.asarray(canvas), cv2.COLOR_RGB2BGR)

    # OpenCV cannot render Chinese. JSON retains exact names, with the same L/R
    # tokens shown here. Escaped Unicode names also make the fallback unambiguous.
    lines = [
        (text.encode("ascii", "backslashreplace").decode("ascii"), rgb)
        for text, rgb in lines
    ]
    text_width = max(
        cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 1)[0][0]
        for text, _ in lines
    )
    canvas = np.full(
        (height + 16 + 26 * len(lines), max(width, text_width + 24), 3), 32, np.uint8
    )
    canvas[:height, :width] = overlay
    for index, (text, rgb) in enumerate(lines):
        cv2.putText(
            canvas,
            text,
            (12, height + 24 + index * 26),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            rgb[::-1],
            1,
            cv2.LINE_AA,
        )
    return canvas


def render(image, response, vimo, exported, module_id, features, font, cv2, np):
    if not isinstance(response, vimo.SegmentationResponse):
        raise TypeError(f"Expected SegmentationResponse, got {type(response)}")
    original_shape = list(response.mask.shape)
    mask = response.mask
    if mask.ndim == 3 and mask.shape[-1] == 1:
        mask = np.squeeze(mask, axis=-1)
    # SDK SegmentationResponse documents a full-input-size uint8 mask, with 0
    # as background. Never stretch, rotate, flatten or guess a cropped ROI offset.
    if mask.ndim != 2 or mask.shape != image.shape[:2] or mask.dtype != np.uint8:
        raise ValueError(
            f"Expected full-image uint8 mask {image.shape[:2]}, got {mask.shape} {mask.dtype}"
        )
    mask = mask.copy()
    label_map = {label: int(code) for label, code in response.label_map.items()}
    if len(set(label_map.values())) != len(label_map) or any(
        code < 0 or code > 255 for code in label_map.values()
    ):
        raise ValueError(f"Invalid/ambiguous response label_map: {label_map}")
    unknown = set(np.unique(mask).tolist()) - {0} - set(label_map.values())
    if unknown:
        raise ValueError(
            f"Mask IDs absent from response.label_map: {sorted(unknown)}; refusing to guess labels"
        )
    labels = (
        set(features)
        | set(label_map)
        | {region.label for region in response.region_infos}
    )
    colors = class_colors(exported, module_id, labels)
    legend = []
    for index, (label, color) in enumerate(colors.items(), 1):
        rgb = tuple(int(color["hex_rgb"][i : i + 2], 16) for i in (1, 3, 5))
        legend.append(
            {
                "token": f"L{index}",
                "label": label,
                "mask_id": label_map.get(label),
                **color,
                "rgb": rgb,
            }
        )
    by_label = {item["label"]: item for item in legend}
    overlay = image.copy()
    for label, code in label_map.items():
        if code == 0:
            continue  # Documented background remains untouched.
        pixels = mask == code
        bgr = np.asarray(by_label[label]["rgb"][::-1], dtype=float)
        overlay[pixels] = np.rint((1 - ALPHA) * image[pixels] + ALPHA * bgr).astype(
            np.uint8
        )

    regions = []
    for index, region in enumerate(response.region_infos, 1):
        x, y, width, height = (int(value) for value in region.bounding_rect)
        if width <= 0 or height <= 0:
            raise ValueError(f"Invalid bounding rectangle: {region.bounding_rect}")
        bgr = by_label[region.label]["rgb"][::-1]
        points = np.asarray(region.outer_contour, dtype=float)
        if points.size:
            if (
                points.ndim != 2
                or points.shape[1] != 2
                or not np.isfinite(points).all()
            ):
                raise ValueError(
                    f"Invalid outer contour for region {index}: {points.shape}"
                )
            # Vendor sample draws these (x, y) points directly in image coordinates.
            contour = np.rint(points).astype(np.int32).reshape(-1, 1, 2)
            cv2.polylines(overlay, [contour], True, bgr, 2, cv2.LINE_AA)
        cv2.rectangle(
            overlay, (x, y), (x + width - 1, y + height - 1), bgr, 1, cv2.LINE_AA
        )
        caption = f"R{index} area={int(region.area)}"
        text_width = cv2.getTextSize(caption, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)[0][0]
        origin = (
            max(0, min(x, image.shape[1] - text_width)),
            max(14, min(y - 5, image.shape[0] - 2)),
        )
        cv2.putText(
            overlay,
            caption,
            origin,
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            (0, 0, 0),
            3,
            cv2.LINE_AA,
        )
        cv2.putText(
            overlay, caption, origin, cv2.FONT_HERSHEY_SIMPLEX, 0.5, bgr, 1, cv2.LINE_AA
        )
        regions.append(
            {
                "token": f"R{index}",
                "label": region.label,
                "area": int(region.area),
                "bounding_rect": [x, y, width, height],
            }
        )
    lines = [
        (
            f"Overlay alpha={ALPHA}; original image at (0,0); background ID=0; regions={len(regions)}",
            (235, 235, 235),
        )
    ]
    for item in legend:
        code = item["mask_id"] if item["mask_id"] is not None else "not in response"
        lines.append(
            (
                f"{item['token']}  {item['label']}  mask ID={code}  {item['hex_rgb']}",
                item["rgb"],
            )
        )
    for region in regions:
        lines.append(
            (
                f"{region['token']}  {region['label']}  area={region['area']}",
                by_label[region["label"]]["rgb"],
            )
        )
    overlay = add_legend(overlay, lines, font, cv2, np)
    metadata = {
        "image_dimensions": {
            "width": int(image.shape[1]),
            "height": int(image.shape[0]),
        },
        "image_shape": list(image.shape),
        "label_map": label_map,
        "total_regions": len(regions),
        "regions": regions,
        "roi": list(response.roi),
        "rotated_roi": list(response.rotated_roi),
        "sdk_mask_shape": original_shape,
        "saved_mask_shape": list(mask.shape),
        "mask_encoding": "Lossless single-channel uint8 SDK label IDs; 0=background. Not a colorized PNG.",
        "mask_pixel_counts": {
            str(code): int(count)
            for code, count in zip(*np.unique(mask, return_counts=True))
        },
        "legend": legend,
        "overlay_alpha": ALPHA,
        "image_bounds_in_overlay_xywh": [
            0,
            0,
            int(image.shape[1]),
            int(image.shape[0]),
        ],
        "coordinates": "Decoded input image coordinates, x/y; rectangles are x/y/width/height. "
        "No resize, flip, rotation, ROI shift or mask interpolation. Legend is outside image bounds.",
    }
    return overlay, mask, metadata


def save_png(path, image, cv2):
    # imencode + tofile handles Windows Unicode filenames like the input loader.
    success, encoded = cv2.imencode(".png", image)
    if not success:
        raise OSError(f"PNG encoding failed: {path}")
    encoded.tofile(path)


def save_json(path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", type=Path, help="Read-only .vimosln source model")
    parser.add_argument("image_dir", type=Path, help="Non-recursive image directory")
    parser.add_argument(
        "--limit",
        type=positive_int,
        help="First N selected files in deterministic order",
    )
    parser.add_argument(
        "--filenames",
        nargs="+",
        help="Exact image filenames in image_dir; selection is still sorted",
    )
    args = parser.parse_args()
    try:
        model, directory = validate_paths(args.model, args.image_dir)
        paths = discover_images(directory, False)
        if args.filenames:
            names = set(args.filenames)
            missing = names - {path.name for path in paths}
            if missing:
                raise ValueError(
                    f"Selected filenames not found among supported images: {sorted(missing)}"
                )
            paths = [path for path in paths if path.name in names]
        paths = paths if args.limit is None else paths[: args.limit]
        if not paths:
            raise ValueError("No supported image files selected")
    except (OSError, ValueError) as exc:
        parser.error(f"{type(exc).__name__}: {exc}")

    timestamp = datetime.now(timezone.utc)
    output = (
        Path(__file__).resolve().parents[1]
        / "outputs/vimo_visual_check"
        / timestamp.strftime("%Y%m%dT%H%M%S_%fZ")
    )
    output.mkdir(parents=True, exist_ok=False)
    summary = {
        "timestamp": timestamp.isoformat(),
        "python_version": sys.version,
        "model_path": str(model),
        "image_directory": str(directory),
        "settings": {
            "max_batch_size": 1,
            "device_id": 0,
            "use_gpu": True,
            "warmup": 0,
            "limit": args.limit,
            "filenames": args.filenames,
            "inferences_per_image": 1,
        },
        "selected_images": [path.name for path in paths],
        "results": [],
        "failures": [],
        "status": "running",
        "fatal_error": None,
        "image_loading": "OpenCV IMREAD_COLOR, identical to the verified serial scripts; decoder orientation retained.",
    }
    for package in ("smartmore.vimo.inference", "smartmore.vimo.inference.custom"):
        try:
            summary[package] = version(package)
        except PackageNotFoundError:
            summary[package] = None
    phase = "import dependencies"
    vimo = None
    try:
        import cv2
        import numpy as np
        import smartmore.vimo.inference as vimo

        font, font_description = chinese_font()
        summary["text_renderer"] = font_description
        print(f"Legend font: {font_description}")
        phase = "read export and stage model"
        exported = read_export(model.with_name("index.html"))
        runtime_model = stage_model(model)
        summary["model_sha256"] = runtime_model.parent.name
        phase = "initialize serial module"
        with vimo.Solution(str(runtime_model)) as solution:
            module_id = select_module(solution.module_info_list, exported, None, vimo)
            summary["module_id"] = module_id
            with solution.create_module(module_id, True, 0, 1) as module:
                if module.max_batch_size != 1:
                    raise RuntimeError("Expected max_batch_size=1")
                # Exactly the prior smoke-test output setup; no new tuning,
                # thresholds, ROI changes, or vimo_params.json loading.
                configure_regions(module, vimo)
                params = module.get_params()
                if (
                    not isinstance(params, vimo.SegmentationParams)
                    or not params.with_mask
                ):
                    raise ValueError(
                        "Verified setup must already provide segmentation masks"
                    )
                summary["module_parameters"] = parameter_snapshot(params)
                summary["parameter_policy"] = (
                    "Same smoke-test region-output flags and SDK defaults; no inference tuning or threshold changes"
                )
                features = module.info.features
                stems = Counter(path.stem.casefold() for path in paths)
                for index, path in enumerate(paths, 1):
                    response = None
                    try:
                        phase = "decode"
                        image = cv2.imdecode(
                            np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR
                        )
                        if image is None:
                            raise ValueError("OpenCV could not decode the image")
                        phase = "construct request"
                        request = vimo.Request(image)
                        phase = "inference"
                        response = module.run(
                            request
                        )  # Exactly once, with no warmup/retry.
                        phase = "render/save"
                        overlay, mask, metadata = render(
                            image,
                            response,
                            vimo,
                            exported,
                            module_id,
                            features,
                            font,
                            cv2,
                            np,
                        )
                        # Same stems with different extensions get separate directories.
                        image_output = (
                            output / path.name
                            if stems[path.stem.casefold()] > 1
                            else output
                        )
                        image_output.mkdir(exist_ok=True)
                        overlay_path = image_output / f"{path.stem}_overlay.png"
                        mask_path = image_output / f"{path.stem}_mask.png"
                        metadata_path = image_output / f"{path.stem}_result.json"
                        metadata.update(
                            filename=path.name, text_renderer=font_description
                        )
                        save_png(overlay_path, overlay, cv2)
                        save_png(mask_path, mask, cv2)
                        save_json(metadata_path, metadata)
                        summary["results"].append(
                            {
                                "filename": path.name,
                                "total_regions": metadata["total_regions"],
                                "overlay": overlay_path.relative_to(output).as_posix(),
                                "mask": mask_path.relative_to(output).as_posix(),
                                "metadata": metadata_path.relative_to(
                                    output
                                ).as_posix(),
                            }
                        )
                        print(
                            f"[{index}/{len(paths)}] {path.name}: {metadata['total_regions']} regions",
                            flush=True,
                        )
                    except Exception as exc:
                        summary["failures"].append(
                            {
                                "filename": path.name,
                                "phase": phase,
                                **exception_details(exc, vimo),
                            }
                        )
                        print(
                            f"[FAILED] {path.name} ({phase}): {type(exc).__name__}: {exc}",
                            file=sys.stderr,
                        )
                        if phase == "inference" and not recoverable_inference_error(
                            exc, vimo
                        ):
                            raise
                    finally:
                        response = None
                phase = "release module"
            phase = "release Solution"
        summary["status"] = (
            "completed_with_errors" if summary["failures"] else "completed"
        )
    except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001
        summary["status"] = (
            "interrupted" if isinstance(exc, KeyboardInterrupt) else "aborted"
        )
        summary["fatal_error"] = {"phase": phase, **exception_details(exc, vimo)}
        traceback.print_exc()
    summary["successfully_saved_images"] = len(summary["results"])
    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    save_json(output / "summary.json", summary)
    print(
        f"Status: {summary['status']}; saved {len(summary['results'])}/{len(paths)} images\nOutput: {output}"
    )
    return (
        0
        if summary["status"] == "completed"
        else 130
        if summary["status"] == "interrupted"
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
