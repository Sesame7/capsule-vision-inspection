"""Real account service, masked forms and queued Qt delivery; isolated data only."""

from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, current_thread, main_thread

import pytest
from PySide6.QtCore import QPoint, QRect, Qt, QTimer
from PySide6.QtGui import QAccessible, QColor, QFont
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import (
    QApplication,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QWidget,
)
from tests.support.application import FakeSession, prepared
from tests.support.qt import (
    VISUAL_CASES,
    VISUAL_LANGUAGES,
    assert_full_caption,
    close_window,
    ensure_test_fonts,
    until,
)

from capsule_vision.application import (
    ApplicationState,
    ProductionApplication,
    passwords,
)
from capsule_vision.application import identity as identity_module
from capsule_vision.application._account_store import AccountStore
from capsule_vision.application.account_maintenance import initialize_administrator
from capsule_vision.application.identity import LocalIdentity
from capsule_vision.application.identity_models import (
    Account,
    AccountManagementSnapshot,
    Role,
    SecurityStorageError,
    SessionSnapshot,
    StaleSessionError,
    StorageState,
)
from capsule_vision.application.passwords import PasswordVerifier
from capsule_vision.display import LatestDisplayFrameStore
from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import Language
from capsule_vision.ui.account_maintenance import MaintenanceWindow
from capsule_vision.ui.identity_controls import (
    CredentialDialog,
    IdentityControls,
    UsersDialog,
)
from capsule_vision.ui.main_window import MainWindow
from capsule_vision.ui.production_bridge import QtProductionBridge
from capsule_vision.ui.shell import NavigationBar
from capsule_vision.ui.style import Style, Theme, apply_theme, theme_colors

SECRET = "isolated UI test credential"
NEW_SECRET = "replacement UI test credential"


@pytest.mark.parametrize("width,height", [(1080, 1920), (1920, 1080)])
def test_account_menu_theme_and_language_in_workstation_navigation(
    identity_screen: tuple[MainWindow, LocalIdentity],
    width: int,
    height: int,
) -> None:
    window, owner = identity_screen
    bar, ui = window.navbar, window.identity_controls
    window.resize(width, height)
    QApplication.processEvents()
    user_slot = bar.user.geometry()
    before = window.bridge.application.snapshot()
    selected: list[int] = []
    bar.page_selected.connect(selected.append)
    ui.controls["identity_login"].trigger()
    submit(ui, username="Admin", password=SECRET)
    until(lambda: ui.form is None)
    assert owner.snapshot().role is Role.ADMINISTRATOR
    assert bar.user.geometry() == user_slot and "Admin" in bar.user.accessibleName()
    ui.menu.popup(bar.user.mapToGlobal(QPoint(0, bar.user.height())))
    until(ui.menu.isVisible)
    accessible = QAccessible.queryAccessibleInterface(ui.menu)
    assert accessible is not None
    header = accessible.child(0)
    assert header and header.role() is QAccessible.Role.Separator
    assert header.text(QAccessible.Text.Name) == "Admin · Administrator"
    QTest.keyClick(ui.menu, Qt.Key.Key_Down)
    assert ui.menu.activeAction() is ui.controls["identity_logout"]
    assert (
        ui.controls["identity_manage"].isVisible()
        and ui.controls["identity_manage"].isEnabled()
    )
    ui.menu.hide()
    for theme in ("light", "dark"):
        window.theme_button.click()
        assert window.theme == theme
        for language in ("zh", "en"):
            window.language_selector.activated.emit(
                window.language_selector.findData(language)
            )
            QApplication.processEvents()
            assert window.language == language and window.pages.currentIndex() == 0
    ui.controls["identity_logout"].trigger()
    assert owner.snapshot().role is Role.GUEST
    assert bar.user.geometry() == user_slot and selected == []
    assert window.bridge.application.snapshot() == before


def test_navigation_guest_and_logged_in_captions(
    identity_screen: tuple[MainWindow, LocalIdentity],
) -> None:
    window, owner = identity_screen
    bar, ui = window.navbar, window.identity_controls
    before = window.bridge.application.snapshot()
    button_size = bar.user.size()
    guest = owner.snapshot()
    for language in VISUAL_LANGUAGES:
        window.set_language(language)
        QApplication.processEvents()
        assert_full_caption(bar.user, "Guest")
        assert (
            bar.user.toolTip()
            == bar.user.accessibleName()
            == text("identity_guest", language)
        )
        assert ui.controls["identity_login"].text() == text("identity_login", language)
        assert not ui.menu.actions()[0].isVisible()
        assert bar.user.size() == button_size and owner.snapshot() == guest

    login(ui)
    authenticated = owner.snapshot()
    assert authenticated.account is not None
    for language in VISUAL_LANGUAGES:
        window.set_language(language)
        QApplication.processEvents()
        assert_full_caption(bar.user, authenticated.account.username)
        assert (
            bar.user.toolTip()
            == bar.user.accessibleName()
            == (
                f"{authenticated.account.username} · {text('identity_administrator', language)}"
            )
        )
        assert ui.menu.actions()[0].isVisible()
        assert ui.menu_identity.text() == bar.user.accessibleName()
        assert bar.user.size() == button_size and owner.snapshot() == authenticated
    ui.logout()
    assert bar.user.text() == "Guest"
    assert window.bridge.application.snapshot() == before


@pytest.mark.parametrize("username", ["Guest", "QA_用户", "LongUsername_" * 4])
def test_navigation_preserves_original_usernames_and_full_descriptions(
    qapp: QApplication, username: str
) -> None:
    ensure_test_fonts()
    bar = NavigationBar()
    apply_theme(bar, "dark")
    for language in VISUAL_LANGUAGES:
        bar.present(language, "dark", 0)
        bar.present_user(username, text("identity_engineer", language))
        assert bar.user.text() == bar.user.fontMetrics().elidedText(
            username, Qt.TextElideMode.ElideRight, bar.user.width() - 64
        )
        assert (
            bar.user.toolTip()
            == bar.user.accessibleName()
            == (f"{username} · {text('identity_engineer', language)}")
        )
    bar.close()


def completed[T](future: Future[T]) -> T:
    until(future.done)
    return future.result(timeout=0)


@pytest.fixture
def identity_screen(
    qapp: QApplication, tmp_path: Path
) -> Iterator[tuple[MainWindow, LocalIdentity]]:
    path = tmp_path / "data/security.sqlite3"
    initialize_administrator(path, "Admin", SECRET)
    owner = LocalIdentity(path)
    owner.ready.result(5)
    owner.login("Admin", SECRET).result(5)
    owner.create_account(
        "Worker", Role.OPERATOR, "temporary fixture credential"
    ).result(5)
    pending = owner.login("Worker", "temporary fixture credential").result(5)
    owner.rotate_password(SECRET, pending.revision).result(5)
    owner.logout()
    ensure_test_fonts()
    candidate = prepared(tmp_path)
    session = FakeSession()
    application = ProductionApplication(
        lambda _p, _g: session, identity=owner, initial_candidate=candidate
    )
    window = MainWindow(
        QtProductionBridge(
            application,
            LatestDisplayFrameStore(candidate.configuration.layout.camera_ids),
        )
    )
    window.set_language("en")
    window.resize(1080, 1000)
    window.show()
    try:
        yield window, owner
    finally:
        close_window(window)
        assert owner.join(0)


