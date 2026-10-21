"""UI catalog and logical-pixel visual tokens for the workstation."""

from dataclasses import dataclass
from functools import cache
from typing import Literal

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPalette, QPen, QPixmap
from PySide6.QtWidgets import QPushButton, QStyle, QStyleOptionButton, QWidget

from vimo_demo.application.preferences import LANGUAGES as LANGUAGES
from vimo_demo.application.preferences import Language as Language

from .production_presentation import Indicator
from .translations import EXTRA_STRINGS

type Theme = Literal["dark", "light"]

STRINGS: dict[str, tuple[str, str]] = {
    "vision_remove_cam6": ("移除 CAM6 配置", "Remove CAM6 configuration"),
    "vision_restore_cam6": (
        "从 CAM5 ROI 模板初始化 CAM6；需校准并配置来源",
        "Initialize CAM6 from CAM5 ROI template; calibrate and assign a source",
    ),
    "vision_remove_cam6_question": (
        "移除草稿中的 CAM6 几何和来源配置？保存并应用后生效；不会自动保留备份。",
        "Remove CAM6 geometry and source assignment from this draft? Save/apply makes it effective; no backup is created automatically.",
    ),
    "interface_language": ("界面语言", "Interface language"),
    "theme_light": ("切换浅色主题", "Switch to light theme"),
    "theme_dark": ("切换深色主题", "Switch to dark theme"),
    "nav_diagnostics": ("通信", "Comms"),
    "inspection": ("检测", "Inspection"),
    "tune_size": ("调整尺寸", "Tune Size"),
    "tune_color": ("调整颜色", "Tune Color"),
    "size_check": ("尺寸检测", "Size inspection"),
    "color_check": ("颜色检测", "Color inspection"),
    "size": ("尺寸", "Size"),
    "color": ("颜色", "Color"),
    "target_width": ("目标宽度 (px)", "Target W (px)"),
    "target_height": ("目标高度 (px)", "Target H (px)"),
    "width_tolerance": ("宽度容差", "W tolerance"),
    "height_tolerance": ("高度容差", "H tolerance"),
    "color_sample": ("取色", "Sample"),
    "color_sample_tip": ("从当前画面取色", "Use current image color"),
    "target_color": ("目标颜色", "Target color"),
    "color_tolerance": ("颜色容差", "Color tolerance"),
    "target": ("目标", "Target"),
    "unavailable": ("不可用", "Unavailable"),
    "preview_select": ("点选上／下胶囊查看测量", "Select an upper/lower capsule"),
    "preview_upper": ("上", "upper"),
    "preview_lower": ("下", "lower"),
    "preview_values": (
        "数值来自当前冻结图和配方草稿。",
        "Values use this frozen image and Recipe draft.",
    ),
    "preview_sample_missing": (
        "没有可用的双视图颜色，未更改目标颜色。",
        "No usable two-view color evidence; target unchanged.",
    ),
    "preview_measuring": ("正在测量冻结参考图…", "Measuring frozen reference…"),
    "service_disabled": ("未启用", "Disabled"),
    "service_next_start": ("待启动", "Next Start"),
    "service_ack": ("上次 ACK", "Last ACK"),
    "service_failed": ("报告失败", "Report failed"),
    "service_reporting": ("报告中", "Reporting"),
    "service_off": ("未启用", "Off"),
    "history_all_dates": ("所有日期", "All dates"),
    "history_date": ("完成日期", "Completion date"),
    "history_batch": ("批次分段", "Batch segment"),
    "history_result": ("检测结果", "Result"),
    "history_all_batches": ("所有分段", "All segments"),
    "history_all": ("所有结果", "All results"),
    "history_ok": ("周期 OK", "Cycle OK"),
    "history_ng": ("周期 NG", "Cycle NG"),
    "history_error": ("技术 ERROR", "Technical ERROR"),
    "history_empty": ("含 EMPTY 通道", "Contains EMPTY"),
    "history_unknown": ("含 UNKNOWN 通道", "Contains UNKNOWN"),
    "history_refresh": ("刷新", "Refresh"),
    "history_newer": ("较新", "Newer"),
    "history_older": ("较早", "Older"),
    "history_loading": ("正在读取历史…", "Loading history…"),
    "history_none": ("没有符合条件的记录", "No matching records"),
    "history_unavailable": (
        "历史记录不可用 · 查看提示",
        "History unavailable · see tooltip",
    ),
    "history_segment_observed": (
        "最早留存周期完成时间；并非批次建立时间。",
        "Earliest retained cycle completion; not Batch creation time.",
    ),
    "history_warning": (
        "历史持久化警告 · 未确认保存 {lost} 条 · 已提交 {saved} 条",
        "History warning · {lost} not confirmed · {saved} committed",
    ),
    "history_clock": (
        "本机墙钟完成观察时间；不是相机曝光时间。历史为有界、尽力保存。",
        "Host wall-clock completion observation, not exposure time. History is bounded and best-effort.",
    ),
    "history_time": ("完成时间", "Completed"),
    "history_cycle": ("周期 ID", "Cycle ID"),
    "history_camera": ("相机", "Camera"),
    "history_channel": ("通道", "Channel"),
    "history_judgment": ("检查判断", "Judgment"),
    "history_run": ("运行", "Run"),
    "history_unassigned": ("未分配缺陷标签", "Unassigned defect labels"),
    "history_matrix_key": (
        "实际通道 ID 与业务状态；ERR 为独立技术错误。点击查看原因。",
        "Actual ChannelId and business status; ERR is an independent technical error. Select for reasons.",
    ),
    "history_cycle_error": ("周期 ERR", "Cycle ERR"),
    "history_cycle_disabled": ("周期 DISABLED", "Cycle DISABLED"),
    "history_cycle_unknown": ("周期 UNKNOWN", "Cycle UNKNOWN"),
    "history_select_channel": ("选择通道查看详情", "Select a channel for details"),
    "history_recipe_legacy": (
        "未记录配方名称；显示配方 ID",
        "Recipe name not recorded; showing Recipe ID",
    ),
    "image_archive": ("图像归档", "Image archive"),
    "archive_directory": ("归档目录", "Archive directory"),
    "archive_browse": ("选择归档目录", "Choose archive directory"),
    "archive_open": ("打开归档目录", "Open archive directory"),
    "archive_issue": (
        "图像归档异常 · 请检查目录与磁盘",
        "Image archive issue · check directory and disk",
    ),
    "archive_ok": ("保存 OK 相机图像", "Save OK camera images"),
    "archive_ng": ("保存 NG 相机图像", "Save NG camera images"),
    "archive_error": ("保存 ERROR 相机图像", "Save ERROR camera images"),
    "archive_empty": (
        "保存确认 EMPTY 的相机图像",
        "Save confirmed EMPTY camera images",
    ),
    "configured_hash": ("配置 SHA-256", "Configured SHA-256"),
    "recipe_labels": ("缺陷标签", "Defect labels"),
    "recipe_default_label": ("默认缺陷标签：", "Default defect label:"),
    "recipe_labels_error": (
        "完整模型标签不可用；请检查模型与 SDK，草稿未变。",
        "Complete model labels unavailable; check the model/SDK. Draft unchanged.",
    ),
    "recipe_labels_retained": (
        "保留其他已保存的排除项：",
        "Other saved exclusions retained:",
    ),
    "delete_recipe": ("删除", "Delete"),
    "delete_recipe_question": (
        "删除此配方及其未保存修改？仅删除该配方文件。",
        "Delete this Recipe and its unsaved edits? Only its Recipe file is deleted.",
    ),
    "recipe_delete_protected": (
        "当前活动或已应用配方不可删除。",
        "The active/applied Recipe cannot be deleted.",
    ),
    "recipe_delete_error": (
        "删除失败；请检查配方引用、文件权限和详情。",
        "Delete failed; check Recipe references, file access and details.",
    ),
    "recipe_discard": ("放弃修改", "Discard changes"),
    "recipe_discard_question": (
        "放弃该配方未保存修改，重新读取已保存文件？",
        "Discard this Recipe's unsaved edits and reload the saved file?",
    ),
    "recipe_discard_error": (
        "恢复失败，草稿已保留；请检查已保存文件。",
        "Restore failed; draft kept. Check the saved file.",
    ),
    "production_stop": ("停止", "Stop"),
    "pause_unsupported": (
        "当前采集模式不支持暂停",
        "Pause is unavailable for this acquisition mode",
    ),
    "plc_reporting": ("生产上报", "Production reporting"),
    "plc_enable": ("启用", "Enable"),
    "plc_disable": ("停用", "Disable"),
    "plc_next_start": (
        "仅保存成功的设置用于下次 Start；ACK 不代表机械剔除。",
        "Only saved settings apply at next Start; ACK is not physical rejection.",
    ),
    "comm_diagnostics": ("诊断", "Diagnostics"),
    "comm_discard": ("放弃更改", "Discard changes"),
    "comm_discard_question": (
        "重新加载已保存的通信设置？未保存的更改将丢失。",
        "Reload saved communication settings? Unsaved edits will be lost.",
    ),
    "comm_reload_failed": (
        "重新加载失败；草稿已保留",
        "Reload failed; draft preserved",
    ),
    "comm_endpoint_hint": (
        "请求使用已保存的端点；尚未保存时使用启动默认值。草稿不改变操作目标。",
        "Requests use the saved endpoint, or startup defaults before the first Save. Drafts do not change the operation target.",
    ),
    "comm_effective_endpoint": ("有效目标", "Effective target"),
    "comm_save_settings": ("保存设置", "Save settings"),
    "comm_settings_defaults": ("默认设置 · 尚未保存", "Defaults · not saved"),
    "comm_settings_saved": ("设置已保存", "Settings saved"),
    "comm_settings_dirty": ("设置未保存", "Unsaved settings"),
    "comm_settings_failed": (
        "保存失败，请检查设置或文件权限",
        "Save failed; check settings or file permissions",
    ),
    "startup_communication": (
        "通信设置无效；请在通信页面检查并保存",
        "Invalid communication settings; review and save on Communication",
    ),
    "web_observer": ("Web 观察器", "Web observer"),
    "web_bind": ("绑定 IPv4", "Bind IPv4"),
    "web_refresh": ("刷新间隔", "Refresh interval"),
    "web_live": ("当前服务运行中", "Current service running"),
    "web_failed": ("当前服务不可用", "Current service unavailable"),
    "web_off": ("当前服务未启用", "Current service off"),
    "web_starting": ("正在启动", "Starting"),
    "web_running": ("运行中", "Running"),
    "web_stopping": ("正在停止", "Stopping"),
    "web_stopped": ("已停止", "Stopped"),
    "web_turn_on": ("开启", "Turn on"),
    "web_turn_off": ("关闭", "Turn off"),
    "web_apply_changes": ("应用更改", "Apply changes"),
    "web_power_tip": (
        "保存 Web 启用偏好并使用已保存的网络设置；其他草稿保持不变。",
        "Saves only Web enablement and uses saved network settings; other drafts stay unchanged.",
    ),
    "web_power_failed": (
        "Web 操作失败，请查看服务详情",
        "Web operation failed; see service details",
    ),
    "web_saved_on": ("已保存：开启", "Saved: on"),
    "web_saved_off": ("已保存：关闭", "Saved: off"),
    "web_choose_address": (
        "选择本机 IPv4（也可手动输入）",
        "Choose local IPv4 (or enter manually)",
    ),
    "web_open": ("打开运行地址", "Open running URL"),
    "web_addresses": ("刷新本机 IPv4", "Refresh local IPv4 addresses"),
    "web_save_first": ("请先保存或放弃未保存的设置", "Save or discard settings first"),
    "web_restored": (
        "已恢复先前运行设置；新设置未能启动",
        "Previous running settings restored; new settings failed to start",
    ),
    "web_boundary": (
        "只读且无身份验证；仅绑定一个本机 IPv4，请仅用于可信网络。",
        "Read-only and unauthenticated; binds one local IPv4. Trusted networks only.",
    ),
    "plc_disabled": ("自动报告已关闭", "Reporting disabled"),
    "plc_enabled": ("已启用 · 待启动", "Enabled · next Start"),
    "plc_starting": ("已启用 · 启动中", "Enabled · starting"),
    "plc_stopping": ("正在结束报告", "Stopping reporting"),
    "plc_acknowledged": ("最近 ACK", "Last ACK"),
    "plc_active": ("报告会话运行中", "Reporting session active"),
    "plc_report_failed": ("报告失败", "Reporting failed"),
    "plc_success": ("成功", "Successful"),
    "plc_failed": ("失败", "Failed"),
    "plc_last_error": ("最近 ERR", "Last ERR"),
    "plc_ack_hint": (
        "ACK 仅确认 FINS 写响应，不代表 PLC 已消费结果或持续在线。计数属于标注的报告会话。",
        "ACK confirms the FINS write response only, not PLC consumption or persistent connectivity. Counts belong to the indicated reporting generation.",
    ),
    "plc_reserved_write": (
        "自动报告运行期间，禁止手动写入 W49–W52。",
        "Manual W49–W52 writes are blocked while automatic reporting owns the session.",
    ),
    "preferences_error": (
        "启动偏好未保存；请修复 app.json 后重启。",
        "Application preferences not saved; repair app.json and restart.",
    ),
    "recipe_error": (
        "配方操作失败；请查看详情。",
        "Recipe operation failed; see details.",
    ),
    "recipe_validation_error": (
        "配方配置无效；请检查名称和检测设置。",
        "Recipe configuration invalid; check its name and inspection settings.",
    ),
    "recipe_save_error": (
        "配方保存失败；请检查文件访问权限。",
        "Recipe save failed; check file access.",
    ),
    "recipe_prepare_error": (
        "配方准备/应用失败；请检查模型和配置。",
        "Recipe preparation/apply failed; check the model and configuration.",
    ),
    "model_prepare_error": (
        "模型文件不可用或校验失败；请检查模型文件。",
        "Model artifact unavailable or verification failed; check the model file.",
    ),
    "cache_prepare": ("准备 TensorRT 缓存", "Prepare TensorRT cache"),
    "cache_unknown": (
        "尚未在本次应用会话中准备",
        "Not prepared in this application session",
    ),
    "cache_preparing": (
        "正在初始化模型；首次构建可能需要数分钟，不能强制取消",
        "Initializing model; first build may take minutes and cannot be force-cancelled",
    ),
    "cache_initialized": (
        "模型初始化及清理成功；未执行样本推理。不是永久缓存有效性证明",
        "Initialization and cleanup succeeded; no sample inference. Not permanent cache validation",
    ),
    "cache_sampled": (
        "模型初始化、样本推理及清理成功；不是永久缓存有效性证明",
        "Initialization, sample inference and cleanup succeeded; not permanent cache validation",
    ),
    "cache_failed": ("模型缓存准备失败", "Model cache preparation failed"),
    "vision_error": (
        "相机配置操作失败；请查看详情。",
        "Camera configuration operation failed; see details.",
    ),
    "folder_reference_error": (
        "图像不可用；请检查路径和图片。",
        "Image unavailable; check the path and image.",
    ),
    "source_validation_error": (
        "图片源验证失败；请检查相机文件夹和图片。",
        "Folder source validation failed; check camera folders and images.",
    ),
    "vision_validation_error": (
        "相机配置验证失败；请检查来源和 ROI。",
        "Camera configuration validation failed; check sources and ROIs.",
    ),
    "vision_save_error": (
        "相机配置保存失败；请检查文件访问权限。",
        "Camera configuration save failed; check file access.",
    ),
    "roi_geometry_error": (
        "ROI 几何无效；请检查坐标和图像范围。",
        "Invalid ROI geometry; check coordinates and image bounds.",
    ),
    "startup_preferences": (
        "启动设置不可用；请修复 app.json 后重启。",
        "Startup settings unavailable; repair app.json and restart.",
    ),
    "startup_language": (
        "语言设置无效；本次使用中文，请修复 app.json。",
        "Invalid language setting; using Chinese for this session. Repair app.json.",
    ),
    "startup_models": (
        "模型定义不可用；请检查 data/models。",
        "Model definitions unavailable; check data/models.",
    ),
    "startup_recipes": (
        "配方定义不可用；请检查 data/recipes。",
        "Recipe definitions unavailable; check data/recipes.",
    ),
    "startup_model_identity": (
        "默认模型 ID 未设置或不存在；请检查 app.json。",
        "Default Model ID missing or unknown; check app.json.",
    ),
    "startup_recipe_identity": (
        "已应用配方 ID 未设置或不存在；请选择有效配方或修复 app.json。",
        "Last-applied Recipe missing or unknown; select a valid Recipe or repair app.json.",
    ),
    "startup_machine": (
        "机器布局不可用；请修复 machine.json 后重启。",
        "Machine layout unavailable; repair machine.json and restart.",
    ),
    "startup_sources": (
        "相机源配置不可用；请修复 camera_sources.json 后重启。",
        "Camera-source configuration unavailable; repair camera_sources.json and restart.",
    ),
    "startup_artifact": (
        "模型文件不可用或校验失败；请检查模型文件。",
        "Model artifact unavailable or verification failed; check the model file.",
    ),
    "startup_configuration": (
        "检测配置无效；请检查配方和机器配置。",
        "Inspection configuration invalid; check Recipe and machine configuration.",
    ),
    "startup_source_unavailable": (
        "图片源不可用；请在视觉页检查文件夹。",
        "Folder source unavailable; check folders on Vision.",
    ),
    "reference_folder": ("文件夹", "Folder"),
    "reference_runtime": ("运行帧", "Runtime"),
    "reference_runtime_tip": (
        "冻结当前相机同源的最新运行帧",
        "Freeze the latest runtime frame from this camera/source",
    ),
    "reference_unavailable": (
        "当前相机没有同源运行帧",
        "No eligible runtime frame for this camera/source",
    ),
    "reference_previous": ("上一张图像", "Previous image"),
    "reference_next": ("下一张图像", "Next image"),
    "vision_import": ("导入图像", "Import image"),
    "vision_imported": ("已导入", "Imported"),
    "vision_captured": ("已采集", "Captured"),
    "vision_runtime": ("使用运行图像", "Use runtime image"),
    "vision_capture": ("采集图像", "Capture image"),
    "vision_capture_waiting": (
        "等待所选相机的下一帧外部触发图像（10 秒）…",
        "Waiting for the selected camera's next external-triggered frame (10 s)…",
    ),
    "vision_capture_timeout": (
        "采集超时；未收到匹配图像。",
        "Capture timed out; no matching image received.",
    ),
    "vision_capture_cancelled": ("采集已取消。", "Capture cancelled."),
    "vision_capture_success": (
        "已采集图像，仅用于当前画布。",
        "Image captured for this canvas only.",
    ),
    "vision_capture_failed": (
        "图像采集失败；请查看详情。",
        "Image capture failed; see details.",
    ),
    "cli_sources_short": ("CLI 图片", "CLI images"),
    "roi_x": ("X 坐标（像素）", "X coordinate (pixels)"),
    "roi_y": ("Y 坐标（像素）", "Y coordinate (pixels)"),
    "roi_w": ("宽度（像素）", "Width (pixels)"),
    "roi_h": ("高度（像素）", "Height (pixels)"),
    "roi_set_rect": ("应用", "Apply"),
    "setup_saving": ("保存中", "Saving"),
    "setup_failed": ("保存/应用失败", "Save/apply failed"),
    "source_error": ("采集源错误", "Acquisition source error"),
    "source_mode": ("全局采集模式", "Global source mode"),
    "vision_folder_mode": ("文件夹图像", "Folder images"),
    "vision_basler_mode": ("工业相机", "Industrial cameras"),
    "source_connect": ("连接全部", "Connect all"),
    "source_disconnect": ("断开", "Disconnect"),
    "source_disconnected": ("未连接", "Disconnected"),
    "source_connecting": ("连接中", "Connecting"),
    "source_connected": ("已连接", "Connected"),
    "source_disconnecting": ("断开中", "Disconnecting"),
    "source_fault": ("连接异常", "Source fault"),
    "source_connection_error": (
        "相机连接操作失败，请查看详情。",
        "Camera connection failed; see details.",
    ),
    "source_save_first": (
        "请先保存并应用来源配置。",
        "Save and apply source configuration first.",
    ),
    "vision_fps_preserved": (
        "保留当前值；仅主动输入 1–10 整数才会更改草稿。",
        "Current value is preserved; explicitly enter an integer 1–10 to change the draft.",
    ),
    "vision_fps": ("文件夹回放 FPS", "Folder replay FPS"),
    "vision_fps_decrease": ("降低回放 FPS", "Decrease replay FPS"),
    "vision_fps_increase": ("提高回放 FPS", "Increase replay FPS"),
    "vision_fps_invalid": (
        "请输入 1–10 的整数 FPS。",
        "Enter an integer FPS from 1 to 10.",
    ),
    "source_folder_status": ("文件夹", "Folder"),
    "source_folder_status_tip": (
        "已保存的文件夹来源；实际图像可用性在保存/启动时验证，不代表检测已就绪。",
        "Saved Folder source; image availability is checked at Save/Start, not a claim of inspection readiness.",
    ),
    "source_basler_status_tip": (
        "已保存的相机来源；计数仅含匹配映射的已连接相机，不代表检测已就绪。",
        "Saved Camera source; count includes connected cameras matching the mappings, not inspection readiness.",
    ),
    "vision_image_select": ("选择图像", "Select image"),
    "vision_roi_select": ("选择 ROI 通道", "Select ROI channel"),
    "vision_connection_unchecked": (
        "仅显示已配置序列号，未检查连接状态。",
        "Configured serial only; connection status has not been checked.",
    ),
    "source_mapping_hint": (
        "按实际接线明确分配已配置的相机。刷新只读取设备信息，不打开相机；选择仅更新草稿。",
        "Assign configured cameras explicitly by physical wiring. Refresh reads device info only; selection updates the draft only.",
    ),
    "source_discover": ("刷新设备", "Refresh devices"),
    "source_discovering": ("正在读取设备信息…", "Reading device information…"),
    "source_duplicate": (
        "该设备已分配给其他相机，请先解除原分配。",
        "Device already assigned to another camera; clear that assignment first.",
    ),
    "vision_schematic": ("ROI 示意", "ROI schematic"),
    "vision_schematic_tip": (
        "仅按 ROI 几何绘制示意；不代表相机或图像尺寸。",
        "Illustrative ROI extents only; not camera dimensions or valid image bounds.",
    ),
    "vision_discard": ("放弃视觉配置修改", "Discard Vision changes"),
    "vision_reloading": ("正在恢复…", "Restoring…"),
    "vision_discard_question": (
        "放弃全部未保存的来源和 ROI 修改，并从已保存文件重新加载？不会应用到检测会话。",
        "Discard all unsaved source and ROI changes and reload saved files? This will not apply to the inspection session.",
    ),
    "vision_reload_error": (
        "无法恢复已保存配置；草稿保留，请检查配置文件和详情。",
        "Could not reload saved configuration; draft kept. Check the files and details.",
    ),
    "vision_dimensions_only": (
        "无图像 · {width}×{height}",
        "No pixels · {width}×{height}",
    ),
    "source_discovery_failed": (
        "设备读取失败；请检查 pypylon/pylon 安装及相机连接，再刷新。原分配保持不变。",
        "Discovery failed. Check pypylon/pylon and camera connections, then Refresh. Assignments are retained.",
    ),
    "source_unconfigured": ("未配置", "Unconfigured"),
    "source_not_discovered": ("未发现", "Not discovered"),
    "source_mode_tip": (
        "已配置的相机统一使用此模式；切换只修改草稿，保存并应用后生效。",
        "One mode for all configured cameras; changes edit the draft until Save and apply.",
    ),
    "source_serial": ("相机序列号", "Camera serial"),
    "source_serial_tip": (
        "选择已发现的设备；保留未发现的已存映射，不自动分配。Basler 每台已配置相机需对应不同设备。",
        "Select a discovered device; undiscovered saved mappings are retained, never assigned automatically. Each configured camera requires a distinct Basler device.",
    ),
    "basler_preparing": (
        "Basler 准备中；尚未就绪，请勿触发",
        "Preparing Basler; not ARMED, do not trigger",
    ),
    "basler_armed": (
        "ARMED · 六相机已就绪，等待外部硬件触发",
        "ARMED · Six cameras ready; waiting for external hardware triggers",
    ),
    "basler_processing": (
        "Basler 采集完成，正在处理单周期",
        "Basler acquisition finished; processing one cycle",
    ),
    "camera_setup": ("相机设置", "Camera setup"),
    "camera_number": ("相机 {number}", "Camera {number}"),
    "source_folder": ("图片文件夹", "Image folder"),
    "browse": ("浏览…", "Browse…"),
    "source_loop": ("循环", "Loop"),
    "source_loop_on": ("开", "ON"),
    "source_loop_off": ("关", "OFF"),
    "cli_sources": (
        "本次进程使用命令行图片；已保存的文件夹在不带图片参数重新启动后生效。",
        "CLI images override saved folders for this process. Relaunch without image arguments to use folders.",
    ),
    "source_partial_save": (
        "机器布局已保存，但相机源未保存；未应用。",
        "Machine layout saved, camera sources not saved; not applied.",
    ),
    "roi_editor": ("机器 ROI 设置", "Machine ROI setup"),
    "roi_workspace": ("机器 ROI", "Machine ROI"),
    "roi_back": ("返回", "Back"),
    "roi_no_frame": ("无图像", "No image"),
    "roi_no_frame_tip": (
        "绘图需要图像；可浏览、导入或采集。无图像时仍可进行数值配置。",
        "Drawing needs an image: browse, import or capture one. Numeric setup remains available without pixels.",
    ),
    "roi_fit": ("适应窗口", "Fit image"),
    "roi_actual": ("原始比例 1:1", "Actual size 1:1"),
    "roi_refresh": ("刷新图像", "Refresh image"),
    "roi_common": ("应用尺寸到全部", "Apply size to all"),
    "roi_distribute": ("均匀分布", "Distribute evenly"),
    "roi_save_apply": ("保存并应用", "Save and apply"),
    "roi_applied": (
        "已保存。下次成功启动后生效。",
        "Saved. Changes will become active on the next successful Start.",
    ),
    "roi_recipe_save": (
        "请先返回并保存当前配方。",
        "Go back and save the current Recipe first.",
    ),
    "duplicate_recipe": ("复制", "Duplicate"),
    "create_recipe": ("创建", "Create"),
    "save_apply": ("保存并应用", "Save and apply"),
    "inspection_model": ("检测模型", "Inspection model"),
    "unavailable_model": ("模型不可用", "Model unavailable"),
    "save_machine_first": (
        "请先在设置中保存机器 ROI。",
        "Save machine ROI in Settings first.",
    ),
    "new_recipe": ("新建", "New"),
    "reload": ("重新加载", "Reload"),
    "save": ("保存", "Save"),
    "apply": ("应用为候选", "Apply candidate"),
    "discard": ("放弃修改", "Discard"),
    "cancel": ("取消", "Cancel"),
    "dirty_question": (
        "草稿尚未保存。保存并应用、放弃修改或取消？",
        "Unsaved draft. Save and apply, discard changes, or cancel?",
    ),
    "saved": ("已保存", "Saved"),
    "unsaved": ("未保存修改", "Unsaved changes"),
    "new_draft": ("新草稿 · 尚未保存", "New draft · not saved"),
    "invalid": ("验证失败", "Invalid"),
    "candidate": ("候选配置", "Candidate"),
    "read_only": ("当前状态仅可查看", "View only in current application state"),
    "preparing": (
        "正在准备",
        "Preparing",
    ),
    "no_catalog": ("未配置配方目录", "No Recipe catalog configured"),
    "recipe_id": ("配方 ID", "Recipe ID"),
    "display_name": ("配方名称", "Recipe name"),
    "product_code": ("产品代码（可选）", "Product code (optional)"),
    "module_id": ("模块 ID", "Module ID"),
    "production": ("生产", "Production"),
    "parameters": ("配方", "Recipe"),
    "diagnostics": ("通信", "Communication"),
    "history": ("历史", "History"),
    "settings": ("视觉", "Vision"),
    "start": ("开始检测", "Start inspection"),
    "stop": ("停止检测", "Stop inspection"),
    "replay_pause": ("暂停", "Pause"),
    "replay_start": ("开始", "Start"),
    "replay_continue": ("继续", "Continue"),
    "replay_step": ("单步", "Step"),
    "replay_end": ("结束", "End"),
    "replay_pausing": ("暂停中…", "Pausing…"),
    "replay_stepping": ("单步中…", "Stepping…"),
    "replay_paused": ("回放已暂停", "Replay paused"),
    "state_READY": ("就绪", "Ready"),
    "state_STARTING": ("预热中", "Warming up"),
    "state_RUNNING": ("运行", "Running"),
    "state_replay_paused": ("暂停", "Paused"),
    "state_source_pausing": ("暂停中", "Pausing"),
    "state_source_paused": ("暂停", "Paused"),
    "source_paused": ("检测准入已暂停", "Inspection admission paused"),
    "state_source_stepping": ("单步", "Step"),
    "state_source_resuming": ("继续中", "Resuming"),
    "source_pausing": ("暂停中", "Pausing"),
    "source_stepping": (
        "单步：等待外部拍照或结果收尾",
        "Step: waiting for external capture or settlement",
    ),
    "source_resuming": ("核对采集边界后继续", "Resuming after SDK boundary check"),
    "basler_control_help": (
        "仅控制检测准入，不控制设备或 PLC。暂停期图像被丢弃；单步等待后续外部触发并完成结果/报告收尾。",
        "Inspection admission only; no machine/PLC control. Paused images are discarded; Step waits for a subsequent external trigger and result/report settlement.",
    ),
    "state_STOPPING": ("停止中", "Stopping"),
    "state_FAULT": ("故障", "Fault"),
    "state_closing": ("关闭中", "Closing"),
    "replay_help": (
        "仅暂停本地图片源；检测会话仍在运行，配置保持锁定。单步完成不代表 PLC 已确认。",
        "Local source only; the inspection session stays RUNNING and editing stays locked. Step completion is not PLC ACK.",
    ),
    "reset": ("复位", "Reset"),
    "recipe": ("选择配方", "Select recipe"),
    "batch": ("新建 / 更换批次", "New / change batch"),
    "batch_prompt": (
        "批次号（可留空）。确认后清零计数及最近结果。\n上一批统计不会归档。",
        "Batch ID (optional). Confirm resets counters and the latest result.\nPrevious batch statistics are not archived.",
    ),
    "production_recipe": ("配方", "Recipe"),
    "pending_recipe": ("待启动激活", "Pending Start"),
    "pending_badge": ("待激活", "Pending"),
    "production_ng": ("CH NG", "CH NG"),
    "production_ok": ("CH OK", "CH OK"),
    "production_empty": ("CH EMPTY", "CH EMPTY"),
    "preview_scope_tip": (
        "仅检查当前冻结图和配方草稿的尺寸或颜色；不执行占位检测。同一图像在生产中可由占位检测判为空位。",
        "Checks this frozen image with the Recipe draft, without Occupancy. Production may independently classify the same image as EMPTY.",
    ),
    "preview_no_reference": ("无参考图像", "No reference image"),
    "preview_disabled_short": ("禁用", "OFF"),
    "preview_error_short": ("异常", "ERR"),
    "preview_pending": ("正在测量…", "Measuring…"),
    "preview_unavailable": ("测量不可用", "Measurement unavailable"),
    "preview_measurement_error": (
        "测量失败 · {count} 通道",
        "Measurement error · {count} channels",
    ),
    "preview_missing_body": ("无可测主体", "No measurable body"),
    "preview_competing_bodies": ("存在多个主体", "Multiple bodies"),
    "preview_truncated_body": ("主体超出视图边界", "Body touches view boundary"),
    "preview_invalid_geometry": ("主体几何不可用", "Unusable body geometry"),
    "preview_insufficient_foreground": (
        "有效颜色像素不足",
        "Insufficient usable color pixels",
    ),
    "preview_unusable": ("颜色证据不可用", "Unusable color evidence"),
    "preview_width": ("宽度超出范围", "Width outside limits"),
    "preview_height": ("高度超出范围", "Height outside limits"),
    "preview_tolerance": ("色相超出容差", "Hue outside tolerance"),
    "channel_count_help": (
        "通道业务检测次数；同一胶囊重复观察会重复计数，不是实际胶囊或产品数量。",
        "Channel business detection occurrences; repeated observations count again, not physical capsules or products.",
    ),
    "configure_first": (
        "请在配方页保存并应用有效配方，再开始检测。",
        "Save and apply a valid Recipe on the Recipe page before starting inspection.",
    ),
    "software_stop": (
        "停止软件检测，不是硬件急停。",
        "Stop software inspection; not a hardware emergency stop.",
    ),
    "inspection_issue": ("检测异常", "Inspection error"),
    "execution_error": ("执行失败", "execution failed"),
    "invalid_result": ("结果无效", "invalid result"),
    "result_unavailable": ("结果不可用", "result unavailable"),
    "cycle_timeout": ("周期超时", "cycle timed out"),
    "cycle_shutdown": ("周期未完成：已停止", "cycle incomplete: stopped"),
    "runtime_error": ("运行时故障", "Runtime fault"),
    "startup_error": ("启动失败", "Startup failed"),
    "shutdown_error": ("清理失败", "Cleanup failed"),
    "operation_error": ("操作未完成", "Operation incomplete"),
    "traditional_short": ("尺寸/颜色", "Size/Color"),
    "collection_short": ("帧收集", "Frame collection"),
    "selected": ("已选", "Selected"),
    "active": ("已激活", "Active"),
    "model": ("模型", "Model"),
    "batch_id": ("批号", "Batch"),
    "cycles": ("完成周期", "Completed cycles"),
    "channel_ok": ("通道 OK 次数", "Channel OK occurrences"),
    "channel_ng": ("通道 NG 次数", "Channel NG occurrences"),
    "unknown_count": ("未知", "Unknown"),
    "error_cycles": ("技术错误周期", "Technical error cycles"),
    "processing": ("最近检测", "Last inspection"),
    "UNCONFIGURED": ("未配置", "Unconfigured"),
    "READY": ("就绪", "Ready"),
    "STARTING": ("启动 / 预热中", "Starting / warming up"),
    "RUNNING": ("检测中", "Running"),
    "STOPPING": ("正在停止", "Stopping"),
    "FAULT": ("系统故障", "System fault"),
    "CLOSED": ("已关闭", "Closed"),
    "closing": ("正在关闭 · 等待运行时安全退出", "Closing · waiting for runtime exit"),
    "close_wait": (
        "关闭未完成 · 仍等待运行时退出",
        "Close incomplete · still waiting for runtime exit",
    ),
    "pending": ("等待结果", "Pending"),
    "no_display_frame": ("暂无检测图像", "No inspection image"),
    "no_image": ("等待图像", "Waiting for image"),
    "reference": ("参考图像 · 非当前运行", "Reference image · previous run"),
    "ok": ("OK", "OK"),
    "ng": ("NG", "NG"),
    "error": ("技术异常", "Technical error"),
    "error_ng": ("NG + 技术异常", "NG + technical error"),
    "unknown": ("未知", "Unknown"),
    "disabled": ("禁用", "Disabled"),
    "empty": ("空位", "EMPTY"),
    "business_status": ("业务状态", "Business status"),
    "occupancy": ("占位检测", "Occupancy"),
    "occupancy_empty": ("空位", "EMPTY"),
    "occupancy_present": ("有料", "PRESENT"),
    "occupancy_unknown": ("未知", "UNKNOWN"),
    "upper_view": ("上视图", "Upper"),
    "lower_view": ("下视图", "Lower"),
    "product_judgment": ("原始产品判断", "Original product judgment"),
    "technical_status": ("技术状态", "Technical status"),
    "technical_success": ("执行成功", "SUCCESS"),
    "technical_error": ("技术异常", "ERROR"),
    "technical_disabled": ("未执行", "DISABLED"),
    "traditional_skip_empty": (
        "尺寸/颜色未执行：确认空位",
        "Size/Color skipped: confirmed EMPTY",
    ),
    "reasons": ("原因", "Reasons"),
    "placeholder": ("此页面尚未实现。", "This page is not implemented yet."),
    "parameters_note": (
        "后续提供配方与检测参数设置。",
        "Recipe and inspection settings will follow.",
    ),
    "comm_manual_connection": ("手动诊断连接", "Manual diagnostic connection"),
    "comm_write_busy": ("正在写入…", "Writing…"),
    "comm_last_write_ack": ("上次 ACK", "Last ACK"),
    "comm_watch_edit": ("添加组", "Add group"),
    "comm_table_group": ("组", "Group"),
    "comm_dec": ("DEC", "DEC"),
    "comm_table_hex": ("HEX", "HEX"),
    "comm_protocol": ("协议", "Protocol"),
    "comm_tcp_nodes": (
        "TCP 节点由握手分配；保留的 UDP 地址不参与 TCP。",
        "TCP nodes are negotiated; retained UDP addresses are not used for TCP.",
    ),
    "comm_host": ("目标 IPv4", "Target IPv4"),
    "comm_port": ("端口", "Port"),
    "comm_timeout": ("请求超时（秒）", "Timeout (seconds)"),
    "comm_advanced": ("高级 FINS 地址", "Advanced FINS addressing"),
    "comm_destination": ("目标", "Destination"),
    "comm_source": ("来源", "Source"),
    "comm_network": ("网络", "Network"),
    "comm_node": ("节点", "Node"),
    "comm_unit": ("单元", "Unit"),
    "comm_test": ("测试读取", "Test read"),
    "comm_test_hint": (
        "读取手动操作所选地址的一个字；不建立连接。",
        "Read one word at the manual address; no connection handshake.",
    ),
    "comm_registers": ("写寄存器", "Write registers"),
    "comm_area": ("存储区", "Area"),
    "comm_watch": ("寄存器监视", "Register watch"),
    "comm_watch_name": ("名称（可选）", "Name (optional)"),
    "comm_watch_group": ("监视组", "Watch group"),
    "comm_watch_add": ("添加", "Add"),
    "comm_watch_remove": ("移除", "Remove"),
    "comm_watch_once": ("读取一次", "Read once"),
    "comm_watch_auto": ("自动刷新 · 1s", "Auto refresh · 1s"),
    "comm_watch_stop": ("停止刷新", "Stop refresh"),
    "comm_watch_idle": ("未发起监视读取", "No watch read sent"),
    "comm_watch_empty": ("请先添加监视组", "Add a watch group first"),
    "comm_watch_busy": ("正在读取监视组", "Reading watch groups"),
    "comm_watch_ok": ("最近一批读取成功", "Last pass succeeded"),
    "comm_watch_wait": (
        "最近一批读取成功 · 等待下次刷新",
        "Last pass succeeded · waiting for refresh",
    ),
    "comm_watch_stopped": (
        "刷新已停止 · 保留历史观察",
        "Refresh stopped · observations retained",
    ),
    "comm_watch_failed": (
        "监视读取失败 · 自动刷新已停止，请显式重启",
        "Watch read failed · auto refresh stopped; restart explicitly",
    ),
    "comm_watch_limit": (
        "最多 16 组，合计 2048 字；每组独立读取，非原子快照。",
        "Up to 16 groups / 2048 words; separate reads, not an atomic snapshot.",
    ),
    "comm_hex": ("十六进制", "Hex"),
    "comm_observed": ("观察时间 / 状态", "Observed / status"),
    "comm_current": ("本批成功", "Latest pass"),
    "comm_previous": ("历史", "Previous"),
    "comm_not_read": ("尚未读取", "Not read"),
    "comm_last_read": ("最近成功读取", "Last successful read"),
    "comm_address": ("起始地址", "Start address"),
    "comm_count": ("读取字数", "Read words"),
    "comm_read": ("读取", "Read"),
    "comm_write": ("写入", "Write"),
    "comm_values": ("写入值", "Write values"),
    "comm_values_hint": (
        "无符号 16 位字，逗号或空格分隔",
        "Unsigned 16-bit words, comma or space separated",
    ),
    "comm_local_write": (
        "使用配置的 PLC 地址；写入确认不代表读回或 PLC 消费。",
        "Uses the configured PLC endpoint; ACK is not readback or PLC consumption.",
    ),
    "comm_feedback": ("通信反馈", "Communication feedback"),
    "comm_idle": ("未发起请求", "No request sent"),
    "comm_busy": ("请求进行中 · {operation}", "Request in progress · {operation}"),
    "comm_closing": (
        "正在关闭 · 等待通信线程退出",
        "Closing · waiting for communication thread exit",
    ),
    "comm_read_ok": ("读取完成", "Read completed"),
    "comm_test_ok": ("测试读取成功", "Test read succeeded"),
    "comm_write_ok": (
        "写入已确认 · 尚未读回验证",
        "Write acknowledged · readback not verified",
    ),
    "comm_last_success": ("最近成功响应", "Last successful response"),
    "comm_no_success": ("尚无成功响应", "No successful response yet"),
    "comm_observation": (
        "最近响应是历史观察，不代表持续在线。",
        "A recent response is an observation, not continuously verified connectivity.",
    ),
    "comm_word": ("无符号值", "Unsigned value"),
    "comm_word_address": ("地址", "Address"),
    "comm_log": ("最近操作（最多 100 条）", "Recent operations (latest 100)"),
    "comm_invalid": (
        "输入无效 · 请检查地址、字数或数值",
        "Invalid input · check address, count or values",
    ),
    "comm_permission": (
        "通信权限错误 · 请查看详细信息",
        "Communication permission error · see details",
    ),
    "comm_timeout_error": ("请求超时", "Request timed out"),
    "comm_write_unknown": (
        "写入结果不确定 · 未自动重试，请显式读取核实",
        "Write outcome unknown · no retry; read explicitly to verify",
    ),
    "comm_transport_error": ("UDP 传输错误", "UDP transport error"),
    "comm_protocol_error": ("FINS 响应协议错误", "FINS response protocol error"),
    "comm_plc_error": ("PLC 结束码错误", "PLC end-code error"),
    "comm_unexpected_error": ("通信操作未完成", "Communication operation incomplete"),
    "settings_note": (
        "使用顶部控件切换界面语言。",
        "Use the top control to change interface language.",
    ),
    "ready_note": ("就绪 · 点击开始检测", "Ready · select Start inspection"),
    "running_note": (
        "运行中 · 等待或显示最近周期",
        "Running · waiting for or showing the latest cycle",
    ),
    "fault_note": (
        "运行时故障 · 停止接纳新周期；安全清理后可复位",
        "Runtime fault · admission stopped; reset after safe cleanup",
    ),
    "issue_note": (
        "操作未完成 · 请检查配置或联系维护人员",
        "Operation incomplete · check configuration or contact maintenance",
    ),
    "command_error": (
        "操作未完成；状态未被强制改变",
        "Command incomplete; application state was not forced",
    ),
    "command_ok": ("操作完成", "Command completed"),
    "ng_note": (
        "最近周期发现 NG · 请查看标记",
        "Latest cycle contains NG · review marked evidence",
    ),
    "technical_note": (
        "最近周期存在技术异常 · 不等同于产品 NG",
        "Latest cycle has technical errors · not equivalent to product NG",
    ),
    "simulation": ("模拟输入 · 非硬件检测", "SIMULATED INPUT · no hardware"),
    "channel": ("通道", "Channel"),
}


