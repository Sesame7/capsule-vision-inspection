"""Small bilingual catalog and logical-pixel visual tokens for the workstation."""

from functools import cache
from typing import Literal

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap

from .production_presentation import Indicator

type Language = Literal["zh", "en"]

STRINGS: dict[str, tuple[str, str]] = {
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
    "source_folder": ("图片文件夹", "Image folder"),
    "browse": ("浏览…", "Browse…"),
    "source_loop": ("循环", "Loop"),
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
    "validate": ("验证", "Validate"),
    "save": ("保存", "Save"),
    "save_as": ("另存为", "Save As"),
    "apply": ("应用为候选", "Apply candidate"),
    "discard": ("放弃修改", "Discard"),
    "cancel": ("取消", "Cancel"),
    "dirty_question": (
        "草稿尚未保存。保存并应用、放弃修改或取消？",
        "Unsaved draft. Save and apply, discard changes, or cancel?",
    ),
    "editing": ("编辑草稿", "Editing draft"),
    "saved": ("已保存", "Saved"),
    "unsaved": ("未保存修改", "Unsaved changes"),
    "new_draft": ("新草稿 · 尚未保存", "New draft · not saved"),
    "valid": ("验证通过", "Valid"),
    "invalid": ("验证失败", "Invalid"),
    "unchecked": ("尚未验证", "Not validated"),
    "candidate": ("候选配置", "Candidate"),
    "read_only": ("当前状态仅可查看", "View only in current application state"),
    "apply_help": (
        "保存不会应用。仅已保存且无修改的有效配方可应用；启动成功后才激活。",
        "Save does not Apply. Apply a valid saved draft without edits; activation requires successful Start.",
    ),
    "preparing": (
        "正在准备",
        "Preparing",
    ),
    "no_catalog": ("未配置配方目录", "No Recipe catalog configured"),
    "recipe_id": ("配方 ID", "Recipe ID"),
    "display_name": ("配方名称", "Recipe name"),
    "product_code": ("产品代码（可选）", "Product code (optional)"),
    "model_choice": ("模型选择", "Model selection"),
    "default_model": ("使用默认模型", "Use default model"),
    "model_details": ("模型信息（只读）", "Model metadata (read only)"),
    "layout_details": (
        "机器布局与基础 ROI（只读）",
        "Machine layout / base ROI (read only)",
    ),
    "camera_scope": ("相机覆盖", "Camera override"),
    "roi_width": ("宽度", "Width"),
    "roi_height": ("高度", "Height"),
    "camera_count": ("相机数", "Cameras"),
    "channel_count": ("通道数", "Channels"),
    "module_id": ("模块 ID", "Module ID"),
    "catalog_issues": ("配方目录问题", "Recipe catalog issues"),
    "id_save_as": (
        "已保存的 ID 不可直接修改，请使用另存为。",
        "Use Save As to give a saved Recipe a new identity.",
    ),
    "production": ("生产", "Production"),
    "parameters": ("配方", "Recipe"),
    "diagnostics": ("诊断", "Diagnostics"),
    "history": ("历史", "History"),
    "settings": ("设置", "Settings"),
    "start": ("开始检测", "Start inspection"),
    "stop": ("停止检测", "Stop inspection"),
    "reset": ("复位", "Reset"),
    "recipe": ("选择配方", "Select recipe"),
    "batch": ("新建 / 更换批次", "New / change batch"),
    "batch_prompt": (
        "批次号（留空表示未指定）；确认后清零累计计数。",
        "Batch ID (blank for none). Confirming resets counters.",
    ),
    "selected": ("已选", "Selected"),
    "active": ("已激活", "Active"),
    "model": ("模型", "Model"),
    "batch_id": ("批次", "Batch"),
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
    "diagnostics_note": ("后续提供诊断详情。", "Diagnostic details will follow."),
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
    QWidget#parameters QScrollArea {{ background: {Style.background}; border: 1px solid {Style.border}; }}
    QWidget#parameters QGroupBox {{ background: {Style.surface}; border: 1px solid {Style.border}; border-radius: 8px; margin-top: 18px; padding: 12px; }}
    QWidget#parameters QGroupBox::title {{ subcontrol-origin: margin; left: 12px; color: {Style.accent}; }}
    QWidget#parameters QPlainTextEdit {{ background: {Style.background}; border: 1px solid {Style.border}; color: {Style.primary}; }}
    QWidget#parameters QCheckBox {{ background: transparent; spacing: 8px; }}
    QWidget#parameters QCheckBox::indicator {{ width: 20px; height: 20px; }}
    QWidget#parameters QLineEdit:disabled {{ color: {Style.secondary}; }}
    QWidget#parameters QScrollBar:vertical {{ background: {Style.background}; width: 16px; }}
    QWidget#parameters QScrollBar::handle:vertical {{ background: {Style.raised}; min-height: 40px; border-radius: 6px; }}
    QWidget#parameters QScrollBar::add-line:vertical, QWidget#parameters QScrollBar::sub-line:vertical {{ height: 0px; }}
    QWidget#parameters QScrollBar::add-page:vertical, QWidget#parameters QScrollBar::sub-page:vertical {{ background: none; }}
    """


def navigation_icon(index: int) -> QIcon:
    """Internal vector strokes, rendered at common DPRs; no OS icon theme/font."""
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
        elif index in (1, 4):
            for i in range(3):
                x = 4 + i * 7
                painter.drawLine(QPointF(x, 3), QPointF(x, 19))
                painter.drawEllipse(QPointF(x, 7 if (i + index) % 2 else 14), 2, 2)
        elif index == 2:
            points = (
                QPointF(2, 12),
                QPointF(6, 12),
                QPointF(9, 4),
                QPointF(13, 18),
                QPointF(16, 11),
                QPointF(20, 11),
            )
            for a, b in zip(points, points[1:], strict=False):
                painter.drawLine(a, b)
        else:
            painter.drawEllipse(QRectF(3, 3, 16, 16))
            painter.drawLine(QPointF(11, 6), QPointF(11, 11))
            painter.drawLine(QPointF(11, 11), QPointF(16, 13))
        painter.end()
        icon.addPixmap(pixmap)
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
