"""Production benchmark verification with real runtime ownership and fake inference."""

import csv
import json
from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path
from threading import enumerate as threads
from threading import get_ident
from weakref import ref

import benchmark_production as bench
import numpy as np
import pytest
from numpy.typing import NDArray
from tests.application_support import prepared
from tests.production_support import cycle_result, inferred, layout
from visualize_inference import save_png

from vimo_demo.application import ProductionCycle
from vimo_demo.inference import ImageResult
from vimo_demo.inspection.production import (
    CameraTiming,
    ProductionInspectionResult,
    ProductionPipeline,
)


def test_modes_preserve_workload_and_all_non_enablement_settings(
    tmp_path: Path,
) -> None:
    base = prepared(tmp_path).configuration
    channels = list(base.channels)
    channels[16] = replace(
        channels[16],
        checks=replace(
            channels[16].checks,
            vimo=replace(
                channels[16].checks.vimo, required=False, disabled_labels=("黑点",)
            ),
        ),
    )
    base = replace(base, channels=tuple(channels))
    original = base
    assert [(m.name, m.vimo, m.size, m.color) for m in bench.MODES] == [
        ("A", True, False, False),
        ("B", True, True, False),
        ("C", True, True, True),
        ("D", False, True, True),
        ("E", True, False, True),
        ("F", False, False, True),
    ]
    for mode in bench.MODES:
        selected = bench.select_mode(base, mode)
        assert selected.layout is base.layout and selected.model is base.model
        assert (
            replace(selected.capsule.size, enabled=base.capsule.size.enabled)
            == base.capsule.size
        )
        assert (
            replace(selected.capsule.color, enabled=base.capsule.color.enabled)
            == base.capsule.color
        )
        assert all(c.checks.vimo.enabled is mode.vimo for c in selected.channels)
        for before, after in zip(base.channels, selected.channels, strict=True):
            assert replace(after, checks=before.checks) == before
            assert (
                replace(after.checks.vimo, enabled=before.checks.vimo.enabled)
                == before.checks.vimo
            )
    assert base == original


def test_preload_order_identity_and_invalid_files(tmp_path: Path) -> None:
    base = prepared(tmp_path).configuration
    paths = tuple(tmp_path / f"{i}.png" for i in range(6))
    for i, path in enumerate(paths):
        save_png(path, np.full((20, 60, 3), i, dtype=np.uint8))
    arrays = bench.preload(paths, base)
    assert [int(a[0, 0, 0]) for a in arrays] == list(range(6))
    assert all(not a.flags.writeable for a in arrays)
    with pytest.raises(ValueError, match="six"):
        bench.preload(paths[:5], base)
    paths[0].write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="decode"):
        bench.preload(paths, base)


def test_primary_timer_includes_submission_and_wait_but_not_processing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = [0.0]
    events: list[str] = []
    result = ProductionPipeline(layout()).from_cycle(cycle_result(layout()))

    class ReceiptFuture(Future[ProductionInspectionResult]):
        def result(self, timeout: float | None = None):
            events.append("result")
            value[0] += 0.03
            return result

    def submit() -> ProductionCycle:
        events.append("submit")
        value[0] += 0.01
        return ProductionCycle(Future(), ReceiptFuture())

    original = bench.result_record

    def record(*args: object):
        value[0] += 50  # Deliberately expensive report processing must be excluded.
        events.append("report")
        return original(result, bench.MODES[0])

    monkeypatch.setattr(bench, "result_record", record)
    row = bench.measure_cycle(submit, bench.MODES[0], clock=lambda: value[0])
    assert events == ["submit", "result", "report"]
    assert row["cycle_e2e_ms"] == pytest.approx(40)
    assert row["submission_ms"] == pytest.approx(10)
    assert row["result_wait_ms"] == pytest.approx(30)


def test_null_stages_and_no_partial_or_overlapping_totals() -> None:
    result = ProductionPipeline(layout()).from_cycle(cycle_result(layout()))
    cameras = tuple(replace(c, timing=CameraTiming(2, 3, 1)) for c in result.cameras)
    result = replace(result, cameras=cameras, aggregation_ms=6)
    a = bench.result_record(result, bench.MODES[0])
    assert a["traditional_ms"] is None and a["CAM1.traditional_ms"] is None
    assert a["vimo_ms"] == 18 and a["mapping_ms"] == 6
    d = bench.result_record(result, bench.MODES[3])
    assert d["traditional_ms"] == 12 and d["vimo_ms"] is None
    assert d["dispatch_ms"] is None and d["aggregation_ms"] == 6
    result = replace(
        result,
        cameras=(replace(cameras[0], timing=CameraTiming(None, None, 1)), *cameras[1:]),
    )
    assert bench.result_record(result, bench.MODES[2])["traditional_ms"] is None
    assert bench.result_record(result, bench.MODES[2])["vimo_ms"] is None