def form(controls: IdentityControls) -> CredentialDialog:
    dialog = controls.form
    assert dialog is not None
    return dialog


def submit(controls: IdentityControls, **values: str) -> CredentialDialog:
    dialog = form(controls)
    for key, value in values.items():
        dialog.fields[f"identity_{key}"].setText(value)
    dialog.submit.click()
    for key, field in dialog.fields.items():
        if key != "identity_username":
            assert field.echoMode() is QLineEdit.EchoMode.Password
            assert field.text() == ""
    return dialog


def login(
    controls: IdentityControls, username: str = "Admin", password: str = SECRET
) -> None:
    controls.login()
    submit(controls, username=username, password=password)
    until(lambda: controls.form is None)


def manager(controls: IdentityControls) -> UsersDialog:
    controls.manage_users()
    dialog = controls.users
    assert dialog is not None
    until(lambda: not dialog.busy)
    return dialog


def select(dialog: UsersDialog, username: str) -> Account:
    for index, account in enumerate(dialog.accounts):
        if account.username == username:
            item = dialog.table.item(index, 0)
            assert item is not None
            dialog.table.setCurrentItem(item)
            return account
    raise AssertionError(username)


def assert_account_table_columns(dialog: UsersDialog) -> None:
    table, header = dialog.table, dialog.table.horizontalHeader()
    width = table.viewport().width()
    widths = tuple(table.columnWidth(i) for i in range(4))
    assert sum(widths) == width
    for actual, percent in zip(widths, (30, 19, 22, 29), strict=True):
        floor, remainder = divmod(width * percent, 100)
        assert floor <= actual <= floor + bool(remainder), (width, widths, percent)
    assert table.horizontalScrollBar().maximum() == 0
    assert not header.stretchLastSection()
    alignment = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
    assert header.defaultAlignment() == alignment
    for column in range(4):
        assert header.sectionResizeMode(column) is QHeaderView.ResizeMode.Fixed
        heading = table.horizontalHeaderItem(column)
        assert heading is not None and heading.textAlignment() == alignment
        assert heading.toolTip() == heading.text()
        for row in range(table.rowCount()):
            item = table.item(row, column)
            assert item is not None and item.textAlignment() == alignment
            detail = item.text()
            if (
                column == 2
                and dialog.accounts[row].account_id == dialog.auto_login_account_id
            ):
                detail = f"{text('identity_enabled', dialog.language)} · {text('identity_auto_login', dialog.language)}"
            assert (
                item.toolTip()
                == item.data(Qt.ItemDataRole.AccessibleTextRole)
                == detail
            )


def confirm_disable(controls: IdentityControls) -> None:
    dialog = controls.confirmation
    assert dialog is not None
    assert dialog.account.username in dialog.text()
    dialog.button(QMessageBox.StandardButton.Yes).click()


def change_role(controls: IdentityControls, role: Role) -> None:
    users = controls.users
    assert users is not None
    users.controls["identity_change_role"].click()
    dialog = controls.role_dialog
    assert dialog is not None and not dialog.submit.isEnabled()
    dialog.role.setCurrentIndex(dialog.role.findData(role))
    assert dialog.submit.isEnabled()
    dialog.submit.click()


@pytest.mark.parametrize(("language", "theme"), [("en", "dark"), ("zh", "light")])
def test_manage_users_presents_and_changes_auto_login(
    identity_screen: tuple[MainWindow, LocalIdentity],
    language: Language,
    theme: Theme,
) -> None:
    window, owner = identity_screen
    window.set_language(language)
    window.set_theme(theme)
    ui = window.identity_controls
    login(ui)
    completed(
        owner.create_account("Pending", Role.OPERATOR, "temporary fixture credential")
    )
    other = completed(
        owner.create_account("Other", Role.OPERATOR, "temporary fixture credential")
    )
    pending = completed(owner.login(other.username, "temporary fixture credential"))
    owner.rotate_password(SECRET, pending.revision).result(5)
    completed(owner.login("Admin", SECRET))
    users = manager(ui)
    button = users.controls["identity_set_auto_login"]
    assert users.table.columnCount() == 4
    below_table = users.body.itemAt(1)
    assert below_table is not None and below_table.widget() is users.action_area
    set_caption, clear_caption, auto_status = (
        ("设为自动", "取消自动", "自动登录")
        if language == "zh"
        else ("Set Auto", "Clear Auto", "Auto login")
    )
    enabled, disabled = (
        text(key, language) for key in ("identity_enabled", "identity_disabled")
    )

    def assert_status(
        username: str, caption: str, description: str | None = None
    ) -> None:
        row = next(
            i
            for i, account in enumerate(users.accounts)
            if account.username == username
        )
        item = users.table.item(row, 2)
        assert item is not None and item.text() == caption
        assert (
            item.toolTip()
            == item.data(Qt.ItemDataRole.AccessibleTextRole)
            == (description or caption)
        )

    def assert_authorized(account: Account) -> None:
        until(
            lambda: not users.busy and users.auto_login_account_id == account.account_id
        )
        assert users.selected() == account and account.enabled
        assert_status(
            account.username,
            auto_status,
            f"{enabled} · {text('identity_auto_login', language)}",
        )
        assert_full_caption(button, clear_caption)
        assert (
            button.toolTip()
            == button.accessibleName()
            == text("identity_disable_auto_login", language)
        )

    for name in ("Admin", "Pending"):
        select(users, name)
        assert not button.isEnabled()
        assert_status(name, enabled)
    users.table.clearSelection()
    assert not button.isEnabled()
    worker = select(users, "Worker")
    assert button.isEnabled()
    assert_status(worker.username, enabled)
    assert_full_caption(button, set_caption)
    assert (
        button.toolTip()
        == button.accessibleName()
        == text("identity_set_auto_login", language)
    )
    QApplication.processEvents()
    slot = QRect(button.mapTo(users, QPoint()), button.size())
    button.click()
    assert_authorized(worker)
    assert QRect(button.mapTo(users, QPoint()), button.size()) == slot
    other = select(users, other.username)
    assert button.isEnabled() and button.text() == set_caption
    button.click()
    assert_authorized(other)
    assert_status(worker.username, enabled)
    users.controls["short_refresh"].click()
    assert_authorized(other)
    button.click()
    until(lambda: not users.busy and users.auto_login_account_id is None)
    assert_status(other.username, enabled)
    assert button.text() == set_caption
    QApplication.processEvents()
    assert QRect(button.mapTo(users, QPoint()), button.size()) == slot
    button.click()
    assert_authorized(other)
    users.controls["identity_disable"].click()
    confirm_disable(ui)
    until(lambda: not users.busy and users.auto_login_account_id is None)
    selected = users.selected()
    assert (
        selected is not None
        and selected.account_id == other.account_id
        and not selected.enabled
    )
    assert_status(other.username, disabled)
    assert not button.isEnabled()
    select(users, worker.username)
    button.click()
    assert_authorized(worker)
    change_role(ui, Role.ENGINEER)
    until(lambda: not users.busy and users.auto_login_account_id is None)
    selected = users.selected()
    assert selected is not None and selected.role is Role.ENGINEER
    assert_status(worker.username, enabled)
    assert not button.isEnabled()