def text(key: str, language: Language) -> str:
    base = STRINGS[key]
    if language in ("zh", "en"):
        return base[0 if language == "zh" else 1]
    translated = EXTRA_STRINGS.get(key)
    return translated[LANGUAGES.index(language) - 2] if translated else base[1]


@dataclass(frozen=True, slots=True)
class ThemeColors:
    background: str
    surface: str
    raised: str
    border: str
    primary: str
    secondary: str
    accent: str
    selected: str
    ok: str
    ng: str
    warning: str
    fault: str
    reject: str


DARK = ThemeColors(
    "#10151d",
    "#19212c",
    "#242e3c",
    "#303c4d",
    "#edf2fa",
    "#9aa9bf",
    "#7895ff",
    "#2c3c61",
    "#49d6a0",
    "#ff6e83",
    "#e6b35c",
    "#ec839b",
    "#3b242d",
)
LIGHT = ThemeColors(
    "#edf2fa",
    "#f9fbff",
    "#e7edf8",
    "#d3deee",
    "#1c2c43",
    "#536780",
    "#2d58db",
    "#dce5ff",
    "#128360",
    "#c12e4c",
    "#936014",
    "#b53456",
    "#ffe5eb",
)


def theme_colors(widget: QWidget) -> ThemeColors:
    owner: QWidget | None = widget
    while owner is not None:
        theme = owner.property("theme")
        if theme in ("dark", "light"):
            return LIGHT if theme == "light" else DARK
        owner = owner.parentWidget()
    return DARK


