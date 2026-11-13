"""Account forms present the local service; they own no account or worker state."""

from collections.abc import Callable
from concurrent.futures import Future
from typing import Literal

from PySide6.QtCore import QEvent, QObject, QSignalBlocker, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QResizeEvent, QShowEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QBoxLayout,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLayout,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from capsule_vision.application.audit import AuditUnavailable
from capsule_vision.application.identity import LocalIdentity
from capsule_vision.application.identity_models import (
    Account,
    AccountConflictError,
    AccountManagementSnapshot,
    AuthenticationError,
    AuthorizationError,
    IdentityError,
    IdentityValidationError,
    LastAdministratorError,
    PasswordReuseError,
    Role,
    SecurityBusyError,
    SecurityStorageError,
    SessionSnapshot,
    StaleSessionError,
    StorageState,
)
from capsule_vision.application.passwords import weak_password
from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import Language

from .future_delivery import FutureDelivery
from .shell import NavigationBar
from .style import Style, production_icon, theme_colors

_ROLES = {
    Role.GUEST: "identity_guest",
    Role.OPERATOR: "identity_operator",
    Role.ENGINEER: "identity_engineer",
    Role.ADMINISTRATOR: "identity_administrator",
}
FormKind = Literal["login", "setup", "change", "rotate", "create", "reset", "recover"]
_USER_GLOBAL_ACTIONS = ("identity_create", "short_refresh", "identity_set_auto_login")
_AUTO_LOGIN_SHORT = {
    "identity_auto_login": "short_auto_login",
    "identity_set_auto_login": "short_set_auto_login",
    "identity_disable_auto_login": "short_clear_auto_login",
}


def _users_caption(key: str, language: Language) -> str:
    # Other languages keep their existing native identity wording.
    return text(
        _AUTO_LOGIN_SHORT.get(key, key) if language in ("zh", "en") else key, language
    )


def identity_feedback(error: object) -> str:
    # Never echo exception messages, passwords, SQLite text or paths into widgets.
    for kind, key in (
        (AuditUnavailable, "audit_required"),
        (AuthenticationError, "identity_invalid"),
        (LastAdministratorError, "identity_last_admin"),
        (AccountConflictError, "identity_conflict"),
        (PasswordReuseError, "identity_rotate"),
        (IdentityValidationError, "identity_validation"),
        (StaleSessionError, "identity_stale"),
        (AuthorizationError, "identity_denied"),
        (SecurityStorageError, "identity_storage"),
        (SecurityBusyError, "identity_busy"),
    ):
        if isinstance(error, kind):
            return key
    return "identity_error"


