"""Serial ViMo replay worker for verified historical camera images."""

import json
import sys
from contextlib import ExitStack
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from multiprocessing import get_context
from multiprocessing.connection import PipeConnection
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import cast
from zipfile import ZipFile

import cv2

from capsule_vision.inference import VimoSegmentationEngine
from capsule_vision.inference.errors import VimoInferenceError, VimoResponseError

from .evidence import Frame
from .replay import (
    CameraImage,
    ImageUnavailable,
    ReplaySetup,
    analysis_owner,
    cached_frame,
    capture,
    file_sha,
    verified_engine,
    verified_png,
)

# The original 3.20.0.2 Windows wheel independently verified in AREA-STUDY-1.
_STUDY_WHEEL = "a7ac1f96a4136fd2117daa91c748f1adbc056ea4a004b28c3c6efe2af217775f"


def _study_binding(config: ReplaySetup) -> None:
    """Allow the verified workstation binding in this child only, without installing it."""
    try:
        version("smartmore-vimo-inference")
        return
    except PackageNotFoundError:
        pass
    receipt = config.study.directory / "repeat-check.json"
    if not receipt.is_file():
        raise ValueError(
            f"ViMo {config.study.sdk} is unavailable in the replay environment"
        )
    proof = json.loads(receipt.read_bytes())
    root = Path(proof["cache_binding"]).resolve(strict=True)
    wheel = Path(proof["original_wheel"])
    if (
        proof["sdk_binding_version"] != config.study.sdk
        or file_sha(wheel) != _STUDY_WHEEL
    ):
        raise ValueError("The study's SDK wheel identity differs")
    with ZipFile(wheel) as package:
        names = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
        if names != set(package.namelist()) or any(
            (root / n).read_bytes() != package.read(n) for n in names
        ):
            raise ValueError(
                "The installed study binding differs from the verified wheel"
            )
    sys.dont_write_bytecode = True
    sys.path.append(str(root))


@dataclass(frozen=True)
class Outcome:
    frame: Frame | None = None
    problem: str | None = None
    reused: bool = False


def serve_replay(pipe: PipeConnection, config: ReplaySetup, model: Path) -> None:
    """Only small job descriptors and owned region evidence cross the process boundary."""
    try:
        with analysis_owner(config.cache), ExitStack() as resources:
            if (
                file_sha(config.study.package / "history.sqlite3")
                != config.database_sha
            ):
                raise ValueError("History changed since discovery. Reopen the archive.")
            if file_sha(model) != config.study.model_sha:
                raise ValueError(
                    "Supplied model differs from the verified historical model"
                )
            engine: VimoSegmentationEngine | None = None
            while (job := cast(CameraImage | None, pipe.recv())) is not None:
                try:
                    data = verified_png(config, job)
                    try:
                        frame = cached_frame(config, job)
                    except ValueError, TypeError, KeyError, AttributeError:
                        frame = None
                    if frame is not None:
                        pipe.send(Outcome(frame=frame, reused=True))
                        continue
                    if engine is None:
                        _study_binding(config)
                        engine = resources.enter_context(verified_engine(config, model))
                    try:
                        frame = capture(config, job, engine, data)
                    except (
                        VimoInferenceError,
                        VimoResponseError,
                        ValueError,
                        cv2.error,
                    ) as exc:
                        problem = str(exc)
                    else:
                        pipe.send(Outcome(frame=frame))
                        continue
                except ImageUnavailable as exc:
                    problem = str(exc)
                pipe.send(Outcome(problem=problem))
        pipe.send(None)
    except EOFError, BrokenPipeError:
        pass
    except Exception as exc:
        pipe.send(f"{type(exc).__name__}: {exc}")
    finally:
        pipe.close()


class ReplayClient:
    """Serial client for the process that verifies inputs and owns the native engine."""

    def __init__(self, config: ReplaySetup, model: Path) -> None:
        self.config = config
        self.model = model
        self.pipe: PipeConnection | None = None
        self.process: BaseProcess | None = None

    def request(self, job: CameraImage) -> Outcome:
        if self.pipe is None:
            ctx = get_context("spawn")
            parent, child = ctx.Pipe()
            process = ctx.Process(
                target=serve_replay,
                args=(child, self.config, self.model),
                name="ArchiveViMoReplay",
            )
            try:
                process.start()
            except BaseException:
                parent.close()
                raise
            finally:
                child.close()
            self.pipe, self.process = parent, process
        self.pipe.send(job)
        reply: object = self.pipe.recv()
        if not isinstance(reply, Outcome):
            raise RuntimeError(str(reply or "Replay process closed unexpectedly"))
        return reply

    def close(self) -> None:
        if self.pipe is None or self.process is None:
            return
        pipe, process = self.pipe, self.process
        self.pipe = self.process = None
        try:
            try:
                pipe.send(None)
                final: object = pipe.recv()
                if isinstance(final, str):
                    raise RuntimeError(final)
            except EOFError, BrokenPipeError, ConnectionResetError:
                pass
        finally:
            pipe.close()
            process.join()
            code = process.exitcode
            process.close()
        if code != 0:
            raise RuntimeError(f"Replay process exited with code {code}")
