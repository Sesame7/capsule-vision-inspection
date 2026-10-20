"""Real Windows WM_CLOSE and normal app.py process termination, without GPU."""

import ctypes
import json
import os
import shutil
import socket
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path
from typing import cast

import pytest

from vimo_demo.application.communication_preferences import (
    CommunicationPreferences,
    WebPreferences,
    save_communication_preferences,
)

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows WM_CLOSE")


def assert_process_exited(pid: int) -> None:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE only.
    if not handle:
        assert ctypes.get_last_error() == 87  # PID no longer exists.
        return
    try:
        assert kernel.WaitForSingleObject(handle, 1000) == 0
    finally:
        kernel.CloseHandle(handle)


@pytest.mark.parametrize(
    "launcher,scenario,web_host",
    [
        ("uv", s, None)
        for s in ("unused", "visit", "success", "timeout", "pending", "switch")
    ]
    + [
        ("direct", "unused", None),
        ("direct", "unused", "off"),
        ("direct", "unused", "127.0.0.1"),
        ("direct", "unused", "0.0.0.0"),
        ("direct", "unused", "192.0.2.99"),
    ],
)
def test_normal_app_native_close_exits_process_tree(
    tmp_path: Path, launcher: str, scenario: str, web_host: str | None
) -> None:
    (tmp_path / "sitecustomize.py").write_text(
        "from tests.app_close_support import install\ninstall()\n", encoding="utf-8"
    )
    env = dict(
        os.environ,
        PYTHONPATH=os.pathsep.join((str(tmp_path), str(ROOT), str(ROOT / "src"))),
        PYTHONDONTWRITEBYTECODE="1",
        QT_QPA_PLATFORM="windows",
        APP_CLOSE_PROBE=str(tmp_path),
        APP_CLOSE_SCENARIO=scenario,
    )
    if launcher == "uv":
        uv = shutil.which("uv")
        assert uv is not None
        command = [uv, "run", "--no-sync", "python", "app.py"]
    else:
        command = [str(ROOT / ".venv/Scripts/python.exe"), "app.py"]
    web_started = web_host in (None, "127.0.0.1")
    with socket.socket() as available:
        available.bind(("127.0.0.1", 0))
        port = available.getsockname()[1]
    settings_path = tmp_path / "data" / "communication.json"
    save_communication_preferences(
        settings_path,
        CommunicationPreferences(
            web=WebPreferences(enabled=web_host != "off", port=port)
        ),
    )
    if web_host not in (None, "off", "127.0.0.1"):
        settings = cast(
            dict[str, object], json.loads(settings_path.read_text(encoding="utf-8"))
        )
        cast(dict[str, object], settings["web"])["host"] = web_host
        settings_path.write_text(json.dumps(settings), encoding="utf-8")
    with subprocess.Popen(
        command, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            # The child watchdog has already recorded boundaries/thread stacks.
            # This is test-process cleanup, never an application exit strategy.
            log = tmp_path / "boundaries.jsonl"
            diagnostics = (
                log.read_text(encoding="utf-8")
                if log.exists()
                else "Probe did not start"
            )
            stacks = tmp_path / "stacks.log"
            if stacks.exists():
                diagnostics += "\n" + stacks.read_text(encoding="utf-8")
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                check=True,
            )
            process.communicate(timeout=5)
            pytest.fail(f"Owned app.py test process hung:\n{diagnostics}")
        assert process.returncode == 0, (stdout, stderr)
    records: list[dict[str, object]] = []
    for line in (
        (tmp_path / "boundaries.jsonl").read_text(encoding="utf-8").splitlines()
    ):
        value: object = json.loads(line)
        assert isinstance(value, dict)
        records.append(cast(dict[str, object], value))
    stages = [r["stage"] for r in records]
    assert "watchdog" not in stages
    assert stages[-1] == "atexit"
    assert stages.index("last_window_closed") < stages.index("exec_return")
    assert "application_joined" in stages
    assert b"Read-only Production snapshots" not in stderr
    if web_host not in (None, "off") and not web_started:
        assert b"Invalid communication settings" in stderr or b"ValueError" in stderr
    for record in records:
        if record["stage"] == "last_window_closed" or (
            record["stage"] == "close_return" and record["accepted"]
        ):
            assert record["application_closed"] and record["communication_closed"]
        if record["stage"] in ("exec_return", "atexit"):
            assert record["threads"] == ["MainThread"]
    if scenario == "pending":
        assert next(r for r in records if r["stage"] == "native_close")["busy"]
    for key in ("pid", "ppid"):
        pid = records[0][key]
        assert isinstance(pid, int)
        assert_process_exited(pid)
    if web_started:
        with socket.socket() as probe:
            assert probe.connect_ex(("127.0.0.1", port)) != 0
