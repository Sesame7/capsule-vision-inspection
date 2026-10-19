"""Post-drain file verification, never invoked in an inspection callback."""

import hashlib
from pathlib import Path

import cv2
import numpy as np

from .analyze import Row, frame_key


def verify_archive(rows: list[Row], root: Path) -> list[str]:
    expected = {frame_key(r): r for r in rows if r.get("kind") == "archive_written"}
    issues: list[str] = []
    for row in rows:
        if row.get("kind") != "archive_file":
            continue
        name = row.get("path")
        if not isinstance(name, str):
            issues.append("archive output path unavailable")
            continue
        path = Path(name).resolve()
        if not path.is_relative_to(root.resolve()):
            issues.append("archive output outside isolated run root")
            continue
        try:
            size = path.stat().st_size
            if size != row.get("bytes") or size > 64 * 1024 * 1024:
                raise ValueError("archive byte count/bound mismatch")
            pixels = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
            if pixels is None or pixels.nbytes > 32 * 1024 * 1024:
                raise ValueError("archive decode failed/exceeds raw frame bound")
            original = expected.get(frame_key(row))
            if original is None or hashlib.sha256(pixels).hexdigest() != original.get(
                "pixel_sha256"
            ):
                raise ValueError("archive decoded BGR digest/FrameId mismatch")
        except (OSError, ValueError, cv2.error) as exc:
            issues.append(f"archive verification {path.name}: {exc}")
    return issues
