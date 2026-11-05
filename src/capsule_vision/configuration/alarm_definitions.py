"""Equipment-owned signal meanings, captured once per monitor activation."""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from ._io import decode_json
from .errors import ConfigurationError

CATEGORIES = ("equipment", "interlock", "startup", "software")
TEMPLATE = Path(__file__).with_name("alarm_definitions.json")


@dataclass(frozen=True, slots=True)
class AlarmSignal:
    id: str
    bit: int
    active_high: bool
    category: str
    zh: str
    en: str


@dataclass(frozen=True, slots=True)
class AlarmDefinitions:
    origin: str
    version: str
    digest: str
    signals: tuple[AlarmSignal, ...]


def _object(
    value: object, required: set[str], optional: tuple[str, ...] = ()
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("Expected an object")
    fields = cast(dict[str, object], value)
    if not required <= fields.keys() <= required.union(optional):
        raise ValueError(f"Invalid alarm definition fields: {sorted(fields)}")
    return fields


def _text(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 128
        or not value.isprintable()
    ):
        raise ValueError(
            "Expected a nonempty printable label or identifier of at most 128 characters"
        )
    return value.strip()


def load_alarm_definitions(local: Path) -> AlarmDefinitions:
    origin = "equipment"
    try:
        try:
            stream = local.open("rb")
        except FileNotFoundError:
            if local.is_symlink():
                raise
            origin = "bundled"
            stream = TEMPLATE.open("rb")
        with stream:
            raw = stream.read(32_769)
        if len(raw) > 32_768:
            raise ValueError("Alarm definition exceeds 32 KiB")
        asset = _object(decode_json(raw.decode("utf-8-sig")), {"version", "signals"})
        version = _text(asset["version"])
        entries = asset["signals"]
        if (
            not isinstance(entries, list)
            or not 1 <= len(cast(list[object], entries)) <= 16
        ):
            raise ValueError("Expected 1..16 alarm signals in one word")
        signals: list[AlarmSignal] = []
        for value in cast(list[object], entries):
            row = _object(
                value, {"id", "bit", "category", "zh", "en"}, ("active_high",)
            )
            key, category = _text(row["id"]), _text(row["category"])
            bit, high = row["bit"], row.get("active_high", True)
            if (
                not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", key)
                or category not in CATEGORIES
            ):
                raise ValueError(f"Invalid signal ID or category: {key}, {category}")
            if type(bit) is not int or not 0 <= bit <= 15 or type(high) is not bool:
                raise ValueError(f"Invalid bit or polarity for {key}")
            if any(s.id == key or s.bit == bit for s in signals):
                raise ValueError(f"Duplicate alarm ID or bit: {key}, {bit}")
            signals.append(
                AlarmSignal(
                    key, bit, high, category, _text(row["zh"]), _text(row["en"])
                )
            )
        return AlarmDefinitions(
            origin, version, hashlib.sha256(raw).hexdigest(), tuple(signals)
        )
    except (OSError, ValueError, RecursionError) as exc:
        raise ConfigurationError(
            f"Invalid alarm definitions ({local if origin == 'equipment' else TEMPLATE}): {exc}"
        ) from exc
