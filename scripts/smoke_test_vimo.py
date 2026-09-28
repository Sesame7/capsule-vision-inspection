"""Run real local ViMo segmentation inference; no UI or environment changes.

API sources: vendor/sdk/samples/{run_module,batch_module,
custom_segmentation_visualize,get_features}.py and the installed 3.20.0.2 SDK.
The bundled HTML docs identify themselves as 3.19.2.2.

Exit codes: 0 = completed (see any batch SKIP), 1 = failure,
2 = invalid CLI arguments, 3 = license blocked, 130 = interrupted.
"""

import argparse
import hashlib
import json
import re
import sys
import time
import traceback
from contextlib import ExitStack, contextmanager
from importlib.metadata import version
from pathlib import Path


@contextmanager
def step(name):
    print(f"\n[START] {name}", flush=True)
    started = time.perf_counter()
    try:
        yield
    except (
        AttributeError,
        ImportError,
        IndexError,
        KeyError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
        KeyboardInterrupt,
    ) as exc:
        exc.add_note(f"Smoke test step: {name}")
        print(f"[FAILED] {name}: {time.perf_counter() - started:.3f} s", flush=True)
        raise
    else:
        print(f"[DONE] {name}: {time.perf_counter() - started:.3f} s", flush=True)


def read_export(path):
    if not path.is_file():
        print(f"No export index at {path}; use SDK module metadata.")
        return None
    text = path.read_text(encoding="utf-8-sig")
    match = re.search(r"window\.DATA_JSON\s*=\s*", text)
    if match is None:
        raise ValueError(f"No window.DATA_JSON in export index: {path}")
    # Read the JSON data only; never execute the HTML/JavaScript.
    solutions, _ = json.JSONDecoder().raw_decode(text[match.end() :])
    modules = [module for solution in solutions for module in solution["Modules"]]
    for module in modules:
        print(f"Export module: {module}")
    return modules


def select_module(infos, exported, requested_id, vimo):
    candidates = [info for info in infos if info.type == vimo.ModuleType.SEGMENTATION]
    if exported is not None:
        exported_ids = {
            str(module["ID"])
            for module in exported
            if module["ModuleType"] == "segmentation"
        }
        candidates = [info for info in candidates if info.id in exported_ids]
    if requested_id is not None:
        candidates = [info for info in candidates if info.id == requested_id]
    if len(candidates) != 1:
        raise ValueError(
            "Cannot select exactly one confirmed segmentation module; "
            f"candidates={[info.id for info in candidates]}. "
            "Check the matching export index and/or supply --module-id."
        )
    print(f"Selected segmentation module: {candidates[0]!r}")
    return candidates[0].id


def stage_model(source):
    # Vendor docs: tune-performance/trt.html says the SDK writes a cache beside
    # the model. Keep that cache away from vendor/ and reuse it on later runs.
    data = source.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    directory = Path(__file__).resolve().parents[1] / "outputs" / "vimo_smoke" / digest
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "model.vimosln"
    if target.exists():
        if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            raise ValueError(f"Staged model checksum mismatch: {target}")
    else:
        target.write_bytes(data)
    print(f"Source model: {source}\nSHA256: {digest}\nRuntime model: {target}")
    print("TensorRT cache, if created, stays beside the runtime copy.")
    return target


def configure_regions(module, vimo):
    params = module.get_params()
    print(f"Parameters type: {type(params)}")
    if not isinstance(params, vimo.SegmentationParams):
        raise TypeError(f"Expected SegmentationParams, got {type(params)}")
    print(f"Original with_region_info: {params.with_region_info}")
    params.with_region_info = True
    flags = vimo.SegRegionInfoFlag
    params.region_info_flag |= (
        flags.AREA
        | flags.LONG_SIDE
        | flags.SHORT_SIDE
        | flags.OUTER_CONTOUR
        | flags.INNER_CONTOURS
        | flags.BOUNDING_RECT
        | flags.MIN_AREA_RECT
    )
    module.set_params(params)
    active = module.get_params()
    for name in (
        "with_region_info",
        "region_info_flag",
        "with_mask",
        "with_score_map",
        "area_thresholds",
        "width_thresholds",
        "height_thresholds",
        "long_side_thresholds",
        "short_side_thresholds",
        "roi",
    ):
        print(f"Active {name}: {getattr(active, name)!r}")
    print(
        "Using SDK module parameters with region output enabled in memory; "
        "vimo_params.json is not loaded or modified by this single-module test."
    )