def test_statistics_boundaries_errors_and_percentile_comparisons() -> None:
    records: list[dict[str, object]] = [
        {"cycle_e2e_ms": t, "technical_status": status, "judgment": "NG"}
        for t, status in ((0, "SUCCESS"), (100, "SUCCESS"), (200, "ERROR"))
    ]
    records.append(
        {"cycle_e2e_ms": None, "technical_status": "EXCEPTION", "judgment": None}
    )
    report = bench.summary(records)
    assert report["successful"] == 2  # NG is a successful inspection.
    assert report["failed_or_error_cycles"] == 2
    assert report["over_100_ms_percent"] == pytest.approx(100 / 3)
    assert bench.number_at(report, "timings", "cycle_e2e_ms", "p50_ms") == 100
    assert bench.number_at(report, "timings", "cycle_e2e_ms", "p95_ms") == 190
    assert bench.number_at(report, "timings", "cycle_e2e_ms", "p99_ms") == 198
    assert bench.summary([])["over_100_ms_percent"] is None
    with pytest.raises(ValueError, match="Nonfinite"):
        bench.summary(
            [
                {
                    "cycle_e2e_ms": float("nan"),
                    "technical_status": "SUCCESS",
                    "judgment": "OK",
                }
            ]
        )
    runs: list[dict[str, object]] = [
        {
            "round": 1,
            "mode_name": m.name,
            "summary": bench.summary(
                [
                    {
                        "cycle_e2e_ms": 10 * i,
                        "technical_status": "SUCCESS",
                        "judgment": "OK",
                    }
                ]
            ),
        }
        for i, m in enumerate(bench.MODES)
    ]
    assert [r["p95_ms"] for r in bench.comparisons(runs)] == [10, 40, 20, -20, -20]


def test_exception_attempt_is_visible_and_not_successful_latency() -> None:
    clock = iter((1.0, 1.01, 1.05))
    receipt = ProductionCycle(Future(), Future())
    receipt.result.set_exception(RuntimeError("synthetic terminal failure"))
    row = bench.measure_cycle(
        lambda: receipt, bench.MODES[0], clock=lambda: next(clock)
    )
    assert row["cycle_e2e_ms"] is None
    assert row["attempt_ms"] == pytest.approx(50)
    assert "RuntimeError: synthetic terminal failure" in str(row["exception"])
    assert bench.summary([row])["exception_cycles"] == 1


@pytest.mark.parametrize("mode", bench.MODES)
def test_real_runtime_warmup_exclusion_and_bounded_ownership(
    tmp_path: Path, mode: bench.Mode
) -> None:
    candidate = prepared(tmp_path)
    candidate = replace(
        candidate, configuration=bench.select_mode(candidate.configuration, mode)
    )
    images = tuple(np.zeros((20, 60, 3), dtype=np.uint8) for _ in range(6))
    trace: list[tuple[str, int]] = []
    masks: list[ref[NDArray[np.uint8]]] = []

    class Engine:
        def __init__(self):
            trace.append(("create", get_ident()))

        def infer(self, image_bgr: NDArray[np.generic]) -> ImageResult:
            trace.append(("infer", get_ident()))
            # At most one previous camera result is live during serial construction,
            # never all prior cycles. Weak refs do not retain mask storage.
            assert sum(r() is not None for r in masks) <= 6
            output = inferred()
            masks.append(ref(output.mask))
            return output

        def close(self):
            trace.append(("close", get_ident()))

    rows: list[dict[str, object]] = []
    before = {t.ident for t in threads()}
    report = bench.run_session(
        candidate, images, mode, 2, 3, rows.append, engine_factory=lambda _: Engine()
    )
    assert report["completed"] and report["terminated"]
    assert bench.number_at(report, "summary", "attempted") == 3
    assert bench.number_at(report, "warmup", "attempted") == 2
    assert len(rows) == 5 and all("cycle_e2e_ms" not in row for row in rows[:2])
    assert sum(e == "infer" for e, _ in trace) == 2 + (30 if mode.vimo else 0)
    assert trace[0][0] == "create" and trace[-1][0] == "close"
    assert len({i for _, i in trace}) == 1 and trace[0][1] != get_ident()
    assert all(r() is None for r in masks)
    assert {t.ident for t in threads()} == before
    json.dumps(
        rows, allow_nan=False
    )  # Only bounded scalar/container data, no arrays/results.
    assert all(np.all(image == 0) for image in images)


