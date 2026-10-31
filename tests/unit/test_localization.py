"""Catalog coverage and the existing Qt-free language preference contract."""

import ast
import subprocess
import sys
from collections import Counter
from pathlib import Path
from string import Formatter
from typing import get_args

import pytest

from capsule_vision import ui
from capsule_vision.application import ApplicationState
from capsule_vision.application.basler_connection import ConnectionState
from capsule_vision.communication.operations import RequestKind
from capsule_vision.configuration.preferences import (
    ApplicationPreferences,
    read_preferences,
    save_preferences,
)
from capsule_vision.inspection.production import ChannelBusinessStatus, TechnicalStatus
from capsule_vision.localization import catalog as base_resources
from capsule_vision.localization import languages as language_resources
from capsule_vision.localization import translations as translation_resources
from capsule_vision.localization.catalog import STRINGS, text
from capsule_vision.localization.languages import (
    LANGUAGE_DISPLAY_ORDER,
    LANGUAGE_LABELS,
    LANGUAGE_NAMES,
    LANGUAGES,
    Language,
    is_language,
)
from capsule_vision.localization.translations import EXTRA_STRINGS
from capsule_vision.ui.production_presentation import Indicator
from capsule_vision.web.runtime import WebState

ENGLISH_DETAIL_KEYS = {
    "preview_scope_tip",
    "history_clock",
    "web_boundary",
}

OPTIONAL_UI_KEYS = {
    "recipe_preparing_short",
    "active_recipe",
    "select_saved_recipe",
    "recipe_dirty_question",
    "plc_modbus_base_required",
    "plc_modbus_base_hint",
    "comm_modbus_transport_error",
    "comm_holding_registers",
    "comm_modbus_hint",
    "comm_modbus_protocol_error",
    "comm_modbus_exception",
    "plc_base_address",
    "plc_reporting_write_protected",
    "model_import",
    "model_rename",
    "model_delete",
    "model_delete_question",
    "model_module",
    "model_name",
    "model_action_error",
}

# These tokens may inherit canonical English; native overrides remain explicit.
TECHNICAL_TOKEN_KEYS = {
    "production_ok",
    "production_ng",
    "production_empty",
    "ok",
    "ng",
    "empty",
    "comm_dec",
    "comm_table_hex",
    "technical_success",
    "technical_error",
    "preview_error_short",
}

WATCH_SHORT_KEYS = (
    "comm_watch_remove",
    "comm_watch_once_short",
    "comm_watch_auto_short",
    "comm_watch_stop_short",
)

COMPACT_KEYS = (
    "short_cycles",
    "short_name",
    "short_code",
    "short_reporting",
    "short_timeout",
    "short_refresh",
    "short_words",
    "short_time",
)

EXPECTED_LANGUAGE_NAMES: dict[Language, str] = {
    "zh": "中文",
    "en": "English",
    "ja": "日本語",
    "ko": "한국어",
    "es": "Español",
    "pt-BR": "Português (Brasil)",
    "it": "Italiano",
    "id": "Bahasa Indonesia",
    "fr": "Français",
    "de": "Deutsch",
    "ru": "Русский",
    "tr": "Türkçe",
    "zh-TW": "繁體中文",
    "nl": "Nederlands",
    "pl": "Polski",
    "uk": "Українська",
    "ro": "Română",
    "cs": "Čeština",
    "hu": "Magyar",
    "sv": "Svenska",
    "da": "Dansk",
    "fi": "Suomi",
    "nb": "Norsk bokmål",
    "sk": "Slovenčina",
    "bg": "Български",
    "hr": "Hrvatski",
    "sr-Latn": "Srpski (latinica)",
    "el": "Ελληνικά",
    "pt-PT": "Português (Portugal)",
    "vi": "Tiếng Việt",
    "ms": "Bahasa Melayu",
    "fil": "Filipino",
    "sw": "Kiswahili",
    "th": "ไทย",
    "hi": "हिन्दी",
    "bn": "বাংলা",
    "ta": "தமிழ்",
    "te": "తెలుగు",
    "mr": "मराठी",
    "ne": "नेपाली",
    "si": "සිංහල",
    "am": "አማርኛ",
    "km": "ខ្មែរ",
    "lt": "Lietuvių",
    "lv": "Latviešu",
    "et": "Eesti",
    "sl": "Slovenščina",
    "sq": "Shqip",
    "ca": "Català",
    "af": "Afrikaans",
    "zu": "isiZulu",
    "ha": "Hausa",
    "kk": "Қазақша",
    "uz": "Oʻzbekcha",
    "mn": "Монгол",
    "so": "Soomaali",
    "tk": "Türkmençe",
    "tg": "Тоҷикӣ",
    "az-Latn": "Azərbaycanca",
    "mg": "Malagasy",
    "ky": "Кыргызча",
    "ka": "ქართული",
    "hy": "Հայերեն",
    "ti": "ትግርኛ",
}