def print_response(response, vimo):
    print(f"Response Python type: {type(response)}")
    if not isinstance(response, vimo.SegmentationResponse):
        raise TypeError(f"Expected SegmentationResponse, got {type(response)}")
    for name in ("type", "roi", "rotated_roi", "label_map"):
        print(f"  {name}: {getattr(response, name)!r}")
    for name in ("mask", "score_map"):
        array = getattr(response, name)
        print(f"  {name}: type={type(array)}, shape={array.shape}, dtype={array.dtype}")
    print(f"  region_infos: count={len(response.region_infos)}")
    # Print every region's measurements, but only a preview of long contours.
    for index, region in enumerate(response.region_infos):
        print(
            f"  region[{index}] type={type(region)}: id={region.id}, "
            f"label={region.label!r}, area={region.area}, "
            f"long_side={region.long_side}, short_side={region.short_side}, "
            f"bounding_rect={region.bounding_rect}, min_area_rect={region.min_area_rect}"
        )
        print(
            f"    outer_contour: count={len(region.outer_contour)}, "
            f"first_5={region.outer_contour[:5]}"
        )
        print(
            f"    inner_contours: count={len(region.inner_contours)}, "
            f"point_counts={[len(contour) for contour in region.inner_contours]}"
        )
    if not response.region_infos:
        print("  No regions returned; this alone does not establish an OK judgment.")


def test_serial(module, paths, requests, warmup, vimo):
    with step("first single-image inference (cold; excluded from averages)"):
        started = time.perf_counter()
        response = module.run(requests[0])
        cold_ms = (time.perf_counter() - started) * 1000
    print(f"First image: {paths[0].name}; cold run: {cold_ms:.3f} ms")
    with step("inspect first single-image response"):
        print_response(response, vimo)

    with step(f"single-image warmup ({warmup} calls per input shape, excluded)"):
        warmed_shapes = set()
        for request in requests:
            shape = request.image.shape
            if shape not in warmed_shapes:
                for _ in range(warmup):
                    module.run(request)
                warmed_shapes.add(shape)

    with step(f"serial inference on {len(requests)} images after warmup"):
        durations, responses = [], []
        total_started = time.perf_counter()
        for path, request in zip(paths, requests):
            started = time.perf_counter()
            try:
                result = module.run(request)
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                exc.add_note(f"Serial image: {path}")
                raise
            durations.append((time.perf_counter() - started) * 1000)
            responses.append(result)
        total_ms = (time.perf_counter() - total_started) * 1000
    print(
        f"Serial total: {total_ms:.3f} ms; sum of run calls: {sum(durations):.3f} ms; "
        f"mean per image: {sum(durations) / len(durations):.3f} ms"
    )
    with step("inspect serial responses (outside inference timing)"):
        for path, duration, response in zip(paths, durations, responses):
            print(f"\n{path.name}: {duration:.3f} ms")
            print_response(response, vimo)


def test_batch(module, paths, requests, warmup, vimo):
    if module.max_batch_size < 6:
        print(
            f"[SKIP] batch=6: SDK reports max_batch_size={module.max_batch_size}; "
            "batch=6 was not validated."
        )
        return
    if len({request.image.shape for request in requests}) != 1:
        print(
            "Input shapes differ; the vendor docs warn that some models cannot "
            "accelerate mixed-size batches. No images will be resized."
        )
    with step(f"batch=6 warmup ({warmup} calls, excluded)"):
        for index in range(warmup):
            started = time.perf_counter()
            module.run(requests)
            print(
                f"Batch warmup {index + 1}: {(time.perf_counter() - started) * 1000:.3f} ms"
            )
    with step("batch=6 inference after warmup"):
        started = time.perf_counter()
        responses = module.run(requests)
        total_ms = (time.perf_counter() - started) * 1000
    print(
        f"Batch total: {total_ms:.3f} ms; amortized per image: {total_ms / 6:.3f} ms "
        "(not individual image latency)"
    )
    with step("inspect batch responses (outside inference timing)"):
        print(f"Batch return type: {type(responses)}; count={len(responses)}")
        if not isinstance(responses, list) or len(responses) != 6:
            raise ValueError("Expected a list of six batch responses")
        for path, response in zip(paths, responses):
            print(f"\nBatch image: {path.name}")
            print_response(response, vimo)