class Style:
    # Dark canvas/legacy drawing tokens; themed controls use their window palette.
    background = DARK.background
    surface = DARK.surface
    raised = DARK.raised
    border = DARK.border
    primary = DARK.primary
    secondary = DARK.secondary
    accent = DARK.accent
    ok = DARK.ok
    ng = DARK.ng
    warning = DARK.warning
    fault = DARK.fault
    space = 12
    small = 6
    radius = 10
    touch = 44
    icon = 22
    icon_text_gap = 4
    body = 15
    title = 24
    number = 32
    minimum = (880, 720)


def button_width(
    button: QPushButton, keys: tuple[str, ...], *, padding: int = Style.small
) -> int:
    """A shared action budget from common captions, not the longest translation."""
    button.ensurePolished()
    return (
        max(
            button.fontMetrics().horizontalAdvance(text(key, language))
            for key in keys
            for language in ("zh", "en")
        )
        + 2 * Style.space
        + padding
        + (button.iconSize().width() + Style.small if not button.icon().isNull() else 0)
    )


def set_button_caption(button: QPushButton, caption: str, width: int) -> None:
    """Keep an action's slot stable and its full meaning available to accessibility.

    Callers retain their localized tooltips, including operation/error guidance.
    """
    button.setFixedWidth(width)
    option = QStyleOptionButton()
    button.initStyleOption(option)
    available = (
        button.style()
        .subElementRect(QStyle.SubElement.SE_PushButtonContents, option, button)
        .width()
    )
    if not button.icon().isNull():
        available -= button.iconSize().width() + Style.icon_text_gap
    button.setText(
        button.fontMetrics().elidedText(
            caption, Qt.TextElideMode.ElideRight, max(0, available)
        )
    )
    button.setAccessibleName(caption)


