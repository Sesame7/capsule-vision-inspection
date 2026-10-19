"""Scalar-only, finite event ledger. Callbacks enqueue; only its writer does I/O."""

import json
import math
import os
import socket
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
from time import get_clock_info, monotonic

type Scalar = str | int | float | bool | None
type Fields = dict[str, Scalar | tuple[Scalar, ...]]


@dataclass(frozen=True, slots=True)
class EventRow:
    run: str
    producer: str
    index: int
    at: float
    kind: str
    fields: Fields


class Ledger:
    def __init__(
        self,
        path: Path,
        run: str,
        producer: str,
        *,
        capacity: int = 1024,
        event_budget: int = 100000,
    ) -> None:
        if (
            not 1 <= len(run) <= 128
            or not 1 <= len(producer) <= 64
            or min(capacity, event_budget) < 1
        ):
            raise ValueError("Finite ledger identity and bounds required")
        self.path, self.run, self.producer = path, run, producer
        self.abort = Event()
        self._stop = Event()
        self._lock = Lock()
        self._queue: Queue[EventRow] = Queue(capacity)
        self._budget, self._index, self.peak = event_budget, 0, 0
        self.error: str | None = None
        self.emit_seconds = self.write_seconds = 0.0
        self._thread = Thread(target=self._write, name=f"BenchmarkLedger-{producer}")
        # Exclusive creation fails synchronously, before any diagnostic admission.
        self._stream = path.open("x", encoding="utf-8")
        self._thread.start()
        info = get_clock_info("monotonic")
        self.emit(
            "clock",
            implementation=info.implementation,
            resolution=info.resolution,
            pid=os.getpid(),
            host=socket.gethostname(),
            utc_anchor=datetime.now(UTC).isoformat(),
        )

    def emit(self, kind: str, **fields: Scalar | tuple[Scalar, ...]) -> bool:
        started = monotonic()
        # Refuse arrays, result objects, nested dictionaries and oversized strings.
        if (
            len(kind) > 64
            or any(len(key) > 64 for key in fields)
            or {"run", "producer", "index", "at", "kind"} & fields.keys()
        ):
            raise ValueError("Invalid/reserved event fields")
        characters = 0
        for value in fields.values():
            for item in value if isinstance(value, tuple) else (value,):
                if type(item) not in (str, int, float, bool, type(None)):
                    raise TypeError("Ledger accepts scalar evidence only")
                if isinstance(item, str) and len(item) > 2000:
                    self.error = "ledger text exceeds scalar bound"
                    self.abort.set()
                    return False
                characters += len(item) if isinstance(item, str) else 24
                if isinstance(item, float) and not math.isfinite(item):
                    raise ValueError("Nonfinite evidence")
            if isinstance(value, tuple) and len(value) > 72:
                self.error = "ledger tuple exceeds bound"
                self.abort.set()
                return False
        if len(fields) > 64 or characters > 16000:
            self.error = "ledger scalar budget exceeded"
            self.abort.set()
            return False
        with self._lock:
            if self.abort.is_set() or self._stop.is_set():
                return False
            if self._index >= self._budget:
                self.error = "event budget exhausted"
                self.abort.set()
                return False
            row = EventRow(
                self.run, self.producer, self._index, monotonic(), kind, fields
            )
            try:
                self._queue.put_nowait(row)
            except Full:
                self.error = "ledger queue overflow"
                self.abort.set()
                return False
            self._index += 1
            self.peak = max(self.peak, self._queue.qsize())
            self.emit_seconds += monotonic() - started
            return True

    def _write(self) -> None:
        try:
            while not self._stop.is_set() or not self._queue.empty():
                try:
                    row = self._queue.get(timeout=0.05)
                except Empty:
                    continue
                started = monotonic()
                self._stream.write(
                    json.dumps(
                        {
                            "run": row.run,
                            "producer": row.producer,
                            "index": row.index,
                            "at": row.at,
                            "kind": row.kind,
                            **row.fields,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                self._stream.flush()
                self.write_seconds += monotonic() - started
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            self.abort.set()
        finally:
            self._stream.close()

    def close(self, timeout: float = 5) -> bool:
        with self._lock:
            self._stop.set()
        self._thread.join(timeout)
        alive = self._thread.is_alive()
        if not alive:
            # Independent footer cannot disappear with a full callback queue.
            footer = self.path.with_suffix(".closed.json")
            if footer.exists():
                return self.error is None
            with footer.open("x", encoding="utf-8") as stream:
                json.dump(
                    {
                        "run": self.run,
                        "producer": self.producer,
                        "events": self._index,
                        "emit_seconds": self.emit_seconds,
                        "write_seconds": self.write_seconds,
                        "peak": self.peak,
                        "error": self.error,
                        "terminated": True,
                    },
                    stream,
                )
        return not alive and self.error is None