# Current coverage rules, independent of language insertion order. These specific
# captions and technical explanations may use English in the named catalogs;
# all other nontechnical/non-English-only keys require an authored value.
OPTIONAL_NATIVE_KEYS = {
    "apply",
    "channel_count_help",
    "channel_ng",
    "channel_ok",
    "cli_sources",
    "collection_short",
    "comm_busy",
    "comm_closing",
    "comm_idle",
    "comm_local_write",
    "comm_transport_error",
    "comm_word",
    "command_error",
    "fault_note",
    "history_cycle_disabled",
    "history_cycle_error",
    "history_cycle_unknown",
    "history_segment_observed",
    "invalid_result",
    "issue_note",
    "module_id",
    "no_catalog",
    "pause_unsupported",
    "pending",
    "preview_invalid_geometry",
    "recipe_id",
    "recipe_labels_retained",
    "reference",
    "reference_runtime_tip",
    "reference_unavailable",
    "roi_applied",
    "roi_recipe_save",
    "roi_workspace",
    "save_machine_first",
    "setup_failed",
    "simulation",
    "source_paused",
    "source_pausing",
    "source_resuming",
    "source_stepping",
    "startup_configuration",
    "startup_machine",
    "startup_models",
    "startup_preferences",
    "startup_recipe_identity",
    "startup_recipes",
    "startup_source_unavailable",
    "startup_sources",
    "technical_note",
    "traditional_short",
    "unavailable_model",
    "vision_fps_decrease",
    "vision_fps_increase",
    "web_power_tip",
}

OPTIONAL_NATIVE_KEYS_BY_LANGUAGE: dict[Language, set[str]] = {
    "ja": OPTIONAL_NATIVE_KEYS,
    "ko": OPTIONAL_NATIVE_KEYS,
    "es": OPTIONAL_NATIVE_KEYS,
    "pt-BR": OPTIONAL_NATIVE_KEYS,
    "it": OPTIONAL_NATIVE_KEYS,
    "id": OPTIONAL_NATIVE_KEYS,
    "fr": OPTIONAL_NATIVE_KEYS,
    "de": OPTIONAL_NATIVE_KEYS,
    "ru": OPTIONAL_NATIVE_KEYS,
    "tr": OPTIONAL_NATIVE_KEYS,
    "zh-TW": OPTIONAL_NATIVE_KEYS,
}


@pytest.mark.parametrize("language", ("ka", "hy", "ti"))
def test_new_scripts_preserve_operational_distinctions(language: Language) -> None:
    for keys in (
        ("replay_start", "replay_pause", "production_stop", "reset"),
        ("save", "roi_set_rect", "discard", "cancel"),
        ("saved", "roi_applied"),
        ("source_unconfigured", "source_connected", "READY", "plc_active"),
        ("ng", "error", "FAULT", "empty", "unknown"),
    ):
        assert len({text(key, language) for key in keys}) == len(keys)
    assert text("plc_enable", language) != text("replay_start", language)
    assert text("comm_watch_stop", language) != text("production_stop", language)
    physical_rejection = {
        "ka": "ACK არ ადასტურებს პროდუქტის ფიზიკურ ამოღებას",
        "hy": "ACK-ը չի հաստատում արտադրանքի ֆիզիկական հեռացումը",
        "ti": "ACK ፍርያት ብኣካል ከም ዝተኣለየ ኣየረጋግጽን",
    }
    assert physical_rejection[language] in text("plc_next_start", language)
    retained_draft = {
        "ka": "მონახაზი უცვლელია",
        "hy": "սևագիրն անփոփոխ է",
        "ti": "ንድፊ ኣይተቐየረን",
    }
    assert retained_draft[language] in text("comm_reload_failed", language)
    assert text("comm_settings_saved", language) not in text(
        "comm_reload_failed", language
    )


def test_georgian_armenian_and_tigrinya_use_native_scripts() -> None:
    scripts: tuple[tuple[Language, str, str], ...] = (
        ("ka", "\u10a0", "\u10ff"),
        ("hy", "\u0530", "\u058f"),
        ("ti", "\u1200", "\u137f"),
    )
    for language, start, end in scripts:
        for key in (
            "page_production",
            "save",
            "discard",
            "source_connected",
            "camera_setup",
        ):
            caption = text(key, language)
            assert any(start <= char <= end for char in caption), (language, key)
            assert not any(c.isascii() and c.isalpha() for c in caption)
    # Shared script does not make Amharic a Tigrinya translation catalog.
    for key, expected in (
        ("size", "ዓቐን"),
        ("color", "ሕብሪ"),
        ("page_history", "ታሪኽ"),
        ("source_connected", "ተራኺቡ"),
    ):
        assert text(key, "ti") == expected
        assert text(key, "ti") != text(key, "am")
    assert text("production_stop", "hy") == "Կանգ"
    assert text("stop", "hy") == "Կանգնեցնել ստուգումը"
    assert text("replay_continue", "ka") == "განაგრძე"