def apply_theme(widget: QWidget, theme: Theme) -> None:
    colors = LIGHT if theme == "light" else DARK
    widget.setProperty("theme", theme)
    palette = QPalette()
    for role, color in (
        (QPalette.ColorRole.Window, colors.surface),
        (QPalette.ColorRole.WindowText, colors.primary),
        (QPalette.ColorRole.Base, colors.surface),
        (QPalette.ColorRole.AlternateBase, colors.raised),
        (QPalette.ColorRole.Text, colors.primary),
        (QPalette.ColorRole.Button, colors.raised),
        (QPalette.ColorRole.ButtonText, colors.primary),
        (QPalette.ColorRole.Highlight, colors.selected),
        (QPalette.ColorRole.HighlightedText, colors.primary),
        (QPalette.ColorRole.PlaceholderText, colors.secondary),
        (QPalette.ColorRole.ToolTipBase, colors.raised),
        (QPalette.ColorRole.ToolTipText, colors.primary),
    ):
        palette.setColor(role, QColor(color))
    palette.setColor(
        QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(colors.secondary)
    )
    palette.setColor(
        QPalette.ColorGroup.Disabled,
        QPalette.ColorRole.ButtonText,
        QColor(colors.secondary),
    )
    widget.setPalette(palette)
    widget.setStyleSheet(stylesheet(theme))


