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

PRIORITY_UI_LANGUAGES = (
    "zh-TW",
    "ja",
    "ko",
    "es",
    "pt-BR",
    "it",
    "id",
    "fr",
    "de",
    "ru",
    "tr",
    "nl",
    "pl",
    "vi",
    "th",
    "hi",
)

IDENTITY_UI_KEYS = {
    "identity_setup",
    "identity_rotate",
    "identity_guidance",
    "identity_weak",
    "identity_grant",
    "identity_maintenance",
    "identity_stop_app",
    "identity_audit_incomplete",
    "identity_new_user",
    "identity_status",
    "identity_last_login",
    "identity_selected",
    "identity_no_selection",
    "identity_change_role",
    "identity_confirm_disable",
    "identity_close",
    "identity_disabled",
    "identity_guest",
    "identity_login",
    "identity_auto_login",
    "identity_set_auto_login",
    "identity_disable_auto_login",
    "identity_logout",
    "identity_switch",
    "identity_change",
    "identity_manage",
    "identity_username",
    "identity_password",
    "identity_old_password",
    "identity_new_password",
    "identity_confirm",
    "identity_role",
    "identity_enabled",
    "identity_operator",
    "identity_engineer",
    "identity_administrator",
    "identity_create",
    "identity_reset",
    "identity_apply",
    "identity_enable",
    "identity_disable",
    "identity_working",
    "identity_invalid",
    "identity_mismatch",
    "identity_validation",
    "identity_conflict",
    "identity_last_admin",
    "identity_denied",
    "identity_stale",
    "identity_storage",
    "identity_missing",
    "identity_error",
    "identity_done",
    "identity_busy",
}

# Untranslated optional entries used outside the covered Qt workflows, including
# report-only text, and compact captions chosen only by the base UI languages.
ENGLISH_FALLBACK_KEYS = {
    "alarm_current",
    "alarm_enable_tip",
    "alarm_file_created",
    "alarm_file_exists",
    "alarm_file_invalid",
    "alarm_file_missing",
    "alarm_file_open",
    "alarm_file_opened",
    "alarm_file_unavailable",
    "alarm_raw_bit",
    "alarm_stale",
    "alarm_station",
    "alarm_word",
    "audit_actor",
    "audit_all_actions",
    "audit_completed",
    "audit_id",
    "audit_remarks",
    "batch_edit_idle",
    "batch_edit_info",
    "batch_first_run",
    "batch_first_run_hint",
    "batch_info_conflict",
    "batch_information",
    "batch_inspection_span",
    "batch_recent_run",
    "batch_recent_run_hint",
    "batch_report_deferred",
    "batch_view_inspections",
    "customer_accounts_missing",
    "customer_alarm_period",
    "customer_audit_account",
    "customer_audit_before",
    "customer_audit_capacity",
    "customer_audit_excluded",
    "customer_audit_from",
    "customer_audit_required",
    "customer_audit_slot",
    "customer_batch_basis",
    "customer_batch_total",
    "customer_classification_unavailable",
    "customer_classified_ng",
    "customer_color",
    "customer_cycle_coverage",
    "customer_end",
    "customer_errors",
    "customer_first",
    "customer_group_coverage",
    "customer_last",
    "customer_length",
    "customer_missing_ng",
    "customer_prepared",
    "customer_reviewed",
    "customer_statistics_unavailable",
    "customer_total_ng",
    "customer_unattributed",
    "customer_unclassified",
    "customer_unit",
    "export_affected",
    "export_all",
    "export_batch",
    "export_before",
    "export_capsule",
    "export_category",
    "export_category_totals",
    "export_channel_detail",
    "export_classified_groups",
    "export_complete",
    "export_conservative",
    "export_count",
    "export_cycle_categories",
    "export_cycles",
    "export_defects",
    "export_durable",
    "export_evidence",
    "export_explained",
    "export_filtered_hint",
    "export_from",
    "export_group_categories",
    "export_groups",
    "export_items",
    "export_measured_cycles",
    "export_member_cycles",
    "export_no_evidence",
    "export_no_groups",
    "export_none",
    "export_note_capsule",
    "export_note_defects",
    "export_note_detections",
    "export_occurrences",
    "export_page",
    "export_period",
    "export_retained",
    "export_source",
    "export_total",
    "export_traditional",
    "export_type",
    "export_unknown_ng",
    "pilot_unknown",
    "plc_four_cycle_hint",
    "plc_mode_four_cycle_short",
    "plc_mode_per_cycle_short",
    "records_account_id",
    "records_action_code",
    "records_batch_id",
    "records_batch_report",
    "records_definition",
    "records_signal",
    "records_technical",
    "report_as_of",
    "report_completed_groups",
    "report_counter",
    "report_created",
    "report_data",
    "report_duration",
    "report_empty",
    "report_end_observed",
    "report_err_cycles",
    "report_exported",
    "report_generated",
    "report_groups",
    "report_in_progress",
    "report_ng_cycles",
    "report_occurrences",
    "report_page_total",
    "report_partial",
    "report_passed",
    "report_recorded",
    "report_reference",
    "report_reject_rate",
    "report_rejected",
    "report_retained_cycles",
    "report_retained_groups",
    "report_runs",
    "report_search",
    "report_signature_date",
    "report_started_cycles",
    "report_summary",
    "report_system_generated",
    "report_workstation",
    "short_auto_login",
    "short_clear_auto_login",
    "short_set_auto_login",
}