def run(args, vimo, cv2, np):
    with step("validate inputs and read vendor export metadata"):
        source = args.model.resolve(strict=True)
        directory = args.image_dir.resolve(strict=True)
        if not source.is_file() or source.suffix.lower() != ".vimosln":
            raise ValueError(f"Expected a .vimosln file: {source}")
        if not directory.is_dir():
            raise NotADirectoryError(str(directory))
        index = (
            args.index.resolve(strict=True)
            if args.index
            else source.with_name("index.html")
        )
        exported = read_export(index)
        paths = sorted(
            (
                path
                for path in directory.iterdir()
                if path.is_file()
                and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".bmp"}
            ),
            key=lambda path: path.name.lower(),
        )[:6]
        if not paths:
            raise ValueError(f"No PNG/JPG/JPEG/BMP images in {directory}")
    with step("stage model copy (file I/O; not initialization timing)"):
        runtime_model = stage_model(source)

    with ExitStack() as solution_stack:
        with step("load Solution"):
            solution = solution_stack.enter_context(vimo.Solution(str(runtime_model)))
        with step("inspect Solution and select segmentation module"):
            infos = solution.module_info_list
            for info in infos:
                print(f"SDK module: {info!r}")
            print(f"Module connections: {solution.edge_list}")
            module_id = select_module(infos, exported, args.module_id, vimo)

        with step("read up to six real images and create BGR requests"):
            requests = []
            for path in paths:
                image = cv2.imdecode(
                    np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR
                )
                if image is None:
                    raise ValueError(f"OpenCV could not decode image: {path}")
                print(f"{path}: shape={image.shape}, dtype={image.dtype}, BGR")
                requests.append(vimo.Request(image))

        for max_batch in (1, 6):
            if max_batch == 6 and len(requests) != 6:
                print(
                    f"[SKIP] batch=6 requires six real images; found {len(requests)}."
                )
                break
            with ExitStack() as module_stack:
                with step(f"initialize GPU module, requested max_batch={max_batch}"):
                    module = module_stack.enter_context(
                        solution.create_module(
                            module_id, True, args.device_id, max_batch
                        )
                    )
                print(
                    f"Module type: {type(module)}; info={module.info!r}; "
                    f"actual max_batch_size={module.max_batch_size}"
                )
                with step("enable segmentation region output in memory"):
                    configure_regions(module, vimo)
                if max_batch == 1:
                    test_serial(module, paths, requests, args.warmup, vimo)
                else:
                    test_batch(module, paths, requests, args.warmup, vimo)
    print(
        "\n[COMPLETE] Requested smoke-test steps finished; check any batch SKIP above."
    )
    print(
        "Inference timings exclude model staging, initialization, image I/O, "
        "request construction, warmup and result printing."
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", type=Path, help="Source .vimosln model (read-only)")
    parser.add_argument(
        "image_dir", type=Path, help="Directory containing real test images"
    )
    parser.add_argument(
        "--index", type=Path, help="Export index.html; default: beside source model"
    )
    parser.add_argument(
        "--module-id", help="Select a confirmed segmentation module if ambiguous"
    )
    parser.add_argument(
        "--device-id", type=int, default=0, help="GPU device ID (default: 0)"
    )
    parser.add_argument(
        "--warmup",
        type=int,
        default=5,
        help="Warmup calls per shape/batch (default: 5)",
    )
    args = parser.parse_args()
    if args.warmup < 1 or args.device_id < 0:
        parser.error("--warmup must be >= 1 and --device-id must be >= 0")
    vimo = None
    try:
        with step("import ViMo SDK and existing image dependencies"):
            import cv2
            import numpy as np
            import smartmore.vimo.inference as vimo

            print(f"Python: {sys.version}")
            for package in (
                "smartmore.vimo.inference",
                "smartmore.vimo.inference.custom",
            ):
                print(f"{package}: {version(package)}")
        run(args, vimo, cv2, np)
        return 0
    except KeyboardInterrupt:
        traceback.print_exc()
        return 130
    except (
        AttributeError,
        ImportError,
        IndexError,
        KeyError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        traceback.print_exc()  # Full original type, message, stack, and step notes.
        if vimo is not None and isinstance(exc, vimo.VimoException):
            print(
                f"SDK error code: {exc.code!r}\nSDK error message: {exc.message}",
                file=sys.stderr,
            )
        message = str(exc).lower()
        license_error = (
            vimo is not None
            and isinstance(exc, vimo.VimoException)
            and exc.code == vimo.ErrorCode.LICENSE_ERROR
        )
        if license_error or any(
            word in message for word in ("license", "virbox", "授权", "许可")
        ):
            print(
                "[BLOCKED: LICENSE] ViMo/Virbox licensing failed at the step above. "
                "Stopped without retry or bypass; inference is not validated.",
                file=sys.stderr,
            )
            return 3
        if any(
            word in message for word in ("dll", "tensorrt", "nvinfer", "cuda", "cudnn")
        ):
            print(
                "[STOPPED: RUNTIME] See the original runtime error above. "
                "No dependencies or environment settings were changed.",
                file=sys.stderr,
            )
        return 1


if __name__ == "__main__":
    # Preserve Chinese labels and SDK errors even with redirected Windows output.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="backslashreplace")
    sys.exit(main())