@pytest.mark.parametrize("names", ["ABCDEF", "ABCEF"])
def test_cli_outputs_metadata_and_readonly_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, names: str
) -> None:
    from vimo_demo.configuration import (
        MachineLayoutDefinition,
        RecipeDefinition,
        save_machine_layout,
        save_model_definition,
        save_recipe,
    )

    candidate = prepared(tmp_path)
    model, machine, recipe = [
        tmp_path / n for n in ("model.json", "machine.json", "recipe.json")
    ]
    save_model_definition(model, candidate.configuration.model)
    save_machine_layout(
        machine, MachineLayoutDefinition(candidate.configuration.layout)
    )
    save_recipe(recipe, RecipeDefinition("test", "Test", camera_overrides=()))
    for i in range(6):
        save_png(tmp_path / f"{i}.png", np.zeros((20, 60, 3), dtype=np.uint8))
    before = {p: p.read_bytes() for p in tmp_path.iterdir()}
    output = tmp_path / "report"
    output.mkdir()

    def output_directory(kind: str) -> Path:
        return output

    monkeypatch.setattr(bench, "output_directory", output_directory)

    def info(command: list[str]) -> dict[str, object]:
        return {"value": "fake-tool-metadata", "unavailable_reason": None}

    monkeypatch.setattr(bench, "command_info", info)
    # Keep the CLI's actual session orchestration, substituting only its engine.
    real = bench.run_session

    class Engine:
        def infer(self, image_bgr: NDArray[np.generic]) -> ImageResult:
            return inferred()

        def close(self):
            pass

    def session(
        p: bench.PreparedConfiguration,
        images: tuple[NDArray[np.uint8], ...],
        mode: bench.Mode,
        warmup: int,
        cycles: int,
        emit: bench.Callable[[dict[str, object]], None],
    ):
        return real(
            p, images, mode, warmup, cycles, emit, engine_factory=lambda _: Engine()
        )

    monkeypatch.setattr(bench, "run_session", session)
    monkeypatch.setattr(
        "sys.argv",
        [
            "benchmark_production",
            "--modes",
            *names,
            "--model-config",
            str(model),
            "--machine-config",
            str(machine),
            "--recipe",
            str(recipe),
            "--frames-dir",
            str(tmp_path),
            "--warmup",
            "1",
            "--cycles",
            "2",
            "--repetitions",
            "2",
        ],
    )
    assert bench.main() == 0
    data = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert data["completed"] and len(data["inputs"]) == 6
    assert [r["mode_name"] for r in data["runs"]] == list(names + names[1:] + names[:1])
    assert len(data["configuration_files"]) == 3
    assert all(
        len(r["file_sha256"]) == 64 and len(r["pixels_sha256"]) == 64
        for r in data["inputs"]
    )
    assert {
        "git_commit",
        "os",
        "python",
        "packages",
        "gpu",
        "configuration",
        "metric_boundaries",
        "comparisons_ms",
    } <= data.keys()
    rows = list(csv.DictReader((output / "cycles.csv").open(encoding="utf-8-sig")))
    assert len(rows) == 6 * len(names) and sum(
        r["phase"] == "measured" for r in rows
    ) == 4 * len(names)
    assert all(r["cycle_e2e_ms"] == "" for r in rows if r["phase"] == "warmup")
    assert all(p.read_bytes() == content for p, content in before.items())


@pytest.mark.parametrize("failure", ["startup", "technical", "terminal"])
def test_failures_are_recorded_and_session_terminated(
    tmp_path: Path, failure: str
) -> None:
    candidate = prepared(tmp_path)
    images = tuple(np.zeros((20, 60, 3), dtype=np.uint8) for _ in range(6))
    before = {t.ident for t in threads()}

    class TerminalFailure(BaseException):
        pass

    class Engine:
        def __init__(self):
            self.calls = 0
            if failure == "startup":
                raise RuntimeError("startup failed")

        def infer(self, image_bgr: NDArray[np.generic]) -> ImageResult:
            self.calls += 1
            if self.calls > 2:
                if failure == "terminal":
                    raise TerminalFailure("runtime terminated")
                raise ValueError("ordinary image error")
            return inferred()

        def close(self):
            pass

    rows: list[dict[str, object]] = []
    report = bench.run_session(
        candidate,
        images,
        bench.MODES[0],
        0,
        3,
        rows.append,
        engine_factory=lambda _: Engine(),
    )
    assert report["terminated"] and {t.ident for t in threads()} == before
    if failure == "startup":
        assert not report["completed"] and not rows
        assert "startup failed" in str(report["lifecycle_errors"])
    elif failure == "terminal":
        assert not report["completed"]
        assert bench.number_at(report, "summary", "exception_cycles") == 1
        assert len(rows) == 1
    else:
        assert report["completed"]
        assert bench.number_at(report, "summary", "technical_error_cycles") == 3
        assert bench.number_at(report, "summary", "cycles_with_check_errors") == 3
        assert (
            bench.number_at(report, "summary", "timings", "cycle_e2e_ms", "calls") == 3
        )
        assert all("ValueError" in str(r["check_errors"]) for r in rows)