# These workflow captions supplement the shared required catalog. Other locales
# can use the existing fallback unless the caption is in their safety subset.
PRIORITY_UI_KEYS = {
    "active_recipe",
    "alarm_activated",
    "alarm_active",
    "alarm_active_count",
    "alarm_all_types",
    "alarm_applied",
    "alarm_count",
    "alarm_details",
    "alarm_equipment",
    "alarm_file",
    "alarm_history",
    "alarm_inactive",
    "alarm_interlock",
    "alarm_last_known",
    "alarm_lost",
    "alarm_monitor",
    "alarm_no_matches",
    "alarm_observed_active",
    "alarm_observed_recovered",
    "alarm_off",
    "alarm_online",
    "alarm_preview",
    "alarm_preview_failed",
    "alarm_preview_refresh",
    "alarm_preview_waiting",
    "alarm_recovered",
    "alarm_signal",
    "alarm_signals",
    "alarm_software",
    "alarm_stale_values",
    "alarm_startup",
    "alarm_type",
    "alarm_unavailable",
    "alarm_waiting",
    "alarm_word_tip",
    "archive_folder",
    "archive_save_images",
    "audit_after",
    "audit_before",
    "audit_cancelled",
    "audit_denied",
    "audit_details",
    "audit_failed",
    "audit_inspection",
    "audit_no_matches",
    "audit_requested",
    "audit_required",
    "audit_stale",
    "audit_success",
    "audit_time",
    "audit_trail",
    "audit_uncertain",
    "audit_unresolved",
    "audit_warning",
    "batch.information",
    "batch_continue",
    "batch_create",
    "batch_current",
    "batch_duration",
    "batch_duration_help",
    "batch_end",
    "batch_end_confirm",
    "batch_expired",
    "batch_info_failed",
    "batch_input",
    "batch_keep_open",
    "batch_no_groups",
    "batch_notes",
    "batch_pending",
    "batch_product",
    "batch_recent_end",
    "batch_run",
    "batch_select",
    "batch_specification",
    "batch_start_account",
    "batch_uncertain",
    "clear_statistics_confirm",
    "comm_holding_registers",
    "comm_modbus_exception",
    "comm_modbus_hint",
    "comm_modbus_protocol_error",
    "comm_modbus_transport_error",
    "comm_watch_read_failed",
    "comm_watch_stale",
    "comm_write_not_sent",
    "comm_write_rejected",
    "configuration_confirmation_incomplete",
    "configure_first",
    "customer_alarm",
    "customer_audit",
    "customer_audit_batch",
    "customer_batch",
    "customer_defects",
    "customer_deferred",
    "customer_detection_only",
    "customer_entire_batch",
    "customer_positions",
    "customer_select_batch",
    "export_basis",
    "export_detections",
    "export_format",
    "export_language",
    "export_run",
    "export_value",
    "fault_reset_available",
    "history_archived_image",
    "history_clock",
    "history_image_link_warning",
    "history_image_missing",
    "history_image_open_failed",
    "history_image_size_mismatch",
    "history_no_image",
    "history_open_image",
    "length_numeric_entry",
    "length_sample",
    "length_sample_missing",
    "model_action_error",
    "model_delete",
    "model_delete_question",
    "model_import",
    "model_module",
    "model_name",
    "model_rename",
    "next_page",
    "permission_engineer",
    "permission_operator",
    "pilot_batches",
    "pilot_batches_help",
    "pilot_channels_help",
    "pilot_raw_cycles",
    "plc_base_address",
    "plc_commit_observation",
    "plc_group_hint",
    "plc_merge_count",
    "plc_merge_count_tip",
    "plc_merge_unsupported",
    "plc_modbus_base_hint",
    "plc_modbus_base_required",
    "plc_mode",
    "plc_mode_four_cycle",
    "plc_mode_per_cycle",
    "plc_mode_single_cycle",
    "plc_reporting_write_protected",
    "plc_status_write_protected",
    "preview_ambiguous_body",
    "preview_scope_tip",
    "previous_page",
    "process_apply_failed",
    "process_apply_required",
    "process_applying",
    "process_applying_short",
    "process_close_comp_short",
    "process_close_compensation",
    "process_close_phase",
    "process_compensation",
    "process_drop",
    "process_drop_short",
    "process_extend",
    "process_extend_short",
    "process_lift",
    "process_lift_short",
    "process_open_comp_short",
    "process_open_compensation",
    "process_open_phase",
    "process_override_count",
    "process_parameters",
    "process_phase",
    "process_photo",
    "process_plc_expand",
    "process_plc_hold",
    "process_plc_loading",
    "process_plc_unavailable",
    "process_plc_unmapped",
    "process_plc_values",
    "process_reject",
    "process_rotation_short",
    "process_rotation_speed",
    "process_running_short",
    "process_running_speed",
    "process_set_count",
    "process_speed",
    "process_title",
    "process_unverified_short",
    "process_value_hint",
    "process_view_plc",
    "recipe_delete_protected",
    "recipe_dirty_question",
    "recipe_preparing_short",
    "recipe_product_code",
    "recipe_reprepare",
    "recipe_reprepare_hint",
    "records_after_gap",
    "records_all_accounts",
    "records_all_batches",
    "records_audit_scope",
    "records_bit",
    "records_config_error",
    "records_config_io",
    "records_create_alarm_file",
    "records_cycle_failed",
    "records_date_retained",
    "records_date_unset",
    "records_deadline",
    "records_find_account",
    "records_find_batch",
    "records_from",
    "records_invalid_date",
    "records_layout",
    "records_missing_frame",
    "records_model_defect",
    "records_no_batch",
    "records_occupancy",
    "records_run",
    "records_seen_active",
    "records_seen_clear",
    "records_software",
    "records_sources",
    "records_success",
    "records_to",
    "records_unassigned",
    "records_uncertain",
    "records_unresolved",
    "report_cancelled",
    "report_export",
    "report_failed",
    "report_inspected",
    "report_interrupted",
    "report_no_batches",
    "report_pass_rate",
    "report_saved",
    "report_started",
    "report_stopped",
    "report_writing",
    "run_statistics_unavailable",
    "run_uncertain",
    "run_uncertain_totals",
    "select_saved_recipe",
    "service_error",
    "service_online",
    "service_partial",
    "service_waiting",
    "source_save_first",
    "start_vision_dirty",
    "status_alarm_address",
    "status_enable_tip",
    "status_fault",
    "status_fault_tip",
    "status_heartbeat",
    "status_heartbeat_tip",
    "status_running",
    "status_running_tip",
    "status_service",
    "status_service_tip",
    "vision_preview_frame_tip",
    "vision_preview_start",
    "vision_preview_stop",
    "vision_sources_layout_incomplete",
    "vision_sources_only_saved",
    "web_apply_disabled",
    "web_boundary",
    "web_restart_restored",
    *base_resources.AUDIT_ACTIONS,
}

