"""Catalog coverage and the existing Qt-free language preference contract."""

import ast
import hashlib
import json
from collections import Counter
from pathlib import Path
from string import Formatter

import pytest

from vimo_demo.application.preferences import (
    LANGUAGES,
    ApplicationPreferences,
    Language,
    is_language,
    read_preferences,
    save_preferences,
)
from vimo_demo.ui import translations as translation_resources
from vimo_demo.ui.production_style import STRINGS, text
from vimo_demo.ui.translations import EXTRA_STRINGS, LANGUAGE_NAMES


@pytest.mark.parametrize("language", LANGUAGES)
def test_language_preferences_round_trip(tmp_path: Path, language: Language) -> None:
    path = tmp_path / "app.json"
    expected = ApplicationPreferences("model-id", "recipe-id", language)
    save_preferences(path, expected)
    loaded = read_preferences(path)
    assert loaded.preferences == expected
    assert not loaded.invalid_language


def test_catalog_essential_coverage_and_format_fields() -> None:
    essential = {
        "production",
        "parameters",
        "diagnostics",
        "history",
        "settings",
        "interface_language",
        "nav_diagnostics",
        "theme_light",
        "theme_dark",
        "replay_start",
        "replay_pause",
        "replay_continue",
        "replay_step",
        "production_stop",
        "reset",
        "cycles",
        "processing",
        "production_empty",
        "state_READY",
        "state_STARTING",
        "state_RUNNING",
        "state_STOPPING",
        "state_FAULT",
        "save_apply",
        "recipe_discard",
        "dirty_question",
        "cancel",
        "saved",
        "unsaved",
        "display_name",
        "inspection_model",
        "recipe_labels",
        "recipe_labels_error",
        "recipe_validation_error",
        "recipe_save_error",
        "recipe_prepare_error",
        "size_check",
        "color_check",
        "preview_pending",
        "preview_no_reference",
        "preview_unavailable",
        "preview_measurement_error",
        "preview_missing_body",
        "vision_folder_mode",
        "vision_basler_mode",
        "source_connect",
        "source_disconnect",
        "source_connected",
        "source_disconnected",
        "vision_capture",
        "vision_import",
        "vision_discard_question",
        "roi_geometry_error",
        "source_connection_error",
        "plc_reporting",
        "plc_enabled",
        "plc_active",
        "plc_ack_hint",
        "plc_next_start",
        "web_observer",
        "web_turn_on",
        "web_turn_off",
        "web_apply_changes",
        "comm_save_settings",
        "comm_discard_question",
        "comm_watch",
        "comm_write",
        "comm_write_unknown",
        "comm_invalid",
        "comm_timeout_error",
        "comm_last_read",
        "comm_effective_endpoint",
        "comm_current",
        "comm_previous",
        "comm_last_success",
        "comm_no_success",
        "history_all_dates",
        "history_batch",
        "history_result",
        "history_time",
        "history_refresh",
        "history_newer",
        "history_older",
        "history_warning",
        "history_camera",
        "history_select_channel",
        "history_recipe_legacy",
        "runtime_error",
        "startup_error",
        "shutdown_error",
        "software_stop",
    }
    assert tuple(LANGUAGE_NAMES) == LANGUAGES
    assert set(EXTRA_STRINGS) == set(LANGUAGES) - {"zh", "en"}
    assert len(set(LANGUAGES)) == len(LANGUAGES) == 13
    assert LANGUAGE_NAMES["zh-TW"] == "繁體中文"
    assert len(set(LANGUAGE_NAMES.values())) == len(LANGUAGES)
    formatter = Formatter()
    for language, translations in EXTRA_STRINGS.items():
        assert essential <= translations.keys() <= STRINGS.keys(), language
        for key, value in translations.items():
            fields = Counter(
                (name, spec, conversion)
                for _, name, spec, conversion in formatter.parse(STRINGS[key][1])
                if name is not None
            )
            assert value.strip(), (key, language)
            assert (
                Counter(
                    (name, spec, conversion)
                    for _, name, spec, conversion in formatter.parse(value)
                    if name is not None
                )
                == fields
            ), (key, language)
            assert text(key, language) == value
    for key, (zh, en) in STRINGS.items():
        assert text(key, "zh") == zh and text(key, "en") == en


@pytest.mark.parametrize("language", LANGUAGES[2:])
def test_optional_translation_falls_back_to_english(language: Language) -> None:
    assert "history_clock" not in EXTRA_STRINGS[language]
    for key in STRINGS.keys() - EXTRA_STRINGS[language].keys():
        assert text(key, language) == STRINGS[key][1], (key, language)


