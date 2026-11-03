"""Account forms present the local service; they own no account or worker state."""

from collections.abc import Callable
from concurrent.futures import Future
from typing import Literal, cast

from PySide6.QtCore import QEvent, QObject, QSignalBlocker, Qt, QTimer, Slot
from PySide6.QtGui import QAction, QResizeEvent, QShowEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QBoxLayout,
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

from capsule_vision.application.identity import LocalIdentity
from capsule_vision.application.identity_models import (
    Account,
    AccountConflictError,
    AuthenticationError,
    AuthorizationError,
    IdentityError,
    IdentityValidationError,
    LastAdministratorError,
    Role,
    SecurityBusyError,
    SecurityStorageError,
    SessionSnapshot,
    StaleSessionError,
    StorageState,
)
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
FormKind = Literal["login", "change", "create", "reset"]


def _feedback(error: object) -> str:
    # Never echo exception messages, passwords, SQLite text or paths into widgets.
    for kind, key in (
        (AuthenticationError, "identity_invalid"),
        (LastAdministratorError, "identity_last_admin"),
        (AccountConflictError, "identity_conflict"),
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
    def __init__(self, parent: QWidget) -> None:
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
        self, kind: FormKind, account: Account | None, parent: QWidget
    ) -> None:
        super().__init__(parent)
        self.kind, self.account = kind, account
        self.actor_id: str | None = None
        self.busy = False
        self.form = QFormLayout()
        self.body.addLayout(self.form)
        self.fields: dict[str, QLineEdit] = {}
        keys = {
            "login": ("identity_username", "identity_password"),
            "change": (
                "identity_old_password",
                "identity_new_password",
                "identity_confirm",
            ),
            "create": (
                "identity_username",
                "identity_new_password",
                "identity_confirm",
            ),
            "reset": ("identity_new_password", "identity_confirm"),
        }[kind]
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

    @Slot()
    def clear_secrets(self) -> None:
        for key, field in self.fields.items():
            if key != "identity_username":
                field.clear()

    def present(self, language: Language) -> None:
        key = f"identity_{self.kind}"
        self.setWindowTitle(text(key, language))
        self.submit.setText(text(key, language))
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
        self.submit.setEnabled(not busy)
        self.feedback("identity_working" if busy else "", language)