ADDITIONAL_SAFETY_KEYS = {
    "comm_watch_read_failed",
    "comm_watch_stale",
    "comm_write_not_sent",
    "comm_write_rejected",
    "fault_reset_available",
    "permission_engineer",
    "permission_operator",
    "plc_reporting_write_protected",
    "plc_status_write_protected",
    "process_apply_failed",
    "process_apply_required",
    "start_vision_dirty",
}
ADDITIONAL_SAFETY_LANGUAGES = ("uk", "pt-PT", "ms")

# Operational text promoted from optional fallback. The other maintained
# workflows keep their existing coverage requirements.
OPERATIONAL_UI_KEYS = {
    "alarm_active",
    "alarm_active_count",
    "alarm_applied",
    "alarm_count",
    "alarm_details",
    "alarm_inactive",
    "alarm_off",
    "alarm_online",
    "alarm_preview",
    "alarm_preview_failed",
    "alarm_preview_refresh",
    "alarm_preview_waiting",
    "alarm_signal",
    "alarm_signals",
    "alarm_stale_values",
    "alarm_unavailable",
    "alarm_waiting",
    "alarm_word_tip",
    "clear_statistics_confirm",
    "comm_holding_registers",
    "comm_modbus_exception",
    "comm_modbus_hint",
    "comm_modbus_protocol_error",
    "comm_modbus_transport_error",
    "comm_watch_read_failed",
    "comm_watch_stale",
    "comm_write_not_sent",
    "comm_write_rejected",
    "configuration_confirmation_incomplete",
    "configure_first",
    "fault_reset_available",
    "permission_engineer",
    "permission_operator",
    "pilot_batches",
    "pilot_batches_help",
    "pilot_channels_help",
    "pilot_raw_cycles",
    "plc_base_address",
    "plc_commit_observation",
    "plc_group_hint",
    "plc_merge_count",
    "plc_merge_count_tip",
    "plc_merge_unsupported",
    "plc_modbus_base_hint",
    "plc_modbus_base_required",
    "plc_mode",
    "plc_mode_four_cycle",
    "plc_mode_per_cycle",
    "plc_mode_single_cycle",
    "plc_reporting_write_protected",
    "plc_status_write_protected",
    "service_error",
    "service_online",
    "service_partial",
    "service_waiting",
    "source_save_first",
    "start_vision_dirty",
    "status_alarm_address",
    "status_enable_tip",
    "status_fault",
    "status_fault_tip",
    "status_heartbeat",
    "status_heartbeat_tip",
    "status_running",
    "status_running_tip",
    "status_service",
    "status_service_tip",
    "vision_preview_frame_tip",
    "vision_preview_start",
    "vision_preview_stop",
    "vision_sources_layout_incomplete",
    "vision_sources_only_saved",
    "web_apply_disabled",
    "web_boundary",
    "web_restart_restored",
}

# Retain truthful English for these specific technical explanations until their
# translation can be reviewed confidently. This is not a whole-locale exemption.
OPERATIONAL_REVIEW_KEYS = {
    "pilot_channels_help",
    "plc_commit_observation",
    "plc_group_hint",
    "plc_modbus_base_hint",
    "status_running_tip",
}
OPERATIONAL_REVIEW_BY_LANGUAGE: dict[Language, set[str]] = {
    "am": OPERATIONAL_REVIEW_KEYS,
    "ha": OPERATIONAL_REVIEW_KEYS,
    "km": OPERATIONAL_REVIEW_KEYS,
    "mg": OPERATIONAL_REVIEW_KEYS,
    "si": OPERATIONAL_REVIEW_KEYS,
    "ti": OPERATIONAL_REVIEW_KEYS,
    "tk": {"pilot_channels_help", "plc_commit_observation", "plc_group_hint"},
    "zu": OPERATIONAL_REVIEW_KEYS,
}


