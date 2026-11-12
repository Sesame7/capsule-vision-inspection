"""Strict, inert device catalogs and flat Recipe process overrides."""

import json
from dataclasses import replace
from pathlib import Path
from typing import Never

import pytest
from tests.support.recipe import editor
from tests.support.startup import close, saved_data

from capsule_vision.application.startup import load_startup
from capsule_vision.configuration import (
    ConfigurationError,
    RecipeDefinition,
    load_recipe,
    save_recipe,
)
from capsule_vision.configuration.catalog import RecipeCatalog
from capsule_vision.configuration.drafts import RecipeDraft
from capsule_vision.configuration.process_parameters import (
    BUILTIN_PROCESS_CATALOG,
    DINT_MAX,
    DINT_MIN,
    ProcessCatalog,
    load_process_catalog,
)
from capsule_vision.localization.languages import LANGUAGES


def device_catalog(path: Path, content: object) -> ProcessCatalog:
    path.write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")
    return load_process_catalog(path)


def extension() -> dict[str, object]:
    return {
        "id": "feeder_speed",
        "group": "feeder",
        "labels": {"en": "Feeder speed", "zh": "供料速度"},
        "address": 180,
        "minimum": -100,
        "maximum": 100,
    }


def test_default_definitions_follow_electrical_order_and_have_no_defaults(
    tmp_path: Path,
) -> None:
    catalog = load_process_catalog(tmp_path / "absent.json")
    assert catalog is BUILTIN_PROCESS_CATALOG
    assert not (tmp_path / "absent.json").exists()
    addresses = (
        102,
        108,
        114,
        122,
        124,
        130,
        132,
        134,
        136,
        138,
        140,
        142,
        144,
        146,
        148,
        150,
        152,
    )
    maxima = (
        150,
        600,
        36000,
        36000,
        1000,
        36000,
        36000,
        1000,
        1000,
        36000,
        36000,
        1000,
        1000,
        36000,
        36000,
        1000,
        1000,
    )
    assert len(catalog.parameters) == 17
    for parameter, address, maximum in zip(
        catalog.parameters, addresses, maxima, strict=True
    ):
        assert parameter.address == address
        assert (parameter.minimum, parameter.maximum) == (0, maximum)
        for language in LANGUAGES:
            assert parameter.label(language) and catalog.group_label(
                parameter.group, language
            )
    assert catalog.groups == (
        "speed",
        "photo",
        "reject",
        "lift_valve",
        "drop_valve",
        "extend_valve",
    )
    assert RecipeDefinition("sample", "Sample").process_parameters == ()


def test_overrides_extensions_and_translations_are_atomic_and_ordered(
    tmp_path: Path,
) -> None:
    catalog = device_catalog(
        tmp_path / "parameters.json",
        {
            "overrides": [
                {
                    "id": "running_speed",
                    "labels": {"en": "Line speed", "zh": "线速度"},
                    "address": 300,
                    "minimum": -20,
                    "maximum": 200,
                }
            ],
            "groups": {"feeder": {"en": "Feeder", "zh": "供料"}},
            "parameters": [extension()],
        },
    )
    assert catalog.parameters[0].id == "running_speed"  # Original electrical slot.
    speed = catalog.get("running_speed")
    assert speed.group == "speed" and speed.address == 300
    assert speed.label("zh") == "线速度" and speed.label("it") == "Line speed"
    feeder = catalog.get("feeder_speed")
    assert feeder == catalog.parameters[-1] and feeder.address == 180
    assert feeder.label("zh") == "供料速度" and feeder.label("de") == "Feeder speed"
    assert catalog.group_label("feeder", "ti") == "Feeder"
    assert catalog.validate((("running_speed", -20), ("feeder_speed", 0))) == (
        ("feeder_speed", 0),
        ("running_speed", -20),
    )
    existing_group = device_catalog(
        tmp_path / "existing.json", {"parameters": [{**extension(), "group": "speed"}]}
    )
    assert existing_group.groups == BUILTIN_PROCESS_CATALOG.groups


