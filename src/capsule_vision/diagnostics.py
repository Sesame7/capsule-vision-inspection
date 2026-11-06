"""Optional, process-local software observations. Only the writer touches files."""

import json
import os
import re
import sys
from collections import deque
from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite
from pathlib import Path
from threading import Condition, Thread
from time import monotonic_ns, time_ns
from typing import BinaryIO, Literal, cast
from uuid import uuid4

if os.name == "nt":
    import msvcrt
else:
    import fcntl

type DiagnosticLevel = Literal["standard", "detailed"]
type Field = str | int | float | bool | None | tuple[int, ...]
_SCHEMA = "capsule.runtime.v1"
_SEGMENT = re.compile(r"capsule-runtime-v1-([0-9a-f]{32})-[0-9]{8}\.jsonl")


def _owned(path: Path) -> bool:
    match = _SEGMENT.fullmatch(path.name)
    if not match or path.is_symlink() or not path.is_file():
        return False
    with path.open("rb") as stream:
        header = stream.readline(64 * 1024)
    try:
        value: object = json.loads(header)
    except ValueError, RecursionError:
        return False
    if not isinstance(value, dict):
        return False
    row = cast(dict[str, object], value)
    return row.get("schema") == _SCHEMA and row.get("session_id") == match[1]


def _bounded(value: Field) -> bool:
    if type(value) is str:
        return len(value) <= 512
    if type(value) is tuple:
        return len(value) <= 4 and all(
            type(v) is int and v.bit_length() <= 128 for v in value
        )
    if type(value) is int:
        return value.bit_length() <= 128
    if type(value) is float:
        return isfinite(value)
    return value is None or type(value) is bool


@dataclass(frozen=True, slots=True)
class DiagnosticStatus:
    written: int
    dropped: int
    error: str


@dataclass(frozen=True, slots=True)
class _Observation:
    event: str
    utc_ns: int
    monotonic_ns: int
    fields: dict[str, Field]


@contextmanager
def _directory_lease(directory: Path) -> Generator[None]:
    # Two launches must never prune one another's open segments. The OS releases
    # this advisory lease even on abnormal exit. The empty lock file can remain.
    directory.mkdir(parents=True, exist_ok=True)
    lock = directory / "capsule-runtime-v1.lock"
    if directory.is_symlink() or directory.is_junction() or lock.is_symlink():
        raise OSError("Diagnostic storage is redirected")
    with lock.open("a+b") as stream:
        stream.seek(0)
        if os.name == "nt":
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


