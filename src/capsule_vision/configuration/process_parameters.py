"""Atomic, inert product-process definitions; no PLC client or activation policy."""

import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import Language, is_language

from ._io import decode_json
from .errors import ConfigurationError, ConfigurationIOError

type ProcessValues = tuple[tuple[str, int], ...]
type Translations = tuple[tuple[Language, str], ...]
DINT_MIN, DINT_MAX = -(2**31), 2**31 - 1
GROUP_KEYS = {
    "speed": "process_speed",
    "photo": "process_photo",
    "reject": "process_reject",
    "lift_valve": "process_lift",
    "drop_valve": "process_drop",
    "extend_valve": "process_extend",
}


def _id(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value):
        raise ConfigurationError(f"Invalid process parameter/group ID: {value!r}")
    return value


def _integer(value: object, minimum: int, maximum: int, name: str) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ConfigurationError(f"{name}: expected integer {minimum}..{maximum}")
    return value


def process_values(values: ProcessValues) -> ProcessValues:
    """Freeze identity/value pairs without assuming a device's allowed ranges."""
    result = tuple(
        (_id(key), _integer(value, DINT_MIN, DINT_MAX, key)) for key, value in values
    )
    if len({key for key, _ in result}) != len(result):
        raise ConfigurationError("Duplicate process parameter IDs")
    return tuple(sorted(result))


def _label(labels: Translations, language: Language) -> str:
    return dict(labels).get(language, dict(labels)["en"])


@dataclass(frozen=True, slots=True)
class ProcessParameter:
    id: str
    group: str
    address: int
    minimum: int
    maximum: int
    label_key: str = ""
    labels: Translations = ()

    def __post_init__(self) -> None:
        _id(self.id)
        _id(self.group)
        _integer(self.minimum, DINT_MIN, DINT_MAX, self.id)
        _integer(self.maximum, self.minimum, DINT_MAX, self.id)
        _integer(self.address, 0, 65534, f"{self.id} DM address")
        if not self.label_key and "en" not in dict(self.labels):
            raise ConfigurationError(f"{self.id}: English label required")

    def label(self, language: Language) -> str:
        return (
            _label(self.labels, language)
            if self.labels
            else text(self.label_key, language)
        )


@dataclass(frozen=True, slots=True)
class ProcessCatalog:
    parameters: tuple[ProcessParameter, ...]
    group_labels: tuple[tuple[str, Translations], ...] = ()

    def __post_init__(self) -> None:
        if len({p.id for p in self.parameters}) != len(self.parameters):
            raise ConfigurationError("Duplicate process parameter IDs")
        occupied: set[int] = set()
        for parameter in self.parameters:
            if parameter.group not in GROUP_KEYS and parameter.group not in dict(
                self.group_labels
            ):
                raise ConfigurationError(f"Unknown process group: {parameter.group}")
            words = {parameter.address, parameter.address + 1}
            if occupied & words:
                raise ConfigurationError(
                    f"Overlapping DINT DM mapping: {parameter.id} D{parameter.address}"
                )
            occupied.update(words)

    def get(self, key: str) -> ProcessParameter:
        for parameter in self.parameters:
            if parameter.id == key:
                return parameter
        raise ConfigurationError(f"Unknown process parameter ID: {key!r}")

    def validate(self, values: ProcessValues) -> ProcessValues:
        values = process_values(values)
        for key, value in values:
            parameter = self.get(key)
            _integer(value, parameter.minimum, parameter.maximum, key)
        return values

    def parse_draft(self, values: dict[str, str]) -> ProcessValues:
        parsed: list[tuple[str, int]] = []
        for key, raw in values.items():
            self.get(key)
            value = raw.strip()
            if value in ("", "-"):
                continue
            if not re.fullmatch(r"-?[0-9]+", value):
                raise ConfigurationError(
                    f"{key}: expected an integer or an empty field"
                )
            parsed.append((key, int(value)))
        return self.validate(tuple(parsed))

    @property
    def groups(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(p.group for p in self.parameters))

    def group_label(self, group: str, language: Language) -> str:
        labels = dict(self.group_labels).get(group)
        return _label(labels, language) if labels else text(GROUP_KEYS[group], language)


def _builtins() -> ProcessCatalog:
    fields = [
        ("running_speed", "speed", "process_running_speed", 102, 150),
        ("rotation_speed", "speed", "process_rotation_speed", 108, 600),
        ("photo_phase", "photo", "process_phase", 114, 36000),
        ("reject_phase", "reject", "process_phase", 122, 36000),
        ("reject_compensation", "reject", "process_compensation", 124, 1000),
    ]
    for group, start in (
        ("lift_valve", 130),
        ("drop_valve", 138),
        ("extend_valve", 146),
    ):
        fields.extend(
            (f"{group}_{field}", group, f"process_{field}", start + index * 2, maximum)
            for index, (field, maximum) in enumerate(
                (
                    ("open_phase", 36000),
                    ("close_phase", 36000),
                    ("open_compensation", 1000),
                    ("close_compensation", 1000),
                )
            )
        )
    return ProcessCatalog(
        tuple(
            ProcessParameter(key, group, address, 0, maximum, label)
            for key, group, label, address, maximum in fields
        )
    )