def test_old_users_refresh_cannot_restore_previous_auto_login(
    identity_screen: tuple[MainWindow, LocalIdentity], monkeypatch: pytest.MonkeyPatch
) -> None:
    window, owner = identity_screen
    ui = window.identity_controls
    login(ui)
    users = manager(ui)
    worker = select(users, "Worker")
    read_accounts = owner.account_management
    old_snapshot = completed(read_accounts())
    old_refresh: Future[AccountManagementSnapshot] = Future()
    monkeypatch.setattr(owner, "account_management", lambda: old_refresh)
    users.controls["short_refresh"].click()
    assert users.busy and not users.controls["identity_set_auto_login"].isEnabled()
    users.close()
    assert ui.users is None

    monkeypatch.setattr(owner, "account_management", read_accounts)
    users = manager(ui)
    select(users, worker.username)
    button = users.controls["identity_set_auto_login"]
    button.click()
    until(lambda: not users.busy and users.auto_login_account_id == worker.account_id)
    old_refresh.set_result(old_snapshot)
    QApplication.processEvents()

    assert ui.users is users and users.selected() == worker
    item = users.table.item(users.table.currentRow(), 2)
    assert item is not None and item.text() == "Auto login"
    assert button.text() == "Clear Auto"


def test_rejected_auto_login_keeps_status_and_shows_feedback(
    identity_screen: tuple[MainWindow, LocalIdentity],
) -> None:
    window, owner = identity_screen
    ui = window.identity_controls
    login(ui)
    users = manager(ui)
    worker = select(users, "Worker")
    # Keep the last UI snapshot while the real owner makes the target ineligible.
    completed(owner.set_enabled(worker.account_id, False))
    button = users.controls["identity_set_auto_login"]
    assert button.isEnabled()
    button.click()
    until(lambda: not users.busy)
    assert users.status.isVisible() and users.status.text() == text(
        "identity_denied", "en"
    )
    item = users.table.item(users.table.currentRow(), 2)
    assert item is not None and item.text() == "Enabled"
    assert completed(owner.account_management()).auto_login_account_id is None

    users.controls["short_refresh"].click()
    until(lambda: not users.busy)
    item = users.table.item(users.table.currentRow(), 2)
    assert item is not None and item.text() == "Disabled"
    assert not button.isEnabled() and not users.status.isVisible()


def test_guest_login_wrong_disabled_switch_logout_and_self_password(
    identity_screen: tuple[MainWindow, LocalIdentity],
) -> None:
    window, owner = identity_screen
    ui = window.identity_controls
    before = window.bridge.application.snapshot()
    assert window.navbar.user.text() == "Guest"
    assert ui.controls["identity_login"].isVisible()
    assert not ui.controls["identity_manage"].isVisible()
    ui.login()
    dialog = submit(ui, username="Admin", password="wrong")
    until(lambda: not dialog.busy)
    assert dialog.status.text() == text("identity_invalid", "en")
    assert owner.snapshot().role is Role.GUEST
    submit(ui, username="Admin", password=SECRET)
    until(lambda: ui.form is None)
    assert ui.controls["identity_manage"].isVisible()
    assert "Admin" in window.navbar.user.accessibleName()
    ui.change_password()
    dialog = form(ui)
    assert dialog.windowTitle() == dialog.submit.text() == text("identity_change", "en")
    assert (
        dialog.buttons.button(QDialogButtonBox.StandardButton.Cancel).text() == "Cancel"
    )
    dialog = submit(
        ui, old_password=SECRET, new_password=NEW_SECRET, confirm="mismatch"
    )
    assert dialog.status.text() == text("identity_mismatch", "en")
    submit(ui, old_password="wrong", new_password=NEW_SECRET, confirm=NEW_SECRET)
    until(lambda: not dialog.busy)
    assert dialog.status.text() == text("identity_invalid", "en")
    submit(ui, old_password=SECRET, new_password=NEW_SECRET, confirm=NEW_SECRET)
    until(lambda: ui.form is None)
    assert owner.snapshot().role is Role.ADMINISTRATOR
    worker = next(a for a in completed(owner.list_accounts()) if a.username == "Worker")
    completed(owner.set_enabled(worker.account_id, False))
    current = owner.snapshot()
    ui.switch_user()
    assert owner.snapshot() == current
    dialog = form(ui)
    assert dialog.windowTitle() == dialog.submit.text() == "Switch user"
    assert dialog.submit.accessibleName() == dialog.submit.text()
    dismiss = dialog.buttons.button(QDialogButtonBox.StandardButton.Cancel)
    assert dismiss.text() == dismiss.accessibleName() == "Cancel"
    dismiss.click()
    assert owner.snapshot() == current and ui.form is None
    ui.switch_user()
    dialog = submit(ui, username="Worker", password=SECRET)
    until(lambda: not dialog.busy)
    assert dialog.status.text() == text("identity_invalid", "en")
    assert owner.snapshot() == current
    assert ui.controls["identity_manage"].isVisible()
    submit(ui, username="Admin", password=NEW_SECRET)
    until(lambda: ui.form is None)
    assert owner.snapshot().revision == current.revision + 1
    ui.logout()
    assert window.navbar.user.text() == "Guest"
    assert window.bridge.application.snapshot() == before
    assert window.pages.count() == 5