class _AccountDialog(QDialog):
    def __init__(self, parent: QWidget | None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(420)
        self.body = QVBoxLayout(self)
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.status_key = ""

    def feedback(self, key: str, language: Language) -> None:
        self.status_key = key
        self.status.setText(text(key, language) if key else "")
        self.status.setAccessibleName(self.status.text())
        self.status.setVisible(bool(key))


class CredentialDialog(_AccountDialog):
    """Modeless, masked and cleared at submission and dismissal."""

    def __init__(
        self, kind: FormKind, account: Account | None, parent: QWidget | None
    ) -> None:
        super().__init__(parent)
        self.kind, self.account = kind, account
        self.revision = 0
        self.busy = False
        self.form = QFormLayout()
        self.body.addLayout(self.form)
        self.fields: dict[str, QLineEdit] = {}
        keys = ["identity_new_password", "identity_confirm"]
        if kind == "login":
            keys = ["identity_username", "identity_password"]
        elif kind == "change":
            keys.insert(0, "identity_old_password")
        elif kind in ("create", "setup", "recover"):
            keys.insert(0, "identity_username")
        self.labels: dict[str, QLabel] = {}
        if account is not None:
            target = QLabel(account.username)
            target.setTextFormat(Qt.TextFormat.PlainText)
            target.setWordWrap(True)
            self.body.insertWidget(0, target)
        for key in keys:
            field = QLineEdit()
            field.setMinimumHeight(Style.touch)
            if key != "identity_username":
                field.setEchoMode(QLineEdit.EchoMode.Password)
                field.setMaxLength(1024)
            else:
                field.setMaxLength(64)
            label = QLabel()
            label.setBuddy(field)
            self.form.addRow(label, field)
            self.fields[key], self.labels[key] = field, label
        self.role = QComboBox(self)
        self.role.setMinimumHeight(Style.touch)
        for role in (Role.OPERATOR, Role.ENGINEER, Role.ADMINISTRATOR):
            self.role.addItem("", role)
        self.role_label = QLabel(self)
        self.role_label.setBuddy(self.role)
        if kind == "create":
            self.form.addRow(self.role_label, self.role)
        else:
            self.role.hide()
            self.role_label.hide()
        self.guidance = QLabel()
        self.guidance.setWordWrap(True)
        self.guidance.setVisible(kind != "login")
        self.body.addWidget(self.guidance)
        self.weak = QCheckBox()
        self.grant = QCheckBox()
        for check in (self.weak, self.grant):
            check.setMinimumHeight(Style.touch)
            self.body.addWidget(check)
            check.hide()
            check.toggled.connect(self._enable_submission)
        self.body.addWidget(self.status)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.body.addWidget(self.buttons)
        self.submit = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        for button in self.buttons.buttons():
            button.setMinimumHeight(Style.touch)
        self.buttons.rejected.connect(self.reject)
        self.finished.connect(self.clear_secrets)
        if kind != "login":
            for key in ("identity_username", "identity_new_password"):
                if key in self.fields:
                    self.fields[key].textChanged.connect(self._choices_changed)
            self.role.currentIndexChanged.connect(self._choices_changed)

    @Slot()
    def _choices_changed(self) -> None:
        field = self.fields.get("identity_new_password")
        name = self.fields.get("identity_username")
        username = (
            name.text() if name else self.account.username if self.account else ""
        )
        self.weak.setVisible(
            bool(field and field.text() and weak_password(field.text(), username))
        )
        self.grant.setVisible(
            self.kind == "create" and self.role.currentData() == Role.ADMINISTRATOR
        )
        self.weak.setChecked(False)
        self.grant.setChecked(False)
        self._enable_submission()

    @Slot()
    def _enable_submission(self) -> None:
        self.submit.setEnabled(
            not self.busy
            and all(c.isHidden() or c.isChecked() for c in (self.weak, self.grant))
        )

    def submission(self, language: Language) -> dict[str, str] | None:
        if self.busy or not self.submit.isEnabled():
            return None
        values = {key: field.text() for key, field in self.fields.items()}
        self.clear_secrets()
        if (
            "identity_confirm" in values
            and values["identity_confirm"] != values["identity_new_password"]
        ):
            self.feedback("identity_mismatch", language)
            return None
        self.set_busy(True, language)
        return values

    @Slot()
    def clear_secrets(self) -> None:
        for key, field in self.fields.items():
            if key != "identity_username":
                field.clear()

    def present(self, language: Language) -> None:
        key = "identity_reset" if self.kind == "recover" else f"identity_{self.kind}"
        self.setWindowTitle(text(key, language))
        self.submit.setText(
            text("identity_change" if self.kind == "rotate" else key, language)
        )
        self.guidance.setText(text("identity_guidance", language))
        self.weak.setText(text("identity_weak", language))
        self.grant.setText(text("identity_grant", language))
        for key, label in self.labels.items():
            label.setText(text(key, language))
            self.fields[key].setAccessibleName(label.text())
        self.role_label.setText(text("identity_role", language))
        self.role.setAccessibleName(self.role_label.text())
        for index, role in enumerate(
            (Role.OPERATOR, Role.ENGINEER, Role.ADMINISTRATOR)
        ):
            self.role.setItemText(index, text(_ROLES[role], language))
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(
            text("cancel", language)
        )
        self.feedback(self.status_key, language)

    def set_busy(self, busy: bool, language: Language) -> None:
        self.busy = busy
        for field in self.fields.values():
            field.setEnabled(not busy)
        self.role.setEnabled(not busy)
        self._enable_submission()
        self.feedback("identity_working" if busy else "", language)


class UsersDialog(_AccountDialog):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.resize(850, 500)
        self.accounts: tuple[Account, ...] = ()
        self.auto_login_account_id: str | None = None
        self._installed = False
        self._first_show = True
        self._layout_timer = QTimer(self)
        self._layout_timer.setSingleShot(True)
        self._layout_timer.timeout.connect(self._reflow)
        self.busy = False
        self.language: Language = "en"
        self.controls: dict[str, QPushButton] = {}
        self.table = QTableWidget(0, 4)
        self.table.setObjectName("accounts")
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(Style.touch)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.horizontalHeader().setMinimumSectionSize(0)
        self.table.horizontalHeader().setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self.table.horizontalHeader().setTextElideMode(Qt.TextElideMode.ElideRight)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.table.setMinimumHeight(5 * Style.touch + 36)
        self.body.addWidget(self.table, 1)
        self.action_area = QWidget()
        self.action_area.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed
        )
        self._action_grid = QGridLayout(self.action_area)
        self._action_grid.setContentsMargins(0, 0, 0, 0)
        self._action_grid.setHorizontalSpacing(18)
        self._action_grid.setVerticalSpacing(8)
        self._action_grid.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self._action_grid.setColumnStretch(1, 1)
        self.global_actions = QWidget()
        self.account_actions = QWidget()
        self._global_row = QHBoxLayout(self.global_actions)
        self._account_row = QHBoxLayout(self.account_actions)
        for row in (self._global_row, self._account_row):
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(6)
        for key in (
            "identity_create",
            "short_refresh",
            "identity_change_role",
            "identity_disable",
            "identity_reset",
            "identity_set_auto_login",
        ):
            button = QPushButton()
            button.setMinimumHeight(Style.touch)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            row = self._global_row if key in _USER_GLOBAL_ACTIONS else self._account_row
            row.addWidget(button)
            self.controls[key] = button
            button.installEventFilter(self)
        self.controls["short_refresh"].setFixedWidth(Style.touch)
        self.controls["identity_create"].setObjectName("start")
        self._action_grid.addWidget(
            self.global_actions, 0, 0, Qt.AlignmentFlag.AlignLeft
        )
        self._action_grid.addWidget(self.account_actions, 0, 1)
        self._action_mode = (False, False, False)
        self.body.addWidget(self.action_area)
        self.body.addWidget(self.status)
        self.table.itemSelectionChanged.connect(self.selection_changed)
        for widget in (self.table, self.table.viewport(), self.action_area):
            widget.installEventFilter(self)

    def selected(self) -> Account | None:
        item = self.table.currentItem() if self.table.selectedItems() else None
        account_id = item.data(Qt.ItemDataRole.UserRole) if item else None
        return next((a for a in self.accounts if a.account_id == account_id), None)

    def _is_auto_login(self, account: Account) -> bool:
        return (
            account.auto_login_eligible
            and account.account_id == self.auto_login_account_id
        )

    @Slot()
    def selection_changed(self) -> None:
        self.set_busy(self.busy)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._queue_layout()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if self._first_show:
            self._first_show = False
            available = self.screen().availableGeometry()
            frame = self.frameGeometry().size() - self.size()
            self.resize(self.size().boundedTo(available.size() - frame))
            geometry = self.frameGeometry()
            self.move(
                max(
                    available.left(),
                    min(geometry.x(), available.right() - geometry.width() + 1),
                ),
                max(
                    available.top(),
                    min(geometry.y(), available.bottom() - geometry.height() + 1),
                ),
            )
        self._queue_layout()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() in (
            QEvent.Type.Resize,
            QEvent.Type.FontChange,
            QEvent.Type.ApplicationFontChange,
            QEvent.Type.StyleChange,
        ):
            self._queue_layout()
        return super().eventFilter(watched, event)

    def _queue_layout(self) -> None:
        if not self._layout_timer.isActive():
            self._layout_timer.start(0)

    @Slot()
    def _reflow(self) -> None:
        self._layout_actions()
        self._layout_columns()

    def _layout_actions(self) -> None:
        variants = {
            "identity_disable": ("identity_enable", "identity_disable"),
            "identity_set_auto_login": (
                "identity_set_auto_login",
                "identity_disable_auto_login",
            ),
        }
        for key, button in self.controls.items():
            if key == "short_refresh":
                continue
            button.setMinimumHeight(
                max(Style.touch, button.fontMetrics().height() + 14)
            )
            captions = tuple(
                _users_caption(k, self.language) for k in variants.get(key, ())
            )
            width = max(
                104,
                max(
                    button.fontMetrics().horizontalAdvance(c)
                    for c in captions or (button.text(),)
                )
                + 28,
            )
            if key == "identity_set_auto_login":
                button.setFixedWidth(width)
            else:
                button.setMinimumWidth(width)
        widths = {
            key: max(button.minimumWidth(), button.sizeHint().width())
            for key, button in self.controls.items()
        }
        global_width = sum(widths[key] for key in _USER_GLOBAL_ACTIONS) + (
            self._global_row.spacing() * (self._global_row.count() - 1)
        )
        account_width = sum(
            width for key, width in widths.items() if key not in _USER_GLOBAL_ACTIONS
        ) + self._account_row.spacing() * (self._account_row.count() - 1)
        width = self.action_area.width()
        wrapped = width < global_width + 18 + account_width
        mode = wrapped, width < global_width, width < account_width
        if mode != self._action_mode:
            self._action_mode = mode
            self._action_grid.removeWidget(self.global_actions)
            self._action_grid.removeWidget(self.account_actions)
            for row, stacked in zip(
                (self._global_row, self._account_row), mode[1:], strict=True
            ):
                row.setDirection(
                    QBoxLayout.Direction.TopToBottom
                    if stacked
                    else QBoxLayout.Direction.LeftToRight
                )
            if wrapped:
                self._action_grid.addWidget(
                    self.global_actions, 0, 0, 1, 2, Qt.AlignmentFlag.AlignLeft
                )
                self._action_grid.addWidget(self.account_actions, 1, 0, 1, 2)
            else:
                self._action_grid.addWidget(
                    self.global_actions, 0, 0, Qt.AlignmentFlag.AlignLeft
                )
                self._action_grid.addWidget(self.account_actions, 0, 1)

    def _layout_columns(self) -> None:
        width = self.table.viewport().width()
        if width <= 0:
            return
        # Round shared edges for 30:19:22:29 so the Status column has room for
        # localized captions while the columns still fill the viewport exactly.
        edges = [width * percent // 100 for percent in (0, 30, 49, 71, 100)]
        for column in range(4):
            value = edges[column + 1] - edges[column]
            if self.table.columnWidth(column) != value:
                self.table.setColumnWidth(column, value)
        row_height = max(Style.touch, self.table.fontMetrics().height() + 12)
        if self.table.verticalHeader().defaultSectionSize() != row_height:
            self.table.verticalHeader().setDefaultSectionSize(row_height)

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        selected = self.selected()
        editable = selected is not None and selected.role is not Role.ADMINISTRATOR
        self.table.setEnabled(not busy)
        for key, button in self.controls.items():
            button.setEnabled(
                not busy and (key in ("identity_create", "short_refresh") or editable)
            )
        self.controls["identity_disable"].setText(
            text(
                "identity_disable"
                if selected and selected.enabled
                else "identity_enable",
                self.language,
            )
        )
        auto_login = self.controls["identity_set_auto_login"]
        auto_login.setEnabled(
            not busy and selected is not None and selected.auto_login_eligible
        )
        key = (
            "identity_disable_auto_login"
            if selected is not None and self._is_auto_login(selected)
            else "identity_set_auto_login"
        )
        caption = text(key, self.language)
        auto_login.setText(_users_caption(key, self.language))
        auto_login.setAccessibleName(caption)
        auto_login.setToolTip(caption)
        for key in ("identity_change_role", "identity_disable", "identity_reset"):
            button = self.controls[key]
            button.setAccessibleName(button.text())
            button.setToolTip(
                text("identity_denied", self.language)
                if selected is not None and not editable
                else button.text()
            )
        self._queue_layout()

    def install_accounts(
        self, accounts: tuple[Account, ...], auto_login_account_id: str | None = None
    ) -> None:
        previous = self.selected()
        self.accounts = accounts
        self.auto_login_account_id = auto_login_account_id
        with QSignalBlocker(self.table):
            self.table.setRowCount(0)
            self.table.setRowCount(len(accounts))
            for row, account in enumerate(accounts):
                for column in range(4):
                    item = QTableWidgetItem()
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                    )
                    item.setData(Qt.ItemDataRole.UserRole, account.account_id)
                    self.table.setItem(row, column, item)
                if previous and previous.account_id == account.account_id:
                    self.table.setCurrentCell(row, 0)
            if not self._installed and accounts:
                self.table.setCurrentCell(0, 0)
        self._installed = True
        self.present(self.language)
        self.selection_changed()

    def present(self, language: Language) -> None:
        self.language = language
        self.setWindowTitle(text("identity_manage", language))
        self.table.setAccessibleName(self.windowTitle())
        self.table.setHorizontalHeaderLabels(
            [
                text("identity_username", language),
                text("identity_role", language),
                text("identity_status", language),
                text("identity_last_login", language),
            ]
        )
        for column in range(4):
            item = self.table.horizontalHeaderItem(column)
            assert item is not None
            item.setTextAlignment(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
            )
            item.setToolTip(item.text())
            item.setData(Qt.ItemDataRole.AccessibleTextRole, item.text())
        for index, account in enumerate(self.accounts):
            status = text(
                "identity_enabled" if account.enabled else "identity_disabled", language
            )
            authorized = self._is_auto_login(account)
            values = (
                account.username,
                text(_ROLES[account.role], language),
                _users_caption("identity_auto_login", language)
                if authorized
                else status,
                account.last_login_utc.astimezone().strftime("%Y-%m-%d %H:%M")
                if account.last_login_utc is not None
                else "—",
            )
            for column, value in enumerate(values):
                item = self.table.item(index, column)
                assert item is not None
                detail = (
                    f"{status} · {text('identity_auto_login', language)}"
                    if column == 2 and authorized
                    else value
                )
                item.setText(value)
                item.setToolTip(detail)
                item.setData(Qt.ItemDataRole.AccessibleTextRole, detail)
        for key, button in self.controls.items():
            if key == "identity_set_auto_login":
                continue
            caption = text(
                "identity_new_user" if key == "identity_create" else key, language
            )
            button.setText("" if key == "short_refresh" else caption)
            button.setAccessibleName(caption)
            button.setToolTip(caption)
        self.controls["short_refresh"].setIcon(
            production_icon("reset", theme_colors(self).primary)
        )
        self.set_busy(self.busy)
        self.feedback(self.status_key, language)


