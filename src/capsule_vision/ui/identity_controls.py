"""Account forms present the local service; they own no account or worker state."""

from collections.abc import Callable
from concurrent.futures import Future
from typing import Literal, cast

from PySide6.QtCore import QObject, QSignalBlocker, Qt, Slot
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
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
from .style import Style

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
        self.resize(640, 440)
        self.accounts: tuple[Account, ...] = ()
        self.table = QTableWidget(0, 3)
        self.table.setObjectName("accounts")
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setStretchLastSection(True)
        self.body.addWidget(self.table)
        row = QHBoxLayout()
        self.body.addLayout(row)
        self.role = QComboBox()
        for role in (Role.OPERATOR, Role.ENGINEER, Role.ADMINISTRATOR):
            self.role.addItem("", role)
        row.addWidget(self.role)
        self.controls: dict[str, QPushButton] = {}
        for key in ("identity_apply", "identity_disable", "identity_reset"):
            button = QPushButton()
            button.setMinimumHeight(Style.touch)
            row.addWidget(button)
            self.controls[key] = button
        row2 = QHBoxLayout()
        self.body.addLayout(row2)
        for key in ("identity_create", "short_refresh", "identity_close"):
            button = QPushButton()
            button.setMinimumHeight(Style.touch)
            row2.addWidget(button)
            self.controls[key] = button
        self.body.addWidget(self.status)
        self.controls["identity_close"].clicked.connect(self.reject)
        self.table.itemSelectionChanged.connect(self.selection_changed)
        self.busy = False
        self.language: Language = "en"

    def selected(self) -> Account | None:
        item = self.table.currentItem()
        account_id = item.data(Qt.ItemDataRole.UserRole) if item else None
        return next((a for a in self.accounts if a.account_id == account_id), None)

    @Slot()
    def selection_changed(self) -> None:
        account = self.selected()
        if account is not None:
            self.role.setCurrentIndex(self.role.findData(account.role))
        self.set_busy(self.busy)

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        selected = self.selected()
        self.table.setEnabled(not busy)
        self.role.setEnabled(not busy and selected is not None)
        for key, button in self.controls.items():
            button.setEnabled(
                key == "identity_close"
                or (
                    not busy
                    and (
                        key in ("identity_create", "short_refresh")
                        or selected is not None
                    )
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

    def install_accounts(self, accounts: tuple[Account, ...]) -> None:
        previous = self.selected()
        self.accounts = accounts
        with QSignalBlocker(self.table):
            self.table.setRowCount(0)
            self.table.setRowCount(len(accounts))
            for row, account in enumerate(accounts):
                for column in range(3):
                    item = QTableWidgetItem()
                    item.setData(Qt.ItemDataRole.UserRole, account.account_id)
                    self.table.setItem(row, column, item)
                if previous and previous.account_id == account.account_id:
                    self.table.setCurrentCell(row, 0)
            if not self.table.selectedItems() and accounts:
                self.table.setCurrentCell(0, 0)
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
                text("identity_enabled", language),
            ]
        )
        for index, account in enumerate(self.accounts):
            values = (
                account.username,
                text(_ROLES[account.role], language),
                text(
                    "identity_enabled" if account.enabled else "identity_disabled",
                    language,
                ),
            )
            for column, value in enumerate(values):
                item = self.table.item(index, column)
                assert item is not None
                item.setText(value)
                item.setToolTip(value)
        self.table.setColumnWidth(0, 225)
        self.table.setColumnWidth(1, 170)
        self.role.setAccessibleName(text("identity_role", language))
        for index, role in enumerate(
            (Role.OPERATOR, Role.ENGINEER, Role.ADMINISTRATOR)
        ):
            self.role.setItemText(index, text(_ROLES[role], language))
        for key, button in self.controls.items():
            button.setText(text(key, language))
        self.set_busy(self.busy)
        self.feedback(self.status_key, language)


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
        for dialog in (self.form, self.users):
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
            detail = text(
                "identity_missing"
                if state is StorageState.MISSING
                else "identity_storage",
                self.language,
            )
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
        if self._pending is not None:
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
        if self.form is not None:
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
        users.controls["identity_apply"].clicked.connect(self._set_role)
        users.controls["identity_disable"].clicked.connect(self._set_enabled)
        users.controls["short_refresh"].clicked.connect(self._list_accounts)
        users.show()
        self._list_accounts()

    @Slot(int)
    def _users_finished(self, result: int) -> None:
        users, self.users = self.users, None
        if users is not None:
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
        if users is not None and owner is not None and account is not None:
            role = Role(users.role.currentData())
            users.set_busy(True)
            self._request("update", lambda: owner.set_role(account.account_id, role))

    @Slot()
    def _set_enabled(self) -> None:
        users, owner = self.users, self.owner
        account = users.selected() if users else None
        if users is not None and owner is not None and account is not None:
            users.set_busy(True)
            self._request(
                "update",
                lambda: owner.set_enabled(account.account_id, not account.enabled),
            )

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
