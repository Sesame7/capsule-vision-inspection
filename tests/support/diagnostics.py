import json
from pathlib import Path
from typing import cast


def observations(directory: Path, event: str | None = None) -> list[dict[str, object]]:
    rows = [
        cast(dict[str, object], json.loads(line))
        for segment in sorted(directory.glob("capsule-runtime-v1-*.jsonl"))
        for line in segment.read_text(encoding="utf-8").splitlines()
    ]
    return [row for row in rows if event is None or row["event"] == event]