class RoleDialog(_AccountDialog):
    """Capture a stable target independently of subsequent table selection."""

    def __init__(self, account: Account, revision: int, parent: QWidget) -> None:
        super().__init__(parent)
        self.account, self.revision = account, revision
        self.busy = False
        target = QLabel(account.username)
        target.setTextFormat(Qt.TextFormat.PlainText)
        target.setWordWrap(True)
        target.setToolTip(account.username)
        target.setAccessibleName(account.username)
        self.body.addWidget(target)
        self.role = QComboBox()
        self.role.setMinimumHeight(Style.touch)
        for role in (Role.OPERATOR, Role.ENGINEER, Role.ADMINISTRATOR):
            self.role.addItem("", role)
        self.role.setCurrentIndex(self.role.findData(account.role))
        self.body.addWidget(self.role)
        self.grant = QCheckBox()
        self.grant.setMinimumHeight(Style.touch)
        self.body.addWidget(self.grant)
        self.body.addWidget(self.status)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        self.submit = self.buttons.button(QDialogButtonBox.StandardButton.Save)
        for button in self.buttons.buttons():
            button.setMinimumHeight(Style.touch)
        self.body.addWidget(self.buttons)
        self.buttons.rejected.connect(self.reject)
        self.role.currentIndexChanged.connect(self._role_changed)
        self.grant.toggled.connect(self._selection_changed)

    @Slot()
    def _role_changed(self) -> None:
        self.grant.setVisible(self.role.currentData() == Role.ADMINISTRATOR)
        self.grant.setChecked(False)
        self._selection_changed()

    @Slot()
    def _selection_changed(self) -> None:
        self.submit.setEnabled(
            not self.busy
            and self.role.currentData() != self.account.role
            and (
                self.role.currentData() != Role.ADMINISTRATOR or self.grant.isChecked()
            )
        )

    def set_busy(self, busy: bool, language: Language) -> None:
        self.busy = busy
        self.role.setEnabled(not busy)
        self.grant.setEnabled(not busy)
        self._selection_changed()
        self.feedback("identity_working" if busy else "", language)

    def present(self, language: Language) -> None:
        self.setWindowTitle(text("identity_change_role", language))
        self.grant.setText(text("identity_grant", language))
        self.grant.setVisible(self.role.currentData() == Role.ADMINISTRATOR)
        self.role.setAccessibleName(text("identity_role", language))
        for index, role in enumerate(
            (Role.OPERATOR, Role.ENGINEER, Role.ADMINISTRATOR)
        ):
            self.role.setItemText(index, text(_ROLES[role], language))
        self.submit.setText(text("save", language))
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(
            text("cancel", language)
        )
        self._selection_changed()
        self.feedback(self.status_key, language)