def test_admin_create_role_enable_reset_and_last_admin(
    identity_screen: tuple[MainWindow, LocalIdentity],
) -> None:
    window, owner = identity_screen
    ui = window.identity_controls
    login(ui)
    users = manager(ui)
    for theme in ("light", "dark"):
        window.set_theme(theme)
        QApplication.processEvents()
        background = users.table.viewport().grab().toImage().pixelColor(5, 120)
        assert background == QColor(theme_colors(window).surface)
    select(users, "Admin")
    users.table.setCurrentCell(users.table.currentRow(), 1)
    selected = users.selected()
    assert selected is not None and selected.username == "Admin"
    assert not users.controls["identity_disable"].isEnabled()
    assert not users.controls["identity_change_role"].isEnabled()
    assert not users.controls["identity_reset"].isEnabled()
    assert users.controls["identity_disable"].toolTip() == text("identity_denied", "en")
    users.controls["identity_create"].click()
    form(ui).role.setCurrentIndex(form(ui).role.findData(Role.ADMINISTRATOR))
    credentials = form(ui)
    credentials.fields["identity_username"].setText("Backup")
    credentials.fields["identity_new_password"].setText(SECRET)
    credentials.fields["identity_confirm"].setText(SECRET)
    assert credentials.grant.isVisible() and not credentials.submit.isEnabled()
    credentials.grant.click()
    credentials.submit.click()
    until(lambda: ui.form is None and not users.busy)
    assert any(a.username == "Backup" for a in users.accounts)
    users.controls["identity_create"].click()
    duplicate = submit(ui, username="worker", new_password=SECRET, confirm=SECRET)
    until(lambda: not duplicate.busy)
    assert duplicate.status.text() == text("identity_conflict", "en")
    duplicate.reject()
    worker = select(users, "Worker")
    users.controls["identity_disable"].click()
    confirm_disable(ui)
    until(lambda: not users.busy)
    assert not select(users, "Worker").enabled
    users.controls["identity_disable"].click()
    until(lambda: not users.busy)
    assert select(users, "Worker").enabled
    assert ui.confirmation is None  # Enabling has no second confirmation.
    change_role(ui, Role.ENGINEER)
    until(lambda: not users.busy)
    assert select(users, "Worker").role is Role.ENGINEER
    users.controls["identity_reset"].click()
    submit(ui, new_password=NEW_SECRET, confirm=NEW_SECRET)
    until(lambda: ui.form is None and not users.busy)
    assert worker.account_id == select(users, "Worker").account_id
    select(users, "Backup")
    assert all(
        not users.controls[key].isEnabled()
        for key in ("identity_reset", "identity_change_role", "identity_disable")
    )
    users.reject()
    ui.switch_user()
    submit(ui, username="Worker", password=NEW_SECRET)
    until(lambda: ui.form is not None and ui.form.kind == "rotate")
    assert (
        owner.snapshot().rotation_required
        and not ui.controls["identity_manage"].isVisible()
    )
    assert set(form(ui).fields) == {"identity_new_password", "identity_confirm"}
    submit(ui, new_password=SECRET, confirm=SECRET)
    until(lambda: ui.form is None)
    assert owner.snapshot().role is Role.ENGINEER


def test_promoting_an_administrator_requires_explicit_confirmation(
    identity_screen: tuple[MainWindow, LocalIdentity],
) -> None:
    window, owner = identity_screen
    ui = window.identity_controls
    login(ui)
    users = manager(ui)
    worker = select(users, "Worker")
    users.controls["identity_change_role"].click()
    role = ui.role_dialog
    assert role
    role.role.setCurrentIndex(role.role.findData(Role.ADMINISTRATOR))
    assert role.grant.isVisible() and not role.submit.isEnabled()
    role.grant.click()
    role.submit.click()
    until(lambda: ui.role_dialog is None and not users.busy)
    selected = select(users, "Worker")
    assert (
        selected.account_id == worker.account_id and selected.role is Role.ADMINISTRATOR
    )
    assert all(
        not users.controls[key].isEnabled()
        for key in ("identity_reset", "identity_change_role", "identity_disable")
    )
    assert owner.snapshot().account and owner.snapshot().role is Role.ADMINISTRATOR


@pytest.mark.parametrize("committed", [False, True])
def test_switch_dismissal_is_ordered_with_service_commit(
    identity_screen: tuple[MainWindow, LocalIdentity],
    monkeypatch: pytest.MonkeyPatch,
    committed: bool,
) -> None:
    window, owner = identity_screen
    ui = window.identity_controls
    login(ui)
    current = owner.snapshot()
    production = window.bridge.application.snapshot()
    changes = QSignalSpy(ui.session_changed)
    ui.switch_user()
    QTest.keyClick(form(ui), Qt.Key.Key_Escape)
    assert owner.snapshot() == current and changes.count() == 0
    entered, release = Event(), Event()
    original_login = owner.login
    attempts: list[Future[SessionSnapshot]] = []

    def request(username: str, password: str) -> Future[SessionSnapshot]:
        attempt = original_login(username, password)
        attempts.append(attempt)
        return attempt

    def verify(password: str, verifier: PasswordVerifier) -> bool:
        entered.set()
        assert release.wait(10)
        return passwords.verify_password(password, verifier)

    monkeypatch.setattr(owner, "login", request)
    monkeypatch.setattr(identity_module, "verify_password", verify)
    try:
        ui.switch_user()
        dialog = submit(ui, username="Worker", password=SECRET)
        until(entered.is_set)
        assert owner.snapshot() == current
        assert window.navbar.user.text() == "Admin"
        assert ui.controls["identity_manage"].isVisible()
        assert changes.count() == 0
        attempt = attempts[0]
        if committed:
            release.set()
            # Deliberately withhold Qt dispatch until the service has committed.
            replacement = attempt.result(5)
            assert replacement.role is Role.OPERATOR and dialog.busy
        dialog.close()
        release.set()
        until(attempt.done)
        if committed:
            assert owner.snapshot().revision == current.revision + 1
            assert window.navbar.user.text() == "Worker"
            assert not ui.controls["identity_manage"].isVisible()
            assert changes.count() == 1
        else:
            with pytest.raises(StaleSessionError):
                attempt.result()
            assert owner.snapshot() == current
            assert window.navbar.user.text() == "Admin"
            assert ui.controls["identity_manage"].isVisible()
            assert changes.count() == 0
        QApplication.processEvents()  # Late delivery cannot reopen or replace a form.
        assert ui.form is None
        assert window.bridge.application.snapshot() == production
    finally:
        release.set()


def test_stale_login_cancel_switch_logout_and_real_close(
    identity_screen: tuple[MainWindow, LocalIdentity], monkeypatch: pytest.MonkeyPatch
) -> None:
    window, owner = identity_screen
    ui = window.identity_controls
    entered, release = Event(), Event()
    original = passwords.verify_password

    def verify(password: str, verifier: PasswordVerifier) -> bool:
        assert current_thread() is not main_thread()
        entered.set()
        assert release.wait(10)
        return original(password, verifier)

    monkeypatch.setattr(identity_module, "verify_password", verify)
    try:
        ui.login()
        pending = submit(ui, username="Admin", password=SECRET)
        until(entered.is_set)
        ticked: list[bool] = []
        QTimer.singleShot(0, lambda: ticked.append(True))
        until(lambda: bool(ticked))
        pending.reject()
        revision = owner.snapshot().revision
        ui.switch_user()
        submit(ui, username="Worker", password=SECRET)
        ui.logout()
        release.set()
        until(lambda: owner.snapshot().revision > revision)
        # Drain the serial worker before observing queued late completions.
        login(ui, "Worker")
        assert owner.snapshot().account is not None
        ui.logout()
        entered.clear()
        release.clear()
        ui.login()
        submit(ui, username="Admin", password=SECRET)
        until(entered.is_set)
        window.close()
        assert not window.bridge.closed
        assert owner.snapshot().role is Role.GUEST
        assert ui.form is None and not window.navbar.user.isEnabled()
    finally:
        release.set()
    until(lambda: window.bridge.closed and window.communication.closed)
    assert owner.join(0)
    assert owner.snapshot().role is Role.GUEST