@pytest.mark.parametrize("language", ("so", "tk", "tg", "az-Latn", "mg", "ky"))
def test_additional_languages_preserve_operational_distinctions(
    language: Language,
) -> None:
    for keys in (
        ("replay_start", "replay_pause", "production_stop", "reset"),
        ("save", "apply", "discard", "cancel"),
        ("source_unconfigured", "source_connected", "READY", "plc_active"),
        ("ng", "error", "FAULT"),
    ):
        assert len({text(key, language) for key in keys}) == len(keys)
    assert text("plc_enable", language) != text("replay_start", language)
    assert text("comm_watch_stop", language) != text("production_stop", language)
    physical_rejection = {
        "so": "ma xaqiijiyo ka-saaridda dhabta ah ee badeecadda",
        "tk": "fiziki çykarylandygyny tassyklamaýar",
        "tg": "хориҷкунии воқеии маҳсулотро тасдиқ намекунад",
        "az-Latn": "fiziki olaraq kənarlaşdırıldığını təsdiqləmir",
        "mg": "tsy manamarina ny fanesorana ara-batana ny vokatra",
        "ky": "физикалык чыгарылганын ырастабайт",
    }
    assert physical_rejection[language] in text("plc_next_start", language)
    retained_draft = {
        "so": "qabyadu isma beddelin",
        "tk": "garalama üýtgemedi",
        "tg": "пешнавис бетағйир монд",
        "az-Latn": "qaralama dəyişməyib",
        "mg": "tsy niova ny volavola",
        "ky": "долбоор өзгөргөн жок",
    }
    assert retained_draft[language] in text("comm_reload_failed", language)
    assert text("comm_settings_saved", language) not in text(
        "comm_reload_failed", language
    )


def test_additional_catalogs_keep_latin_and_cyrillic_orthography() -> None:
    for language in ("so", "tk", "az-Latn", "mg"):
        assert not any(
            "\u0400" <= c <= "\u04ff" or "\u0600" <= c <= "\u06ff"
            for value in EXTRA_STRINGS[language].values()
            for c in value
        ), language
    for language in ("tg", "ky"):
        for key in (
            "page_production",
            "save",
            "discard",
            "source_connected",
            "camera_setup",
        ):
            assert any("\u0400" <= c <= "\u04ff" for c in text(key, language))
            assert not any(c.isascii() and c.isalpha() for c in text(key, language))
    assert set("äňöşüý") <= set("".join(EXTRA_STRINGS["tk"].values()).lower())
    assert not any(c in value for value in EXTRA_STRINGS["tk"].values() for c in "ıİəƏ")
    assert set("ғқҳҷӣӯ") <= set("".join(EXTRA_STRINGS["tg"].values()).lower())
    assert set("əğıöşüç") <= set("".join(EXTRA_STRINGS["az-Latn"].values()).lower())
    assert set("ңөү") <= set("".join(EXTRA_STRINGS["ky"].values()).lower())


@pytest.mark.parametrize("language", ("af", "zu", "ha", "kk", "uz", "mn"))
def test_african_central_asian_operational_distinctions(language: Language) -> None:
    for keys in (
        ("replay_start", "replay_pause", "production_stop", "reset"),
        ("save", "apply", "discard", "cancel"),
        ("source_connected", "READY", "plc_active", "plc_enabled"),
        ("ng", "error", "FAULT"),
    ):
        assert len({text(key, language) for key in keys}) == len(keys)
    assert text("plc_enable", language) != text("replay_start", language)
    assert text("comm_watch_stop", language) != text("production_stop", language)
    physical_rejection = {
        "af": "bevestig nie fisiese uitsortering nie",
        "zu": "ayiqinisekisi ukukhishwa komkhiqizo ngokomzimba",
        "ha": "ba ya tabbatar da fitar da samfur a zahiri",
        "kk": "физикалық шығарылғанын растамайды",
        "uz": "jismonan chiqarilganini tasdiqlamaydi",
        "mn": "бодитоор зайлуулсныг батлахгүй",
    }
    assert physical_rejection[language] in text("plc_next_start", language)
    retained_draft = {
        "af": "konsep behou",
        "zu": "uhlaka alushintshile",
        "ha": "daftari na nan",
        "kk": "нобай өзгеріссіз қалды",
        "uz": "qoralama oʻzgarmadi",
        "mn": "ноорог хэвээр",
    }
    assert retained_draft[language] in text("comm_reload_failed", language)
    assert text("comm_settings_saved", language) not in text(
        "comm_reload_failed", language
    )