def stylesheet(theme: Theme = "dark") -> str:
    c = LIGHT if theme == "light" else DARK
    return f"""
    QWidget {{ color: {c.primary}; font-size: {Style.body}px; }}
    QMainWindow, QWidget#shell, QWidget#recipeEditor, QDialog {{ background: {c.background}; }}
    QFrame#summary, QFrame#camera, QFrame#navigation, QFrame#event, QFrame#commPanel {{
        background: {c.surface}; border: 1px solid {c.border}; border-radius: {Style.radius}px;
    }}
    QLabel {{ background: transparent; border: none; }}
    QLabel[role="secondary"] {{ color: {c.secondary}; }}
    QLabel[role="number"] {{ font-size: {Style.number}px; font-weight: 600; }}
    QLabel[role="state"] {{ font-size: {Style.title}px; font-weight: 600; }}
    QLabel[role="error"] {{ color: {c.warning}; }}
    QFrame#visionSources {{ background: {c.surface}; border: 1px solid {c.border}; border-radius: {Style.radius}px; }}
    QFrame#visionRoi {{ background: {c.raised}; border: 1px solid {c.border}; border-radius: {Style.radius}px; }}
    QFrame#recipeSection {{ background: {c.surface}; border: 1px solid {c.border}; border-radius: {Style.radius}px; }}
    QFrame#visionModeSwitch QPushButton {{ background: transparent; border: none; border-radius: 20px; padding: 0; }}
    QFrame#visionModeSwitch QPushButton:checked, QFrame#visionModeSwitch QPushButton:hover, QFrame#visionModeSwitch QPushButton:disabled {{ background: transparent; border: none; }}
    QFrame#visionModeSwitch QPushButton:focus:enabled {{ border: 1px dotted {c.accent}; }}
    QPushButton, QToolButton, QComboBox {{ background: {c.raised}; border: 1px solid {c.border}; border-radius: 7px; padding: 5px 10px; }}
    QPushButton:hover, QToolButton:hover, QComboBox:hover {{ background: {c.selected}; border-color: {c.accent}; }}
    QPushButton:pressed, QToolButton:pressed {{ background: {c.selected}; }}
    QPushButton#recipeRule {{ text-align: left; }}
    QPushButton:checked {{ color: {c.accent}; background: {c.selected}; border-color: {c.accent}; }}
    QPushButton:focus:enabled {{ border: 2px solid {c.accent}; }}
    QComboBox:focus, QLineEdit:focus {{ border-color: {c.accent}; }}
    QWidget#production QPushButton#start:focus:enabled {{ border-color: {c.primary}; }}
    QPushButton:disabled, QComboBox:disabled {{ color: {c.secondary}; background: {c.surface}; border-color: {c.border}; }}
    QDateEdit#historyDate {{ color: {c.primary}; background: {c.raised}; border: 1px solid {c.border}; border-radius: 7px; padding: 5px 10px; }}
    QDateEdit#historyDate:disabled {{ color: {c.secondary}; background: {c.surface}; }}
    QCalendarWidget#historyCalendar {{ background: {c.surface}; color: {c.primary}; }}
    QCalendarWidget#historyCalendar QWidget#qt_calendar_navigationbar {{ background: {c.raised}; }}
    QCalendarWidget#historyCalendar QToolButton {{ background: {c.raised}; color: {c.primary}; padding: 4px 6px; min-height: 24px; }}
    QCalendarWidget#historyCalendar QMenu {{ background: {c.surface}; color: {c.primary}; }}
    QCalendarWidget#historyCalendar QSpinBox {{ background: {c.surface}; color: {c.primary}; padding: 2px; }}
    QCalendarWidget#historyCalendar QAbstractItemView {{ background: {c.surface}; color: {c.primary}; alternate-background-color: {c.raised}; selection-background-color: {c.selected}; selection-color: {c.primary}; outline: 0; }}
    QCalendarWidget#historyCalendar QAbstractItemView::item:selected {{ border: 1px solid {c.accent}; }}
    QCalendarWidget#historyCalendar QAbstractItemView:disabled {{ color: {c.secondary}; }}
    QPushButton#visionSourceCard {{ background: {c.background}; border-radius: {Style.radius}px; }}
    QPushButton#visionSourceCard:hover {{ background: {c.raised}; border-color: {c.secondary}; }}
    QPushButton#visionSourceCard:checked {{ background: {c.selected}; border-color: {c.accent}; }}
    QPushButton#visionSourceCard:focus:enabled {{ border: 2px solid {c.accent}; }}
    QPushButton#visionSourceCard:disabled {{ background: {c.surface}; border-color: {c.border}; }}
    QFrame#visionSources QLineEdit#visionSourceValue, QFrame#visionSources QComboBox#visionSourceValue {{ font-weight: 600; }}
    QFrame#visionFps {{ background: {c.background}; border: 1px solid {c.border}; border-radius: 7px; }}
    QFrame#visionFps QPushButton, QFrame#visionFps QLineEdit {{ background: transparent; border: none; padding: 0; }}
    QFrame#visionFps QPushButton:hover:enabled {{ background: {c.selected}; }}
    QFrame#visionFps QPushButton:focus:enabled, QFrame#visionFps QLineEdit:focus {{ border: 1px solid {c.accent}; }}
    QPushButton#start {{ background: #5b7ffc; color: #ffffff; font-weight: 600; border-color: #5b7ffc; }}
    QPushButton#start:hover {{ background: #6d8eff; }}
    QPushButton#start:pressed {{ background: #4265de; }}
    QPushButton#start:disabled {{ background: {c.raised}; color: {c.secondary}; border-color: {c.border}; }}
    QFrame#navigation QPushButton {{ border: none; background: transparent; padding: 6px 10px; }}
    QFrame#navigation QPushButton:hover {{ background: {c.raised}; }}
    QFrame#navigation QPushButton:checked {{ color: {c.accent}; background: {c.selected}; }}
    QFrame#navigation QPushButton:focus {{ border: 1px solid {c.accent}; }}
    QFrame#navigation QPushButton:checked:focus {{ border: none; }}
    QPushButton[iconAlignment="leading"] {{ text-align: left; }}
    QFrame#batchBadge {{ background: {c.raised}; border: 1px solid {c.border}; border-radius: 8px; }}
    QPushButton#productionBatch {{ background: transparent; border-color: transparent; }}
    QFrame#productionMetrics {{ background: {c.raised}; border: 1px solid {c.border}; border-radius: 8px; }}
    QFrame#navigation QComboBox {{ padding: 5px 8px; }}
    QComboBox::drop-down {{ border: none; width: 22px; }}
    QComboBox#language::down-arrow {{ image: none; width: 0; height: 0; }}
    QComboBox#webAddress::drop-down {{ width: {Style.touch}px; }}
    QComboBox#webAddress::down-arrow {{ image: none; }}
    QPushButton#webPower, QPushButton#reportingPower {{ color: {c.primary}; border-color: {c.accent}; background: {c.selected}; }}
    QPushButton#webPower:disabled, QPushButton#reportingPower:disabled {{ color: {c.secondary}; border-color: {c.border}; background: {c.raised}; }}
    QComboBox QAbstractItemView, QMenu {{ background: {c.surface}; color: {c.primary}; border: 1px solid {c.border}; selection-background-color: {c.selected}; }}
    QLineEdit, QSpinBox, QDoubleSpinBox {{ background: {c.surface}; color: {c.primary}; padding: 7px; border: 1px solid {c.border}; border-radius: 6px; selection-background-color: {c.selected}; }}
    QLineEdit:disabled, QSpinBox:disabled {{ color: {c.secondary}; background: {c.raised}; }}
    QScrollArea, QWidget#commBody, QWidget#sourceMappings, QWidget#recipeSummary {{ background: {c.background}; border: none; }}
    QTableWidget, QPlainTextEdit {{ background: {c.surface}; alternate-background-color: {c.raised}; border: 1px solid {c.border}; gridline-color: {c.border}; }}
    QTableWidget#historyCycles, QTableWidget#historyChannels {{ border-radius: {Style.radius}px; }}
    QTableWidget#commWatchTable::item:selected, QTableWidget#historyCycles::item:selected, QTableWidget#historyChannels::item:selected {{ background: {c.selected}; color: {c.primary}; }}
    QHeaderView::section {{ background: {c.raised}; color: {c.secondary}; padding: 6px; border: none; }}
    QTableWidget#historyCycles QHeaderView, QTableWidget#historyChannels QHeaderView {{ background: {c.raised}; }}
    QTableWidget#historyCycles QScrollBar:horizontal, QTableWidget#historyChannels QScrollBar:horizontal {{ background: {c.background}; height: 14px; }}
    QTableWidget#historyCycles QScrollBar::handle:horizontal, QTableWidget#historyChannels QScrollBar::handle:horizontal {{ background: {c.border}; min-width: 40px; border-radius: 6px; }}
    QTableWidget#historyCycles QScrollBar::add-line:horizontal, QTableWidget#historyCycles QScrollBar::sub-line:horizontal, QTableWidget#historyChannels QScrollBar::add-line:horizontal, QTableWidget#historyChannels QScrollBar::sub-line:horizontal {{ width: 0; }}
    QTableWidget#historyCycles QScrollBar::add-page:horizontal, QTableWidget#historyCycles QScrollBar::sub-page:horizontal, QTableWidget#historyChannels QScrollBar::add-page:horizontal, QTableWidget#historyChannels QScrollBar::sub-page:horizontal {{ background: none; }}
    QTableWidget#historyCycles::corner, QTableWidget#historyChannels::corner {{ background: {c.surface}; border: none; }}
    QCheckBox {{ background: transparent; spacing: 8px; }}
    QCheckBox::indicator {{ width: 20px; height: 20px; }}
    QCheckBox#archiveCondition {{ color: {c.primary}; background: {c.surface}; border: 1px solid {c.border}; border-radius: 6px; padding: 4px 8px; spacing: 6px; }}
    QCheckBox#archiveCondition:hover:enabled {{ border-color: {c.secondary}; }}
    QCheckBox#archiveCondition:checked:enabled {{ background: {c.selected}; border-color: {c.accent}; }}
    QCheckBox#archiveCondition:focus:enabled {{ border-color: {c.accent}; }}
    QCheckBox#archiveCondition:disabled {{ color: {c.secondary}; background: {c.raised}; border-color: {c.border}; }}
    QTabBar::tab {{ padding: 12px 10px; min-height: 20px; color: {c.primary}; background: {c.surface}; border: none; }}
    QTabBar::tab:selected {{ color: {c.accent}; background: {c.selected}; }}
    QSlider::groove:horizontal {{ height: 6px; background: {c.border}; border-radius: 3px; }}
    QSlider::handle:horizontal {{ width: 22px; margin: -8px 0; border-radius: 11px; background: {c.accent}; }}
    QScrollBar:vertical {{ background: {c.background}; width: 14px; }}
    QScrollBar::handle:vertical {{ background: {c.border}; min-height: 40px; border-radius: 6px; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}
    QToolTip {{ color: {c.primary}; background: {c.raised}; border: 1px solid {c.border}; padding: 6px; }}
    """


