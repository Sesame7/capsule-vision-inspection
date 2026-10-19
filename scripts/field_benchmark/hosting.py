"""Detached Windows supervisor hosting with one authenticated loopback channel."""

import json
import os
import secrets
import select
import socket
import struct
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from time import monotonic
from typing import cast

from .ledger import Ledger
from .schedule import BurstPlan, Slot
from .trigger import (
    Controller,
    Readiness,
    Reply,
    Request,
    SupervisorSettings,
    supervise,
)


class SocketChannel:
    def __init__(self, connection: socket.socket) -> None:
        self.socket = connection
        self.socket.settimeout(0.5)

    def send(self, obj: object) -> None:
        if isinstance(obj, Request):
            value = {
                "type": "request",
                "run": obj.run,
                "kind": obj.kind,
                "slot": asdict(obj.slot) if obj.slot else None,
            }
        elif isinstance(obj, Reply):
            value = {"type": "reply", **asdict(obj)}
        elif isinstance(obj, str):
            value = {"type": "hello", "token": obj}
        else:
            raise TypeError("Only diagnostic request/reply messages are permitted")
        raw = json.dumps(value, allow_nan=False).encode()
        if len(raw) > 4096:
            raise ValueError("Oversized diagnostic IPC")
        self.socket.sendall(struct.pack("!I", len(raw)) + raw)

    def _exact(self, size: int, deadline: float) -> bytes:
        data = bytearray()
        while len(data) < size:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError("Diagnostic IPC deadline elapsed")
            self.socket.settimeout(remaining)
            part = self.socket.recv(size - len(data))
            if not part:
                raise EOFError("Controller channel closed")
            data.extend(part)
        return bytes(data)

    def recv(self) -> object:
        deadline = monotonic() + 0.5
        size = struct.unpack("!I", self._exact(4, deadline))[0]
        if not 1 <= size <= 4096:
            raise ValueError("Invalid diagnostic IPC length")
        row = cast(dict[str, object], json.loads(self._exact(size, deadline)))
        if row["type"] == "hello":
            return str(row["token"])
        run, kind = str(row["run"]), str(row["kind"])
        if row["type"] == "reply":
            return Reply(
                run,
                kind,
                row.get("released") is True,
                cast(str | None, row.get("issue")),
            )
        slot = row.get("slot")
        if row["type"] != "request":
            raise ValueError("Unexpected diagnostic IPC type")
        decoded: Slot | None = None
        if slot is not None:
            s = cast(dict[str, int | float], slot)
            decoded = Slot(
                int(s["pulse"]),
                int(s["group"]),
                int(s["offset"]),
                float(s["planned_at"]),
            )
        return Request(run, kind, decoded)

    def poll(self, timeout: float = 0) -> bool:
        return bool(select.select([self.socket], [], [], timeout)[0])

    def close(self) -> None:
        self.socket.close()


def read_settings(path: Path) -> tuple[SupervisorSettings, str, int, Path, Path]:
    row = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
    values = cast(dict[str, object], row["settings"])
    plan = cast(dict[str, float | int], values["plan"])
    gate = cast(dict[str, object], values["readiness"])
    settings = SupervisorSettings(
        str(values["run"]),
        str(values["host"]),
        int(cast(int, values["port"])),
        BurstPlan(
            float(plan["start"]),
            int(plan["groups"]),
            int(plan["pulse_budget"]),
            float(plan["end"]),
            float(plan["max_lateness"]),
        ),
        Readiness(
            str(gate["run"]),
            int(cast(int, gate["generation"])),
            str(gate["source_epoch"]),
            str(gate["configuration_sha256"]),
            float(cast(float, gate["expires_at"])),
            gate["manual_stopped"] is True,
            gate["exclusive_photo_writer"] is True,
            gate["inspection_attached"] is True,
            gate["release_qualified"] is True,
        ),
        float(cast(float, values["hold"])),
        float(cast(float, values["lease"])),
        float(cast(float, values["timeout"])),
        values.get("live") is True,
    )
    return (
        settings,
        str(row["token"]),
        int(cast(int, row["ipc_port"])),
        Path(str(row["directory"])),
        Path(str(row["locks"])),
    )


def supervisor_entry(path: Path) -> None:
    settings, token, port, directory, locks = read_settings(path)
    settings.validate()
    connection = socket.create_connection(("127.0.0.1", port), timeout=2)
    channel = SocketChannel(connection)
    channel.send(token)
    supervise(settings, channel, directory, locks)


def start_detached(
    settings: SupervisorSettings, directory: Path, locks: Path
) -> tuple[subprocess.Popen[bytes], Controller]:
    settings.validate()
    token = secrets.token_hex(32)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(10)
        path = directory / "supervisor-settings.json"
        with path.open("x", encoding="utf-8") as stream:
            json.dump(
                {
                    "settings": asdict(settings),
                    "token": token,
                    "ipc_port": listener.getsockname()[1],
                    "directory": str(directory.resolve()),
                    "locks": str(locks.resolve()),
                },
                stream,
            )
        flags = 0
        if os.name == "nt":
            flags = (
                subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.CREATE_BREAKAWAY_FROM_JOB
            )
        with (directory / "supervisor-stdio.log").open("xb") as output:
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(
                        Path(__file__).resolve().parents[1]
                        / "field_benchmark_supervisor.py"
                    ),
                    str(path.resolve()),
                ],
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=output,
                creationflags=flags,
                close_fds=True,
            )
        (directory / "supervisor-host.json").write_text(
            json.dumps(
                {"pid": process.pid, "creationflags": flags, "parent_pid": os.getpid()}
            ),
            encoding="utf-8",
        )
        connection, _ = listener.accept()
    channel = SocketChannel(connection)
    if channel.recv() != token:
        channel.close()
        raise RuntimeError("Independent supervisor authentication failed")
    return process, Controller(settings, channel)


def join_detached(
    process: subprocess.Popen[bytes], ledger: Ledger, timeout: float
) -> bool:
    try:
        process.wait(timeout)
    except subprocess.TimeoutExpired:
        pass
    terminated = process.poll() is not None
    ledger.emit(
        "owner_stopped",
        owner="supervisor_process",
        terminated=terminated,
        cleanup_safe=terminated and process.returncode == 0,
        exitcode=process.returncode,
    )
    return terminated and process.returncode == 0