def test_new_catalogs_use_the_requested_writing_systems() -> None:
    for language in ("af", "zu", "ha", "uz"):
        assert not any(
            "\u0400" <= char <= "\u04ff" or "\u0600" <= char <= "\u06ff"
            for value in EXTRA_STRINGS[language].values()
            for char in value
        ), language
    for language in ("kk", "mn"):
        for key in (
            "page_production",
            "save",
            "discard",
            "source_connected",
            "camera_setup",
        ):
            assert any("\u0400" <= c <= "\u04ff" for c in text(key, language))
            assert not any(c.isascii() and c.isalpha() for c in text(key, language))
    assert set("ɓɗƙ") <= set("".join(EXTRA_STRINGS["ha"].values()))
    assert set("әғқңөұүі") <= set("".join(EXTRA_STRINGS["kk"].values()).lower())
    assert set("өү") <= set("".join(EXTRA_STRINGS["mn"].values()).lower())
    uzbek = " ".join(EXTRA_STRINGS["uz"].values()).lower()
    assert "oʻ" in uzbek and "gʻ" in uzbek
    assert not any(mark in uzbek for mark in ("o'", "g'", "o‘", "g‘", "o’", "g’"))


def test_language_identities_and_current_catalog_coverage() -> None:
    assert get_args(Language.__value__) == LANGUAGES == tuple(EXPECTED_LANGUAGE_NAMES)
    assert LANGUAGE_NAMES == EXPECTED_LANGUAGE_NAMES
    assert set(LANGUAGE_LABELS) == set(LANGUAGES)
    assert (
        OPTIONAL_NATIVE_KEYS
        | OPTIONAL_UI_KEYS
        | ENGLISH_DETAIL_KEYS
        | TECHNICAL_TOKEN_KEYS
        <= STRINGS.keys()
    )
    assert OPTIONAL_NATIVE_KEYS_BY_LANGUAGE.keys() <= EXTRA_STRINGS.keys()
    exceptions = {"zh": "中文", "zh-TW": "繁中", "sr-Latn": "SR", "az-Latn": "AZ"}
    for language in LANGUAGES:
        assert LANGUAGE_LABELS[language] == exceptions.get(language, language.upper())
    for language, native_name in EXPECTED_LANGUAGE_NAMES.items():
        assert is_language(language) and LANGUAGE_NAMES[language] == native_name
        if language in ("zh", "en"):
            continue
        required = (
            STRINGS.keys()
            - OPTIONAL_UI_KEYS
            - ENGLISH_DETAIL_KEYS
            - TECHNICAL_TOKEN_KEYS
            - OPTIONAL_NATIVE_KEYS_BY_LANGUAGE.get(language, set())
        )
        assert required <= EXTRA_STRINGS[language].keys(), language
        assert (
            not (OPTIONAL_UI_KEYS | ENGLISH_DETAIL_KEYS)
            & EXTRA_STRINGS[language].keys()
        ), language


@pytest.mark.parametrize("language", ("lt", "lv", "et", "sl", "sq", "ca"))
def test_new_european_actions_and_operational_distinctions(language: Language) -> None:
    for keys in (
        ("replay_start", "replay_pause", "production_stop", "reset"),
        ("save", "apply", "discard", "cancel"),
        ("source_connected", "READY", "plc_active", "plc_enabled"),
        ("ng", "error", "FAULT"),
    ):
        assert len({text(key, language) for key in keys}) == len(keys)
    assert text("plc_enable", language) != text("replay_start", language)
    assert text("comm_watch_stop", language) != text("production_stop", language)
    physical_rejection = {
        "lt": "nepatvirtina fizinio atmetimo",
        "lv": "neapstiprina fizisku izbrāķēšanu",
        "et": "ei kinnita füüsilist väljapraakimist",
        "sl": "ne potrjuje fizičnega izmeta",
        "sq": "nuk konfirmon nxjerrjen fizike",
        "ca": "no confirma el rebuig físic",
    }
    assert physical_rejection[language] in text("plc_next_start", language)
    retained_draft = {
        "lt": "juodraštis išlaikytas",
        "lv": "melnraksts ir paturēts",
        "et": "mustand säilitati",
        "sl": "osnutek je ohranjen",
        "sq": "drafti mbetet i pandryshuar",
        "ca": "es conserva l'esborrany",
    }
    assert retained_draft[language] in text("comm_reload_failed", language)


def test_machine_recovery_message_names_the_authoritative_tsv() -> None:
    for language in LANGUAGES:
        value = text("startup_machine", language)
        assert "machine.tsv" in value and "machine.json" not in value, language