@pytest.mark.parametrize("corrupt", [False, True])
def test_missing_or_corrupt_store_visible_guest_login(
    qapp: QApplication, tmp_path: Path, corrupt: bool
) -> None:
    from capsule_vision.application.startup import ApplicationData, load_startup
    from capsule_vision.ui.bootstrap import create_window
    from capsule_vision.ui.future_delivery import FutureDelivery

    data = ApplicationData.at_entry(tmp_path)
    if corrupt:
        data.root.mkdir()
        data.security.write_bytes(b"corrupt isolated account fixture")
    startup = load_startup(data)
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    try:
        owner = startup.application.identity
        assert owner is not None
        assert owner.storage_snapshot().state is (
            StorageState.UNAVAILABLE if corrupt else StorageState.MISSING
        )
        window.set_language("en")
        window.identity_controls.offer_setup()
        if corrupt:
            assert window.identity_controls.form is None
        else:
            setup = form(window.identity_controls)
            assert setup.kind == "setup"
            setup.reject()
            assert not data.security.exists()
        window.identity_controls.login()
        dialog = form(window.identity_controls)
        dialog.fields["identity_username"].setText("Unavailable account")
        assert not dialog.submit.isEnabled()
        for language in ("en", "zh", "af", "ti"):
            window.set_language(language)
            status = text(
                "identity_storage" if corrupt else "identity_missing", language
            )
            assert dialog.status.text() == status
            assert window.navbar.user.text() == "Guest"
            assert (
                window.navbar.user.toolTip()
                == window.navbar.user.accessibleName()
                == f"{text('identity_guest', language)} · {status}"
            )
        assert owner.snapshot().role is Role.GUEST
    finally:
        close_window(window)
        delivery.detach()


def test_first_setup_weak_confirmation_and_immediate_login(
    qapp: QApplication, tmp_path: Path
) -> None:
    from capsule_vision.application.startup import ApplicationData, load_startup
    from capsule_vision.ui.bootstrap import create_window
    from capsule_vision.ui.future_delivery import FutureDelivery

    startup = load_startup(ApplicationData.at_entry(tmp_path))
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    window.show()
    ui, owner = window.identity_controls, startup.application.identity
    assert owner
    try:
        ui.offer_setup()
        setup = form(ui)
        guest = owner.snapshot()
        setup.fields["identity_username"].setText("Lin")
        setup.fields["identity_new_password"].setText("abc")
        setup.fields["identity_confirm"].setText("abc")
        assert setup.weak.isVisible() and not setup.submit.isEnabled()
        setup.weak.click()
        setup.fields["identity_new_password"].setText("abcd")
        assert not setup.weak.isChecked() and not setup.submit.isEnabled()
        window.set_language("en")
        assert setup.windowTitle() == text("identity_setup", "en")
        assert (
            setup.submit.text()
            == setup.submit.accessibleName()
            == "Create administrator"
        )
        new_password = setup.fields["identity_new_password"]
        guidance = text("identity_guidance", "en")
        assert (
            new_password.toolTip() == new_password.accessibleDescription() == guidance
        )
        assert all(label.text() != guidance for label in setup.findChildren(QLabel))
        assert setup.fields["identity_username"].text() == "Lin"
        setup.fields["identity_confirm"].setText("abcd")
        setup.weak.click()
        setup.submit.click()
        until(lambda: ui.form is None)
        assert owner.snapshot() == guest and ui.controls["identity_login"].isVisible()
        login(ui, "Lin", "abcd")
        assert owner.snapshot().role is Role.ADMINISTRATOR
    finally:
        close_window(window)
        delivery.detach()


def test_temporary_login_cancel_retry_and_rotate_without_old_password(
    identity_screen: tuple[MainWindow, LocalIdentity],
) -> None:
    window, owner = identity_screen
    ui = window.identity_controls
    login(ui)
    username = "R&D <Tech>"
    completed(owner.create_account(username, Role.ENGINEER, SECRET))
    for dismissal in ("button", "escape", "close", "submit"):
        ui.login()
        submit(ui, username=username, password=SECRET)
        until(lambda: ui.form is not None and ui.form.kind == "rotate")
        rotation = form(ui)
        assert set(rotation.fields) == {"identity_new_password", "identity_confirm"}
        pending = owner.snapshot()
        assert (
            pending.rotation_required
            and not window.production_recipes.recipes.isEnabled()
        )
        assert (
            ui.controls["identity_logout"].isVisible()
            and not ui.controls["identity_switch"].isVisible()
        )
        window.set_language("zh")
        assert rotation.windowTitle() == text("identity_rotation_required", "zh")
        assert rotation.submit.text() == text("identity_change", "zh")
        dismiss = rotation.buttons.button(QDialogButtonBox.StandardButton.Cancel)
        assert (
            dismiss.text() == dismiss.accessibleName() == text("identity_logout", "zh")
        )
        assert ui.menu_identity.text() == f"{username} · {rotation.windowTitle()}"
        if dismissal != "submit":
            if dismissal == "button":
                dismiss.click()
            elif dismissal == "escape":
                QTest.keyClick(rotation, Qt.Key.Key_Escape)
            else:
                rotation.close()
            assert owner.snapshot().account is None and ui.form is None
        else:
            submit(ui, new_password=SECRET, confirm=SECRET)
            until(lambda dialog=rotation: not dialog.busy)
            reason = text("identity_temporary_password_reused", "zh")
            assert owner.snapshot() == pending
            assert rotation.status.text() == rotation.status.accessibleName() == reason
            assert rotation.windowTitle() != reason
            assert [
                label
                for label in rotation.findChildren(QLabel)
                if label.isVisible() and label.text() == reason
            ] == [rotation.status]
            submit(ui, new_password=NEW_SECRET, confirm=NEW_SECRET)
            until(lambda: ui.form is None)
            assert owner.snapshot().role is Role.ENGINEER
            assert owner.snapshot().revision > pending.revision
    ui.menu.popup(
        window.navbar.user.mapToGlobal(QPoint(0, window.navbar.user.height()))
    )
    until(ui.menu.isVisible)
    accessible = QAccessible.queryAccessibleInterface(ui.menu)
    assert accessible is not None
    header = accessible.child(0)
    assert header is not None
    assert (
        header.text(QAccessible.Text.Name)
        == f"{username} · {text('identity_engineer', 'zh')}"
    )
    QTest.keyClick(ui.menu, Qt.Key.Key_Escape)


def test_offline_gui_uses_live_lease_and_reports_audit_loss(
    qapp: QApplication, tmp_path: Path
) -> None:
    path = tmp_path / "data/security.sqlite3"
    path.parent.mkdir()
    path.with_name("audit.sqlite3").mkdir()
    owner = LocalIdentity(path)
    with ThreadPoolExecutor(max_workers=1) as worker:
        offline = MaintenanceWindow(tmp_path, "en", worker)
        offline.show()

        def operate(kind: str, password: str) -> None:
            (offline.setup if kind == "setup" else offline.recover).click()
            dialog = offline.form
            assert dialog and dialog.isVisible()
            for key, value in (
                ("identity_username", "Lin"),
                ("identity_new_password", password),
                ("identity_confirm", password),
            ):
                dialog.fields[key].setText(value)
            dialog.submit.click()
            until(lambda: not offline.pending)

        try:
            operate("setup", SECRET)
            assert (
                offline.status.text() == text("identity_busy", "en")
                and not path.exists()
            )
            owner.request_stop()
            assert owner.join(5)
            operate("setup", SECRET)
            assert text("identity_done", "en") in offline.status.text()
            operate("recover", NEW_SECRET)
            assert text("identity_audit_incomplete", "en") in offline.status.text()
        finally:
            owner.request_stop()
            assert owner.join(5)
            offline.close()
            offline.delivery.detach()
    reopened = LocalIdentity(path)
    try:
        assert completed(reopened.login("Lin", NEW_SECRET)).rotation_required
    finally:
        reopened.request_stop()
        assert reopened.join(5)