@cache
def camera_icon(color: str = Style.primary) -> QIcon:
    """Shared Vision/Production camera strokes, including the existing dimmed mode."""
    icon = QIcon()
    for checked in (False, True):
        for dpr in (1.0, 1.25, 1.5, 2.0):
            pixmap = QPixmap(round(24 * dpr), round(24 * dpr))
            pixmap.setDevicePixelRatio(dpr)
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QPen(QColor(color), 1.6))
            painter.drawRect(3, 8, 18, 12)
            painter.drawRect(7, 5, 7, 3)
            painter.drawEllipse(8, 10, 8, 8)
            painter.drawPoint(18, 11)
            painter.end()
            state = QIcon.State.On if checked else QIcon.State.Off
            icon.addPixmap(pixmap, QIcon.Mode.Normal, state)
            dimmed = QPixmap(pixmap.size())
            dimmed.setDevicePixelRatio(dpr)
            dimmed.fill(Qt.GlobalColor.transparent)
            muted = QPainter(dimmed)
            muted.setOpacity(0.35)
            muted.drawPixmap(0, 0, pixmap)
            muted.end()
            icon.addPixmap(dimmed, QIcon.Mode.Disabled, state)
    return icon


@cache
def navigation_icon(index: int, color: str = Style.secondary) -> QIcon:
    """Internal vector strokes, rendered at common DPRs; no OS icon theme/font."""
    if index == 4:
        return camera_icon(color)
    icon = QIcon()
    for dpr in (1.0, 1.25, 1.5, 2.0):
        pixmap = QPixmap(round(Style.icon * dpr), round(Style.icon * dpr))
        pixmap.setDevicePixelRatio(dpr)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(color), 1.6))
        if index == 0:
            for x in (3, 12):
                for y in (3, 12):
                    painter.drawRoundedRect(QRectF(x, y, 6, 6), 1, 1)
        elif index == 1:
            for i in range(3):
                x = 4 + i * 7
                painter.drawLine(QPointF(x, 3), QPointF(x, 19))
                painter.drawEllipse(QPointF(x, 7 if (i + index) % 2 else 14), 2, 2)
        elif index == 2:
            painter.drawRoundedRect(QRectF(2, 3, 6, 7), 1, 1)
            painter.drawRoundedRect(QRectF(14, 4, 6, 7), 1, 1)
            for a, b in (
                (QPointF(4, 1), QPointF(4, 3)),
                (QPointF(6, 1), QPointF(6, 3)),
                (QPointF(16, 2), QPointF(16, 4)),
                (QPointF(18, 2), QPointF(18, 4)),
                (QPointF(5, 10), QPointF(5, 16)),
                (QPointF(5, 16), QPointF(17, 16)),
                (QPointF(17, 16), QPointF(17, 11)),
            ):
                painter.drawLine(a, b)
        else:
            painter.drawEllipse(QRectF(3, 3, 16, 16))
            painter.drawLine(QPointF(11, 6), QPointF(11, 11))
            painter.drawLine(QPointF(11, 11), QPointF(16, 13))
        painter.end()
        icon.addPixmap(pixmap)
    return icon


