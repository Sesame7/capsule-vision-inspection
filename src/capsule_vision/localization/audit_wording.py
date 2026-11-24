"""Approved Chinese and English captions scoped to Audit presentation."""

from .catalog import text
from .languages import Language

_CAPTIONS: dict[str, tuple[str, str]] = {
    "identity_login": ("登录", "Sign in"),
    "identity_logout": ("退出登录", "Logout"),
    "identity_auto_login": ("启动时自动登录", "Startup auto-login"),
    "identity_create": ("创建账户", "Create account"),
    "identity_enable": ("启用账户", "Enable account"),
    "identity_disable": ("停用账户", "Disable account"),
    "account.enabled": ("更改账户状态", "Change account status"),
    "identity_reset": ("重置为临时密码", "Reset to temporary password"),
    "identity_set_auto_login": ("授权启动时自动登录", "Authorize startup auto-login"),
    "identity_disable_auto_login": (
        "撤销启动自动登录授权",
        "Revoke startup auto-login",
    ),
    "identity_setup": ("首次管理员设置", "First Administrator setup"),
    "replay_start": ("启动检测", "Start inspection"),
    "production_stop": ("停止检测", "Stop inspection"),
    "recipe": ("选择生产配方", "Select Production Recipe"),
    "batch.select": ("选择批次", "Select Batch"),
    "batch.end": ("结束批次选择", "End Batch selection"),
    "recipe.save": ("保存配方定义", "Save Recipe definition"),
    "model.stage": ("暂存并检查模型", "Stage Model import"),
    "model.import": ("模型导入", "Model import"),
    "vision.sources": ("保存相机源设置", "Save camera source settings"),
    "vision.layout": ("保存设备布局", "Save machine layout"),
    "records_create_alarm_file": ("创建本地报警文件", "Create local alarm file"),
    "comm_save_settings": ("保存通讯设置", "Save communication settings"),
    "communication.web_power": ("保存 Web 启用选项", "Save Web on/off setting"),
    "recipe.process_apply": ("应用配方 PLC 参数", "Apply Recipe PLC values"),
    "recipe.process_value": ("写入并回读工艺参数", "Write and read back process value"),
    "security.admission": ("操作授权检查", "Command authorization check"),
    "security.storage": ("账户存储异常", "Account storage issue"),
    "history.storage": ("生产历史存储异常", "Production History storage issue"),
    "records_user": ("账户", "Account"),
    "records_layout": ("设备布局", "Machine layout"),
    "audit_process_parameters": ("配方工艺参数", "Recipe process parameters"),
    "audit_process_parameter": ("工艺参数", "Process parameter"),
    "audit_account_storage": ("账户存储", "Account storage"),
    "audit_production_history": ("生产历史", "Production History"),
    "audit_preferences": ("应用首选项", "Application preferences"),
    "audit_success": ("成功", "Success"),
    "audit_denied": ("已拒绝", "Denied"),
    "audit_stale": ("请求已失效", "Superseded"),
    "records_uncertain": ("执行结果不确定", "Execution uncertain"),
    "records_unresolved": ("无完成记录", "No completion recorded"),
    "audit_unresolved": ("无完成记录", "No completion recorded"),
    "reasons": ("原因", "Reason"),
    "batch_id": ("批号", "Batch label"),
    "audit_previous_batch": ("原批号", "Previous Batch label"),
    "product_code": ("产品代码", "Product code"),
    "audit_web_enabled": ("启用选项", "Enabled setting"),
    "audit_recipe_id": ("配方 ID", "Recipe ID"),
    "audit_module_id": ("模块 ID", "Module ID"),
    "audit_from_template": ("从模板创建", "Created from template"),
    "audit_approved_values": (
        "本报告省略了部分值。",
        "Some values are omitted from this report.",
    ),
    "identity_audit_incomplete": ("已记录的值不完整", "Recorded values are incomplete"),
    "identity_invalid": (
        "凭据无效或账户不可用。",
        "Invalid credentials or account unavailable.",
    ),
    "identity_storage": ("账户存储不可用", "Account storage unavailable"),
    "identity_stale": ("请求已失效", "Request superseded"),
    "identity_conflict": ("账户状态冲突", "Account conflict"),
    "identity_rotate": ("新旧密码相同", "New and previous passwords match"),
    "audit_required": ("无法确认审计记录", "Audit confirmation unavailable"),
    "audit_identity_check_failed": ("身份校验未通过", "Identity check failed"),
    "audit_authorization_denied": ("操作未获准", "Operation denied"),
    "audit_access_denied": ("访问被拒绝", "Access denied"),
    "audit_state_unavailable": (
        "当前状态无法执行此操作",
        "Operation unavailable in the current state",
    ),
    "audit_start_unavailable": ("当前无法启动检测", "Start unavailable"),
    "audit_validation_failed": ("校验失败", "Validation failed"),
    "audit_file_error": ("文件或系统操作失败", "File or system operation failed"),
    "plc_reporting_write_protected": (
        "写入已被阻止，地址受保护",
        "Write blocked for a protected address",
    ),
    "audit_plc_rejected": ("PLC 拒绝写入", "PLC rejected the write"),
    "audit_plc_timeout": ("PLC 通讯超时", "PLC communication timed out"),
    "audit_plc_unknown": ("无法确认是否写入", "Write execution unknown"),
    "audit_plc_acknowledged": ("PLC 已确认写入请求", "PLC acknowledged the write"),
    "audit_registers_matched": (
        "所请求的寄存器值回读一致",
        "Requested register values matched on readback",
    ),
    "audit_readback_matched": ("回读一致", "Readback matched"),
}


def has_approved_wording(language: Language) -> bool:
    return language in ("zh", "en")


def audit_text(key: str, language: Language) -> str:
    if has_approved_wording(language) and (caption := _CAPTIONS.get(key)) is not None:
        return caption[0 if language == "zh" else 1]
    return text(key, language)