def test_optional_translation_can_be_omitted_for_only_one_language(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = text("production", "ko")
    monkeypatch.delitem(EXTRA_STRINGS["ja"], "production")
    assert text("production", "ja") == STRINGS["production"][1]
    assert text("production", "ko") == other
    assert text("production", "zh") == STRINGS["production"][0]


@pytest.mark.parametrize("language", LANGUAGES)
def test_unknown_key_fails_before_optional_translation_fallback(
    language: Language, monkeypatch: pytest.MonkeyPatch
) -> None:
    if language not in ("zh", "en"):
        monkeypatch.setitem(EXTRA_STRINGS[language], "unknown_catalog_key", "unused")
    with pytest.raises(KeyError):
        text("unknown_catalog_key", language)


def test_catalog_literals_have_no_duplicate_language_or_text_keys() -> None:
    module = ast.parse(Path(translation_resources.__file__).read_text(encoding="utf-8"))
    for statement in module.body:
        if (
            isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and statement.target.id in ("LANGUAGE_NAMES", "EXTRA_STRINGS")
        ):
            catalog = statement.value
            assert isinstance(catalog, ast.Dict)
            mappings = [catalog]
            if statement.target.id == "EXTRA_STRINGS":
                for value in catalog.values:
                    assert isinstance(value, ast.Dict)
                    mappings.append(value)
            for mapping in mappings:
                keys: list[str] = []
                for key in mapping.keys:
                    assert isinstance(key, ast.Constant) and isinstance(key.value, str)
                    keys.append(key.value)
                assert len(keys) == len(set(keys)), statement.target.id


def test_resolved_catalog_matches_pre_migration_text_verbatim() -> None:
    # Pre-L2-0 resolved text and explicit keys, including English-equivalent values.
    # Review these migration snapshots explicitly when catalog content changes.
    expected: dict[Language, str] = {
        "zh": "172cc2b271d593db07941b65f57444e39d8840a2bbbe0df7c57f3e87ac0de1d1",
        "en": "acd161c640b7bdcc814543fd761bf171461c14f0261f1619a7389737f345981d",
        "ja": "c9c1a400ff6acb1ea7ce4b27ec397a1f7e40ee41da38b81cf20f05d1e36adc98",
        "ko": "acfc10e455dd54ed65ddbfdc68e18b6560c8c835da668f30433f52792ea86740",
        "es": "2f38fe275e6cfda755a3485b1466f897391860017a0a2bc5aeae1be1907c465e",
        "pt-BR": "5ad97359caba63a03674acde659163b7b092d42be8809310c92842e3cbdcf680",
        "it": "f59190b5c320d7664ed49c8b33257911808f1c184331052aa92d5401a25e4970",
        "id": "a6d6a777777c8309c4b819588f3487a227aa1dc6d213b5968ee1535cf35cb71a",
        "fr": "23db4c69629df64eecd90728bbf24a897ce78de4de2838cbefd4f5b572539367",
        "de": "7e0f244b6caf3127cd3c054daaf78842c7110ca963d6a7c894af77f907a7dd31",
        "ru": "4dd119af259579e6d0a65b25b7337625531e421c390b9a2c9d331bbd2619a8b1",
        "tr": "108426368dbb70b4119277fc4266d0a14a17c3a2fbcfc0165056caf04304ec29",
        "zh-TW": "d8911e1cb916af4bbf2340e724e335bdbe8909aaaf970d2327cdb5361939eafe",
    }
    assert tuple(expected) == LANGUAGES
    for language, digest in expected.items():
        catalog = {
            "resolved": {key: text(key, language) for key in STRINGS},
            "translated_keys": sorted(EXTRA_STRINGS.get(language, {})),
        }
        serialized = json.dumps(catalog, ensure_ascii=False, sort_keys=True)
        assert hashlib.sha256(serialized.encode("utf-8")).hexdigest() == digest, (
            language
        )


def test_language_codes_are_exact_and_invalid_types_remain_rejected() -> None:
    invalid_mapping: dict[str, object] = {}
    for value in ("pt_BR", "EN", "zh-CN", "vi", "th", None, 1, ["en"], invalid_mapping):
        assert not is_language(value)


def test_traditional_chinese_uses_reviewed_industrial_terms() -> None:
    for key, expected in (
        ("diagnostics", "通訊"),
        ("save_apply", "儲存並套用"),
        ("source_connected", "已連線"),
        ("comm_timeout", "逾時（秒）"),
        ("cache_prepare", "準備 TensorRT 快取"),
        ("plc_reporting", "生產結果回報"),
        ("comm_effective_endpoint", "實際目標"),
    ):
        assert text(key, "zh-TW") == expected
    assert "ACK" in text("plc_ack_hint", "zh-TW")
    assert "PLC" in text("plc_ack_hint", "zh-TW")
    assert text("history_clock", "zh-TW") == STRINGS["history_clock"][1]