@pytest.mark.parametrize("language", LANGUAGES)
def test_operational_coverage_and_specific_review_fallback(language: Language) -> None:
    assert len(OPERATIONAL_UI_KEYS) == 75
    assert OPERATIONAL_UI_KEYS <= PRIORITY_UI_KEYS
    maintained = (
        STRINGS.keys()
        - ENGLISH_FALLBACK_KEYS
        - TECHNICAL_TOKEN_KEYS
        - OPTIONAL_NATIVE_KEYS
    )
    assert len(maintained) == 737
    assert OPERATIONAL_UI_KEYS <= maintained
    if language in ("zh", "en"):
        return
    review = OPERATIONAL_REVIEW_BY_LANGUAGE.get(language, set())
    assert OPERATIONAL_UI_KEYS - EXTRA_STRINGS[language].keys() == review
    for key in review:
        assert text(key, language) == STRINGS[key][1]


@pytest.mark.parametrize("language", LANGUAGES)
def test_operational_translations_preserve_protocol_and_outcome_boundaries(
    language: Language,
) -> None:
    for key, tokens in (
        ("plc_group_hint", ("Basler", "FINS/TCP", "W49", "W149")),
        ("plc_commit_observation", ("PLC",)),
        ("comm_local_write", ("PLC",)),
        ("status_running_tip", ("WR", "0", "1")),
        ("status_fault_tip", ("FAULT", "1")),
        ("status_heartbeat_tip", ("16",)),
        ("plc_merge_count_tip", ("0", "1", "4", "PLC")),
        ("alarm_word_tip", ("WR", "W0", "W511")),
        ("comm_modbus_hint", ("Modbus TCP", "255")),
        ("plc_modbus_base_hint", ("HR", "0", "65532", "FINS")),
    ):
        assert all(token in text(key, language) for token in tokens), (language, key)
    for keys in (
        ("plc_mode_per_cycle", "plc_mode_single_cycle", "plc_mode_four_cycle"),
        ("comm_write_not_sent", "comm_write_rejected", "comm_write_unknown"),
        ("comm_watch_read_failed", "comm_watch_stale"),
        ("alarm_active", "alarm_inactive", "alarm_waiting", "alarm_unavailable"),
        ("service_online", "service_waiting", "service_error", "service_partial"),
        ("ng", "error", "FAULT"),
        ("batch_input", "export_run", "short_cycles", "pilot_batches"),
        ("vision_sources_layout_incomplete", "vision_sources_only_saved"),
        ("vision_preview_start", "vision_preview_stop"),
    ):
        assert len({text(key, language) for key in keys}) == len(keys), (language, keys)
    assert "17" in text("pilot_raw_cycles", language).format(count=17)


def test_operational_regional_spelling_and_meaningful_joiners() -> None:
    assert "câmaras" in text("source_save_first", "pt-PT")
    assert "Sačuvajte" in text("configure_first", "sr-Latn")
    assert "yoxlama" in text("pilot_batches_help", "az-Latn")
    assert "\u200d" in text("comm_write_rejected", "ne")
    for language in ("sr-Latn", "az-Latn", "uz"):
        assert not any(
            "\u0400" <= char <= "\u04ff"
            for key in OPERATIONAL_UI_KEYS
            for char in text(key, language)
        )


RECIPE_WORKFLOW_KEYS = {
    "active_recipe",
    "archive_folder",
    "archive_save_images",
    "length_numeric_entry",
    "length_sample",
    "length_sample_missing",
    "model_action_error",
    "model_delete",
    "model_delete_question",
    "model_import",
    "model_module",
    "model_name",
    "model_rename",
    "preview_ambiguous_body",
    "preview_scope_tip",
    "process_apply_failed",
    "process_apply_required",
    "process_applying",
    "process_applying_short",
    "process_close_comp_short",
    "process_close_compensation",
    "process_close_phase",
    "process_compensation",
    "process_drop",
    "process_drop_short",
    "process_extend",
    "process_extend_short",
    "process_lift",
    "process_lift_short",
    "process_open_comp_short",
    "process_open_compensation",
    "process_open_phase",
    "process_override_count",
    "process_parameters",
    "process_phase",
    "process_photo",
    "process_plc_expand",
    "process_plc_hold",
    "process_plc_loading",
    "process_plc_unavailable",
    "process_plc_unmapped",
    "process_plc_values",
    "process_reject",
    "process_rotation_short",
    "process_rotation_speed",
    "process_running_short",
    "process_running_speed",
    "process_set_count",
    "process_speed",
    "process_title",
    "process_unverified_short",
    "process_value_hint",
    "process_view_plc",
    "recipe_delete_protected",
    "recipe_dirty_question",
    "recipe_preparing_short",
    "recipe_product_code",
    "recipe_reprepare",
    "recipe_reprepare_hint",
    "select_saved_recipe",
}