@cache
def production_icon(kind: str, foreground: str | None = None) -> QIcon:
    """Compact state/action strokes using the same DPR-aware Qt icon infrastructure."""
    color = (
        Style.fault
        if kind == "FAULT"
        else Style.warning
        if kind in ("STOPPING", "replay_paused")
        else Style.accent
        if kind == "STARTING"
        else Style.ok
        if kind in ("READY", "RUNNING")
        else Style.background
        if kind in ("start", "stop", "reset", "replay_pause", "replay_continue")
        else Style.secondary
    )
    icon = QIcon()
    for dpr in (1.0, 1.25, 1.5, 2.0):
        pixmap = QPixmap(round(Style.icon * dpr), round(Style.icon * dpr))
        pixmap.setDevicePixelRatio(dpr)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(foreground or color), 1.7))
        if kind == "RUNNING":
            painter.drawEllipse(QRectF(3, 3, 16, 16))
            painter.setBrush(QColor(color))
            painter.drawEllipse(QRectF(8, 8, 6, 6))
        elif kind in ("start", "replay_continue", "replay_step"):
            for a, b in (
                (QPointF(6, 4), QPointF(18, 11)),
                (QPointF(18, 11), QPointF(6, 18)),
                (QPointF(6, 18), QPointF(6, 4)),
            ):
                painter.drawLine(a, b)
            if kind == "replay_step":
                painter.drawLine(19, 4, 19, 18)
        elif kind in ("replay_pause", "replay_paused"):
            painter.drawLine(7, 5, 7, 17)
            painter.drawLine(15, 5, 15, 17)
        elif kind in ("stop", "replay_end"):
            painter.drawRect(QRectF(5, 5, 12, 12))
        elif kind == "power":
            painter.drawArc(QRectF(4, 4, 15, 15), 135 * 16, 270 * 16)
            painter.drawLine(11, 2, 11, 11)
        elif kind == "reset":
            painter.drawArc(4, 4, 14, 14, 30 * 16, 290 * 16)
            painter.drawLine(19, 3, 19, 9)
            painter.drawLine(19, 9, 13, 9)
        elif kind == "tag":
            path = QPainterPath(QPointF(3, 3))
            for point in (
                QPointF(12, 3),
                QPointF(21, 12),
                QPointF(12, 21),
                QPointF(3, 12),
            ):
                path.lineTo(point)
            path.closeSubpath()
            painter.drawPath(path)
            painter.drawEllipse(QPointF(7, 7), 1.5, 1.5)
        elif kind == "cycles":
            painter.drawRoundedRect(QRectF(4, 3, 14, 17), 2, 2)
            painter.drawRoundedRect(QRectF(8, 1, 6, 4), 1, 1)
            for y in (8, 12, 16):
                painter.drawLine(7, y, 15, y)
        elif kind == "clock":
            painter.drawEllipse(QRectF(3, 3, 16, 16))
            painter.drawLine(11, 6, 11, 11)
            painter.drawLine(11, 11, 16, 13)
        elif kind == "batch":
            for a, b in (
                (QPointF(4, 15), QPointF(15, 4)),
                (QPointF(15, 4), QPointF(19, 8)),
                (QPointF(19, 8), QPointF(8, 19)),
                (QPointF(8, 19), QPointF(3, 20)),
                (QPointF(3, 20), QPointF(4, 15)),
            ):
                painter.drawLine(a, b)
        elif kind == "FAULT":
            for a, b in (
                (QPointF(11, 3), QPointF(20, 19)),
                (QPointF(20, 19), QPointF(2, 19)),
                (QPointF(2, 19), QPointF(11, 3)),
            ):
                painter.drawLine(a, b)
            painter.drawLine(11, 8, 11, 13)
            painter.drawPoint(11, 16)
        elif kind == "STOPPING":
            painter.drawRect(QRectF(5, 5, 12, 12))
        elif kind == "STARTING":
            painter.drawEllipse(QRectF(3, 3, 16, 16))
            painter.drawLine(11, 6, 11, 11)
            painter.drawLine(11, 11, 16, 13)
        else:
            painter.drawEllipse(QRectF(3, 3, 16, 16))
            if kind == "READY":
                painter.drawLine(6, 11, 10, 15)
                painter.drawLine(10, 15, 16, 7)
            else:
                painter.drawLine(7, 11, 15, 11)
        painter.end()
        icon.addPixmap(pixmap)
        if kind == "batch":
            disabled = QPixmap(pixmap.size())
            disabled.setDevicePixelRatio(dpr)
            disabled.fill(Qt.GlobalColor.transparent)
            painter = QPainter(disabled)
            painter.setOpacity(0.35)
            painter.drawPixmap(0, 0, pixmap)
            painter.end()
            icon.addPixmap(disabled, QIcon.Mode.Disabled)
    return icon


