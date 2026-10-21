"""Catalog coverage and the existing Qt-free language preference contract."""

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
    assert essential <= EXTRA_STRINGS.keys() <= STRINGS.keys()
    assert set(LANGUAGE_NAMES) == set(LANGUAGES)
    assert len(LANGUAGES) == 13
    assert LANGUAGE_NAMES["zh-TW"] == "繁體中文"
    assert len(set(LANGUAGE_NAMES.values())) == len(LANGUAGES)
    formatter = Formatter()
    for key, translations in EXTRA_STRINGS.items():
        assert len(translations) == len(LANGUAGES) - 2, key
        fields = {name for _, name, _, _ in formatter.parse(STRINGS[key][1]) if name}
        for language, value in zip(LANGUAGES[2:], translations, strict=True):
            assert value.strip(), (key, language)
            assert {
                name for _, name, _, _ in formatter.parse(value) if name
            } == fields, (key, language)
            assert text(key, language) == value
    for key, (zh, en) in STRINGS.items():
        assert text(key, "zh") == zh and text(key, "en") == en


@pytest.mark.parametrize("language", LANGUAGES[2:])
def test_optional_translation_falls_back_to_english(language: Language) -> None:
    assert "history_clock" not in EXTRA_STRINGS
    assert text("history_clock", language) == STRINGS["history_clock"][1]
    with pytest.raises(KeyError):
        text("unknown_catalog_key", language)


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