def test_explicit_variants_have_independent_translations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert text("save", "nb") == "Lagre"
    assert text("save", "sr-Latn") == "Sačuvaj"
    assert text("save", "pt-PT") == "Guardar"
    assert text("save", "pt-BR") == "Salvar"
    brazilian = EXTRA_STRINGS["pt-BR"].copy()
    monkeypatch.delitem(EXTRA_STRINGS["pt-PT"], "save")
    assert text("save", "pt-PT") == STRINGS["save"][1]
    assert EXTRA_STRINGS["pt-BR"] == brazilian


def test_display_order_is_separate_from_saved_language_identities() -> None:
    assert LANGUAGE_DISPLAY_ORDER == (
        "zh",
        "zh-TW",
        "en",
        "af",
        "az-Latn",
        "id",
        "ms",
        "ca",
        "cs",
        "da",
        "de",
        "et",
        "es",
        "fil",
        "fr",
        "ha",
        "hr",
        "zu",
        "it",
        "sw",
        "lv",
        "lt",
        "hu",
        "mg",
        "nl",
        "nb",
        "uz",
        "pl",
        "pt-BR",
        "pt-PT",
        "ro",
        "sq",
        "sk",
        "sl",
        "so",
        "sr-Latn",
        "fi",
        "sv",
        "vi",
        "tr",
        "tk",
        "el",
        "bg",
        "ky",
        "mn",
        "ru",
        "tg",
        "uk",
        "kk",
        "hy",
        "ka",
        "ja",
        "ko",
        "th",
        "ne",
        "mr",
        "hi",
        "bn",
        "ta",
        "te",
        "si",
        "ti",
        "am",
        "km",
    )
    assert set(LANGUAGE_DISPLAY_ORDER) == set(LANGUAGES)
    assert len(LANGUAGE_DISPLAY_ORDER) == len(set(LANGUAGE_DISPLAY_ORDER))


@pytest.mark.parametrize("language", tuple(EXTRA_STRINGS))
def test_new_catalogs_preserve_critical_protocol_and_result_distinctions(
    language: Language,
) -> None:
    assert "ACK" in text("plc_next_start", language)
    assert "EMPTY" in text("preview_scope_tip", language)
    assert "Occupancy" in text("preview_scope_tip", language)
    for key, code in (
        ("technical_success", "SUCCESS"),
        ("technical_error", "ERROR"),
    ):
        expected = (
            {"technical_success": "執行成功", "technical_error": "技術異常"}[key]
            if language == "zh-TW"
            else code
        )
        assert text(key, language) == expected


@pytest.mark.parametrize("language", LANGUAGES)
def test_language_preferences_round_trip(tmp_path: Path, language: Language) -> None:
    path = tmp_path / "app.json"
    expected = ApplicationPreferences("recipe-id", language)
    save_preferences(path, expected)
    loaded = read_preferences(path)
    assert loaded.preferences == expected
    assert not loaded.invalid_language


def test_vietnamese_ack_does_not_confirm_physical_product_rejection() -> None:
    assert text("plc_next_start", "vi") == (
        "Chỉ áp dụng cấu hình đã lưu ở lần Start tới; "
        "ACK không xác nhận sản phẩm đã thực sự bị loại bỏ."
    )


def test_tamil_fault_and_error_remain_distinct() -> None:
    assert text("FAULT", "ta") == "அமைப்புக் கோளாறு"
    assert text("error_ng", "ta") == "NG + தொழில்நுட்ப பிழை"


def test_telugu_reporting_enable_does_not_mean_start() -> None:
    assert text("plc_enable", "te") == "సక్రియం చేయి"
    assert text("plc_enable", "te") != text("replay_start", "te")


@pytest.mark.parametrize("language, camera", [("te", "కెమెరా"), ("mr", "कॅमेरा")])
def test_reviewed_camera_terms_preserve_camera_number_identity(
    language: Language, camera: str
) -> None:
    assert text("camera_setup", language).startswith(camera + " ")
    assert text("camera_number", language).format(number=6) == camera + " 6"


def test_nepali_unsaved_and_failed_save_are_not_safety_or_success_claims() -> None:
    assert text("unsaved", "ne") == "सेभ नगरिएका परिवर्तनहरू"
    assert text("dirty_question", "ne") == (
        "सेभ नगरिएको मस्यौदा। सेभ गरी लागू गर्ने, परिवर्तन त्याग्ने वा रद्द गर्ने?"
    )
    assert text("comm_settings_failed", "ne") == (
        "सेभ गर्न असफल; सेटिङ वा फाइल अनुमति जाँच्नुहोस्"
    )
    assert text("result_unavailable", "ne") == "नतिजा उपलब्ध छैन"


def test_sinhala_start_spelling_preserves_next_start_and_failure_states() -> None:
    assert text("start", "si") == "පරීක්ෂාව ආරම්භ කරන්න"
    assert text("plc_enabled", "si") == "සක්‍රිය · ඊළඟ ආරම්භය"
    assert text("plc_starting", "si") == "සක්‍රිය · ආරම්භ කරමින්"
    assert text("startup_error", "si") == "ආරම්භය අසාර්ථක"


