"""Equipment-owned signal meanings, captured once per monitor activation."""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, cast

from capsule_vision.localization.languages import Language, is_language

from ._io import atomic_write, decode_json
from .errors import ConfigurationError

CATEGORIES = ("equipment", "interlock", "startup", "software")
TEMPLATE = Path(__file__).with_name("alarm_definitions.json")
type AlarmTranslations = tuple[tuple[Language, str], ...]


def alarm_label(
    language: Language, zh: str, en: str, translations: AlarmTranslations
) -> str:
    if language == "zh":
        return zh
    return next((label for code, label in translations if code == language), en)


@dataclass(frozen=True, slots=True)
class AlarmSignal:
    id: str
    bit: int
    active_high: bool
    category: str
    zh: str
    en: str
    translations: AlarmTranslations = ()

    def label(self, language: Language) -> str:
        return alarm_label(language, self.zh, self.en, self.translations)


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


def parse_translations(value: object, zh: str, en: str) -> AlarmTranslations:
    if not isinstance(value, dict):
        raise ValueError("Expected an alarm translations object")
    labels: list[tuple[Language, str]] = []
    for code, label in cast(dict[str, object], value).items():
        if not is_language(code):
            raise ValueError(f"Unsupported alarm language: {code!r}")
        name = _text(label)
        if code in ("zh", "en"):
            if name != (zh if code == "zh" else en):
                raise ValueError(f"Conflicting alarm labels for {code}")
        else:
            labels.append((code, name))
    return tuple(labels)


def _read(stream: BinaryIO) -> bytes:
    raw = stream.read(32_769)
    if len(raw) > 32_768:
        raise ValueError("Alarm definition exceeds 32 KiB")
    return raw


def _decode(raw: bytes, origin: str) -> AlarmDefinitions:
    asset = _object(decode_json(raw.decode("utf-8-sig")), {"version", "signals"})
    version = _text(asset["version"])
    entries = asset["signals"]
    if not isinstance(entries, list) or not 1 <= len(cast(list[object], entries)) <= 16:
        raise ValueError("Expected 1..16 alarm signals in one word")
    signals: list[AlarmSignal] = []
    for value in cast(list[object], entries):
        row = _object(
            value,
            {"id", "bit", "category", "zh", "en"},
            ("active_high", "translations"),
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
        zh, en = _text(row["zh"]), _text(row["en"])
        signals.append(
            AlarmSignal(
                key,
                bit,
                high,
                category,
                zh,
                en,
                parse_translations(row.get("translations", {}), zh, en),
            )
        )
    return AlarmDefinitions(
        origin, version, hashlib.sha256(raw).hexdigest(), tuple(signals)
    )


def load_alarm_definitions(
    local: Path, *, bundled_fallback: bool = True
) -> AlarmDefinitions:
    origin = "equipment"
    try:
        try:
            stream = local.open("rb")
        except FileNotFoundError as exc:
            if local.is_symlink():
                raise ValueError("Local alarm definition link has no target") from exc
            if not bundled_fallback:
                raise
            origin = "bundled"
            stream = TEMPLATE.open("rb")
        with stream:
            return _decode(_read(stream), origin)
    except (OSError, ValueError, RecursionError) as exc:
        raise ConfigurationError(
            f"Invalid alarm definitions ({local if origin == 'equipment' else TEMPLATE}): {exc}"
        ) from exc


def create_local_alarm_definitions(local: Path) -> None:
    """Publish the exact bundled file once, including in a race with an external editor."""
    try:
        local.lstat()
    except FileNotFoundError:
        with TEMPLATE.open("rb") as stream:
            raw = _read(stream)
        _decode(raw, "bundled")
        local.parent.mkdir(parents=True, exist_ok=True)
        try:
            atomic_write(local, raw.decode("utf-8"), exclusive=True, newline="")
            return
        except FileExistsError:
            pass  # Inspect the file that won the publication race.
    load_alarm_definitions(local, bundled_fallback=False)
    raise FileExistsError(f"Local alarm file already exists: {local}")