def test_runtime_database_failure_is_sanitized_and_returns_guest(
    identity_screen: tuple[MainWindow, LocalIdentity], monkeypatch: pytest.MonkeyPatch
) -> None:
    window, owner = identity_screen
    before = window.bridge.application.snapshot()
    login(window.identity_controls)

    def failed_read(self: AccountStore, username: str) -> None:
        assert current_thread() is not main_thread()
        raise SecurityStorageError("private database diagnostic " + SECRET)

    monkeypatch.setattr(AccountStore, "by_name", failed_read)
    ui = window.identity_controls
    ui.login()
    dialog = submit(ui, username="Admin", password=SECRET)
    until(lambda: not dialog.busy)
    assert dialog.status.text() == text("identity_storage", "en")
    assert SECRET not in dialog.status.text() + window.navbar.user.toolTip()
    assert owner.snapshot().role is Role.GUEST
    assert owner.storage_snapshot().state is StorageState.UNAVAILABLE
    assert window.navbar.user.text() == "Guest"
    dialog.reject()
    assert owner.snapshot().role is Role.GUEST
    assert (
        window.navbar.user.toolTip()
        == window.navbar.user.accessibleName()
        == (f"{text('identity_guest', 'en')} · {text('identity_storage', 'en')}")
    )
    assert window.bridge.application.snapshot() == before


def test_live_identity_language_changes_preserve_open_forms_and_role(
    identity_screen: tuple[MainWindow, LocalIdentity],
) -> None:
    window, owner = identity_screen
    ui = window.identity_controls
    before = window.bridge.application.snapshot()
    ui.login()
    credentials = form(ui)
    credentials.fields["identity_username"].setText("QA_{CAM01}_用户")
    credentials.fields["identity_password"].setText("unsubmitted fixture input")
    credentials.fields["identity_password"].setFocus()
    for language in VISUAL_LANGUAGES:
        window.set_language(language)
        QApplication.processEvents()
        assert ui.form is credentials
        assert credentials.windowTitle() == text("identity_login", language)
        assert (
            credentials.submit.text()
            == credentials.submit.accessibleName()
            == credentials.windowTitle()
        )
        assert credentials.fields["identity_username"].text() == "QA_{CAM01}_用户"
        assert (
            credentials.fields["identity_password"].text()
            == "unsubmitted fixture input"
        )
        assert credentials.fields["identity_password"].accessibleName() == text(
            "identity_password", language
        )
        assert credentials.fields["identity_password"].hasFocus()
        assert window.navbar.user.text() == "Guest"
        assert window.navbar.user.toolTip() == text("identity_guest", language)
    credentials.reject()
    login(ui)
    revision = owner.snapshot().revision
    ui.controls["identity_switch"].trigger()
    switching = form(ui)
    switching.fields["identity_username"].setText("Draft")
    for language in VISUAL_LANGUAGES:
        window.set_language(language)
        assert (
            switching.windowTitle()
            == switching.submit.text()
            == text("identity_switch", language)
        )
        assert switching.submit.accessibleName() == switching.submit.text()
        assert switching.fields["identity_username"].text() == "Draft"
    QTest.keyClick(switching, Qt.Key.Key_Escape)
    assert ui.form is None and owner.snapshot().revision == revision
    users = manager(ui)
    worker = select(users, "Worker")
    users.controls["identity_change_role"].click()
    role = ui.role_dialog
    assert role is not None
    role.role.setCurrentIndex(role.role.findData(Role.ENGINEER))
    role.role.setFocus()
    for language in VISUAL_LANGUAGES:
        window.set_language(language)
        QApplication.processEvents()
        assert ui.users is users and ui.role_dialog is role
        assert users.windowTitle() == text("identity_manage", language)
        assert role.windowTitle() == text("identity_change_role", language)
        assert role.role.currentData() == Role.ENGINEER and role.submit.isEnabled()
        assert role.role.itemText(role.role.currentIndex()) == text(
            "identity_engineer", language
        )
        assert role.role.hasFocus()
        selected = users.selected()
        assert selected is not None and selected.account_id == worker.account_id
        assert owner.snapshot().revision == revision
        assert_account_table_columns(users)
        assert ui.controls["identity_manage"].text() == text(
            "identity_manage", language
        )
    role.reject()
    for kind in ("create", "reset", "change"):
        if kind == "change":
            users.reject()
            ui.change_password()
        else:
            users.controls[
                "identity_create" if kind == "create" else "identity_reset"
            ].click()
        current = form(ui)
        saved = {
            key: "UnsavedUser"
            if key == "identity_username"
            else "unsubmitted fixture input"
            for key in current.fields
        }
        for key, value in saved.items():
            current.fields[key].setText(value)
        if kind == "create":
            current.role.setCurrentIndex(current.role.findData(Role.ENGINEER))
        current.fields["identity_new_password"].setFocus()
        current.feedback("identity_mismatch", window.language)
        for language in VISUAL_LANGUAGES:
            window.set_language(language)
            QApplication.processEvents()
            assert ui.form is current
            assert current.windowTitle() == text(f"identity_{kind}", language)
            assert current.submit.text() == current.windowTitle()
            assert {key: field.text() for key, field in current.fields.items()} == saved
            assert current.fields["identity_new_password"].hasFocus()
            assert current.status.text() == text("identity_mismatch", language)
            assert current.role.currentData() == (
                Role.ENGINEER if kind == "create" else Role.OPERATOR
            )
            assert owner.snapshot().revision == revision
        current.reject()
    users = manager(ui)
    select(users, "Worker")
    users.controls["identity_disable"].click()
    confirmation = ui.confirmation
    assert confirmation is not None
    for language in VISUAL_LANGUAGES:
        window.set_language(language)
        QApplication.processEvents()
        assert ui.confirmation is confirmation
        assert confirmation.text() == text("identity_confirm_disable", language).format(
            username="Worker"
        )
        assert confirmation.account.account_id == worker.account_id
    confirmation.reject()
    assert (
        next(
            a
            for a in completed(owner.list_accounts())
            if a.account_id == worker.account_id
        ).role
        is Role.OPERATOR
    )
    assert window.bridge.application.snapshot() == replace(before, start_blocker=None)


def test_queued_success_after_logout_or_external_role_change_is_not_installed(
    identity_screen: tuple[MainWindow, LocalIdentity],
) -> None:
    window, owner = identity_screen
    ui = window.identity_controls
    ui.login()
    submit(ui, username="Admin", password=SECRET)
    # Observe worker completion without dispatching its queued Qt success.
    from time import monotonic

    deadline = monotonic() + 5
    while owner.snapshot().role is Role.GUEST and monotonic() < deadline:
        Event().wait(0.005)
    assert owner.snapshot().role is Role.ADMINISTRATOR
    ui.logout()
    QApplication.processEvents()
    assert owner.snapshot().role is Role.GUEST and ui.form is None
    login(ui)
    manager(ui)
    completed(owner.login("Worker", SECRET))
    until(lambda: ui.users is None)
    assert not ui.controls["identity_manage"].isVisible()
    assert owner.snapshot().role is Role.OPERATOR