@pytest.mark.parametrize("language", ("ta", "te", "mr", "ne", "si", "am", "km"))
def test_additional_catalogs_translate_text_and_keep_protocol_codes(
    language: Language,
) -> None:
    assert {
        key
        for key, value in EXTRA_STRINGS[language].items()
        if value == STRINGS[key][1]
    } == {"preview_disabled_short"}
    # Reviewed terms distinguish physical removal from product type, and
    # retention of the unsaved draft from a successful save.
    terms: dict[Language, tuple[str, str]] = {
        "ta": ("வெளியேற்ற", "வரைவு பாதுகாக்கப்பட்டது"),
        "te": ("భౌతికంగా", "ముసాయిదా నిలబడింది"),
        "mr": ("प्रत्यक्ष", "मसुदा कायम"),
        "ne": ("भौतिकरूपमा", "मस्यौदा कायम छ"),
        "si": ("භෞතිකව", "කෙටුම්පත රැකී ඇත"),
        "am": ("በአካል", "ረቂቁ ተጠብቋል"),
        "km": ("ពិតប្រាកដ", "សេចក្តីព្រាងនៅដដែល"),
    }
    physical_rejection, retained_draft = terms[language]
    assert physical_rejection in text("plc_next_start", language)
    assert retained_draft in text("comm_reload_failed", language)


def test_catalog_essential_coverage_and_format_fields() -> None:
    essential = {
        *COMPACT_KEYS,
        *WATCH_SHORT_KEYS,
        "comm_watch_short",
        "page_production",
        "page_recipe",
        "page_communication",
        "page_history",
        "page_vision",
        "interface_language",
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
        "READY",
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
        "history_recipe_name_missing",
        "runtime_error",
        "startup_error",
        "shutdown_error",
        "software_stop",
    }
    assert tuple(LANGUAGE_NAMES) == LANGUAGES
    assert set(EXTRA_STRINGS) == set(LANGUAGES) - {"zh", "en"}
    assert len(set(LANGUAGES)) == len(LANGUAGES) == 64
    assert LANGUAGE_NAMES["zh-TW"] == "繁體中文"
    assert len(set(LANGUAGE_NAMES.values())) == len(LANGUAGES)
    formatter = Formatter()
    for language, translations in EXTRA_STRINGS.items():
        assert (
            essential - TECHNICAL_TOKEN_KEYS <= translations.keys() <= STRINGS.keys()
        ), language
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
        assert Counter(
            (name, spec, conversion)
            for _, name, spec, conversion in formatter.parse(zh)
            if name is not None
        ) == Counter(
            (name, spec, conversion)
            for _, name, spec, conversion in formatter.parse(en)
            if name is not None
        ), key


def test_watch_short_captions_are_explicit_for_every_language() -> None:
    for language in LANGUAGES:
        if language not in ("zh", "en"):
            assert (
                set((*WATCH_SHORT_KEYS, "comm_watch_short"))
                <= EXTRA_STRINGS[language].keys()
            )
        captions = tuple(text(key, language) for key in WATCH_SHORT_KEYS)
        assert all(caption.strip() and "…" not in caption for caption in captions)
        assert "1" in captions[2] and captions[3].endswith("1s")
        assert captions[2] != captions[3]
    assert tuple(text(key, "en") for key in WATCH_SHORT_KEYS) == (
        "Remove",
        "Read",
        "Auto · 1s",
        "Stop · 1s",
    )
    assert tuple(text(key, "pt-PT") for key in WATCH_SHORT_KEYS) == (
        "Remover",
        "Ler",
        "Auto · 1s",
        "Parar · 1s",
    )
    assert text("comm_watch_once", "pt-PT") == "Ler uma vez"
    assert text("comm_watch_stop", "pt-PT") == "Parar atualização"


def test_optional_ui_text_uses_existing_english_fallback() -> None:
    for key in OPTIONAL_UI_KEYS | ENGLISH_DETAIL_KEYS:
        assert key in STRINGS and STRINGS[key][0] and STRINGS[key][1]
        for language in LANGUAGES:
            assert text(key, language) == (
                STRINGS[key][0] if language == "zh" else STRINGS[key][1]
            )


def test_technical_tokens_use_fallback_and_preserve_native_overrides() -> None:
    for language in EXTRA_STRINGS:
        for key in TECHNICAL_TOKEN_KEYS:
            value = EXTRA_STRINGS[language].get(key)
            if value is None:
                assert text(key, language) == STRINGS[key][1], (key, language)
            else:
                assert value != STRINGS[key][1], (key, language)
                assert text(key, language) == value
    for key, value in (
        ("technical_success", "執行成功"),
        ("technical_error", "技術異常"),
        ("preview_error_short", "異常"),
    ):
        assert EXTRA_STRINGS["zh-TW"][key] == text(key, "zh-TW") == value