class RuntimeDiagnostics:
    def __init__(
        self,
        directory: Path,
        level: DiagnosticLevel,
        *,
        capacity: int = 2048,
        segment_bytes: int = 10 * 1024 * 1024,
        segments: int = 10,
    ) -> None:
        if min(capacity, segment_bytes, segments) < 1:
            raise ValueError("Diagnostic limits must be positive")
        self.directory = directory
        self._level: DiagnosticLevel = level
        self.session_id, self._pid = uuid4().hex, os.getpid()
        self._capacity, self._segment_bytes, self._segments = (
            capacity,
            segment_bytes,
            segments,
        )
        self._condition = Condition()
        self._queue: deque[_Observation] = deque()
        self._closed = False
        self._written = self._dropped = 0
        self._error = ""
        self._thread = Thread(target=self._run, name="RuntimeDiagnostics", daemon=False)
        try:
            self._thread.start()
        except RuntimeError as exc:
            self._failed(exc, 0)

    @property
    def level(self) -> DiagnosticLevel:
        return self._level

    @property
    def detailed(self) -> bool:
        return self._level == "detailed"

    def record(self, event: str, **fields: Field) -> None:
        # Call sites supply only small identifiers, codes and measurements, never
        # Future/exception/Frame objects. Reject oversized values without retaining them.
        with self._condition:
            if self._closed or self._error or len(self._queue) >= self._capacity:
                self._dropped += 1
                return
            if (
                len(event) > 80
                or len(fields) > 24
                or not all(len(k) <= 64 and _bounded(v) for k, v in fields.items())
            ):
                self._dropped += 1
                self._condition.notify()
                return
            self._queue.append(_Observation(event, time_ns(), monotonic_ns(), fields))
            self._condition.notify()

    def snapshot(self) -> DiagnosticStatus:
        with self._condition:
            return DiagnosticStatus(self._written, self._dropped, self._error)

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify()
        if self._thread.ident is not None:
            self._thread.join()

    def _failed(self, exc: Exception, in_flight: int) -> None:
        with self._condition:
            self._error = type(exc).__name__
            self._dropped += len(self._queue) + in_flight
            self._queue.clear()
        try:
            print(f"Runtime diagnostics unavailable ({self._error})", file=sys.stderr)
        except OSError, ValueError:
            pass

    def _segment(self, index: int) -> BinaryIO:
        # Exclusive creation and a narrowly reserved filename grammar. Never
        # follow a link or treat other JSONL, archives or application data as logs.
        owned = sorted(
            (p.stat().st_mtime_ns, p) for p in self.directory.iterdir() if _owned(p)
        )
        size = sum(p.stat().st_size for _, p in owned)
        budget = self._segment_bytes * self._segments
        while owned and (
            len(owned) >= self._segments or size + self._segment_bytes > budget
        ):
            _, oldest = owned.pop(0)
            size -= oldest.stat().st_size
            oldest.unlink()
        name = f"capsule-runtime-v1-{self.session_id}-{index:08d}.jsonl"
        return (self.directory / name).open("xb", buffering=0)

    def _encode(self, obs: _Observation) -> bytes:
        utc = datetime.fromtimestamp(obs.utc_ns / 1e9, UTC).isoformat(
            timespec="microseconds"
        )
        row = dict(
            schema=_SCHEMA,
            utc=utc,
            monotonic_ns=obs.monotonic_ns,
            event=obs.event,
            pid=self._pid,
            session_id=self.session_id,
            **obs.fields,
        )
        return (
            json.dumps(row, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            + "\n"
        ).encode()

    def _run(self) -> None:
        reported = in_flight = index = size = last_loss_ns = 0
        stream: BinaryIO | None = None
        try:
            with _directory_lease(self.directory), ExitStack() as files:
                while True:
                    with self._condition:
                        while (
                            not self._queue
                            and not self._closed
                            and self._dropped == reported
                        ):
                            self._condition.wait()
                        if self._dropped != reported and (
                            not self._queue
                            or monotonic_ns() - last_loss_ns >= 1_000_000_000
                        ):
                            last_loss_ns = monotonic_ns()
                            count = self._dropped - reported
                            reported = self._dropped
                            row = _Observation(
                                "diagnostics.loss",
                                time_ns(),
                                monotonic_ns(),
                                {"dropped": count},
                            )
                        elif self._queue:
                            row = self._queue.popleft()
                        else:
                            return
                    in_flight = int(row.event != "diagnostics.loss")
                    line = self._encode(row)
                    if len(line) > self._segment_bytes:
                        raise ValueError("Diagnostic record exceeds segment budget")
                    if stream is None or size + len(line) > self._segment_bytes:
                        files.close()
                        stream = files.enter_context(self._segment(index))
                        index += 1
                        size = 0
                    if stream.write(line) != len(line):
                        raise OSError("Incomplete diagnostic write")
                    size += len(line)
                    with self._condition:
                        self._written += in_flight
                    in_flight = 0
        except Exception as exc:
            self._failed(exc, in_flight)


@dataclass(slots=True)
class RunDiagnostics:
    """Shared by one generation only. Run is bound at activation before frame admission."""

    writer: RuntimeDiagnostics
    generation: int
    recipe_id: str
    run_id: str | None = None

    def record(self, event: str, **fields: Field) -> None:
        self.writer.record(
            event,
            generation=self.generation,
            recipe_id=self.recipe_id,
            run_id=self.run_id,
            **fields,
        )
