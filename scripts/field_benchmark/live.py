"""Finite backend campaign orchestration; Production remains the passive owner."""

import json
import subprocess
import sys
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from threading import enumerate as threads
from time import monotonic
from typing import Protocol, cast

from vimo_demo.application import ApplicationState, ProductionApplicationSnapshot
from vimo_demo.application.basler_connection import SourceConnectionSnapshot
from vimo_demo.application.basler_control import BaslerControlSnapshot
from vimo_demo.application.continuous_basler import ContinuousArmedAcquisition
from vimo_demo.application.local_replay import ReplaySnapshot
from vimo_demo.communication.simulator import FinsUdpSimulator
from vimo_demo.configuration import load_camera_sources, load_machine_layout
from vimo_demo.ui.local_production import compose, real_engine

from .configuration import Mode, RunFiles, digest, prepare_run
from .hosting import join_detached, start_detached
from .ledger import Fields, Ledger
from .observations import Observations
from .resources import Sampler, require_resources, resource_sample
from .schedule import BurstPlan
from .target import IsolatedTarget
from .trigger import (
    FIELD_ENDPOINT,
    PhotoClient,
    Readiness,
    SupervisorSettings,
    require_manual_idle,
)


@dataclass(frozen=True, slots=True)
class Stage:
    name: str
    mode: Mode
    groups: int
    pulses: int


STAGES = (
    Stage("single", Mode.B, 1, 1),
    Stage("burst", Mode.B, 2, 8),
    *(Stage(mode.value, mode, 20, 80) for mode in Mode),
)


class Backend(Protocol):
    def connect_source(self) -> None: ...
    def disconnect_source(self) -> None: ...
    def start(self) -> Future[None]: ...
    def snapshot(self) -> ProductionApplicationSnapshot: ...
    def source_snapshot(self) -> SourceConnectionSnapshot: ...
    def control_snapshot(self) -> ReplaySnapshot | None: ...
    def stop(self) -> Future[None]: ...
    def close(self) -> Future[None]: ...
    def join(self, timeout: float | None = None) -> bool: ...


def claim_stage(campaign: Path, name: str) -> tuple[Stage, Path]:
    selected = next(row for row in STAGES if row.name == name)
    campaign.mkdir(parents=True, exist_ok=True)
    for previous in STAGES[: STAGES.index(selected)]:
        outcome = campaign / previous.name / "stage-result.json"
        if (
            not outcome.exists()
            or json.loads(outcome.read_text(encoding="utf-8")).get("passed") is not True
        ):
            raise RuntimeError(
                f"Previous stage {previous.name} did not pass; no escalation"
            )
    directory = campaign / name
    directory.mkdir(exist_ok=False)  # An attempted/blocked stage is never replaced.
    with (directory / "claim.json").open("x", encoding="utf-8") as stream:
        json.dump(
            {
                "stage": name,
                "pulse_budget": selected.pulses,
                "campaign_max": sum(row.pulses for row in STAGES),
            },
            stream,
        )
    return selected, directory


def counters(snapshot: ProductionApplicationSnapshot) -> Fields:
    c = snapshot.counters
    return {
        "generation": snapshot.generation,
        "state": snapshot.state.name,
        "cycles_started": c.cycles_started,
        "cycles_completed": c.cycles_completed,
        "business_ok": c.business_ok,
        "business_ng": c.business_ng,
        "business_empty": c.business_empty,
        "business_unknown": c.business_unknown,
        "technical_error_cycles": c.technical_error_cycles,
        "production_ng_cycles": c.production_ng_cycles,
    }


def require_admission(app: Backend) -> ProductionApplicationSnapshot:
    state, source, control = (
        app.snapshot(),
        app.source_snapshot(),
        app.control_snapshot(),
    )
    if (
        state.state is not ApplicationState.RUNNING
        or not state.runtime_owned
        or source.inspection_generation != state.generation
    ):
        raise RuntimeError("Production is not attached RUNNING admission")
    if not source.thread_alive or source.state.name != "CONNECTED":
        raise RuntimeError("Source is not connected")
    if (
        len(source.cameras) != 6
        or not isinstance(control, BaslerControlSnapshot)
        or not control.admission_armed
    ):
        raise RuntimeError("Six cameras are not actually ARMED for inspection")
    if (
        source.received_frames
        or source.discarded_frames
        or state.counters.cycles_started
        or state.counters.cycles_completed
    ):
        raise RuntimeError(
            "Unexpected pre-stimulus frames/counters; no warm-up isolation"
        )
    return state


