"""Small bilingual catalog and logical-pixel visual tokens for the workstation."""

from functools import cache
from typing import Literal

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap

from .production_presentation import Indicator

type Language = Literal["zh", "en"]

STRINGS: dict[str, tuple[str, str]] = {
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
    "source_error": ("回放源错误", "Replay source error"),
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
    "channel_count_help": (
        "通道判断次数，不是实际胶囊或产品数量。",
        "Channel judgment occurrences, not physical capsule or product counts.",
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
    "no_image": ("等待图像", "Waiting for image"),
    "reference": ("参考图像 · 非当前运行", "Reference image · previous run"),
    "ok": ("OK", "OK"),
    "ng": ("NG", "NG"),
    "error": ("技术异常", "Technical error"),
    "error_ng": ("NG + 技术异常", "NG + technical error"),
    "unknown": ("未知", "Unknown"),
    "disabled": ("禁用", "Disabled"),
    "placeholder": ("此页面尚未实现。", "This page is not implemented yet."),
    "parameters_note": (
        "后续提供配方与检测参数设置。",
        "Recipe and inspection settings will follow.",
    ),
    "diagnostics_note": (
        "通信功能尚未实现。",
        "Communication functionality is not implemented yet.",
    ),
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
        else Style.ok
        if kind in ("READY", "RUNNING")
        else Style.background
        if kind in ("start", "stop", "reset")
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
        elif kind == "start":
            for a, b in (
                (QPointF(6, 4), QPointF(18, 11)),
                (QPointF(18, 11), QPointF(6, 18)),
                (QPointF(6, 18), QPointF(6, 4)),
            ):
                painter.drawLine(a, b)
        elif kind == "stop":
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
        elif kind in ("STARTING", "STOPPING"):
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