def test_close_drains_pending_account_creation_before_releasing_storage(
    identity_screen: tuple[MainWindow, LocalIdentity], monkeypatch: pytest.MonkeyPatch
) -> None:
    window, owner = identity_screen
    ui = window.identity_controls
    login(ui)
    users = manager(ui)
    users.controls["identity_create"].click()
    entered, release = Event(), Event()
    original = passwords.hash_password

    def hash_password(password: str) -> PasswordVerifier:
        assert current_thread() is not main_thread()
        entered.set()
        assert release.wait(10)
        return original(password)

    monkeypatch.setattr(identity_module, "hash_password", hash_password)
    try:
        submit(ui, username="Uncommitted", new_password=SECRET, confirm=SECRET)
        until(entered.is_set)
        window.close()
        assert ui.users is None and ui.form is None
        assert owner.snapshot().role is Role.GUEST
        assert not window.bridge.closed
    finally:
        release.set()
    until(lambda: window.bridge.closed and window.communication.closed)
    assert owner.join(0)
    reopened = LocalIdentity(owner.path)
    try:
        completed(reopened.ready)
        completed(reopened.login("Admin", SECRET))
        assert all(
            a.username != "Uncommitted" for a in completed(reopened.list_accounts())
        )
    finally:
        reopened.request_stop()
        assert reopened.join(5)


def test_failed_close_restores_login_controls_for_fresh_authentication(
    identity_screen: tuple[MainWindow, LocalIdentity], monkeypatch: pytest.MonkeyPatch
) -> None:
    window, owner = identity_screen
    app = window.bridge.application
    join = FakeSession.join
    unsafe = True

    def join_session(session: FakeSession, timeout: float | None = None) -> bool:
        return not unsafe and join(session, timeout)

    monkeypatch.setattr(FakeSession, "join", join_session)
    try:
        login(window.identity_controls)
        previous = owner.snapshot()
        assert previous.role is Role.ADMINISTRATOR
        completed(app.start())
        window.close()
        until(
            lambda: (
                window.snapshot.state is ApplicationState.FAULT
                and not window.bridge.closing
                and window.navbar.user.isEnabled()
            )
        )
        guest = owner.snapshot()
        assert guest.role is Role.GUEST and guest.revision > previous.revision
        assert window.navbar.user.text() == "Guest"
        assert window.isVisible() and window.snapshot.runtime_owned
        login(window.identity_controls)
        renewed = owner.snapshot()
        assert renewed.role is Role.ADMINISTRATOR and renewed.revision > guest.revision
    finally:
        unsafe = False
    window.close()
    until(lambda: window.bridge.closed and window.communication.closed)
    assert owner.join(0) and owner.snapshot().role is Role.GUEST


def test_logout_and_administrator_protection_keep_an_existing_synthetic_session_running(
    identity_screen: tuple[MainWindow, LocalIdentity],
) -> None:
    window, owner = identity_screen
    ui = window.identity_controls
    login(ui)
    # This is the controlled FakeSession, without engine, camera or PLC owners.
    completed(window.bridge.application.start())
    until(lambda: window.snapshot.state is ApplicationState.RUNNING)
    before = window.bridge.application.snapshot()
    assert before.state is ApplicationState.RUNNING
    users = manager(ui)
    select(users, "Admin")
    assert not users.controls["identity_disable"].isEnabled()
    ui.logout()
    until(lambda: ui.users is None)
    assert owner.snapshot().role is Role.GUEST
    assert window.bridge.application.snapshot() == before
    assert window.buttons["stop"].isEnabled()