# Exact technical wording still requiring confident native-language review.
RECIPE_REVIEW_BY_LANGUAGE: dict[Language, set[str]] = {
    "am": {
        "preview_ambiguous_body",
        "preview_scope_tip",
        "process_value_hint",
    },
    "ha": {
        "preview_ambiguous_body",
        "preview_scope_tip",
        "process_value_hint",
    },
    "km": {
        "preview_ambiguous_body",
        "preview_scope_tip",
        "process_value_hint",
    },
    "mg": {
        "preview_ambiguous_body",
        "preview_scope_tip",
        "process_value_hint",
    },
    "si": {
        "preview_ambiguous_body",
        "preview_scope_tip",
        "process_close_comp_short",
        "process_close_compensation",
        "process_close_phase",
        "process_compensation",
        "process_open_comp_short",
        "process_open_compensation",
        "process_open_phase",
        "process_phase",
        "process_value_hint",
    },
    "ti": {
        "preview_ambiguous_body",
        "preview_scope_tip",
        "process_value_hint",
    },
    "tk": {
        "preview_ambiguous_body",
        "preview_scope_tip",
        "process_value_hint",
    },
    "zu": {
        "preview_ambiguous_body",
        "preview_scope_tip",
        "process_value_hint",
    },
}


RECORDS_WORKFLOW_KEYS = {
    "account.change_password",
    "account.create",
    "account.enabled",
    "account.reset_password",
    "account.role",
    "alarm_activated",
    "alarm_all_types",
    "alarm_definitions.create",
    "alarm_equipment",
    "alarm_file",
    "alarm_history",
    "alarm_interlock",
    "alarm_last_known",
    "alarm_lost",
    "alarm_monitor",
    "alarm_no_matches",
    "alarm_observed_active",
    "alarm_observed_recovered",
    "alarm_recovered",
    "alarm_software",
    "alarm_startup",
    "alarm_type",
    "audit_after",
    "audit_before",
    "audit_cancelled",
    "audit_denied",
    "audit_details",
    "audit_failed",
    "audit_inspection",
    "audit_no_matches",
    "audit_requested",
    "audit_required",
    "audit_stale",
    "audit_success",
    "audit_time",
    "audit_trail",
    "audit_uncertain",
    "audit_unresolved",
    "audit_warning",
    "batch.end",
    "batch.information",
    "batch.select",
    "batch_continue",
    "batch_create",
    "batch_current",
    "batch_duration",
    "batch_duration_help",
    "batch_end",
    "batch_end_confirm",
    "batch_expired",
    "batch_info_failed",
    "batch_input",
    "batch_keep_open",
    "batch_no_groups",
    "batch_notes",
    "batch_pending",
    "batch_product",
    "batch_recent_end",
    "batch_run",
    "batch_select",
    "batch_specification",
    "batch_start_account",
    "batch_uncertain",
    "communication.save",
    "communication.web_power",
    "customer_alarm",
    "customer_audit",
    "customer_audit_batch",
    "customer_batch",
    "customer_defects",
    "customer_deferred",
    "customer_detection_only",
    "customer_entire_batch",
    "customer_positions",
    "customer_select_batch",
    "export_basis",
    "export_detections",
    "export_format",
    "export_language",
    "export_run",
    "export_value",
    "history.storage",
    "history_archived_image",
    "history_clock",
    "history_image_link_warning",
    "history_image_missing",
    "history_image_open_failed",
    "history_image_size_mismatch",
    "history_no_image",
    "history_open_image",
    "model.cache",
    "model.delete",
    "model.import",
    "model.publish",
    "model.rename",
    "model.stage",
    "next_page",
    "plc.write",
    "preferences.save",
    "previous_page",
    "production.clear_statistics",
    "production.recipe",
    "production.start",
    "production.stop",
    "recipe.delete",
    "recipe.process_apply",
    "recipe.process_value",
    "recipe.save",
    "records_after_gap",
    "records_all_accounts",
    "records_all_batches",
    "records_audit_scope",
    "records_bit",
    "records_config_error",
    "records_config_io",
    "records_create_alarm_file",
    "records_cycle_failed",
    "records_date_retained",
    "records_date_unset",
    "records_deadline",
    "records_find_account",
    "records_find_batch",
    "records_from",
    "records_invalid_date",
    "records_layout",
    "records_missing_frame",
    "records_model_defect",
    "records_no_batch",
    "records_occupancy",
    "records_run",
    "records_seen_active",
    "records_seen_clear",
    "records_software",
    "records_sources",
    "records_success",
    "records_to",
    "records_unassigned",
    "records_uncertain",
    "records_unresolved",
    "report_cancelled",
    "report_export",
    "report_failed",
    "report_inspected",
    "report_interrupted",
    "report_no_batches",
    "report_pass_rate",
    "report_saved",
    "report_started",
    "report_stopped",
    "report_writing",
    "run_statistics_unavailable",
    "run_uncertain",
    "run_uncertain_totals",
    "security.admission",
    "security.storage",
    "session.login",
    "session.logout",
    "vision.layout",
    "vision.sources",
}