def test_technical_fallback_tracks_english_without_changing_native_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chinese, _ = STRINGS["technical_error"]
    monkeypatch.setitem(STRINGS, "technical_error", (chinese, "UPDATED ERROR"))
    for language in LANGUAGES:
        expected = (
            chinese
            if language == "zh"
            else "技術異常"
            if language == "zh-TW"
            else "UPDATED ERROR"
        )
        assert text("technical_error", language) == expected, language


def test_english_details_preserve_operational_limits() -> None:
    assert "PLC consumption" in text("comm_local_write", "en")
    assert "without Occupancy" in text("preview_scope_tip", "en")
    assert "not physical capsules" in text("channel_count_help", "en")
    assert "not exposure time" in text("history_clock", "en")
    assert "Trusted networks only" in text("web_boundary", "en")


def test_compact_captions_preserve_full_context_resources() -> None:
    for language in LANGUAGES:
        for compact, full in zip(
            COMPACT_KEYS,
            (
                "cycles",
                "display_name",
                "product_code",
                "plc_reporting",
                "comm_timeout",
                "web_refresh",
                "comm_count",
                "history_time",
            ),
            strict=True,
        ):
            assert text(compact, language).strip()
            assert "…" not in text(compact, language)
            assert text(full, language).strip()
            if language not in ("zh", "en"):
                assert compact in EXTRA_STRINGS[language]


@pytest.mark.parametrize("language", tuple(EXTRA_STRINGS))
def test_optional_translation_falls_back_to_english(language: Language) -> None:
    assert "history_clock" not in EXTRA_STRINGS[language]
    for key in STRINGS.keys() - EXTRA_STRINGS[language].keys():
        assert text(key, language) == STRINGS[key][1], (key, language)


@pytest.mark.parametrize("language", tuple(EXTRA_STRINGS))
def test_optional_translation_can_be_omitted_for_only_one_language(
    language: Language,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other_language = "ja" if language == "ko" else "ko"
    other = text("page_production", other_language)
    monkeypatch.delitem(EXTRA_STRINGS[language], "page_production")
    assert text("page_production", language) == STRINGS["page_production"][1]
    assert text("page_production", other_language) == other
    assert text("page_production", "zh") == STRINGS["page_production"][0]


@pytest.mark.parametrize("language", LANGUAGES)
def test_unknown_key_fails_before_optional_translation_fallback(
    language: Language, monkeypatch: pytest.MonkeyPatch
) -> None:
    if language not in ("zh", "en"):
        monkeypatch.setitem(EXTRA_STRINGS[language], "unknown_catalog_key", "unused")
    with pytest.raises(KeyError):
        text("unknown_catalog_key", language)


def test_catalog_literals_have_no_duplicate_language_or_text_keys() -> None:
    for resource in (base_resources, language_resources, translation_resources):
        assert resource.__file__ is not None
        module = ast.parse(Path(resource.__file__).read_text(encoding="utf-8"))
        for statement in module.body:
            if not isinstance(statement, (ast.AnnAssign, ast.Assign)):
                continue
            target = (
                statement.target
                if isinstance(statement, ast.AnnAssign)
                else statement.targets[0]
                if len(statement.targets) == 1
                else None
            )
            if isinstance(target, ast.Name) and target.id in (
                "STRINGS",
                "LANGUAGE_NAMES",
                "LANGUAGE_LABELS",
                "EXTRA_STRINGS",
            ):
                catalog = statement.value
                assert isinstance(catalog, ast.Dict)
                mappings = [catalog]
                if target.id == "EXTRA_STRINGS":
                    for value in catalog.values:
                        assert isinstance(value, ast.Dict)
                        mappings.append(value)
                for mapping in mappings:
                    keys: list[str] = []
                    for key in mapping.keys:
                        assert isinstance(key, ast.Constant) and isinstance(
                            key.value, str
                        )
                        keys.append(key.value)
                    assert len(keys) == len(set(keys)), target.id


def test_literal_ui_translation_consumers_have_catalog_keys() -> None:
    for path in Path(ui.__file__).parent.glob("*.py"):
        module = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(module):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "text"
                and node.args
                and isinstance(key := node.args[0], ast.Constant)
            ):
                assert key.value in STRINGS, (path.name, node.lineno, key.value)