@cache
def indicator_icon(state: Indicator, foreground: str | None = None) -> QIcon:
    """Semantic shapes, not font-dependent checkmark/warning glyphs."""
    color = {
        Indicator.OK: Style.ok,
        Indicator.NG: Style.ng,
        Indicator.ERROR_NG: Style.ng,
        Indicator.ERROR: Style.warning,
        Indicator.UNKNOWN: Style.warning,
        Indicator.PENDING: Style.secondary,
        Indicator.DISABLED: "#727985",
        Indicator.EMPTY: Style.secondary,
    }[state]
    color = foreground or color
    icon = QIcon()
    for dpr in (1.0, 1.25, 1.5, 2.0):
        pixmap = QPixmap(round(18 * dpr), round(18 * dpr))
        pixmap.setDevicePixelRatio(dpr)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(color), 1.5))
        if state is Indicator.PENDING:
            painter.drawEllipse(QRectF(6, 6, 6, 6))
        elif state is Indicator.DISABLED:
            painter.drawLine(QPointF(4, 9), QPointF(14, 9))
        elif state is Indicator.EMPTY:
            # An empty outlined slot, distinct from pending dot and disabled dash.
            painter.drawRoundedRect(QRectF(2, 3, 14, 12), 2, 2)
        elif state is Indicator.UNKNOWN:
            painter.drawEllipse(QRectF(2, 2, 14, 14))
            path = QPainterPath(QPointF(6, 6))
            path.cubicTo(6, 2, 14, 3, 12, 7)
            path.cubicTo(11, 9, 9, 8, 9, 11)
            painter.drawPath(path)
            painter.drawPoint(9, 14)
        elif state is Indicator.ERROR:
            for a, b in (
                (QPointF(9, 2), QPointF(17, 16)),
                (QPointF(17, 16), QPointF(1, 16)),
                (QPointF(1, 16), QPointF(9, 2)),
            ):
                painter.drawLine(a, b)
            painter.drawLine(QPointF(9, 7), QPointF(9, 11))
            painter.drawPoint(QPointF(9, 14))
        else:
            painter.setBrush(QColor(color))
            if state is Indicator.OK:
                painter.drawEllipse(QRectF(2, 2, 14, 14))
                painter.setPen(QPen(QColor(Style.background), 1.5))
                painter.drawLine(QPointF(5, 9), QPointF(8, 12))
                painter.drawLine(QPointF(8, 12), QPointF(13, 6))
            else:
                painter.drawRoundedRect(QRectF(2, 2, 14, 14), 3, 3)
                painter.setPen(QPen(QColor(Style.background), 1.5))
                painter.drawLine(QPointF(6, 6), QPointF(12, 12))
                painter.drawLine(QPointF(6, 12), QPointF(12, 6))
        painter.end()
        icon.addPixmap(pixmap)
    return icon


@cache
def shell_icon(kind: str, color: str = Style.secondary) -> QIcon:
    """Shared monochrome shell symbols drawn with Qt, independent of icon fonts."""
    if kind == "plc":
        return navigation_icon(2, color)
    if kind == "state":
        return production_icon("READY", color)
    icon = QIcon()
    for dpr in (1.0, 1.25, 1.5, 2.0):
        pixmap = QPixmap(round(24 * dpr), round(24 * dpr))
        pixmap.setDevicePixelRatio(dpr)
        pixmap.fill(Qt.GlobalColor.transparent)
        p = QPainter(pixmap)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(color), 1.6))
        if kind == "brand":
            p.drawEllipse(QRectF(2, 6, 20, 12))
            p.setBrush(QColor(color))
            p.drawEllipse(QRectF(8, 8, 8, 8))
        elif kind == "sun":
            p.drawEllipse(QRectF(8, 8, 8, 8))
            for x, y, dx, dy in (
                (12, 2, 0, 3),
                (12, 19, 0, 3),
                (2, 12, 3, 0),
                (19, 12, 3, 0),
                (4, 4, 2, 2),
                (18, 18, 2, 2),
                (4, 20, 2, -2),
                (18, 6, 2, -2),
            ):
                p.drawLine(x, y, x + dx, y + dy)
        elif kind == "moon":
            path = QPainterPath()
            path.addEllipse(QRectF(3, 3, 18, 18))
            cutout = QPainterPath()
            cutout.addEllipse(QRectF(9, 0, 16, 16))
            p.fillPath(path.subtracted(cutout), QColor(color))
        elif kind == "chevron":
            p.drawLine(6, 9, 12, 15)
            p.drawLine(12, 15, 18, 9)
        elif kind == "globe":
            p.drawEllipse(QRectF(3, 3, 18, 18))
            p.drawEllipse(QRectF(8, 3, 8, 18))
            p.drawLine(3, 12, 21, 12)
            p.drawLine(5, 7, 19, 7)
            p.drawLine(5, 17, 19, 17)
        elif kind == "folder":
            path = QPainterPath(QPointF(3, 8))
            for x, y in ((3, 5), (10, 5), (13, 8), (21, 8), (21, 19), (3, 19), (3, 8)):
                path.lineTo(x, y)
            p.drawPath(path)
        else:
            p.drawEllipse(QRectF(3, 3, 18, 18))
            p.drawLine(12, 6, 12, 12)
            p.drawLine(12, 12, 17, 15)
        p.end()
        icon.addPixmap(pixmap)
    return icon