def drive_backend(
    app: Backend,
    obs: Observations,
    trigger: Callable[[ProductionApplicationSnapshot], str],
    pulses: int,
) -> dict[str, object]:
    outcome: dict[str, object] = {"trigger": "not_started", "cleanup": False}
    try:
        app.connect_source()
        source = app.source_snapshot()
        for camera in source.cameras:
            obs.ledger.emit(
                "connected_camera",
                camera=str(camera.camera_id),
                serial=camera.serial,
                width=camera.width,
                height=camera.height,
                model=camera.model,
            )
        app.start().result()  # Backend thread; native work is not cancelled.
        state = require_admission(app)
        obs.ledger.emit("production_ready", **counters(state))
        outcome["trigger"] = trigger(state)
        if not obs.wait_cycles(pulses, 5):
            obs.fail("Expected terminal cycles did not settle")
    except Exception as exc:
        obs.ledger.emit("run_failure", issue=f"{type(exc).__name__}: {exc}"[:1200])
        obs.fail(str(exc))
        outcome["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        obs.ledger.emit("stop_requested")
        try:
            if app.snapshot().state in (
                ApplicationState.RUNNING,
                ApplicationState.STARTING,
            ):
                app.stop().result()
            state = app.snapshot()
            obs.ledger.emit("accounting_final", **counters(state))
            source = app.source_snapshot()
            obs.ledger.emit(
                "source_final",
                received=source.received_frames,
                discarded=source.discarded_frames,
                alive=source.thread_alive,
                generation=source.inspection_generation,
            )
            outcome["accounting"] = counters(state)
        except Exception as exc:
            obs.ledger.emit("cleanup_failure", issue=str(exc)[:1200])
            obs.fail(f"Stop: {exc}")
            outcome["stop_error"] = str(exc)
        try:
            app.disconnect_source()
        except Exception as exc:
            obs.ledger.emit("cleanup_failure", issue=str(exc)[:1200])
            obs.fail(f"Disconnect: {exc}")
            outcome["disconnect_error"] = str(exc)
        try:
            app.close().result()
            joined = app.join()
            obs.owner(
                "application", joined, joined and not app.snapshot().runtime_owned
            )
            outcome["cleanup"] = joined and not app.source_snapshot().thread_alive
        except Exception as exc:
            obs.ledger.emit("cleanup_failure", issue=str(exc)[:1200])
            obs.fail(f"Cleanup: {exc}")
            # Keep actual resources/threads observable. Never kill or pretend idle.
            outcome["cleanup_error"] = str(exc)
    return outcome


def execute(
    campaign: Path,
    stage_name: str,
    *,
    model: Path,
    machine: Path,
    recipe: Path,
    sources: Path,
) -> Path:
    stage, stage_dir = claim_stage(campaign, stage_name)
    reserved: FinsUdpSimulator | None = None
    obs: Observations | None = None
    target: IsolatedTarget | None = None
    sampler: Sampler | None = None
    ledger: Ledger | None = None
    files: RunFiles | None = None
    outcome: dict[str, object] = {"cleanup": False, "trigger": "not_started"}
    try:

        def committed(sequence: int, words: tuple[int, int, int]) -> None:
            assert obs is not None
            obs.target(sequence, words)

        if stage.mode in (Mode.R, Mode.RA):
            reserved = FinsUdpSimulator(port=0, on_first_station_write=committed)
        files = prepare_run(
            stage_dir,
            stage.mode,
            model_file=model,
            machine_file=machine,
            recipe_file=recipe,
            sources_file=sources,
            simulator_port=reserved.port if reserved else None,
        )
        manifest = cast(
            dict[str, object], json.loads((files.root / "manifest.json").read_text())
        )
        manifest.update(
            live_field=True,
            stage=stage.name,
            pulse_budget=stage.pulses,
            groups=stage.groups,
            revision=subprocess.check_output(
                ["git", "rev-parse", "HEAD"], text=True
            ).strip(),
        )
        (files.root / "manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )
        pin = campaign / "frozen-inputs.json"
        if pin.exists():
            if json.loads(pin.read_text()) != manifest["source_hashes"]:
                raise RuntimeError("Saved field definitions changed between stages")
        else:
            with pin.open("x", encoding="utf-8") as stream:
                json.dump(manifest["source_hashes"], stream)
        layout = load_machine_layout(files.machine).layout
        camera_sources = load_camera_sources(files.sources)
        if camera_sources.mode != "basler":
            raise RuntimeError("Live benchmark requires the saved Basler source mode")
        mapping = tuple(
            zip(
                layout.camera_ids,
                camera_sources.basler_serials(layout.camera_ids),
                strict=True,
            )
        )
        ledger = Ledger(files.root / "backend.jsonl", files.run, "backend")
        obs = Observations(ledger, files.run, mapping, strict_run=True)
        initial = resource_sample(files.root)
        ledger.emit("resources", **initial)
        require_resources(initial)
        with PhotoClient(
            *FIELD_ENDPOINT[:1], port=FIELD_ENDPOINT[1], timeout=0.5, live=True
        ) as plc:
            preflight = plc.preflight()
            ledger.emit("plc_preflight", **preflight)
            require_manual_idle(preflight)
        if reserved:
            target = IsolatedTarget(obs, stage.pulses, server=reserved)
            assert files.reporting == target.settings
        sampler = Sampler(obs, files.root)
        sampler.start()
        observer = obs

        def armed(row: ContinuousArmedAcquisition) -> None:
            observer.ledger.emit("source_armed", armed_at=row.armed_at)

        def observe_source(future: Future[None]) -> None:
            def finished(done: Future[None]) -> None:
                error = (
                    done.exception()
                    if not done.cancelled()
                    else RuntimeError("cancelled")
                )
                observer.ledger.emit(
                    "source_finished", issue=str(error)[:1200] if error else None
                )
                if error:
                    observer.fail(f"Acquisition: {error}")

            future.add_done_callback(finished)

        def stimulate(state: ProductionApplicationSnapshot) -> str:
            assert files is not None and ledger is not None
            with PhotoClient(
                FIELD_ENDPOINT[0], port=FIELD_ENDPOINT[1], timeout=0.5, live=True
            ) as plc:
                fresh = plc.preflight()
                ledger.emit("plc_preflight", **fresh)
                require_manual_idle(fresh)
            now = monotonic()
            plan = BurstPlan(
                now + 3, stage.groups, stage.pulses, now + 3 + stage.groups * 0.5
            )
            gate = Readiness(
                files.run,
                state.generation,
                files.run,
                digest(files.root / "manifest.json"),
                plan.end + 2,
                True,
                True,
                True,
                True,
            )
            settings = SupervisorSettings(
                files.run,
                FIELD_ENDPOINT[0],
                FIELD_ENDPOINT[1],
                plan,
                gate,
                0.020,
                1,
                0.25,
                True,
            )
            process, controller = start_detached(
                settings, files.root, campaign.parent / "photo-locks"
            )
            ledger.emit(
                "supervisor_hosting", pid=process.pid, detached=True, breakaway=True
            )
            try:
                return controller.run(ledger, observer.stop)
            finally:
                controller.connection.close()
                if not join_detached(process, ledger, 10):
                    observer.fail("Independent supervisor still owns resources")
                with PhotoClient(
                    FIELD_ENDPOINT[0], port=FIELD_ENDPOINT[1], timeout=0.5, live=True
                ) as plc:
                    low = plc.read_photo() == 0
                    ledger.emit("final_photo_readback", low=low)
                    if not low:
                        observer.fail("URGENT: final W10.7 is not low")

        with obs.composition_scope():
            composed = compose(
                files.model,
                files.machine,
                files.recipe,
                sources_file=files.sources,
                engine_factory=obs.engine(real_engine),
                reporting=files.reporting,
                observe_acquisition=armed,
                observe_source=observe_source,
            )
            outcome = drive_backend(composed.application, obs, stimulate, stage.pulses)
    except Exception as exc:
        outcome["error"] = f"{type(exc).__name__}: {exc}"
        if ledger:
            ledger.emit("run_failure", issue=str(exc)[:1200])
    finally:
        if sampler:
            outcome["sampler_closed"] = sampler.close()
        if target:
            outcome["target_closed"] = target.close()
        elif reserved:
            reserved.close()
        if files:
            outcome["run_directory"] = str(files.root)
            protected = cast(
                dict[str, str],
                json.loads((files.root / "manifest.json").read_text())["source_hashes"],
            )
            unchanged = all(
                digest(Path(path)) == sha for path, sha in protected.items()
            )
            if ledger:
                ledger.emit("saved_definitions_final", unchanged=unchanged)
            outcome["saved_definitions_unchanged"] = unchanged
        if ledger:
            outcome["ledger_closed"] = ledger.close()
        outcome["residual_runtime_threads"] = [
            t.name
            for t in threads()
            if t.name.startswith(
                (
                    "InferenceWorker",
                    "Early",
                    "LocalProduction",
                    "BaslerSource",
                    "FirstStation",
                    "ImageArchive",
                    "ProductionApplication",
                )
            )
        ]
        with (stage_dir / "execution.json").open("x", encoding="utf-8") as stream:
            json.dump(outcome, stream, ensure_ascii=False, indent=2)
    if files is None:
        raise RuntimeError(f"Readiness blocked before run preparation: {outcome}")
    subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "field_benchmark_tool.py"),
            "analyze",
            str(files.root),
        ],
        check=False,
    )
    summary = cast(
        dict[str, object], json.loads((files.root / "reconciliation.json").read_text())
    )
    passed = (
        summary["evidence_complete"] is True
        and outcome.get("trigger") == "budget_complete"
        and outcome.get("cleanup") is True
        and outcome.get("ledger_closed") is True
        and not outcome["residual_runtime_threads"]
    )
    with (stage_dir / "stage-result.json").open("x", encoding="utf-8") as stream:
        json.dump(
            {"passed": passed, "run_directory": str(files.root)}, stream, indent=2
        )
    return stage_dir
