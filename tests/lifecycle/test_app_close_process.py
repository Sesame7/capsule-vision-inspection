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
from typing import Literal, cast

import pytest

from capsule_vision.configuration.communication_preferences import (
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
    "launcher,scenario,web_enabled,window_size",
    [
        pytest.param("uv", "idle", True, None, id="maximized-web"),
        pytest.param("uv", "switch", False, None, id="completed-read-after-navigation"),
        pytest.param("uv", "timeout", False, None, id="completed-timeout"),
        pytest.param("uv", "pending", False, None, id="pending-read"),
        pytest.param("direct", "idle", False, "1080x1920", id="portrait"),
        pytest.param("uv", "idle", False, "1600x900", id="landscape"),
        pytest.param("installed", "idle", False, "880x720", id="installed-minimum"),
    ],
)
def test_normal_app_native_close_exits_process_tree(
    tmp_path: Path,
    launcher: Literal["uv", "direct", "installed"],
    scenario: str,
    web_enabled: bool,
    window_size: str | None,
) -> None:
    # Give both normal entry points a disposable data root even if the probe
    # fails to import. Only the checked-in Python entry file is copied.
    shutil.copyfile(ROOT / "app.py", tmp_path / "app.py")
    (tmp_path / "sitecustomize.py").write_text(
        "from tests.support.app_close_probe import install\ninstall()\n",
        encoding="utf-8",
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
        command = [uv, "run", "--project", str(ROOT), "--no-sync", "python", "app.py"]
    elif launcher == "installed":
        command = [str(Path(sys.executable).with_name("capsule-vision.exe"))]
    else:
        command = [sys.executable, "app.py"]
    if window_size is not None:
        env["APP_CLOSE_WINDOW_SIZE"] = window_size
        command.extend(["--window-size", window_size])
    with socket.socket() as available:
        available.bind(("127.0.0.1", 0))
        port = available.getsockname()[1]
    settings_path = tmp_path / "data" / "communication.json"
    save_communication_preferences(
        settings_path,
        CommunicationPreferences(web=WebPreferences(enabled=web_enabled, port=port)),
    )
    with subprocess.Popen(
        command, cwd=tmp_path, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE
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
    log = tmp_path / "boundaries.jsonl"
    boundaries = log.read_text(encoding="utf-8") if log.exists() else ""
    assert process.returncode == 0, (process.returncode, stdout, stderr, boundaries)
    records: list[dict[str, object]] = []
    for line in boundaries.splitlines():
        value: object = json.loads(line)
        assert isinstance(value, dict)
        records.append(cast(dict[str, object], value))
    stages = [r["stage"] for r in records]
    assert "watchdog" not in stages
    assert stages[-1] == "atexit"
    assert stages.index("last_window_closed") < stages.index("exec_return")
    assert "application_joined" in stages
    assert b"Read-only Production snapshots" not in stderr
    assert any(r["stage"] == "close_return" and r["accepted"] for r in records)
    assert next(r for r in records if r["stage"] == "window_shown")["web_running"] is (
        web_enabled
    )
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
    if web_enabled:
        with socket.socket() as probe:
            probe.settimeout(1)
            assert probe.connect_ex(("127.0.0.1", port)) != 0
