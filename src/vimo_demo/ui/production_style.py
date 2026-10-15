"""Small bilingual catalog and logical-pixel visual tokens for the workstation."""

from functools import cache
from typing import Literal

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap

from .production_presentation import Indicator

type Language = Literal["zh", "en"]

STRINGS: dict[str, tuple[str, str]] = {
    "plc_reporting": ("PLC 自动报告", "PLC automatic reporting"),
    "plc_enable": ("启用", "Enable"),
    "plc_session_only": (
        "仅本次应用有效；下次启动检测时生效，不保存到配置。",
        "This application only; takes effect at the next Start and is never saved.",
    ),
    "plc_disabled": ("自动报告已关闭", "Reporting disabled"),
    "plc_enabled": ("已启用 · 待启动", "Enabled · next Start"),
    "plc_starting": ("已启用 · 启动中", "Enabled · starting"),
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
    "vision_error": (
        "相机配置操作失败；请查看详情。",
        "Camera configuration operation failed; see details.",
    ),
    "folder_reference_error": (
        "文件夹参考图不可用；请检查路径和图片。",
        "Folder reference unavailable; check the path and images.",
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
    "reference_folder_tip": (
        "浏览文件夹参考图；不运行检测",
        "Browse folder reference images; no inference",
    ),
    "reference_runtime_tip": (
        "冻结当前相机同源的最新运行帧",
        "Freeze the latest runtime frame from this camera/source",
    ),
    "reference_unavailable": (
        "当前相机没有同源运行帧",
        "No eligible runtime frame for this camera/source",
    ),
    "reference_previous": ("上一张参考图", "Previous reference image"),
    "reference_next": ("下一张参考图", "Next reference image"),
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
    "source_settings": ("相机来源设置…", "Camera source settings…"),
    "source_confirm": ("确认", "Confirm"),
    "source_mapping_hint": (
        "请按实际接线明确分配六台相机。刷新只读取设备信息，不打开相机；确认仅更新草稿。",
        "Assign all six cameras explicitly by physical wiring. Refresh reads device info only; Confirm updates the draft only.",
    ),
    "source_discover": ("刷新设备", "Refresh devices"),
    "source_discovery_idle": (
        "尚未刷新；点击刷新设备以读取已连接相机。",
        "Not refreshed; click Refresh devices to discover connected cameras.",
    ),
    "source_discovering": ("正在读取设备信息…", "Reading device information…"),
    "source_discovered": (
        "发现 {count} 台设备；请明确选择对应关系。未发现的原序列号保持不变。",
        "Found {count} devices; select assignments explicitly. Undiscovered assignments are retained.",
    ),
    "source_discovery_failed": (
        "设备读取失败；请检查 pypylon/pylon 安装及相机连接，再刷新。原分配保持不变。",
        "Discovery failed. Check pypylon/pylon and camera connections, then Refresh. Assignments are retained.",
    ),
    "source_unconfigured": ("未配置", "Unconfigured"),
    "source_not_discovered": ("未发现 / 离线", "Not discovered / offline"),
    "source_mapping_invalid": (
        "Basler 模式需要六个非空且互不重复的数字序列号。",
        "Basler requires six nonempty, distinct numeric serials.",
    ),
    "source_mode_tip": (
        "六台相机统一使用此模式；文件夹保留为参考和预热输入",
        "One mode for all six cameras; folders remain reference/warm-up inputs",
    ),
    "source_folder_mode": ("文件夹回放", "Folder replay"),
    "source_basler_mode": ("Basler 单周期", "Basler single cycle"),
    "source_serial": ("相机序列号", "Camera serial"),
    "source_serial_tip": (
        "显式对应当前相机；Basler 模式需要六个不同的数字序列号",
        "Explicit identity for this camera; Basler requires six distinct numeric serials",
    ),
    "basler_preparing": (
        "Basler 准备中；尚未就绪，请勿触发",
        "Preparing Basler; not ARMED, do not trigger",
    ),
    "basler_armed": (
        "ARMED · 六相机就绪，等待一次外部拍照触发",
        "ARMED · Six cameras ready; awaiting one external trigger",
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
    "roi_no_frame": ("无参考帧；请刷新参考图像", "No reference image; refresh to edit"),
    "roi_fit": ("适应窗口", "Fit image"),
    "roi_actual": ("原始比例 1:1", "Actual size 1:1"),
    "roi_refresh": ("刷新参考帧", "Refresh reference"),
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
    "all_model_labels": (
        "检测规则：所有模型缺陷标签",
        "Detection rule: all model defect labels",
    ),
    "configured_detection_rule": (
        "检测规则：使用已保存的检测配置",
        "Detection rule: saved inspection configuration",
    ),
    "advanced_recipe_notice": (
        "此配方含相机或通道专用配置，本页不可编辑。",
        "This Recipe has camera/channel configuration not editable on this page.",
    ),
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
    "occupancy_unavailable": ("无可用证据", "Unavailable"),
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
    "comm_settings": ("通信设置", "Communication settings"),
    "comm_settings_close": ("关闭", "Close"),
    "comm_manual_connection": ("手动诊断连接", "Manual diagnostic connection"),
    "comm_details": ("操作详情", "Operation details"),
    "comm_watch_edit": ("添加组", "Add group"),
    "comm_table_group": ("组", "Group"),
    "comm_dec": ("DEC", "DEC"),
    "comm_table_hex": ("HEX", "HEX"),
    "comm_table_observed": ("观察 / 状态", "Observed / status"),
    "comm_protocol": ("协议", "Protocol"),
    "comm_tcp_nodes": (
        "TCP 节点由握手分配；保留的 UDP 地址不参与 TCP。",
        "TCP nodes are negotiated; retained UDP addresses are not used for TCP.",
    ),
    "comm_host": ("目标 IPv4", "Target IPv4"),
    "comm_port": ("UDP 端口", "UDP port"),
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
    "comm_registers": ("手动寄存器操作", "Manual register operations"),
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
        "仅允许写入 127.0.0.1 模拟器；写入确认不是读回验证。",
        "Writes are limited to 127.0.0.1; acknowledgement is not readback verification.",
    ),
    "comm_remote_read": (
        "当前目标仅可读取。写入严格限于 127.0.0.1。",
        "Read-only target. Writes require exactly 127.0.0.1.",
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
    "comm_last_read": ("最近读取值", "Last read values"),
    "comm_word": ("无符号值", "Unsigned value"),
    "comm_word_address": ("地址", "Address"),
    "comm_log": ("最近操作（最多 100 条）", "Recent operations (latest 100)"),
    "comm_invalid": (
        "输入无效 · 请检查地址、字数或数值",
        "Invalid input · check address, count or values",
    ),
    "comm_permission": (
        "禁止写入 · 目标必须为 127.0.0.1",
        "Write blocked · target must be 127.0.0.1",
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
    "history_note": (
        "尚无归档或历史数据功能。",
        "Archive and history are not available yet.",
    ),
    "settings_note": (
        "使用顶部按钮切换中文 / English。",
        "Use the top control to switch 中文 / English.",
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
    return STRINGS[key][0 if language == "zh" else 1]


class Style:
    background = "#151619"
    surface = "#202227"
    raised = "#30343d"
    border = "#363b44"
    primary = "#f1f2f5"
    secondary = "#aab2bf"
    accent = "#b5c8ee"
    ok = "#789486"
    ng = "#ff637a"
    warning = "#e5b967"
    fault = "#c9adf1"
    space = 12
    small = 6
    radius = 12
    touch = 44
    icon = 22
    body = 15
    title = 24
    number = 32
    minimum = (880, 720)


def stylesheet() -> str:
    return f"""
    QWidget {{ color: {Style.primary}; font-size: {Style.body}px; }}
    QMainWindow, QWidget#shell, QDialog#roiEditor {{ background: {Style.background}; }}
    QFrame#summary, QFrame#camera, QFrame#event {{ background: {Style.surface}; border: 1px solid {Style.border}; border-radius: {Style.radius}px; }}
    QLabel {{ background: transparent; border: none; }}
    QLabel[role="secondary"] {{ color: {Style.secondary}; }}
    QLabel[role="number"] {{ font-size: {Style.number}px; font-weight: 600; }}
    QLabel[role="state"] {{ font-size: {Style.title}px; font-weight: 600; }}
    QPushButton, QToolButton, QComboBox {{ background: {Style.raised}; border: 1px solid {Style.border}; border-radius: 8px; padding: 5px 10px; }}
    QPushButton:hover, QToolButton:hover {{ border-color: {Style.accent}; }}
    QPushButton:checked {{ color: {Style.accent}; border-color: {Style.accent}; }}
    QPushButton:disabled, QComboBox:disabled {{ color: #707681; background: {Style.surface}; }}
    QPushButton#start {{ background: {Style.accent}; color: #18202c; font-weight: 600; }}
    QPushButton#start:disabled {{ background: {Style.raised}; color: #707681; }}
    QWidget#production QPushButton {{ outline: none; }}
    QWidget#production QPushButton:hover:enabled {{ border-color: {Style.accent}; }}
    QWidget#production QPushButton:focus:enabled {{ border: 2px solid {Style.accent}; }}
    QWidget#production QPushButton#start:focus:enabled {{ border-color: {Style.primary}; }}
    QWidget#production QPushButton:disabled {{ border-color: {Style.border}; }}
    QLineEdit {{ background: {Style.surface}; padding: 8px; border: 1px solid {Style.border}; }}
    QDialog {{ background: {Style.background}; }}
    QComboBox QAbstractItemView {{ background: {Style.raised}; color: {Style.primary}; }}
    QWidget#recipeEditor {{ background: {Style.background}; }}
    QWidget#parameters QCheckBox {{ background: transparent; spacing: 8px; }}
    QWidget#parameters QCheckBox::indicator {{ width: 20px; height: 20px; }}
    QWidget#parameters QLineEdit:disabled {{ color: {Style.secondary}; }}
    QWidget#parameters QScrollBar:vertical {{ background: {Style.background}; width: 16px; }}
    QWidget#parameters QScrollBar::handle:vertical {{ background: {Style.raised}; min-height: 40px; border-radius: 6px; }}
    QWidget#parameters QScrollBar::add-line:vertical, QWidget#parameters QScrollBar::sub-line:vertical {{ height: 0px; }}
    QWidget#parameters QScrollBar::add-page:vertical, QWidget#parameters QScrollBar::sub-page:vertical {{ background: none; }}
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


def navigation_icon(index: int) -> QIcon:
    """Internal vector strokes, rendered at common DPRs; no OS icon theme/font."""
    if index == 4:
        return camera_icon(Style.secondary)
    icon = QIcon()
    for dpr in (1.0, 1.25, 1.5, 2.0):
        pixmap = QPixmap(round(Style.icon * dpr), round(Style.icon * dpr))
        pixmap.setDevicePixelRatio(dpr)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(Style.secondary), 1.6))
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
def production_icon(kind: str) -> QIcon:
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
        painter.setPen(QPen(QColor(color), 1.7))
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
        elif kind == "reset":
            painter.drawArc(4, 4, 14, 14, 30 * 16, 290 * 16)
            painter.drawLine(19, 3, 19, 9)
            painter.drawLine(19, 9, 13, 9)
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
def indicator_icon(state: Indicator) -> QIcon:
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
        elif state in (Indicator.ERROR, Indicator.UNKNOWN):
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