# Keep uncertain timing, attribution and completeness explanations in English.
RECORDS_REVIEW_BY_LANGUAGE: dict[Language, set[str]] = {
    "am": {
        "batch_duration_help",
        "customer_detection_only",
        "history_clock",
        "records_audit_scope",
    },
    "ha": {
        "batch_duration_help",
        "customer_detection_only",
        "history_clock",
        "records_audit_scope",
    },
    "km": {
        "batch_duration_help",
        "customer_detection_only",
        "history_clock",
        "records_audit_scope",
    },
    "mg": {
        "batch_duration_help",
        "customer_detection_only",
        "history_clock",
        "records_audit_scope",
    },
    "si": {
        "batch_duration_help",
        "customer_detection_only",
        "history_clock",
        "records_audit_scope",
    },
    "ti": {
        "batch_duration_help",
        "customer_detection_only",
        "history_clock",
        "records_audit_scope",
    },
    "tk": {
        "batch_duration_help",
        "customer_detection_only",
        "history_clock",
        "records_audit_scope",
    },
    "zu": {
        "batch_duration_help",
        "customer_detection_only",
        "history_clock",
        "records_audit_scope",
    },
}


@pytest.mark.parametrize("language", LANGUAGES)
def test_records_coverage_completes_maintained_interface_with_exact_exceptions(
    language: Language,
) -> None:
    assert len(RECORDS_WORKFLOW_KEYS) == 159
    assert RECORDS_WORKFLOW_KEYS <= PRIORITY_UI_KEYS
    assert not RECORDS_WORKFLOW_KEYS & (OPERATIONAL_UI_KEYS | RECIPE_WORKFLOW_KEYS)
    maintained = (
        STRINGS.keys()
        - ENGLISH_FALLBACK_KEYS
        - TECHNICAL_TOKEN_KEYS
        - OPTIONAL_NATIVE_KEYS
    )
    assert len(maintained) == 737
    if language in ("zh", "en"):
        return
    review = RECORDS_REVIEW_BY_LANGUAGE.get(language, set())
    assert RECORDS_WORKFLOW_KEYS - EXTRA_STRINGS[language].keys() == review
    all_review = (
        review
        | OPERATIONAL_REVIEW_BY_LANGUAGE.get(language, set())
        | RECIPE_REVIEW_BY_LANGUAGE.get(language, set())
    )
    assert maintained - EXTRA_STRINGS[language].keys() == all_review
    for key in review:
        assert text(key, language) == STRINGS[key][1]


@pytest.mark.parametrize("language", LANGUAGES)
def test_recipe_workflow_coverage_and_specific_review_fallback(
    language: Language,
) -> None:
    assert len(RECIPE_WORKFLOW_KEYS) == 60
    assert RECIPE_WORKFLOW_KEYS <= PRIORITY_UI_KEYS
    assert not RECIPE_WORKFLOW_KEYS & OPERATIONAL_UI_KEYS
    maintained = (
        STRINGS.keys()
        - ENGLISH_FALLBACK_KEYS
        - TECHNICAL_TOKEN_KEYS
        - OPTIONAL_NATIVE_KEYS
    )
    assert len(maintained) == 737 and RECIPE_WORKFLOW_KEYS <= maintained
    if language in ("zh", "en"):
        return
    review = RECIPE_REVIEW_BY_LANGUAGE.get(language, set())
    assert RECIPE_WORKFLOW_KEYS - EXTRA_STRINGS[language].keys() == review
    for key in review:
        assert text(key, language) == STRINGS[key][1]


@pytest.mark.parametrize("language", LANGUAGES)
def test_recipe_terms_keep_preview_preparation_and_application_distinct(
    language: Language,
) -> None:
    for keys in (
        (
            "active_recipe",
            "recipe_preparing_short",
            "process_applying_short",
            "process_unverified_short",
        ),
        ("model_import", "model_delete", "model_rename"),
        ("process_plc_loading", "process_plc_values", "process_plc_unavailable"),
        ("process_apply_required", "process_apply_failed"),
    ):
        assert len({text(key, language) for key in keys}) == len(keys)
    for key in (
        "process_plc_hold",
        "process_plc_expand",
        "process_plc_loading",
        "process_plc_values",
        "process_plc_unavailable",
        "process_view_plc",
    ):
        assert "PLC" in text(key, language)
    if language != "zh":
        assert "EMPTY" in text("preview_scope_tip", language)
    hint = text("process_value_hint", language).format(
        name="QA_{CAM1}_用户", minimum=-20, maximum=150, addresses="D102–D103"
    )
    assert all(
        value in hint for value in ("QA_{CAM1}_用户", "-20", "150", "D102–D103", "-")
    )
    for key in ("process_set_count", "process_override_count"):
        assert "17" in text(key, language).format(count=17)


def test_recipe_regional_terms_and_latin_script_identities() -> None:
    assert "guardada" in text("select_saved_recipe", "pt-PT")
    assert "keš" in text("model_delete_question", "sr-Latn")
    assert "yoxlanılmayıb" in text("process_unverified_short", "az-Latn")
    for language in ("sr-Latn", "az-Latn", "uz"):
        assert not any(
            "\u0400" <= char <= "\u04ff"
            for key in RECIPE_WORKFLOW_KEYS
            for char in text(key, language)
        )