class UsersDialog(_AccountDialog):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.resize(850, 500)
        self.accounts: tuple[Account, ...] = ()
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
        global_row = QHBoxLayout(self.global_actions)
        self._account_row = QHBoxLayout(self.account_actions)
        for row in (global_row, self._account_row):
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(6)
        for key in (
            "identity_create",
            "short_refresh",
            "identity_change_role",
            "identity_disable",
            "identity_reset",
        ):
            button = QPushButton()
            button.setMinimumHeight(Style.touch)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            row = (
                global_row
                if key in ("identity_create", "short_refresh")
                else self._account_row
            )
            row.addWidget(button)
            self.controls[key] = button
            button.installEventFilter(self)
        self.controls["short_refresh"].setFixedWidth(Style.touch)
        self.controls["identity_create"].setObjectName("start")
        self._action_grid.addWidget(
            self.global_actions, 0, 0, Qt.AlignmentFlag.AlignLeft
        )
        self._action_grid.addWidget(self.account_actions, 0, 1)
        self._action_mode = (False, False)
        self.body.addWidget(self.action_area)
        self.body.addWidget(self.status)
        self.table.itemSelectionChanged.connect(self.selection_changed)
        for widget in (self.table, self.table.viewport(), self.action_area):
            widget.installEventFilter(self)

    def selected(self) -> Account | None:
        item = self.table.currentItem() if self.table.selectedItems() else None
        account_id = item.data(Qt.ItemDataRole.UserRole) if item else None
        return next((a for a in self.accounts if a.account_id == account_id), None)

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
        for key, button in self.controls.items():
            if key == "short_refresh":
                continue
            button.setMinimumHeight(
                max(Style.touch, button.fontMetrics().height() + 14)
            )
            captions = (
                (
                    text("identity_enable", self.language),
                    text("identity_disable", self.language),
                )
                if key == "identity_disable"
                else (button.text(),)
            )
            button.setMinimumWidth(
                max(
                    104,
                    max(button.fontMetrics().horizontalAdvance(c) for c in captions)
                    + 28,
                )
            )
        widths = {
            key: max(button.minimumWidth(), button.sizeHint().width())
            for key, button in self.controls.items()
        }
        global_width = widths["identity_create"] + widths["short_refresh"] + 6
        account_width = (
            sum(
                widths[k]
                for k in ("identity_change_role", "identity_disable", "identity_reset")
            )
            + 12
        )
        width = self.action_area.width()
        wrapped = width < global_width + 18 + account_width
        stacked = width < account_width
        mode = wrapped, stacked
        if mode != self._action_mode:
            self._action_mode = mode
            self._action_grid.removeWidget(self.global_actions)
            self._action_grid.removeWidget(self.account_actions)
            self._account_row.setDirection(
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
        # Round shared edges so each column stays within one pixel of its
        # 30:22:16:32 share, with no unused space or accumulated rounding error.
        edges = [width * percent // 100 for percent in (0, 30, 52, 68, 100)]
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
        last_admin = (
            selected is not None
            and selected.enabled
            and selected.role is Role.ADMINISTRATOR
            and sum(a.enabled and a.role is Role.ADMINISTRATOR for a in self.accounts)
            == 1
        )
        self.table.setEnabled(not busy)
        for key, button in self.controls.items():
            button.setEnabled(
                not busy
                and (
                    key in ("identity_create", "short_refresh") or selected is not None
                )
                and not (
                    last_admin and key in ("identity_change_role", "identity_disable")
                )
            )
        self.controls["identity_disable"].setText(
            text(
                "identity_disable"
                if selected and selected.enabled
                else "identity_enable",
                self.language,
            )
        )
        for key in ("identity_change_role", "identity_disable", "identity_reset"):
            button = self.controls[key]
            button.setAccessibleName(button.text())
            button.setToolTip(
                text("identity_last_admin", self.language)
                if last_admin and key != "identity_reset"
                else button.text()
            )
        self._queue_layout()

    def install_accounts(self, accounts: tuple[Account, ...]) -> None:
        previous = self.selected()
        self.accounts = accounts
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
            values = (
                account.username,
                text(_ROLES[account.role], language),
                text(
                    "identity_enabled" if account.enabled else "identity_disabled",
                    language,
                ),
                account.last_login_utc.astimezone().strftime("%Y-%m-%d %H:%M")
                if account.last_login_utc is not None
                else "—",
            )
            for column, value in enumerate(values):
                item = self.table.item(index, column)
                assert item is not None
                item.setText(value)
                item.setToolTip(value)
                item.setData(Qt.ItemDataRole.AccessibleTextRole, value)
        for key, button in self.controls.items():
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
        self.role.currentIndexChanged.connect(self._selection_changed)

    @Slot()
    def _selection_changed(self) -> None:
        self.submit.setEnabled(
            not self.busy and self.role.currentData() != self.account.role
        )

    def set_busy(self, busy: bool, language: Language) -> None:
        self.busy = busy
        self.role.setEnabled(not busy)
        self._selection_changed()
        self.feedback("identity_working" if busy else "", language)

    def present(self, language: Language) -> None:
        self.setWindowTitle(text("identity_change_role", language))
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

    def __init__(
        self, navbar: NavigationBar, owner: LocalIdentity | None, parent: QWidget
    ) -> None:
        super().__init__(parent)
        self.navbar, self.owner, self.window = navbar, owner, parent
        self.language: Language = "en"
        self.form: CredentialDialog | None = None
        self.users: UsersDialog | None = None
        self.role_dialog: RoleDialog | None = None
        self.confirmation: DisableDialog | None = None
        self._serial = 0
        self._pending: tuple[str, int] | None = None
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
        account = snapshot.account
        detail = text(_ROLES[snapshot.role], self.language)
        state = (
            self.owner.storage_snapshot().state if self.owner else StorageState.CLOSED
        )
        if state is not StorageState.READY:
            status = text(
                "identity_missing"
                if state is StorageState.MISSING
                else "identity_storage",
                self.language,
            )
            detail = f"{detail} · {status}"
        self.navbar.present_user(account.username if account else None, detail)
        self.navbar.user.setEnabled(not self._closing)
        for key, action in self.controls.items():
            action.setText(text(key, self.language))
            action.setVisible(
                (key == "identity_login" and account is None)
                or (
                    key in ("identity_logout", "identity_switch", "identity_change")
                    and account is not None
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
            and form.kind != "login"
            and (
                account is None
                or account.account_id != form.actor_id
                or (
                    form.kind in ("create", "reset")
                    and snapshot.role is not Role.ADMINISTRATOR
                )
            )
        ):
            form.reject()

    def _invalidate(self) -> None:
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
        self._dismiss()
        if self.owner is not None:
            self.owner.logout()
        self.refresh()

    @Slot()
    def login(self) -> None:
        self._open_form("login")

    @Slot()
    def switch_user(self) -> None:
        self.logout()
        self.login()

    @Slot()
    def change_password(self) -> None:
        self._open_form("change")

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
        actor = self.owner.snapshot().account
        form.actor_id = actor.account_id if actor else None
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
                if self.owner.storage_snapshot().state is StorageState.MISSING
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
            # A dismissed login is a later session decision, not Future cancellation.
            if form.kind == "login" and self.owner is not None:
                self.owner.logout()
        form.deleteLater()
        if self.users is not None:
            self.users.set_busy(False)
        self.refresh()

    @Slot()
    def _submit(self) -> None:
        form, owner = self.form, self.owner
        if form is None or owner is None or form.busy or self._closing:
            return
        values = {key: field.text() for key, field in form.fields.items()}
        if (
            "identity_confirm" in values
            and values["identity_confirm"] != values["identity_new_password"]
        ):
            form.clear_secrets()
            form.feedback("identity_mismatch", self.language)
            return
        form.clear_secrets()
        form.set_busy(True, self.language)

        if form.kind == "login":
            self._request(
                "credential",
                lambda: owner.login(
                    values["identity_username"], values["identity_password"]
                ),
            )
        elif form.kind == "change":
            self._request(
                "credential",
                lambda: owner.change_password(
                    values["identity_old_password"], values["identity_new_password"]
                ),
            )
        elif form.kind == "create":
            role = Role(form.role.currentData())
            self._request(
                "credential",
                lambda: owner.create_account(
                    values["identity_username"], role, values["identity_new_password"]
                ),
            )
        elif form.account is not None:
            account_id = form.account.account_id
            self._request(
                "credential",
                lambda: owner.reset_password(
                    account_id, values["identity_new_password"]
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
            self.role_dialog.feedback(_feedback(error), self.language)
        elif self.form is not None:
            self.form.set_busy(False, self.language)
            self.form.feedback(_feedback(error), self.language)
        elif self.users is not None:
            self.users.set_busy(False)
            self.users.feedback(_feedback(error), self.language)

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
            self._request("list", self.owner.list_accounts)

    @Slot()
    def _reset_password(self) -> None:
        account = self.users.selected() if self.users else None
        if account is not None:
            self._open_form("reset", account)

    @Slot()
    def _set_role(self) -> None:
        users, owner = self.users, self.owner
        account = users.selected() if users else None
        if (
            users is not None
            and owner is not None
            and account is not None
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
        if role == dialog.account.role:
            return
        dialog.set_busy(True, self.language)
        self._request("role", lambda: owner.set_role(dialog.account.account_id, role))

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
                "update", lambda: owner.set_enabled(account.account_id, False)
            )
        else:
            users.set_busy(False)

    @Slot(str, object, object)
    def _completed(self, serial: str, value: object, error: object) -> None:
        if self._closing or serial != str(self._serial) or self._pending is None:
            return
        kind, revision = self._pending
        self._pending = None
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
        if kind == "list" and self.users is not None and isinstance(value, tuple):
            records = cast(tuple[object, ...], value)
            accounts = tuple(
                account for account in records if isinstance(account, Account)
            )
            self.users.install_accounts(accounts)
            self.users.set_busy(False)
            self.users.feedback("", self.language)
        self.refresh()
        if kind != "list" and self.users is not None:
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