BUILTIN_PROCESS_CATALOG = _builtins()


def _object(
    value: object,
    allowed: set[str] | None = None,
    required: set[str] | frozenset[str] = frozenset(),
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ConfigurationError("Expected process catalog object")
    row = cast(dict[str, object], value)
    if not required <= row.keys() or (
        allowed is not None and not row.keys() <= allowed
    ):
        raise ConfigurationError(f"Invalid process catalog fields: {sorted(row)}")
    return row


def _translations(value: object, *, english: bool = True) -> Translations:
    labels: list[tuple[Language, str]] = []
    for language, label in _object(value).items():
        if (
            not is_language(language)
            or not isinstance(label, str)
            or not label.strip()
            or len(label) > 128
            or not label.isprintable()
        ):
            raise ConfigurationError(f"Invalid process translation: {language!r}")
        labels.append((language, label.strip()))
    if english and "en" not in dict(labels):
        raise ConfigurationError("Process label requires an English translation")
    return tuple(sorted(labels))


def _rows(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ConfigurationError("Expected process catalog array")
    return cast(list[object], value)


def _catalog(value: object) -> ProcessCatalog:
    obj = _object(value, {"overrides", "parameters", "groups"})
    definitions = {p.id: p for p in BUILTIN_PROCESS_CATALOG.parameters}
    groups: list[tuple[str, Translations]] = []
    for group, labels in _object(obj.get("groups", {})).items():
        if _id(group) in GROUP_KEYS:
            raise ConfigurationError(f"Built-in process group is fixed: {group}")
        groups.append((group, _translations(labels)))
    seen: set[str] = set()
    for raw in _rows(obj.get("overrides", [])):
        row = _object(raw, {"id", "labels", "address", "minimum", "maximum"}, {"id"})
        key = _id(row["id"])
        if key not in definitions:
            raise ConfigurationError(f"Unknown process override target: {key}")
        if key in seen:
            raise ConfigurationError(f"Duplicate process parameter ID: {key}")
        seen.add(key)
        base = definitions[key]
        labels = base.labels
        if "labels" in row:
            names: dict[Language, str] = {
                "zh": base.label("zh"),
                "en": base.label("en"),
            }
            names.update(_translations(row["labels"], english=False))
            labels = tuple(sorted(names.items()))
        definitions[key] = replace(
            base,
            labels=labels,
            address=_integer(row.get("address", base.address), 0, 65534, "DM address"),
            minimum=_integer(row.get("minimum", base.minimum), DINT_MIN, DINT_MAX, key),
            maximum=_integer(row.get("maximum", base.maximum), DINT_MIN, DINT_MAX, key),
        )
    fields = {"id", "group", "labels", "address", "minimum", "maximum"}
    for raw in _rows(obj.get("parameters", [])):
        row = _object(raw, fields, fields)
        key = _id(row["id"])
        if key in definitions:
            raise ConfigurationError(f"Duplicate process parameter ID: {key}")
        definitions[key] = ProcessParameter(
            key,
            _id(row["group"]),
            _integer(row["address"], 0, 65534, "DM address"),
            _integer(row["minimum"], DINT_MIN, DINT_MAX, key),
            _integer(row["maximum"], DINT_MIN, DINT_MAX, key),
            labels=_translations(row["labels"]),
        )
    # Built-in slots keep their original electrical order even after address overrides.
    original = {p.id: p.address for p in BUILTIN_PROCESS_CATALOG.parameters}
    ordered = tuple(
        sorted(
            definitions.values(),
            key=lambda p: (original.get(p.id, p.address), p.id),
        )
    )
    return ProcessCatalog(ordered, tuple(sorted(groups)))


def load_process_catalog(path: Path | None = None) -> ProcessCatalog:
    """Only an absent optional file selects built-ins; invalid files stay untouched."""
    if path is None:
        return BUILTIN_PROCESS_CATALOG
    try:
        try:
            stream = path.open("rb")
        except FileNotFoundError as exc:
            if path.is_symlink():
                raise ConfigurationError(
                    f"Process catalog link has no target: {path}"
                ) from exc
            return BUILTIN_PROCESS_CATALOG
        with stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ConfigurationError("Process catalog exceeds 64 KiB")
        return _catalog(decode_json(raw.decode("utf-8-sig")))
    except OSError as exc:
        raise ConfigurationIOError(f"Read process catalog {path}: {exc}") from exc
    except (ValueError, TypeError, RecursionError) as exc:
        raise ConfigurationError(f"Process catalog {path}: {exc}") from exc
