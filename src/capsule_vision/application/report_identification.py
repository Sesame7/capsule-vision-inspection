"""Ephemeral export identification, separate from historical inspection identity."""

import hashlib
import re
import secrets
import sys
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


def workstation_label() -> str | None:
    """Read only the exporting Windows installation; never return/log its raw GUID."""
    if sys.platform != "win32":
        return None
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
            0,
            winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        ) as key:
            value, kind = winreg.QueryValueEx(key, "MachineGuid")
        if kind != winreg.REG_SZ or not isinstance(value, str):
            return None
        guid = UUID(value.strip())
        if guid.int in (0, (1 << 128) - 1):
            return None
    except OSError, ValueError:
        # Access/validation failures must not leak registry payloads in feedback.
        return None
    digest = (
        hashlib.sha256(
            b"capsule-vision-inspection/report-workstation/v1\0" + guid.bytes
        )
        .hexdigest()[:12]
        .upper()
    )
    return "WS-" + "-".join(digest[i : i + 4] for i in range(0, 12, 4))


@dataclass(frozen=True, slots=True)
class ExportIdentification:
    exported: datetime
    workstation: str | None
    reference: str

    @classmethod
    def create(cls) -> ExportIdentification:
        exported = datetime.now().astimezone()
        reference = f"EXP-{exported:%Y%m%d-%H%M%S}-{secrets.token_hex(3).upper()}"
        return cls(exported, workstation_label(), reference)


def suggested_report_filename(
    title: str,
    batch_label: str | None,
    extension: str,
    *,
    stamp: datetime | None = None,
) -> str:
    """A safe suggestion only; the operator's selected destination remains final."""
    if extension not in ("pdf", "xlsx"):
        raise ValueError("Unsupported report extension")

    def component(value: str, limit: int) -> str:
        value = re.sub(r'[<>:"/\\|?*]', "_", value)
        value = "".join(char if char.isprintable() else " " for char in value)
        return " ".join(value.split())[:limit].strip(" .")

    # Even supplementary Unicode characters leave this basename below Windows'
    # 255 UTF-16-unit component limit. Stored business labels are never changed.
    parts = [component(title, 48) or "Report"]
    if batch_label and (label := component(batch_label, 64)):
        parts.append(label)
    parts.append((stamp or datetime.now().astimezone()).strftime("%Y%m%d-%H%M%S"))
    return "_".join(parts) + "." + extension