@pytest.mark.parametrize("language", LANGUAGES)
def test_identity_ui_has_complete_native_coverage(language: Language) -> None:
    assert IDENTITY_UI_KEYS == {key for key in STRINGS if key.startswith("identity_")}
    assert not IDENTITY_UI_KEYS & (ENGLISH_FALLBACK_KEYS | PRIORITY_UI_KEYS)
    values = (
        {key: STRINGS[key][0 if language == "zh" else 1] for key in IDENTITY_UI_KEYS}
        if language in ("zh", "en")
        else EXTRA_STRINGS[language]
    )
    assert IDENTITY_UI_KEYS <= values.keys()
    for key in IDENTITY_UI_KEYS:
        value = values[key]
        assert value and value == value.strip() and "\ufffd" not in value
        expected = [
            (field, spec, conversion)
            for _, field, spec, conversion in Formatter().parse(STRINGS[key][1])
            if field is not None
        ]
        actual = [
            (field, spec, conversion)
            for _, field, spec, conversion in Formatter().parse(value)
            if field is not None
        ]
        assert actual == expected, (language, key)
        if expected:
            username = "QA_{CAM01}_用户"
            assert username in value.format(username=username)
    for keys in (
        ("identity_login", "identity_logout", "identity_switch"),
        ("identity_change", "identity_reset", "identity_change_role"),
        ("identity_enable", "identity_disable"),
        ("identity_enabled", "identity_disabled"),
        (
            "identity_guest",
            "identity_operator",
            "identity_engineer",
            "identity_administrator",
        ),
    ):
        assert len({values[key] for key in keys}) == len(keys), (language, keys)
    if language not in ("zh", "en"):
        for key in (
            "identity_login",
            "identity_logout",
            "identity_manage",
            "identity_invalid",
            "identity_storage",
        ):
            assert values[key] != STRINGS[key][1], (language, key)


def test_identity_regional_catalogs_preserve_independent_terms_and_scripts() -> None:
    assert text("identity_username", "pt-BR") == "Nome de usuário"
    assert text("identity_username", "pt-PT") == "Nome de utilizador"
    assert text("identity_password", "pt-BR") == "Senha"
    assert text("identity_password", "pt-PT") == "Palavra-passe"
    assert text("identity_login", "zh-TW") == "登入"
    assert text("identity_password", "zh-TW") == "密碼"
    for key in (
        "identity_guest",
        "identity_login",
        "identity_password",
        "identity_administrator",
    ):
        assert text(key, "am") != text(key, "ti")
    for language in ("sr-Latn", "az-Latn", "uz"):
        assert not any(
            "\u0400" <= c <= "\u04ff"
            for key in IDENTITY_UI_KEYS
            for c in text(key, language)
        )


# These tokens may inherit canonical English; native overrides remain explicit.
TECHNICAL_TOKEN_KEYS = {
    "nav_plc",
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
    "history_segment_observed",
    "module_id",
    "recipe_id",
    "reference",
    "roi_applied",
    "roi_recipe_save",
    "save_machine_first",
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
        | ENGLISH_FALLBACK_KEYS
        | PRIORITY_UI_KEYS
        | TECHNICAL_TOKEN_KEYS
        <= STRINGS.keys()
    )
    assert not ENGLISH_FALLBACK_KEYS & PRIORITY_UI_KEYS
    assert ADDITIONAL_SAFETY_KEYS <= PRIORITY_UI_KEYS
    assert (
        set(PRIORITY_UI_LANGUAGES) | set(ADDITIONAL_SAFETY_LANGUAGES)
        <= EXTRA_STRINGS.keys()
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
            - ENGLISH_FALLBACK_KEYS
            - PRIORITY_UI_KEYS
            - TECHNICAL_TOKEN_KEYS
            - OPTIONAL_NATIVE_KEYS_BY_LANGUAGE.get(language, set())
        )
        if language in PRIORITY_UI_LANGUAGES:
            required |= PRIORITY_UI_KEYS
        elif language in ADDITIONAL_SAFETY_LANGUAGES:
            required |= ADDITIONAL_SAFETY_KEYS
        required |= OPERATIONAL_UI_KEYS - OPERATIONAL_REVIEW_BY_LANGUAGE.get(
            language, set()
        )
        required |= RECIPE_WORKFLOW_KEYS - RECIPE_REVIEW_BY_LANGUAGE.get(
            language, set()
        )
        required |= RECORDS_WORKFLOW_KEYS - RECORDS_REVIEW_BY_LANGUAGE.get(
            language, set()
        )
        assert required <= EXTRA_STRINGS[language].keys(), language
        assert not ENGLISH_FALLBACK_KEYS & EXTRA_STRINGS[language].keys(), language


@pytest.mark.parametrize("language", LANGUAGES)
def test_setup_guidance_and_recorded_recipe_recovery_are_distinct(
    language: Language,
) -> None:
    setup = text("recipe_setup_required", language)
    recovery = text("startup_recipe_identity", language)
    assert len({setup, recovery, text("configure_first", language)}) == 3
    assert "app.json" in recovery and "app.json" not in setup
    if language not in ("zh", "en"):
        assert "recipe_setup_required" in EXTRA_STRINGS[language]
        assert setup != text("recipe_setup_required", "en")


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
    if "preview_scope_tip" not in EXTRA_STRINGS[language]:
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