def test_generated_localization_keys_remain_reachable_in_every_language() -> None:
    keys = {state.name for state in ApplicationState}
    keys.update(
        f"state_{state.name}"
        for state in ApplicationState
        if state
        not in (
            ApplicationState.UNCONFIGURED,
            ApplicationState.CLOSED,
            ApplicationState.READY,
        )
    )
    keys.update(
        "STOPPING" if state is WebState.STOPPING else f"web_{state.name.lower()}"
        for state in WebState
    )
    for state in (
        "closing",
        "replay_paused",
        "source_paused",
        "source_pausing",
        "source_stepping",
        "source_resuming",
    ):
        keys.update((state, f"state_{state}"))
    keys.update(f"source_{state.name.lower()}" for state in ConnectionState)
    keys.update(
        f"technical_{state.name.lower()}"
        for state in TechnicalStatus
        if state is not TechnicalStatus.DISABLED
    )
    keys.update(state.name.lower() for state in ChannelBusinessStatus)
    keys.update(state.value for state in Indicator)
    for kind in RequestKind:
        keys.add(f"comm_{kind.value}_ok")
    for action in ("remove", "once", "auto", "stop"):
        key = f"comm_watch_{action}"
        keys.update((key, key if action == "remove" else key + "_short"))
    keys.update(f"roi_{axis}" for axis in "xywh")
    keys.update(f"archive_{status}" for status in ("ok", "ng", "error", "empty"))
    for mode in ("size", "color"):
        keys.update((f"tune_{mode}", f"{mode}_check"))
    keys.update(
        f"{action}_question"
        for action in ("model_delete", "delete_recipe", "recipe_discard")
    )
    keys.update(
        f"preview_{reason}"
        for reason in (
            "missing_body",
            "competing_bodies",
            "truncated_body",
            "invalid_geometry",
            "insufficient_foreground",
            "unusable",
            "width",
            "height",
            "tolerance",
        )
    )
    for key in keys:
        assert key in STRINGS, key
        for language in LANGUAGES:
            assert text(key, language).strip(), (key, language)


def test_language_codes_are_exact_and_invalid_types_remain_rejected() -> None:
    invalid_mapping: dict[str, object] = {}
    for value in (
        "pt_BR",
        "pt_PT",
        "pt",
        "no",
        "sr",
        "sr-Cyrl",
        "EN",
        "zh-CN",
        "vi-VN",
        "ms-MY",
        "tl",
        "sw-KE",
        "th-TH",
        "hi-IN",
        "bn-BD",
        "ta-IN",
        "te-IN",
        "mr-IN",
        "ne-NP",
        "si-LK",
        "am-ET",
        "km-KH",
        "lt-LT",
        "lv-LV",
        "et-EE",
        "sl-SI",
        "sq-AL",
        "ca-ES",
        "af-ZA",
        "zu-ZA",
        "ha-NG",
        "ha-Arab",
        "kk-KZ",
        "kk-Latn",
        "uz-UZ",
        "uz-Cyrl",
        "mn-MN",
        "mn-Mong",
        "so-SO",
        "tk-TM",
        "tg-TJ",
        "az",
        "az-AZ",
        "az-Cyrl",
        "az-Arab",
        "mg-MG",
        "ky-KG",
        "ka-GE",
        "hy-AM",
        "ti-ER",
        "ti-ET",
        "KA",
        "HY",
        "TI",
        None,
        1,
        ["en"],
        invalid_mapping,
    ):
        assert not is_language(value)


def test_traditional_chinese_uses_reviewed_industrial_terms() -> None:
    for key, expected in (
        ("page_communication", "通訊"),
        ("save_apply", "儲存並套用"),
        ("source_connected", "已連線"),
        ("comm_timeout", "逾時（秒）"),
        ("cache_prepare", "準備 TensorRT 快取"),
        ("plc_reporting", "生產結果回報"),
        ("comm_effective_endpoint", "實際目標"),
    ):
        assert text(key, "zh-TW") == expected
    assert text("history_clock", "zh-TW") == STRINGS["history_clock"][1]


def test_localization_is_pure_and_retired_owners_do_not_export_localization() -> None:
    script = """
import sys
from unittest.mock import patch
with patch('threading.Thread.start', side_effect=AssertionError('import-time thread')):
    with patch('socket.socket', side_effect=AssertionError('import-time socket')):
        from capsule_vision.localization import languages
        assert 'capsule_vision.localization.catalog' not in sys.modules
        from capsule_vision.localization import catalog, translations
assert not any(n.startswith(('PySide6', 'capsule_vision.ui', 'capsule_vision.configuration')) for n in sys.modules)
assert len(languages.LANGUAGES) == 64 and len(catalog.STRINGS) == 423
assert len(translations.EXTRA_STRINGS) == 62
"""
    subprocess.run(
        [sys.executable, "-c", script], check=True, capture_output=True, text=True
    )
    from capsule_vision.configuration import preferences
    from capsule_vision.ui import style

    assert not any(
        hasattr(preferences, name) for name in ("Language", "LANGUAGES", "is_language")
    )
    assert not any(
        hasattr(style, name)
        for name in ("Language", "LANGUAGES", "STRINGS", "text", "EXTRA_STRINGS")
    )
    assert not (Path(ui.__file__).parent / "translations.py").exists()
