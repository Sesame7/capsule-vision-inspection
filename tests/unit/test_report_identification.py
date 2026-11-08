"""Export identifiers never depend on the test machine's installation identity."""

import re
import secrets
import sys
import winreg
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import PureWindowsPath

import pytest

from capsule_vision.application import report_identification as identification

GUID = "01234567-89ab-cdef-0123-456789abcdef"


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, str, int, int]]:
    calls: list[tuple[int, str, int, int]] = []

    @contextmanager
    def open_key(root: int, path: str, reserved: int, access: int) -> Generator[int]:
        calls.append((root, path, reserved, access))
        yield 123

    def query(key: int, name: str) -> tuple[object, int]:
        assert key == 123 and name == "MachineGuid"
        return GUID, winreg.REG_SZ

    monkeypatch.setattr(winreg, "OpenKey", open_key)
    monkeypatch.setattr(winreg, "QueryValueEx", query)
    return calls


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (GUID, "WS-463C-5ED4-6C3C"),
        (GUID.upper(), "WS-463C-5ED4-6C3C"),
        ("{" + GUID + "}", "WS-463C-5ED4-6C3C"),
        ("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "WS-9EA5-A743-C6DD"),
    ],
)
def test_workstation_digest_normalizes_guid_and_reads_only_native_registry(
    monkeypatch: pytest.MonkeyPatch,
    registry: list[tuple[int, str, int, int]],
    value: str,
    expected: str,
) -> None:
    def query(_key: int, _name: str) -> tuple[str, int]:
        return value, winreg.REG_SZ

    monkeypatch.setattr(winreg, "QueryValueEx", query)
    label = identification.workstation_label()
    assert label == expected
    assert label is not None
    assert value not in label
    assert registry == [
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
            0,
            winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        )
    ]


@pytest.mark.parametrize(
    ("value", "kind"),
    [
        ("", winreg.REG_SZ),
        ("not-a-guid", winreg.REG_SZ),
        ("00000000-0000-0000-0000-000000000000", winreg.REG_SZ),
        ("ffffffff-ffff-ffff-ffff-ffffffffffff", winreg.REG_SZ),
        (GUID, winreg.REG_EXPAND_SZ),
        (GUID.encode(), winreg.REG_BINARY),
        (123, winreg.REG_SZ),
    ],
)
def test_unusable_registry_value_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    registry: list[tuple[int, str, int, int]],
    value: object,
    kind: int,
) -> None:
    def query(_key: int, _name: str) -> tuple[object, int]:
        return value, kind

    monkeypatch.setattr(winreg, "QueryValueEx", query)
    assert identification.workstation_label() is None and len(registry) == 1


@pytest.mark.parametrize("call", ["OpenKey", "QueryValueEx"])
@pytest.mark.parametrize("error", [FileNotFoundError, PermissionError])
def test_identity_read_failure_cannot_leak_registry_payload(
    monkeypatch: pytest.MonkeyPatch,
    registry: list[tuple[int, str, int, int]],
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    call: str,
    error: type[OSError],
) -> None:
    def failed(*_args: object) -> None:
        raise error(GUID)

    monkeypatch.setattr(winreg, call, failed)
    assert identification.workstation_label() is None
    assert not caplog.records and capsys.readouterr() == ("", "")


def test_other_platform_does_not_fabricate_or_read_an_identity(
    monkeypatch: pytest.MonkeyPatch, registry: list[tuple[int, str, int, int]]
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    assert identification.workstation_label() is None and not registry


def test_export_reference_uses_one_timestamp_and_fresh_suffix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Instant(datetime):
        def astimezone(self, tz: tzinfo | None = None) -> Instant:
            assert tz is None
            return self

    stamp = Instant(
        2026, 11, 9, 8, 7, 6, tzinfo=timezone(timedelta(hours=5, minutes=45))
    )

    class Clock:
        @staticmethod
        def now() -> datetime:
            return stamp

    suffixes = iter(("a1b2c3", "d4e5f6"))

    def suffix(count: int) -> str:
        assert count == 3
        return next(suffixes)

    monkeypatch.setattr(identification, "datetime", Clock)
    monkeypatch.setattr(identification, "workstation_label", lambda: None)
    monkeypatch.setattr(secrets, "token_hex", suffix)
    first, second = (
        identification.ExportIdentification.create(),
        identification.ExportIdentification.create(),
    )
    assert first.exported == second.exported == stamp
    assert (
        first.exported.isoformat(" ", timespec="seconds") == "2026-11-09 08:07:06+05:45"
    )
    assert first.reference == "EXP-20261109-080706-A1B2C3"
    assert second.reference == "EXP-20261109-080706-D4E5F6"
    assert first.workstation is second.workstation is None


@pytest.mark.parametrize("extension", ["pdf", "xlsx"])
@pytest.mark.parametrize(
    "title",
    [
        "批次生产报告",
        "Defect Classification Report",
        "Audit Records Report",
        "Alarm Records Report",
    ],
)
@pytest.mark.parametrize("batch", [None, "GC261026-04"])
def test_suggested_filename_type_optional_business_batch_and_timestamp(
    extension: str, title: str, batch: str | None
) -> None:
    name = identification.suggested_report_filename(
        title, batch, extension, stamp=datetime(2026, 11, 9, 8, 7, 6)
    )
    assert (
        name == title + ("_" + batch if batch else "") + "_20261109-080706." + extension
    )


@pytest.mark.parametrize(
    "label", ['..\\path/name:*?"<>|\nwith\x00control .', "😀" * 200, " . "]
)
def test_filename_suggestion_is_one_bounded_windows_component(label: str) -> None:
    name = identification.suggested_report_filename(
        "Alarm Records Report", label, "pdf", stamp=datetime(2026, 11, 9)
    )
    assert PureWindowsPath(name).name == name
    assert not re.search(r'[<>:"/\\|?*\x00-\x1f]', name)
    assert len(name.encode("utf-16-le")) // 2 <= 255
    assert name.startswith("Alarm Records Report_") and name.endswith(
        "_20261109-000000.pdf"
    )


def test_unsupported_filename_extension_rejected() -> None:
    with pytest.raises(ValueError, match="extension"):
        identification.suggested_report_filename("Report", None, "../file")