def test_qt_sender_is_destroyed_before_late_worker_completion(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "data/security.sqlite3"
    initialize_administrator(path, "Admin", SECRET)
    owner = LocalIdentity(path)
    completed(owner.ready)
    host = QWidget()
    navbar = NavigationBar()
    navbar.setParent(host)
    controls = IdentityControls(navbar, owner, host)
    sender = next(
        child
        for child in controls.children()
        if child.metaObject().className() == "_Emitter"
    )
    destroyed = QSignalSpy(sender.destroyed)
    entered, release = Event(), Event()
    original = passwords.verify_password

    def verify(password: str, verifier: PasswordVerifier) -> bool:
        entered.set()
        assert release.wait(10)
        return original(password, verifier)

    monkeypatch.setattr(identity_module, "verify_password", verify)
    try:
        controls.login()
        submit(controls, username="Admin", password=SECRET)
        until(entered.is_set)
        controls.begin_close()
        host.deleteLater()
        until(lambda: destroyed.count() == 1)
        assert not release.is_set()
    finally:
        release.set()
        owner.request_stop()
        assert owner.join(5)
    QApplication.processEvents()
    assert owner.snapshot().role is Role.GUEST


def test_account_table_metadata_selection_and_readonly_contract(
    identity_screen: tuple[MainWindow, LocalIdentity],
) -> None:
    window, owner = identity_screen
    ui = window.identity_controls
    login(ui)
    users = manager(ui)
    assert users.table.columnCount() == 4
    headings = [users.table.horizontalHeaderItem(i) for i in range(4)]
    assert all(item is not None for item in headings)
    assert [item.text() for item in headings if item is not None] == [
        "Username",
        "Role",
        "Status",
        "Last Login",
    ]
    assert (
        "identity_close" not in users.controls
        and "identity_apply" not in users.controls
    )
    assert users.controls["identity_create"].text() == "New User"
    assert not users.controls["short_refresh"].icon().isNull()
    assert not users.status.isVisible()
    admin = select(users, "Admin")
    assert admin.last_login_utc is not None
    row = users.table.currentRow()
    item = users.table.item(row, 3)
    assert item is not None
    assert item.text() == admin.last_login_utc.astimezone().strftime("%Y-%m-%d %H:%M")
    worker = select(users, "Worker")
    item = users.table.item(users.table.currentRow(), 3)
    assert item is not None and worker.last_login_utc is not None
    completed(owner.create_account("AAA", Role.ENGINEER, SECRET))
    users.controls["short_refresh"].click()
    until(lambda: not users.busy)
    assert users.selected() == worker
    assert users.table.currentRow() == 2  # Stable ID survives insertion before it.
    never = users.table.item(0, 3)
    assert never is not None and never.text() == "—"
    for row in range(users.table.rowCount()):
        for column in range(4):
            item = users.table.item(row, column)
            assert item is not None
            assert not item.flags() & Qt.ItemFlag.ItemIsEditable
            assert item.data(Qt.ItemDataRole.UserRole) == users.accounts[row].account_id
            assert (
                item.data(Qt.ItemDataRole.AccessibleTextRole)
                == item.text()
                == item.toolTip()
            )
    users.table.clearSelection()
    assert users.selected() is None
    for key in ("identity_change_role", "identity_disable", "identity_reset"):
        assert not users.controls[key].isEnabled()
    users.controls["short_refresh"].click()
    until(lambda: not users.busy)
    assert users.selected() is None


def test_role_and_disable_dialogs_capture_target_and_support_cancel(
    identity_screen: tuple[MainWindow, LocalIdentity],
) -> None:
    window, owner = identity_screen
    ui = window.identity_controls
    login(ui)
    users = manager(ui)
    worker = select(users, "Worker")
    users.controls["identity_change_role"].click()
    role = ui.role_dialog
    assert role is not None and role.account.account_id == worker.account_id
    assert not role.submit.isEnabled()
    role.reject()
    assert ui.role_dialog is None and not users.busy
    users.controls["identity_change_role"].click()
    role = ui.role_dialog
    assert role is not None
    role.role.setCurrentIndex(role.role.findData(Role.ENGINEER))
    select(
        users, "Admin"
    )  # Programmatic selection cannot redirect the captured command.
    role.submit.click()
    until(lambda: ui.role_dialog is None and not users.busy)
    assert select(users, "Worker").role is Role.ENGINEER
    assert select(users, "Admin").role is Role.ADMINISTRATOR
    select(users, "Worker")
    users.controls["identity_disable"].click()
    confirmation = ui.confirmation
    assert confirmation is not None and "Worker" in confirmation.text()
    confirmation.reject()
    assert ui.confirmation is None and not users.busy
    assert select(users, "Worker").enabled
    users.controls["identity_disable"].click()
    select(users, "Admin")
    confirm_disable(ui)
    until(lambda: not users.busy)
    assert not select(users, "Worker").enabled
    assert select(users, "Admin").enabled
    assert owner.snapshot().role is Role.ADMINISTRATOR


def test_users_close_does_not_cancel_accepted_account_creation_or_restore_dialog(
    identity_screen: tuple[MainWindow, LocalIdentity], monkeypatch: pytest.MonkeyPatch
) -> None:
    window, owner = identity_screen
    ui = window.identity_controls
    login(ui)
    users = manager(ui)
    users.controls["identity_create"].click()
    entered, release = Event(), Event()
    original = passwords.hash_password

    def held(password: str) -> PasswordVerifier:
        entered.set()
        assert release.wait(10)
        return original(password)

    monkeypatch.setattr(identity_module, "hash_password", held)
    try:
        submit(ui, username="Accepted", new_password=SECRET, confirm=SECRET)
        until(entered.is_set)
        users.close()
        assert (ui.users, ui.form) == (None, None)
        assert owner.snapshot().role is Role.ADMINISTRATOR
        ui.manage_users()
        reopened = ui.users
        assert reopened is not None and reopened is not users and reopened.busy
        release.set()
        until(lambda: not reopened.busy)
        assert any(a.username == "Accepted" for a in reopened.accounts)
        assert ui.users is reopened and ui.form is None
    finally:
        release.set()


@pytest.mark.parametrize("language,theme", VISUAL_CASES)
def test_manage_users_at_preferred_size(
    qapp: QApplication, language: Language, theme: Theme
) -> None:
    ensure_test_fonts()
    host = QWidget()
    font = QFont()
    font.setFamilies(Style.font_families)
    host.setFont(font)
    apply_theme(host, theme)
    users = UsersDialog(host)
    stamp = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)
    operator = Account("shift", "Shared Shift", Role.OPERATOR, True, stamp)
    users.install_accounts(
        (
            operator,
            Account("eng", "Line Engineer", Role.ENGINEER, True, stamp),
            Account("admin", "Line Admin", Role.ADMINISTRATOR, True, stamp),
            Account("former", "Former Operator", Role.OPERATOR, False),
            *(
                Account(f"op{i}", f"Operator {i}", Role.OPERATOR, True)
                for i in range(8)
            ),
        ),
        operator.account_id,
    )
    users.present(language)
    try:
        assert users.size().toTuple() == (850, 500)
        users.show()
        users.resize(850, 500)
        qapp.processEvents()
        until(
            lambda: (
                sum(users.table.columnWidth(i) for i in range(4))
                == users.table.viewport().width()
                and all(
                    users.rect().contains(QRect(b.mapTo(users, QPoint()), b.size()))
                    for b in users.controls.values()
                )
            )
        )
        assert_account_table_columns(users)
        assert users.selected() == operator
        assert users.table.verticalScrollBar().maximum() > 0
        assert users.table.horizontalScrollBar().maximum() == 0
        if language in ("zh", "en", "de"):
            for column in range(4):
                assert users.table.columnWidth(column) >= max(
                    users.table.sizeHintForColumn(column),
                    users.table.horizontalHeader().sectionSizeHint(column),
                )
        status = users.table.item(0, 2)
        assert status is not None
        assert status.text() == text(
            "short_auto_login" if language in ("en", "zh") else "identity_auto_login",
            language,
        )
        assert (
            status.toolTip()
            == status.data(Qt.ItemDataRole.AccessibleTextRole)
            == (
                f"{text('identity_enabled', language)} · {text('identity_auto_login', language)}"
            )
        )
        rects = [
            QRect(button.mapTo(users, QPoint()), button.size())
            for button in users.controls.values()
        ]
        assert all(users.rect().contains(rect) for rect in rects)
        assert all(
            not left.intersects(right)
            for i, left in enumerate(rects)
            for right in rects[i + 1 :]
        )
        for button in users.controls.values():
            assert (
                button.height() >= Style.touch
                and button.toolTip()
                and button.accessibleName()
            )
        users.controls["identity_change_role"].setFocus()
        QTest.keyClick(users, Qt.Key.Key_Escape)
        assert not users.isVisible()
    finally:
        users.close()
        host.close()


def test_dismissed_role_dialog_ignores_delayed_success_without_retargeting(
    identity_screen: tuple[MainWindow, LocalIdentity], monkeypatch: pytest.MonkeyPatch
) -> None:
    window, owner = identity_screen
    ui = window.identity_controls
    login(ui)
    users = manager(ui)
    worker = select(users, "Worker")
    accepted: list[Future[Account]] = []
    delivery: Future[Account] = Future()
    original = owner.set_role

    def delayed(
        account_id: str, role: Role, *, revision: int, confirm_administrator: bool
    ) -> Future[Account]:
        accepted.append(
            original(
                account_id,
                role,
                revision=revision,
                confirm_administrator=confirm_administrator,
            )
        )
        return delivery

    monkeypatch.setattr(owner, "set_role", delayed)
    change_role(ui, Role.ENGINEER)
    dialog = ui.role_dialog
    assert dialog is not None and dialog.busy
    changed = completed(accepted[0])
    assert changed.account_id == worker.account_id
    dialog.reject()
    assert ui.role_dialog is None and not users.busy
    selected = select(users, "Admin")
    delivery.set_result(changed)
    QApplication.processEvents()
    assert ui.role_dialog is None and ui.users is users
    assert users.selected() == selected
    users.controls["short_refresh"].click()
    until(lambda: not users.busy)
    assert select(users, "Worker").role is Role.ENGINEER
    assert select(users, "Admin").role is Role.ADMINISTRATOR
