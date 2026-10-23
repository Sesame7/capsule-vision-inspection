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
from vimo_demo.ui.translations import (
    EXTRA_STRINGS,
    LANGUAGE_DISPLAY_ORDER,
    LANGUAGE_NAMES,
)

WATCH_SHORT_KEYS = (
    "comm_watch_remove_short",
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

EUROPEAN_LANGUAGES: dict[Language, str] = {
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
}

ASIAN_AFRICAN_LANGUAGES: dict[Language, str] = {
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
}

NEW_EUROPEAN_LANGUAGES: dict[Language, str] = {
    "lt": "Lietuvių",
    "lv": "Latviešu",
    "et": "Eesti",
    "sl": "Slovenščina",
    "sq": "Shqip",
    "ca": "Català",
}


AFRICAN_CENTRAL_ASIAN_LANGUAGES: dict[Language, str] = {
    "af": "Afrikaans",
    "zu": "isiZulu",
    "ha": "Hausa",
    "kk": "Қазақша",
    "uz": "Oʻzbekcha",
    "mn": "Монгол",
}


HIGH_COVERAGE_LANGUAGES: dict[Language, str] = {
    "so": "Soomaali",
    "tk": "Türkmençe",
    "tg": "Тоҷикӣ",
    "az-Latn": "Azərbaycanca",
    "mg": "Malagasy",
    "ky": "Кыргызча",
}


NEW_SCRIPT_LANGUAGES: dict[Language, str] = {
    "ka": "ქართული",
    "hy": "Հայերեն",
    "ti": "ትግርኛ",
}


@pytest.mark.parametrize("language", NEW_SCRIPT_LANGUAGES)
def test_new_scripts_preserve_operational_distinctions(language: Language) -> None:
    assert len(EXTRA_STRINGS[language]) == len(STRINGS) == 502
    for keys in (
        ("replay_start", "replay_pause", "production_stop", "reset"),
        ("save", "roi_set_rect", "discard", "cancel"),
        ("saved", "roi_applied"),
        ("source_unconfigured", "source_connected", "state_READY", "active"),
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
            "production",
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
        ("history", "ታሪኽ"),
        ("source_connected", "ተራኺቡ"),
    ):
        assert text(key, "ti") == expected
        assert text(key, "ti") != text(key, "am")
    assert text("production_stop", "hy") == "Կանգ"
    assert text("stop", "hy") == "Կանգնեցնել ստուգումը"
    assert text("replay_continue", "ka") == "განაგრძე"


@pytest.mark.parametrize("language", HIGH_COVERAGE_LANGUAGES)
def test_additional_languages_preserve_operational_distinctions(
    language: Language,
) -> None:
    for keys in (
        ("replay_start", "replay_pause", "production_stop", "reset"),
        ("save", "apply", "discard", "cancel"),
        ("source_unconfigured", "source_connected", "state_READY", "active"),
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
            "production",
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


@pytest.mark.parametrize("language", AFRICAN_CENTRAL_ASIAN_LANGUAGES)
def test_african_central_asian_operational_distinctions(language: Language) -> None:
    for keys in (
        ("replay_start", "replay_pause", "production_stop", "reset"),
        ("save", "apply", "discard", "cancel"),
        ("source_connected", "state_READY", "active", "plc_enabled"),
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
            "production",
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


def test_european_language_identities_and_complete_catalogs() -> None:
    assert LANGUAGES[13:29] == tuple(EUROPEAN_LANGUAGES)
    assert LANGUAGES[29:43] == tuple(ASIAN_AFRICAN_LANGUAGES)
    assert LANGUAGES[43:49] == tuple(NEW_EUROPEAN_LANGUAGES)
    assert LANGUAGES[49:55] == tuple(AFRICAN_CENTRAL_ASIAN_LANGUAGES)
    assert LANGUAGES[55:61] == tuple(HIGH_COVERAGE_LANGUAGES)
    assert LANGUAGES[61:] == tuple(NEW_SCRIPT_LANGUAGES)
    for language, native_name in (
        EUROPEAN_LANGUAGES
        | ASIAN_AFRICAN_LANGUAGES
        | NEW_EUROPEAN_LANGUAGES
        | AFRICAN_CENTRAL_ASIAN_LANGUAGES
        | HIGH_COVERAGE_LANGUAGES
        | NEW_SCRIPT_LANGUAGES
    ).items():
        assert is_language(language)
        assert LANGUAGE_NAMES[language] == native_name
        assert EXTRA_STRINGS[language].keys() == STRINGS.keys(), language


@pytest.mark.parametrize("language", NEW_EUROPEAN_LANGUAGES)
def test_new_european_actions_and_operational_distinctions(language: Language) -> None:
    assert EXTRA_STRINGS[language].keys() == STRINGS.keys()
    for keys in (
        ("replay_start", "replay_pause", "production_stop", "reset"),
        ("save", "apply", "discard", "cancel"),
        ("source_connected", "state_READY", "active", "plc_enabled"),
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


@pytest.mark.parametrize(
    "count, expected_digest",
    [
        (43, "0a43e286e9f2928f3da887adda6e4f3602644cae7d668f0242c0bd164b296c40"),
        (49, "f4ec294eed483e2e363f69f8ddc88d11d7ec22eeb46abd0a2b6994c0b10584a6"),
        (55, "5cca2ff28d2832b85ec0802886a8a37f1712841ef519bae27cb4793eff4a7f6e"),
        (61, "79e6bfd34ab4dd7d48c0732610315e3916a78212585815adf3fb52ef99fa53bc"),
    ],
)
def test_previous_languages_remain_exactly_unchanged(
    count: int, expected_digest: str
) -> None:
    original = LANGUAGES[:count]
    catalog = {
        "languages": list(original),
        "order": [
            language for language in LANGUAGE_DISPLAY_ORDER if language in original
        ],
        "names": {language: LANGUAGE_NAMES[language] for language in original},
        "catalog": {
            language: {key: text(key, language) for key in STRINGS}
            for language in original
        },
        "resources": {
            language: EXTRA_STRINGS[language]
            for language in original
            if language in EXTRA_STRINGS
        },
    }
    digest = hashlib.sha256(
        json.dumps(catalog, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    # Frozen before each language addition; includes compact captions,
    # explicit/fallback membership, native names, codes and relative order.
    assert digest == expected_digest


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
    assert LANGUAGES[:3] == ("zh", "en", "ja")
    assert LANGUAGES[12] == "zh-TW"


@pytest.mark.parametrize(
    "language",
    ASIAN_AFRICAN_LANGUAGES
    | NEW_EUROPEAN_LANGUAGES
    | AFRICAN_CENTRAL_ASIAN_LANGUAGES
    | HIGH_COVERAGE_LANGUAGES
    | NEW_SCRIPT_LANGUAGES,
)
def test_new_catalogs_preserve_critical_protocol_and_result_distinctions(
    language: Language,
) -> None:
    assert "FINS" in text("plc_ack_hint", language)
    assert "ACK" in text("plc_ack_hint", language)
    assert "PLC" in text("plc_ack_hint", language)
    assert "W49" in text("plc_reserved_write", language)
    assert "W52" in text("plc_reserved_write", language)
    assert "EMPTY" in text("preview_scope_tip", language)
    assert "Occupancy" in text("preview_scope_tip", language)
    for key, code in (
        ("technical_success", "SUCCESS"),
        ("technical_error", "ERROR"),
        ("technical_disabled", "DISABLED"),
        ("occupancy_empty", "EMPTY"),
        ("occupancy_present", "PRESENT"),
        ("occupancy_unknown", "UNKNOWN"),
    ):
        assert text(key, language) == code


@pytest.mark.parametrize("language", LANGUAGES)
def test_language_preferences_round_trip(tmp_path: Path, language: Language) -> None:
    path = tmp_path / "app.json"
    expected = ApplicationPreferences("model-id", "recipe-id", language)
    save_preferences(path, expected)
    loaded = read_preferences(path)
    assert loaded.preferences == expected
    assert not loaded.invalid_language


def test_vietnamese_ack_does_not_confirm_physical_product_rejection() -> None:
    assert text("plc_next_start", "vi") == (
        "Chỉ áp dụng cấu hình đã lưu ở lần Start tới; "
        "ACK không xác nhận sản phẩm đã thực sự bị loại bỏ."
    )


def test_tamil_fault_ack_and_history_observations_keep_their_scope() -> None:
    assert text("FAULT", "ta") == "அமைப்புக் கோளாறு"
    assert text("error_ng", "ta") == "NG + தொழில்நுட்ப பிழை"
    assert text("plc_ack_hint", "ta") == (
        "ACK என்பது FINS எழுதுதல் பதிலை மட்டுமே உறுதிப்படுத்துகிறது; "
        "PLC முடிவைப் பயன்படுத்தியதையோ தொடர்ச்சியான இணைப்பையோ உறுதிப்படுத்தாது. "
        "எண்ணிக்கைகள் காட்டப்பட்ட அறிக்கை தலைமுறைக்கு உரியவை."
    )
    assert text("history_clock", "ta") == (
        "கணினியின் உள்ளூர் கடிகாரப்படி கவனிக்கப்பட்ட நிறைவு நேரம்; "
        "கேமரா வெளிப்பாட்டு நேரம் அல்ல. "
        "வரலாறு வரம்புடன் இயன்றவரை சேமிக்கப்படுகிறது."
    )


def test_telugu_enable_and_admission_do_not_claim_machine_control() -> None:
    assert text("plc_enable", "te") == "సక్రియం చేయి"
    assert text("plc_enable", "te") != text("replay_start", "te")
    assert text("basler_control_help", "te") == (
        "తనిఖీకి చిత్రాల ప్రవేశాన్ని మాత్రమే నియంత్రిస్తుంది; "
        "యంత్రాన్ని లేదా PLCని నియంత్రించదు. "
        "విరామంలో చిత్రాలు విస్మరించబడతాయి; "
        "ఒక అడుగు తదుపరి బాహ్య ట్రిగర్ కోసం మరియు "
        "ఫలితం/నివేదిక పూర్తయ్యే వరకు వేచి ఉంటుంది."
    )


@pytest.mark.parametrize("language, camera", [("te", "కెమెరా"), ("mr", "कॅमेरा")])
def test_reviewed_camera_terms_preserve_camera_number_identity(
    language: Language, camera: str
) -> None:
    assert text("camera_setup", language).startswith(camera + " ")
    assert text("camera_number", language).format(number=6) == camera + " 6"


def test_marathi_manual_write_guard_retains_reporting_ownership() -> None:
    assert text("plc_reserved_write", "mr") == (
        "स्वयंचलित अहवालाकडे सत्राचा ताबा असताना W49–W52 मध्ये हाताने लिहिणे रोखले जाते."
    )


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


@pytest.mark.parametrize("language", LANGUAGES[36:43])
def test_additional_catalogs_translate_text_and_keep_protocol_codes(
    language: Language,
) -> None:
    unchanged_codes = {
        "production_ng",
        "production_ok",
        "production_empty",
        "preview_disabled_short",
        "preview_error_short",
        "ok",
        "ng",
        "empty",
        "occupancy_empty",
        "occupancy_present",
        "occupancy_unknown",
        "technical_success",
        "technical_error",
        "technical_disabled",
        "comm_dec",
        "comm_table_hex",
    }
    assert {
        key
        for key, value in EXTRA_STRINGS[language].items()
        if value == STRINGS[key][1]
    } == unchanged_codes
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
    assert len(set(LANGUAGES)) == len(LANGUAGES) == 64
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


@pytest.mark.parametrize("language", LANGUAGES[2:])
def test_optional_translation_falls_back_to_english(language: Language) -> None:
    if language not in (
        EUROPEAN_LANGUAGES
        | ASIAN_AFRICAN_LANGUAGES
        | NEW_EUROPEAN_LANGUAGES
        | AFRICAN_CENTRAL_ASIAN_LANGUAGES
        | HIGH_COVERAGE_LANGUAGES
        | NEW_SCRIPT_LANGUAGES
    ):
        assert "history_clock" not in EXTRA_STRINGS[language]
    for key in STRINGS.keys() - EXTRA_STRINGS[language].keys():
        assert text(key, language) == STRINGS[key][1], (key, language)


@pytest.mark.parametrize(
    "language",
    (
        "ja",
        *EUROPEAN_LANGUAGES,
        *ASIAN_AFRICAN_LANGUAGES,
        *NEW_EUROPEAN_LANGUAGES,
        *AFRICAN_CENTRAL_ASIAN_LANGUAGES,
        *HIGH_COVERAGE_LANGUAGES,
        *NEW_SCRIPT_LANGUAGES,
    ),
)
def test_optional_translation_can_be_omitted_for_only_one_language(
    language: Language,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = text("production", "ko")
    monkeypatch.delitem(EXTRA_STRINGS[language], "production")
    assert text("production", language) == STRINGS["production"][1]
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
    # Accepted catalogs preceding each expansion, including all pre-L2-B2 text.
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
        "nl": "711c89eabb81fed589a2be1ed0e5024ca899430b7b99f49b626719c622eebe22",
        "pl": "b8ebcdb459b839f178823c19ecdbc73396e428fb200198456f1744f8238b6d32",
        "uk": "b1773797b879dc61ac3e8a6a93f5474456ccc3331a9c56c69ea403f003140f36",
        "ro": "53e172401df9c76907cb3e0b3e88d84be54fe0e399f5c46762be175d299671ac",
        "cs": "345a5b4e332d4f5c6a4511f382bf5886f5540e7a6b6395894cfc0c4dfd713814",
        "hu": "5ba97d3227739ef47c439d8ff1d812857c0526d710acc9bb69eddba182cc9b51",
        "sv": "a14d337c547e29d59ce1021cf2b55acb8c2efd9534dbe01d3009635a49f6ae6d",
        "da": "b238673e678a97d7f134edaf7d2f242d9212fbc45a9bc58a05437ef6a321c90b",
        "fi": "97136b3d3cb8a0969b1889bbcc7922473638daac402d256f704841888d2e435b",
        "nb": "2494537d36c23fb923cf682d903d00c553190f26254358ad1574a1dcfc5ef06a",
        "sk": "19d4514fd7b04a9076466361375c3add10e4e7fe940423498cef5ed4a2f4c70c",
        "bg": "06c77c1e7304e2315b01f9611f29a8b5a373a4e2eb704eac496534e0915a7e95",
        "hr": "1c3119e5acaa56eac97f71f1a205fc2e8b071adacd18aa9f04a3bf8dc722534b",
        "sr-Latn": "e66be778ee69d4fb018713d7cbccf9b7314023bd8532b0b4cafc39ae52416d3a",
        "el": "1155761b93a1e38ea01cb5d3c1e86d6ca3a487fd9d95fccf03c6dd4373151d28",
        "pt-PT": "0cba9d1fbeaf742da4aa9bec5efa2a182fe4d57733efa3722a301eb595566a2f",
        "vi": "fe2234a5fd8a62ebecc9e2f1571b3f4c50ec534d2d472f934d82e8eab669f325",
        "ms": "fa9a6e4972020e4c76406843cfc8dbb707f76b37af10a5f41c3aeeed59e4ebac",
        "fil": "b47418951e2e6bcfd15d12d7bf057b372ac4a7ebe04939821f4d084a47999421",
        "sw": "5188b3d553674f518bd607b40c1dc94ac2fb189f926091a05708380bf7f9c82d",
        "th": "d4a21693d597d2083945244d0094c8bbb054b11bb90e00331da448f22d2af9fb",
        "hi": "7fca2040d86812fe2c58537b6f4cb64dfa5df6eb20a090664ab4ed3ff00ebac2",
        "bn": "d942fb44c47e9bf7f277dfbfca0b4c5283c484bc38553df6e92814b4e78eb7a5",
        "ta": "af84cc3eb007c2c369b86803e3c45a3d8c9b2d58257bdfc5c30d05126d77e4ae",
        "te": "9daa5a779f2a2aa4378f6ba5015e7b855ccf5734a75d644dc9a2aec904560dd0",
        "mr": "8c0bfccaf99ce5749ffde9be0dcc9d0bd324857731f5fa920bc85d0488149ead",
        "ne": "d70206acfb6fcae14d10ea10bd0924fc1ce18b4b59f642cf457250b816d035b0",
        "si": "f58bdfa3f9da3be1a67b83444625615999c2a091df4cd22c98b868645423c3be",
        "am": "40b3874e95c26d2ee5e4c3b47669725de15bd18acb7c8f1e362bf0eed1c6b6a6",
        "km": "e6c09a2da6544453bc603b4be13fc3157f24b7dce8aacb0cf0d248e941f6daac",
    }
    assert tuple(expected) == LANGUAGES[:43]
    for language, digest in expected.items():
        catalog = {
            # New Watch presentation keys extend the catalog; preserve every
            # original resolved value and explicit/fallback membership verbatim.
            "resolved": {
                key: text(key, language)
                for key in STRINGS
                if key not in (*WATCH_SHORT_KEYS, "comm_watch_short", *COMPACT_KEYS)
            },
            "translated_keys": sorted(
                EXTRA_STRINGS.get(language, {}).keys()
                - set((*WATCH_SHORT_KEYS, "comm_watch_short", *COMPACT_KEYS))
            ),
        }
        serialized = json.dumps(catalog, ensure_ascii=False, sort_keys=True)
        assert hashlib.sha256(serialized.encode("utf-8")).hexdigest() == digest, (
            language
        )


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