@pytest.mark.parametrize(
    "content,detail",
    [
        ({"overrides": [{"id": "typo"}]}, "Unknown process override"),
        (
            {"overrides": [{"id": "running_speed"}, {"id": "running_speed"}]},
            "Duplicate",
        ),
        ({"parameters": [{**extension(), "id": "running_speed"}]}, "Duplicate"),
        ({"overrides": [{"id": "running_speed", "group": "photo"}]}, "fields"),
        ({"groups": {"speed": {"en": "Changed"}}}, "fixed"),
        ({"parameters": [extension()]}, "Unknown process group"),
        (
            {"parameters": [{**extension(), "group": "speed", "minimum": 101}]},
            "integer",
        ),
        (
            {
                "parameters": [
                    {**extension(), "group": "speed", "minimum": DINT_MIN - 1}
                ]
            },
            "integer",
        ),
        (
            {
                "parameters": [
                    {**extension(), "group": "speed", "maximum": DINT_MAX + 1}
                ]
            },
            "integer",
        ),
        ({"overrides": [{"id": "running_speed", "minimum": True}]}, "integer"),
        ({"overrides": [{"id": "running_speed", "address": -1}]}, "address"),
        (
            {"overrides": [{"id": "running_speed", "address": 65535}]},
            "address",
        ),
        ({"overrides": [{"id": "running_speed", "address": True}]}, "address"),
        (
            {"overrides": [{"id": "running_speed", "address": "D102"}]},
            "address",
        ),
        ({"overrides": [{"id": "running_speed", "addresses": {"3": 180}}]}, "fields"),
        (
            {"overrides": [{"id": "running_speed", "address": None}]},
            "address",
        ),
        (
            {"overrides": [{"id": "running_speed", "address": 107}]},
            "Overlapping",
        ),
        (
            {"overrides": [{"id": "running_speed", "address": 109}]},
            "Overlapping",
        ),
        ({"overrides": [{"id": "running_speed", "labels": {"en": ""}}]}, "translation"),
        (
            {"overrides": [{"id": "running_speed", "labels": {"xx": "Speed"}}]},
            "translation",
        ),
        (
            {
                "parameters": [
                    {**extension(), "group": "speed", "labels": {"zh": "供料"}}
                ]
            },
            "English",
        ),
        (
            {"overrides": [{"id": "running_speed", "labels": {"en": "Line\nbreak"}}]},
            "translation",
        ),
        ({"visible": False}, "fields"),
    ],
)
def test_invalid_catalogs_are_rejected_without_repair(
    tmp_path: Path, content: object, detail: str
) -> None:
    path = tmp_path / "parameters.json"
    path.write_text(json.dumps(content), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(ConfigurationError, match=detail):
        load_process_catalog(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "raw",
    (
        '{"groups":{},"groups":{}}',
        '{"overrides":[{"id":"running_speed","labels":{"en":"A","en":"B"}}]}',
        "{broken",
        "null",
    ),
)
def test_malformed_and_duplicate_json_is_not_a_missing_catalog(
    tmp_path: Path, raw: str
) -> None:
    path = tmp_path / "parameters.json"
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_process_catalog(path)
    assert path.read_text(encoding="utf-8") == raw


def test_flat_recipe_roundtrip_omissions_zero_and_negative(tmp_path: Path) -> None:
    catalog = device_catalog(
        tmp_path / "parameters.json",
        {"parameters": [{**extension(), "group": "speed"}]},
    )
    path = tmp_path / "sample.json"
    for overrides in ((), (("running_speed", 0), ("feeder_speed", -10))):
        recipe = RecipeDefinition("sample", "Sample", process_parameters=overrides)
        save_recipe(path, recipe, process_catalog=catalog)
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw.get("process_parameters") == (dict(overrides) if overrides else None)
        assert load_recipe(path, process_catalog=catalog) == recipe
    draft = RecipeDraft.from_recipe(recipe)
    draft.process_parameters["running_speed"] = "-"
    draft.process_parameters["photo_phase"] = ""
    assert draft.to_recipe(catalog).process_parameters == (("feeder_speed", -10),)
    path.write_text(
        '{"display_name":"Sample","process_parameters":{}}', encoding="utf-8"
    )
    assert load_recipe(path).process_parameters == ()
    assert '"process_parameters":{}' in path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "overrides",
    (
        {"typo": 1},
        {"running_speed": True},
        {"running_speed": "0"},
        {"running_speed": 1.1},
        {"running_speed": None},
        {"running_speed": -1},
        {"running_speed": 151},
        None,
        [],
    ),
)
def test_recipe_rejects_unknown_or_invalid_values_without_rewriting(
    tmp_path: Path, overrides: object
) -> None:
    path = tmp_path / "sample.json"
    path.write_text(
        json.dumps({"display_name": "Sample", "process_parameters": overrides}),
        encoding="utf-8",
    )
    before = path.read_bytes()
    with pytest.raises(ConfigurationError):
        load_recipe(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize("value", ("bad", "1.5", "+2", "--2", "151", "-1"))
def test_bad_draft_input_is_not_silently_cleared(value: str) -> None:
    draft = RecipeDraft("sample", "Sample", process_parameters={"running_speed": value})
    with pytest.raises(ConfigurationError):
        draft.to_recipe()
    assert draft.process_parameters == {"running_speed": value}


def test_catalog_change_requires_reload_preserves_draft_and_prepared_values(
    tmp_path: Path,
) -> None:
    session = editor(tmp_path)
    path = tmp_path / "process_parameters.json"
    session.catalog = RecipeCatalog(
        session.catalog.directory, process_catalog_path=path
    )
    assert session.entry is not None
    session.draft.process_parameters = {"running_speed": "100"}
    session.save()
    prepared = session.prepare(session.entry.recipe)
    assert prepared.configuration.process_parameters == (("running_speed", 100),)
    before = session.entry.path.read_bytes()
    device_catalog(path, {"overrides": [{"id": "running_speed", "maximum": 90}]})
    session.draft.process_parameters["running_speed"] = "80"
    with pytest.raises(ConfigurationError, match="catalog changed"):
        session.save()
    with pytest.raises(ConfigurationError):
        session.discard()  # Persisted 100 is now invalid; no silent trimming.
    assert session.draft.process_parameters == {"running_speed": "80"}
    assert session.entry.path.read_bytes() == before
    assert prepared.configuration.process_parameters == (("running_speed", 100),)
    snapshot = session.catalog.discover()
    assert any("running_speed" in issue.message for issue in snapshot.issues)
    device_catalog(path, {"overrides": [{"id": "running_speed", "maximum": 200}]})
    session.discard()
    assert not session.dirty and session.draft.process_parameters == {
        "running_speed": "100"
    }
    session.draft.process_parameters["running_speed"] = "180"
    session.save()
    assert session.entry.recipe.process_parameters == (("running_speed", 180),)
    duplicate = RecipeDraft.from_recipe(replace(session.entry.recipe, recipe_id="copy"))
    session.new()
    assert session.draft.process_parameters == {}
    assert duplicate.process_parameters == {"running_speed": "180"}


def test_startup_restores_custom_values_and_reports_invalid_catalog_without_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> Never:
        raise AssertionError("Process metadata restoration must not use equipment")

    monkeypatch.setattr("socket.socket.connect", forbidden)
    monkeypatch.setattr("socket.socket.sendto", forbidden)
    data = saved_data(tmp_path)
    catalog = device_catalog(
        data.process_catalog,
        {"parameters": [{**extension(), "group": "speed"}]},
    )
    recipe_path = data.recipes / "local.json"
    values = (("feeder_speed", -20), ("running_speed", 0))
    save_recipe(
        recipe_path,
        replace(load_recipe(recipe_path), process_parameters=values),
        process_catalog=catalog,
    )
    saved_recipe = recipe_path.read_bytes()
    saved_catalog = data.process_catalog.read_bytes()
    for _ in range(2):
        startup = load_startup(data, engine_factory=forbidden)
        try:
            assert startup.prepared is not None
            assert startup.prepared.configuration.process_parameters == values
            assert startup.editor is not None
            assert startup.editor.process_catalog == catalog
            assert startup.editor.draft.to_recipe(catalog).process_parameters == values
            assert not startup.application.snapshot().runtime_owned
        finally:
            close(startup)
    data.process_catalog.write_text('{"parameters":null}', encoding="utf-8")
    invalid = load_startup(data, engine_factory=forbidden)
    try:
        assert invalid.prepared is None
        assert any(
            issue.key == "startup_recipes" and "process_parameters.json" in issue.detail
            for issue in invalid.issues
        )
        assert data.process_catalog.read_text(encoding="utf-8") == '{"parameters":null}'
        assert recipe_path.read_bytes() == saved_recipe
    finally:
        close(invalid)
    data.process_catalog.write_bytes(saved_catalog)  # Explicit isolated fixture repair.
    restored = load_startup(data, engine_factory=forbidden)
    try:
        assert restored.prepared is not None
        assert restored.prepared.configuration.process_parameters == values
        assert recipe_path.read_bytes() == saved_recipe
    finally:
        close(restored)