class DisableDialog(QMessageBox):
    def __init__(self, account: Account, revision: int, parent: QWidget) -> None:
        super().__init__(parent)
        self.account, self.revision = account, revision
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setIcon(QMessageBox.Icon.Question)
        self.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel
        )
        self.setDefaultButton(QMessageBox.StandardButton.Cancel)
        for button in self.buttons():
            button.setMinimumHeight(Style.touch)

    def present(self, language: Language) -> None:
        self.setWindowTitle(text("identity_disable", language))
        self.setText(
            text("identity_confirm_disable", language).format(
                username=self.account.username
            )
        )
        self.setAccessibleName(self.text())
        self.button(QMessageBox.StandardButton.Yes).setText(
            text("identity_disable", language)
        )
        self.button(QMessageBox.StandardButton.Cancel).setText(text("cancel", language))


class IdentityControls(QObject):
    """One bounded UI request, queued delivery and service-owned session truth."""

    session_changed = Signal()

    def __init__(
        self, navbar: NavigationBar, owner: LocalIdentity | None, parent: QWidget
    ) -> None:
        super().__init__(parent)
        self.navbar, self.owner, self.window = navbar, owner, parent
        self._presented_revision: int | None = None
        self.language: Language = "en"
        self.form: CredentialDialog | None = None
        self.users: UsersDialog | None = None
        self.role_dialog: RoleDialog | None = None
        self.confirmation: DisableDialog | None = None
        self._serial = 0
        self._pending: tuple[str, int] | None = None
        self._login_attempt: Future[SessionSnapshot] | None = None
        self._closing = False
        self._delivery = FutureDelivery()
        # Detach fences worker callbacks before QObject destroys this sender on
        # Qt's thread, even if Python later collects a completed Future elsewhere.
        self._delivery.emitter.setParent(self)
        self._delivery.emitter.completed.connect(
            self._completed, Qt.ConnectionType.QueuedConnection
        )
        self.destroyed.connect(
            self._delivery.detach, Qt.ConnectionType.DirectConnection
        )
        self.menu = QMenu(navbar.user)
        navbar.user.setMenu(self.menu)
        self.controls: dict[str, QAction] = {}
        for key, callback in (
            ("identity_setup", self.offer_setup),
            ("identity_login", self.login),
            ("identity_logout", self.logout),
            ("identity_switch", self.switch_user),
            ("identity_change", self.change_password),
            ("identity_manage", self.manage_users),
        ):
            action = self.menu.addAction("")
            action.triggered.connect(callback)
            self.controls[key] = action
        self.menu.aboutToShow.connect(self.refresh)
        self.refresh()

    def present(self, language: Language) -> None:
        self.language = language
        self.refresh()
        for dialog in (self.form, self.users, self.role_dialog, self.confirmation):
            if dialog is not None:
                dialog.present(language)

    @Slot()
    def refresh(self) -> None:
        snapshot = self.owner.snapshot() if self.owner else SessionSnapshot()
        if snapshot.revision != self._presented_revision:
            self._presented_revision = snapshot.revision
            self.session_changed.emit()
        account = snapshot.account
        detail = text(
            "identity_rotate" if snapshot.rotation_required else _ROLES[snapshot.role],
            self.language,
        )
        storage = self.owner.storage_snapshot() if self.owner else None
        state = storage.state if storage else StorageState.CLOSED
        uninitialized = bool(storage and storage.uninitialized)
        if state is not StorageState.READY:
            status = text(
                "identity_missing" if uninitialized else "identity_storage",
                self.language,
            )
            detail = f"{detail} · {status}"
        self.navbar.present_user(account.username if account else None, detail)
        self.navbar.user.setEnabled(not self._closing)
        for key, action in self.controls.items():
            action.setText(text(key, self.language))
            action.setVisible(
                (key == "identity_setup" and uninitialized)
                or (key == "identity_login" and account is None and not uninitialized)
                or (
                    key in ("identity_logout", "identity_change")
                    and account is not None
                )
                or (
                    key == "identity_switch"
                    and account is not None
                    and not snapshot.rotation_required
                )
                or (key == "identity_manage" and snapshot.role is Role.ADMINISTRATOR)
            )
            action.setEnabled(not self._closing and self.owner is not None)
        if self.users is not None and snapshot.role is not Role.ADMINISTRATOR:
            self.users.reject()
        for dialog in (self.role_dialog, self.confirmation):
            if dialog is not None and dialog.revision != snapshot.revision:
                dialog.reject()
        form = self.form
        if (
            form is not None
            and form.kind == "login"
            and state is not StorageState.READY
        ):
            form.submit.setEnabled(False)
        if (
            form is not None
            and form.kind not in ("login", "setup")
            and form.revision != snapshot.revision
        ):
            # A password commit can advance this same session before queued delivery.
            if not (
                form.busy
                and form.kind in ("change", "rotate")
                and form.account
                and account
                and account.account_id == form.account.account_id
                and not snapshot.rotation_required
            ):
                form.reject()
        if snapshot.rotation_required and self.form is None and not self._closing:
            self._open_form("rotate", account)

    def _invalidate(self) -> None:
        if self._login_attempt is not None and self.owner is not None:
            self.owner.cancel_login(self._login_attempt)
        self._login_attempt = None
        self._serial += 1
        self._pending = None

    def _dismiss(self) -> None:
        if self.form is not None:
            self.form.reject()
        if self.users is not None:
            self.users.reject()
        if self.role_dialog is not None:
            self.role_dialog.reject()
        if self.confirmation is not None:
            self.confirmation.reject()

    @Slot()
    def logout(self) -> None:
        self._invalidate()
        if self.owner is not None:
            self.owner.logout()
        self._dismiss()
        self.refresh()

    @Slot()
    def login(self) -> None:
        self._open_form("login")

    @Slot()
    def offer_setup(self) -> None:
        if self.owner is not None and self.owner.storage_snapshot().uninitialized:
            self._open_form("setup")

    @Slot()
    def switch_user(self) -> None:
        self.login()

    @Slot()
    def change_password(self) -> None:
        if self.owner is not None:
            snapshot = self.owner.snapshot()
            self._open_form(
                "rotate" if snapshot.rotation_required else "change", snapshot.account
            )

    def _open_form(self, kind: FormKind, account: Account | None = None) -> None:
        if self._closing or self.owner is None:
            return
        if self.form is not None:
            self.form.raise_()
            self.form.activateWindow()
            return
        if (
            self._pending is not None
            or self.role_dialog is not None
            or self.confirmation is not None
        ):
            return
        form = self.form = CredentialDialog(kind, account, self.window)
        if self.users is not None:
            self.users.set_busy(True)
        form.revision = self.owner.snapshot().revision
        if kind == "rotate":
            form.setWindowModality(Qt.WindowModality.WindowModal)
        form.present(self.language)
        form.buttons.accepted.connect(self._submit)
        form.finished.connect(self._form_finished)
        if (
            kind == "login"
            and self.owner.storage_snapshot().state is not StorageState.READY
        ):
            form.submit.setEnabled(False)
            form.feedback(
                "identity_missing"
                if self.owner.storage_snapshot().uninitialized
                else "identity_storage",
                self.language,
            )
        form.show()
        next(iter(form.fields.values())).setFocus()

    @Slot(int)
    def _form_finished(self, result: int) -> None:
        form, self.form = self.form, None
        if form is None:
            return
        if form.busy:
            self._invalidate()
        if (
            result == QDialog.DialogCode.Rejected
            and self.owner is not None
            and (form.kind == "rotate" or (form.kind == "setup" and form.busy))
        ):
            self.owner.logout(revision=form.revision)
        form.deleteLater()
        if self.users is not None:
            self.users.set_busy(False)
        self.refresh()

    @Slot()
    def _submit(self) -> None:
        form, owner = self.form, self.owner
        if form is None or owner is None or form.busy or self._closing:
            return
        confirmed = form.grant.isChecked()
        values = form.submission(self.language)
        if values is None:
            return

        if form.kind == "login":

            def authenticate() -> Future[SessionSnapshot]:
                self._login_attempt = owner.login(
                    values["identity_username"], values["identity_password"]
                )
                return self._login_attempt

            self._request("credential", authenticate)
        elif form.kind == "setup":
            self._request(
                "credential",
                lambda: owner.setup_administrator(
                    values["identity_username"],
                    values["identity_new_password"],
                    revision=form.revision,
                ),
            )
        elif form.kind == "rotate":
            self._request(
                "credential",
                lambda: owner.rotate_password(
                    values["identity_new_password"], form.revision
                ),
            )
        elif form.kind == "change":
            self._request(
                "credential",
                lambda: owner.change_password(
                    values["identity_old_password"],
                    values["identity_new_password"],
                    revision=form.revision,
                ),
            )
        elif form.kind == "create":
            role = Role(form.role.currentData())
            self._request(
                "credential",
                lambda: owner.create_account(
                    values["identity_username"],
                    role,
                    values["identity_new_password"],
                    confirm_administrator=confirmed,
                    revision=form.revision,
                ),
            )
        elif form.account is not None:
            account_id = form.account.account_id
            self._request(
                "credential",
                lambda: owner.reset_password(
                    account_id, values["identity_new_password"], revision=form.revision
                ),
            )

    def _request[T](self, kind: str, request: Callable[[], Future[T]]) -> None:
        if self._closing or self.owner is None or self._pending is not None:
            return
        self._serial += 1
        try:
            future = request()
        except IdentityError as error:
            self._show_error(error)
        else:
            self._pending = kind, self.owner.snapshot().revision
            self._delivery.watch(str(self._serial), future)
        self.refresh()

    def _show_error(self, error: object) -> None:
        if self.role_dialog is not None:
            self.role_dialog.set_busy(False, self.language)
            self.role_dialog.feedback(identity_feedback(error), self.language)
        elif self.form is not None:
            self.form.set_busy(False, self.language)
            self.form.feedback(identity_feedback(error), self.language)
        elif self.users is not None:
            self.users.set_busy(False)
            self.users.feedback(identity_feedback(error), self.language)

    @Slot()
    def manage_users(self) -> None:
        if self._closing or self.owner is None:
            return
        if self.users is not None:
            self.users.raise_()
            return
        if self._pending is not None:
            return
        users = self.users = UsersDialog(self.window)
        users.present(self.language)
        users.finished.connect(self._users_finished)
        users.controls["identity_create"].clicked.connect(
            lambda: self._open_form("create")
        )
        users.controls["identity_reset"].clicked.connect(self._reset_password)
        users.controls["identity_change_role"].clicked.connect(self._set_role)
        users.controls["identity_disable"].clicked.connect(self._set_enabled)
        users.controls["identity_set_auto_login"].clicked.connect(self._set_auto_login)
        users.controls["short_refresh"].clicked.connect(self._list_accounts)
        users.show()
        self._list_accounts()

    @Slot(int)
    def _users_finished(self, result: int) -> None:
        users, self.users = self.users, None
        if users is not None:
            if self.role_dialog is not None:
                self.role_dialog.reject()
            if self.confirmation is not None:
                self.confirmation.reject()
            if self.form is not None and self.form.kind in ("create", "reset"):
                self.form.reject()
            self._invalidate()
            users.deleteLater()

    @Slot()
    def _list_accounts(self) -> None:
        if self.owner is not None and self.users is not None:
            self.users.set_busy(True)
            self._request("list", self.owner.account_management)

    @Slot()
    def _set_auto_login(self) -> None:
        users, owner = self.users, self.owner
        account = users.selected() if users else None
        if (
            users is None
            or owner is None
            or account is None
            or users.busy
            or not account.auto_login_eligible
        ):
            return
        enable = account.account_id != users.auto_login_account_id
        revision = owner.snapshot().revision
        users.set_busy(True)
        self._request(
            "auto_login",
            lambda: owner.set_auto_login(account.account_id, enable, revision=revision),
        )

    @Slot()
    def _reset_password(self) -> None:
        account = self.users.selected() if self.users else None
        if account is not None and account.role is not Role.ADMINISTRATOR:
            self._open_form("reset", account)

    @Slot()
    def _set_role(self) -> None:
        users, owner = self.users, self.owner
        account = users.selected() if users else None
        if (
            users is not None
            and owner is not None
            and account is not None
            and account.role is not Role.ADMINISTRATOR
            and not users.busy
        ):
            dialog = self.role_dialog = RoleDialog(
                account, owner.snapshot().revision, users
            )
            users.set_busy(True)
            dialog.present(self.language)
            dialog.buttons.accepted.connect(self._submit_role)
            dialog.finished.connect(self._role_finished)
            dialog.show()
            dialog.role.setFocus()

    @Slot()
    def _submit_role(self) -> None:
        dialog, owner = self.role_dialog, self.owner
        if dialog is None or owner is None or dialog.busy or self._closing:
            return
        if dialog.revision != owner.snapshot().revision:
            self.refresh()
            return
        role = Role(dialog.role.currentData())
        if role == dialog.account.role or not dialog.submit.isEnabled():
            return
        dialog.set_busy(True, self.language)
        self._request(
            "role",
            lambda: owner.set_role(
                dialog.account.account_id,
                role,
                confirm_administrator=dialog.grant.isChecked(),
                revision=dialog.revision,
            ),
        )

    @Slot(int)
    def _role_finished(self, result: int) -> None:
        dialog, self.role_dialog = self.role_dialog, None
        if dialog is not None:
            if dialog.busy:
                self._invalidate()  # Fence delivery; the accepted service work still completes.
            dialog.deleteLater()
            if self.users is not None:
                self.users.set_busy(False)

    @Slot()
    def _set_enabled(self) -> None:
        users, owner = self.users, self.owner
        account = users.selected() if users else None
        if (
            users is not None
            and owner is not None
            and account is not None
            and account.role is not Role.ADMINISTRATOR
            and not users.busy
        ):
            users.set_busy(True)
            if account.enabled:
                dialog = self.confirmation = DisableDialog(
                    account, owner.snapshot().revision, users
                )
                dialog.present(self.language)
                dialog.finished.connect(self._disable_finished)
                dialog.show()
            else:
                self._request(
                    "update", lambda: owner.set_enabled(account.account_id, True)
                )

    @Slot(int)
    def _disable_finished(self, result: int) -> None:
        dialog, self.confirmation = self.confirmation, None
        if dialog is None:
            return
        account, revision = dialog.account, dialog.revision
        dialog.deleteLater()
        owner, users = self.owner, self.users
        if users is None or owner is None:
            return
        if (
            result == QMessageBox.StandardButton.Yes
            and not self._closing
            and revision == owner.snapshot().revision
        ):
            self._request(
                "update",
                lambda: owner.set_enabled(account.account_id, False, revision=revision),
            )
        else:
            users.set_busy(False)

    @Slot(str, object, object)
    def _completed(self, serial: str, value: object, error: object) -> None:
        if self._closing or serial != str(self._serial) or self._pending is None:
            return
        kind, revision = self._pending
        self._pending = None
        self._login_attempt = None
        snapshot = self.owner.snapshot() if self.owner else SessionSnapshot()
        if isinstance(value, SessionSnapshot) and value != snapshot:
            error = StaleSessionError()
        if kind == "list" and revision != snapshot.revision:
            error = StaleSessionError()
        if error is not None:
            self._show_error(error)
            self.refresh()
            return
        if kind == "credential" and self.form is not None:
            self.form.busy = False
            self.form.accept()
        if kind == "role" and self.role_dialog is not None:
            self.role_dialog.busy = False
            self.role_dialog.accept()
        if (
            kind == "list"
            and self.users is not None
            and isinstance(value, AccountManagementSnapshot)
        ):
            self.users.install_accounts(value.accounts, value.auto_login_account_id)
            self.users.set_busy(False)
            self.users.feedback("", self.language)
        self.refresh()
        if kind != "list" and self.users is not None:
            # The refreshed Status cells confirm an auto-login change.
            if kind != "auto_login":
                self.users.feedback("identity_done", self.language)
            self._list_accounts()

    def begin_close(self) -> None:
        self._closing = True
        self.menu.hide()
        self.logout()

    def resume(self) -> None:
        self._closing = False
        self.refresh()

    def dispose(self) -> None:
        self.begin_close()
        self._delivery.detach()
