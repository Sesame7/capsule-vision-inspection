"""Bounded off-callback telemetry; no GPU settings or dependency changes."""

import ctypes
import os
import shutil
import subprocess
from pathlib import Path
from threading import Event, Thread
from time import monotonic, process_time

from vimo_demo.application.folder_cache import available_memory

from .ledger import Fields
from .observations import Observations


class _ProcessMemory(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_ulong), ("faults", ctypes.c_ulong)] + [
        (name, ctypes.c_size_t)
        for name in (
            "peak_working",
            "working",
            "peak_paged",
            "paged",
            "peak_nonpaged",
            "nonpaged",
            "pagefile",
            "peak_pagefile",
            "private",
        )
    ]


def resource_sample(root: Path) -> Fields:
    started = monotonic()
    memory = available_memory()
    rss: int | None = None
    if os.name == "nt":
        counters = _ProcessMemory()
        counters.cb = ctypes.sizeof(counters)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_ProcessMemory),
            ctypes.c_ulong,
        ]
        psapi.GetProcessMemoryInfo.restype = ctypes.c_int
        if psapi.GetProcessMemoryInfo(
            ctypes.c_void_p(-1), ctypes.byref(counters), counters.cb
        ):
            rss = int(counters.working)
    response = subprocess.run(
        [
            "nvidia-smi",
            "--query-gpu=temperature.gpu,utilization.gpu,memory.used,power.draw,clocks.gr",
            "--format=csv,noheader,nounits",
        ],
        capture_output=True,
        text=True,
        timeout=3,
        check=True,
    )
    values = [float(value.strip()) for value in response.stdout.strip().split(",")]
    if len(values) != 5:
        raise ValueError("Expected one GPU and five telemetry fields")
    return {
        "available_memory_bytes": memory.available if memory else None,
        "rss_bytes": rss,
        "disk_free_bytes": shutil.disk_usage(root).free,
        "gpu_temperature_c": values[0],
        "gpu_utilization_percent": values[1],
        "gpu_memory_mib": values[2],
        "gpu_power_w": values[3],
        "gpu_graphics_mhz": values[4],
        "query_ms": (monotonic() - started) * 1000,
    }


def require_resources(row: Fields) -> None:
    memory, disk, temperature = (
        row.get(k)
        for k in ("available_memory_bytes", "disk_free_bytes", "gpu_temperature_c")
    )
    if not isinstance(memory, int) or memory < 2 * 1024**3:
        raise RuntimeError("Missing/insufficient available RAM")
    if not isinstance(disk, int) or disk < 5 * 1024**3:
        raise RuntimeError("Missing/insufficient disk reserve")
    if not isinstance(temperature, (float, int)) or temperature >= 60:
        raise RuntimeError("Missing GPU temperature or 60 C experiment guard reached")


class Sampler:
    def __init__(self, obs: Observations, root: Path) -> None:
        self.obs, self.root = obs, root
        self.stop = Event()
        self._thread = Thread(target=self._run, name="BenchmarkResourceSampler")

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        last_wall, last_cpu = monotonic(), process_time()
        while not self.stop.is_set():
            try:
                row = resource_sample(self.root)
                now, cpu = monotonic(), process_time()
                row["process_cpu_percent_of_one_core"] = (
                    (cpu - last_cpu) / (now - last_wall) * 100
                )
                last_wall, last_cpu = now, cpu
                self.obs.ledger.emit("resources", **row)
                require_resources(row)
            except Exception as exc:
                self.obs.ledger.emit("resource_error", issue=str(exc)[:1200])
                self.obs.fail(f"Resources: {exc}")
                return
            self.stop.wait(1)

    def close(self) -> bool:
        self.stop.set()
        self._thread.join(5)
        safe = not self._thread.is_alive()
        self.obs.owner("resource_sampler", safe, safe)
        return safe