def test_localized_operational_text_preserves_codes_and_distinctions() -> None:
    for language in PRIORITY_UI_LANGUAGES:
        for key, tokens in (
            ("plc_group_hint", ("Basler", "FINS/TCP", "W49", "W149")),
            ("plc_commit_observation", ("ACK", "PLC")),
            ("comm_local_write", ("ACK", "PLC")),
            ("status_running_tip", ("WR", "0", "1")),
            ("status_fault_tip", ("FAULT", "0", "1")),
            ("history_cycle_disabled", ("DISABLED",)),
            ("history_cycle_unknown", ("UNKNOWN",)),
            ("records_invalid_date", ("YYYY-MM-DD",)),
            ("vision_remove_cam6_question", ("CAM6",)),
        ):
            assert all(token in text(key, language) for token in tokens), (
                language,
                key,
            )
        for keys in (
            ("plc_mode_per_cycle", "plc_mode_single_cycle", "plc_mode_four_cycle"),
            ("batch_input", "export_run", "short_cycles", "pilot_batches"),
            ("comm_write_not_sent", "comm_write_rejected", "comm_write_unknown"),
            ("report_writing", "report_saved", "report_failed", "report_cancelled"),
        ):
            assert len({text(key, language) for key in keys}) == len(keys), language
    preview_presence: tuple[tuple[Language, str], ...] = (
        ("zh-TW", "不執行占位判定"),
        ("de", "ohne Anwesenheitserkennung"),
        ("hi", "उपस्थिति नहीं जाँचता"),
    )
    for language, expected in preview_presence:
        assert expected in text("preview_scope_tip", language)


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
        "save",
        "recipe_discard",
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
    for key in ENGLISH_FALLBACK_KEYS:
        assert key in STRINGS and STRINGS[key][0] and STRINGS[key][1]
        for language in LANGUAGES:
            assert text(key, language) == (
                STRINGS[key][0] if language == "zh" else STRINGS[key][1]
            )


def test_fins_transport_caption_is_neutral_in_every_language() -> None:
    for language in LANGUAGES:
        caption = text("comm_transport_error", language)
        assert "FINS" in caption and "UDP" not in caption, language


def test_record_captions_distinguish_witnessed_transitions_and_audit_outcomes() -> None:
    for language in LANGUAGES:
        for keys in (
            (
                "records_seen_active",
                "records_seen_clear",
                "alarm_activated",
                "alarm_recovered",
            ),
            (
                "records_success",
                "audit_failed",
                "audit_denied",
                "records_uncertain",
                "records_unresolved",
                "audit_cancelled",
                "audit_stale",
            ),
            ("report_writing", "report_saved", "report_cancelled", "report_failed"),
            ("batch_input", "records_run", "history_cycle", "history_group"),
            ("history_none", "history_unavailable", "history_loading"),
            ("history_no_image", "history_image_missing", "history_image_open_failed"),
        ):
            assert len({text(key, language) for key in keys}) == len(keys), language
        assert "Chip · Black spot" in text("records_unassigned", language).format(
            labels="Chip · Black spot"
        )
        assert "LOT-A · 海蓝" in text("customer_entire_batch", language).format(
            batch="LOT-A · 海蓝"
        )
        assert "YYYY-MM-DD" in text("records_invalid_date", language)
        assert "PLC" in text("batch_no_groups", language)


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


def test_navigation_has_native_short_captions_and_one_protocol_identifier() -> None:
    keys = ("nav_inspect", "page_recipe", "nav_plc", "nav_records", "nav_cameras")
    assert tuple(text(key, "zh") for key in keys) == (
        "检测",
        "配方",
        "PLC",
        "记录",
        "相机",
    )
    assert tuple(text(key, "en") for key in keys) == (
        "Inspect",
        "Recipe",
        "PLC",
        "Records",
        "Cameras",
    )
    for language in LANGUAGES:
        captions = tuple(text(key, language) for key in keys)
        assert len(set(captions)) == 5 and captions[2] == "PLC"
        assert all(caption.strip() and "…" not in caption for caption in captions)
        if language not in ("zh", "en"):
            assert set(keys) - {"nav_plc"} <= EXTRA_STRINGS[language].keys()
    assert text("nav_records", "zh-TW") == "紀錄"
    assert text("nav_records", "pt-BR") == "Registros"
    assert text("nav_records", "pt-PT") == "Registos"


@pytest.mark.parametrize("language", tuple(EXTRA_STRINGS))
def test_optional_translation_falls_back_to_english(language: Language) -> None:
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
            "length",
            "ambiguous_body",
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
        ("save", "儲存"),
        ("source_connected", "已連線"),
        ("comm_timeout", "逾時（秒）"),
        ("cache_prepare", "準備 TensorRT 快取"),
        ("plc_reporting", "生產結果回報"),
        ("comm_effective_endpoint", "實際目標"),
        ("batch_select", "設定批號"),
        ("alarm_stale_values", "歷史值 · 非目前讀值"),
    ):
        assert text(key, "zh-TW") == expected
    assert "完成時刻" in text("history_clock", "zh-TW")
    assert "曝光時間" in text("history_clock", "zh-TW")


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
assert len(languages.LANGUAGES) == 64 and 'comm_transport_error' in catalog.STRINGS
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
