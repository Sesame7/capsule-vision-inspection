"""Real account service, masked forms and queued Qt delivery; isolated data only."""

from collections.abc import Iterator
from concurrent.futures import Future
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, current_thread, main_thread

import pytest
from PySide6.QtCore import QPoint, QRect, Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import (
    QApplication,
    QHeaderView,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QStyle,
    QStyleOptionButton,
    QWidget,
)
from tests.support.application import FakeSession, prepared
from tests.support.qt import (
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
    Role,
    SecurityStorageError,
    StorageState,
)
from capsule_vision.application.passwords import PasswordVerifier
from capsule_vision.display import LatestDisplayFrameStore
from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import LANGUAGES, Language
from capsule_vision.ui import identity_controls as identity_ui_module
from capsule_vision.ui.identity_controls import (
    CredentialDialog,
    IdentityControls,
    RoleDialog,
    UsersDialog,
)
from capsule_vision.ui.main_window import MainWindow
from capsule_vision.ui.production_bridge import QtProductionBridge
from capsule_vision.ui.shell import NavigationBar, NavigationMode
from capsule_vision.ui.style import Style, apply_theme, theme_colors

SECRET = "isolated UI test credential"
NEW_SECRET = "replacement UI test credential"


@pytest.mark.parametrize(
    "font_pixels,mode", [(15, "comfortable"), (26, "compact"), (34, "two_row")]
)
def test_account_menu_theme_and_language_in_each_navigation_mode(
    identity_screen: tuple[MainWindow, LocalIdentity],
    font_pixels: int,
    mode: NavigationMode,
) -> None:
    window, owner = identity_screen
    bar, ui = window.navbar, window.identity_controls
    bar.setStyleSheet(f"QWidget {{font-size: {font_pixels}px;}}")
    QApplication.processEvents()
    assert bar.mode == mode
    before = window.bridge.application.snapshot()
    selected: list[int] = []
    bar.page_selected.connect(selected.append)
    ui.controls["identity_login"].trigger()
    submit(ui, username="Admin", password=SECRET)
    until(lambda: ui.form is None)
    assert owner.snapshot().role is Role.ADMINISTRATOR
    assert bar.mode == mode and "Admin" in bar.user.accessibleName()
    ui.menu.popup(bar.user.mapToGlobal(QPoint(0, bar.user.height())))
    until(ui.menu.isVisible)
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
    assert bar.mode == mode and selected == []
    assert window.bridge.application.snapshot() == before


def test_navigation_guest_and_logged_in_caption_in_all_languages(
    identity_screen: tuple[MainWindow, LocalIdentity],
) -> None:
    window, owner = identity_screen
    bar, ui = window.navbar, window.identity_controls
    before = window.bridge.application.snapshot()
    button_size = bar.user.size()
    guest = owner.snapshot()
    for language in LANGUAGES:
        window.set_language(language)
        QApplication.processEvents()
        assert_full_caption(bar.user, "Guest")
        assert (
            bar.user.toolTip()
            == bar.user.accessibleName()
            == text("identity_guest", language)
        )
        assert ui.controls["identity_login"].text() == text("identity_login", language)
        assert bar.user.size() == button_size and owner.snapshot() == guest

    login(ui)
    authenticated = owner.snapshot()
    assert authenticated.account is not None
    for language in LANGUAGES:
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
    for language in LANGUAGES:
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
    owner.create_account("Worker", Role.OPERATOR, SECRET).result(5)
    owner.logout()
    ensure_test_fonts()
    candidate = prepared(tmp_path)
    session = FakeSession()
    application = ProductionApplication(lambda _p, _g: session, identity=owner)
    application.configure(candidate)
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


def assert_account_table_columns(dialog: UsersDialog) -> tuple[int, ...]:
    table, header = dialog.table, dialog.table.horizontalHeader()
    width = table.viewport().width()
    widths = tuple(table.columnWidth(i) for i in range(4))
    assert sum(widths) == width
    for actual, percent in zip(widths, (30, 22, 16, 32), strict=True):
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
            assert (
                item.text()
                == item.toolTip()
                == item.data(Qt.ItemDataRole.AccessibleTextRole)
            )
    return widths


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
    ui.switch_user()
    assert owner.snapshot().role is Role.GUEST
    dialog = submit(ui, username="Worker", password=SECRET)
    until(lambda: not dialog.busy)
    assert dialog.status.text() == text("identity_invalid", "en")
    submit(ui, username="Admin", password=NEW_SECRET)
    until(lambda: ui.form is None)
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
    assert users.controls["identity_reset"].isEnabled()
    assert users.controls["identity_disable"].toolTip() == text(
        "identity_last_admin", "en"
    )
    users.controls["identity_create"].click()
    form(ui).role.setCurrentIndex(form(ui).role.findData(Role.ADMINISTRATOR))
    submit(ui, username="Backup", new_password=SECRET, confirm=SECRET)
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
    select(users, "Admin")
    change_role(ui, Role.ENGINEER)
    until(lambda: ui.users is None)
    assert owner.snapshot().role is Role.ENGINEER
    assert "Engineer" in window.navbar.user.toolTip()
    assert not ui.controls["identity_manage"].isVisible()
    # Even a direct Qt method call reaches the real service authorization boundary.
    ui.manage_users()
    assert ui.users is None
    ui.switch_user()
    submit(ui, username="Worker", password=NEW_SECRET)
    until(lambda: ui.form is None)
    assert owner.snapshot().role is Role.ENGINEER


def test_self_disable_and_admin_reset_refresh_without_production_effect(
    identity_screen: tuple[MainWindow, LocalIdentity],
) -> None:
    window, owner = identity_screen
    ui = window.identity_controls
    before = window.bridge.application.snapshot()
    login(ui)
    completed(owner.create_account("Backup", Role.ADMINISTRATOR, SECRET))
    users = manager(ui)
    select(users, "Admin")
    users.controls["identity_reset"].click()
    submit(ui, new_password=NEW_SECRET, confirm=NEW_SECRET)
    until(lambda: ui.users is None and ui.form is None)
    assert owner.snapshot().role is Role.GUEST
    login(ui, password=NEW_SECRET)
    users = manager(ui)
    select(users, "Admin")
    users.controls["identity_disable"].click()
    confirm_disable(ui)
    until(lambda: ui.users is None)
    assert owner.snapshot().role is Role.GUEST
    assert window.navbar.user.text() == "Guest"
    assert window.bridge.application.snapshot() == before


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
        window.identity_controls.login()
        dialog = form(window.identity_controls)
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


def test_runtime_database_failure_is_sanitized_and_returns_guest(
    identity_screen: tuple[MainWindow, LocalIdentity], monkeypatch: pytest.MonkeyPatch
) -> None:
    window, owner = identity_screen
    before = window.bridge.application.snapshot()

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
    languages: tuple[Language, ...] = (
        "zh",
        "en",
        "zh-TW",
        "ja",
        "ko",
        "de",
        "it",
        "th",
        "hi",
        "ti",
    )
    before = window.bridge.application.snapshot()
    ui.login()
    credentials = form(ui)
    credentials.fields["identity_username"].setText("QA_{CAM01}_用户")
    credentials.fields["identity_password"].setText("unsubmitted fixture input")
    credentials.fields["identity_password"].setFocus()
    for language in languages:
        window.set_language(language)
        QApplication.processEvents()
        assert ui.form is credentials
        assert credentials.windowTitle() == text("identity_login", language)
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
    users = manager(ui)
    worker = select(users, "Worker")
    users.controls["identity_change_role"].click()
    role = ui.role_dialog
    assert role is not None
    role.role.setCurrentIndex(role.role.findData(Role.ENGINEER))
    role.role.setFocus()
    for language in languages:
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
        for language in languages:
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
    for language in languages:
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
    assert window.bridge.application.snapshot() == before


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
    completed(owner.create_account("Backup", Role.ADMINISTRATOR, SECRET))
    users = manager(ui)
    current = select(users, "Admin")
    completed(owner.set_role(current.account_id, Role.ENGINEER))
    until(lambda: ui.users is None)
    assert not ui.controls["identity_manage"].isVisible()
    assert owner.snapshot().role is Role.ENGINEER


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


def test_self_disable_keeps_an_existing_synthetic_production_session_running(
    identity_screen: tuple[MainWindow, LocalIdentity],
) -> None:
    window, owner = identity_screen
    ui = window.identity_controls
    login(ui)
    completed(owner.create_account("Backup", Role.ADMINISTRATOR, SECRET))
    # This is the controlled FakeSession, without engine, camera or PLC owners.
    completed(window.bridge.application.start())
    until(lambda: window.snapshot.state is ApplicationState.RUNNING)
    before = window.bridge.application.snapshot()
    assert before.state is ApplicationState.RUNNING
    users = manager(ui)
    select(users, "Admin")
    users.controls["identity_disable"].click()
    confirm_disable(ui)
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
    assert item is not None and item.text() == "—"
    completed(owner.create_account("AAA", Role.ENGINEER, SECRET))
    users.controls["short_refresh"].click()
    until(lambda: not users.busy)
    assert users.selected() == worker
    assert users.table.currentRow() == 2  # Stable ID survives insertion before it.
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


def test_users_and_role_geometry_all_supported_languages(qapp: QApplication) -> None:
    ensure_test_fonts()
    host = QWidget()
    users = UsersDialog(host)
    stamp = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)
    accounts = tuple(
        Account(str(i), "Account" + str(i), Role.OPERATOR, i != 2, stamp if i else None)
        for i in range(9)
    )
    accounts += (Account("long", "LongUser" * 8, Role.ADMINISTRATOR, True),)
    users.install_accounts(accounts)
    role = RoleDialog(accounts[-1], 1, users)
    try:
        for theme in ("dark", "light"):
            apply_theme(host, theme)
            for language in LANGUAGES:
                users.present(language)
                users.show()
                dispatched = Event()
                QTimer.singleShot(0, dispatched.set)
                until(dispatched.is_set)
                until(
                    lambda: (
                        sum(users.table.columnWidth(i) for i in range(4))
                        == users.table.viewport().width()
                        and all(
                            users.rect().contains(
                                QRect(b.mapTo(users, QPoint()), b.size())
                            )
                            for b in users.controls.values()
                        )
                    )
                )
                available = users.screen().availableGeometry()
                frame = users.frameGeometry().width() - users.width()
                assert users.width() == min(850, available.width() - frame)
                assert (
                    7 * Style.touch
                    <= users.table.viewport().height()
                    <= 10 * Style.touch
                )
                assert users.table.verticalScrollBar().maximum() > 0
                assert users.table.horizontalScrollBar().maximum() == 0
                assert_account_table_columns(users)
                for key, button in users.controls.items():
                    assert users.rect().contains(
                        QRect(button.mapTo(users, QPoint()), button.size())
                    )
                    assert button.height() >= Style.touch
                    assert button.accessibleName() and button.toolTip()
                    if key != "short_refresh":
                        assert_full_caption(button, button.text())
                long_item = users.table.item(len(accounts) - 1, 0)
                assert long_item is not None
                assert long_item.text() == long_item.toolTip() == accounts[-1].username
                assert users.table.columnWidth(
                    0
                ) < users.table.fontMetrics().horizontalAdvance(long_item.text())
                select(users, accounts[-1].username)
                assert users.selected() == accounts[-1]
                assert (
                    long_item.data(Qt.ItemDataRole.AccessibleTextRole)
                    == accounts[-1].username
                )
                ys = {
                    button.mapTo(users, QPoint()).y()
                    for button in users.controls.values()
                }
                if language in ("zh", "en"):
                    assert len(ys) == 1
                role.present(language)
                role.show()
                QApplication.processEvents()
                assert role.role.accessibleName() and not role.submit.isEnabled()
                for button in role.buttons.buttons():
                    assert isinstance(button, QPushButton)
                    assert button.height() >= Style.touch
                    assert_full_caption(button, button.text())
                role.hide()
        users.present("en")
        users.show()
        users.setFocus()
        from PySide6.QtTest import QTest

        QTest.keyClick(users, Qt.Key.Key_Escape)
        assert not users.isVisible()
    finally:
        role.close()
        users.close()
        host.close()


@pytest.mark.parametrize("count", [0, 2, 10])
def test_users_layout_counts_resize_and_complete_timestamps(
    qapp: QApplication, count: int
) -> None:
    ensure_test_fonts()
    host = QWidget()
    apply_theme(host, "dark")
    users = UsersDialog(host)
    stamp = datetime(2099, 12, 31, 15, 59, tzinfo=UTC)
    accounts = tuple(
        Account(
            str(i),
            ("LongUsername" * 5) if i == 1 else f"User{i}",
            Role.OPERATOR,
            True,
            stamp,
        )
        for i in range(count)
    )
    try:
        users.present("en")
        users.show()
        QApplication.processEvents()
        original_size = users.size()
        users.install_accounts(accounts)
        until(lambda: users.table.horizontalScrollBar().maximum() == 0)
        assert users.size() == original_size
        assert users.table.rowCount() == count
        assert users.table.horizontalScrollBar().maximum() == 0
        assert_account_table_columns(users)
        assert (users.table.verticalScrollBar().maximum() > 0) == (count == 10)
        for row in range(count):
            item = users.table.item(row, 3)
            assert item is not None
            assert item.text() == stamp.astimezone().strftime("%Y-%m-%d %H:%M")
            assert users.table.fontMetrics().horizontalAdvance(
                item.text()
            ) + 12 <= users.table.columnWidth(3)
        if count:
            selected = select(users, accounts[-1].username)
        else:
            selected = None
        identities = dict(users.controls)
        for width in (850, 560, 850, 500, 850, 420, 850):
            users.resize(width, 500)
            until(
                lambda: (
                    sum(users.table.columnWidth(i) for i in range(4))
                    == users.table.viewport().width()
                )
            )
            QApplication.processEvents()
            assert users.controls == identities
            assert users.selected() == selected
            assert users.table.horizontalScrollBar().maximum() == 0
            assert_account_table_columns(users)
            rectangles = [
                QRect(b.mapTo(users, QPoint()), b.size())
                for b in users.controls.values()
            ]
            assert all(users.rect().contains(r) for r in rectangles)
            assert all(
                not left.intersects(right)
                for i, left in enumerate(rectangles)
                for right in rectangles[i + 1 :]
            )
            global_y = users.controls["identity_create"].mapTo(users, QPoint()).y()
            account_y = (
                users.controls["identity_change_role"].mapTo(users, QPoint()).y()
            )
            if width == 850:
                assert global_y == account_y
            elif width <= 500:
                assert global_y < account_y
            assert global_y > users.table.geometry().bottom()
            signature = tuple(r.getRect() for r in rectangles)
            resized = QSignalSpy(users.table.horizontalHeader().sectionResized)
            for _ in range(6):
                QApplication.processEvents()
                assert (
                    tuple(
                        QRect(b.mapTo(users, QPoint()), b.size()).getRect()
                        for b in users.controls.values()
                    )
                    == signature
                )
            assert resized.count() == 0
        narrow_columns = [users.table.columnWidth(i) for i in range(4)]
        users.resize(1000, 500)
        until(lambda: users.table.columnWidth(0) > narrow_columns[0])
        assert users.table.columnWidth(3) > narrow_columns[3]
        assert users.table.columnWidth(1) > narrow_columns[1]
        assert users.table.columnWidth(2) > narrow_columns[2]
        assert_account_table_columns(users)
        users.install_accounts(tuple(reversed(accounts)))
        QApplication.processEvents()
        assert users.selected() == selected
        assert users.size().width() == 1000
        for key in ("identity_change_role", "identity_disable", "identity_reset"):
            assert users.controls[key].isEnabled() == bool(count)
    finally:
        users.close()
        host.close()


@pytest.mark.parametrize("pixels", [30, 48])
def test_users_enlarged_fonts_wrap_actions_without_losing_focus(
    qapp: QApplication, pixels: int
) -> None:
    host = QWidget()
    apply_theme(host, "light")
    users = UsersDialog(host)
    users.install_accounts(
        (Account("user", "A long account username " * 2, Role.OPERATOR, True),)
    )
    users.present("en")
    users.show()
    users.resize(850, 500)
    button = users.controls["identity_reset"]
    button.setFocus()
    try:
        users.setStyleSheet(f"QWidget {{font-size: {pixels}px;}}")
        until(
            lambda: (
                users.controls["identity_create"].mapTo(users, QPoint()).y()
                < users.controls["identity_change_role"].mapTo(users, QPoint()).y()
            )
        )
        QApplication.processEvents()
        assert button.hasFocus()
        assert users.table.horizontalScrollBar().maximum() == 0
        for action in users.controls.values():
            assert users.rect().contains(
                QRect(action.mapTo(users, QPoint()), action.size())
            )
            assert action.height() >= Style.touch
            if action.text():
                assert_full_caption(action, action.text())
                option = QStyleOptionButton()
                action.initStyleOption(option)
                content = action.style().subElementRect(
                    QStyle.SubElement.SE_PushButtonContents, option, action
                )
                assert action.fontMetrics().height() <= content.height()
        assert users.table.rowHeight(0) >= users.table.fontMetrics().height() + 12
        users.setStyleSheet("")
        until(
            lambda: (
                len({b.mapTo(users, QPoint()).y() for b in users.controls.values()})
                == 1
            )
        )
        assert button.hasFocus()
    finally:
        users.close()
        host.close()


def test_account_columns_depend_only_on_viewport_not_records_or_selection(
    qapp: QApplication,
) -> None:
    host = QWidget()
    apply_theme(host, "dark")
    users = UsersDialog(host)
    users.table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
    users.present("en")
    users.show()
    try:
        short = tuple(
            Account(str(i), f"User{i}", Role.OPERATOR, True) for i in range(10)
        )
        long = tuple(
            Account(str(i), f"{i}" + "LongUsername" * 5, Role.ENGINEER, i != 2)
            for i in range(10)
        )
        expected: tuple[int, ...] | None = None
        for records in ((), short[:2], short, long, short[:2], ()):
            users.install_accounts(records)
            until(
                lambda: (
                    sum(users.table.columnWidth(i) for i in range(4))
                    == users.table.viewport().width()
                )
            )
            widths = assert_account_table_columns(users)
            if expected is None:
                expected = widths
            assert widths == expected
            if records:
                selected = select(users, records[-1].username)
                spy = QSignalSpy(users.table.horizontalHeader().sectionResized)
                users.present("zh")
                QApplication.processEvents()
                assert users.selected() == selected
                assert assert_account_table_columns(users) == expected
                assert spy.count() == 0
        users.install_accounts(long)
        select(users, long[1].username)
        for language in ("zh", "en", "af", "ti"):
            users.present(language)
            QApplication.processEvents()
            assert assert_account_table_columns(users) == expected
            selected = users.selected()
            assert selected is not None and selected.account_id == long[1].account_id
        for pixels in (30, 48, 15):
            users.table.setStyleSheet(f"QTableWidget {{font-size: {pixels}px;}}")
            QApplication.processEvents()
            assert assert_account_table_columns(users) == expected
        users.table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        until(lambda: tuple(users.table.columnWidth(i) for i in range(4)) != expected)
        assert_account_table_columns(users)
        selected = users.selected()
        assert selected is not None and selected.account_id == long[1].account_id
    finally:
        users.close()
        host.close()


def test_users_long_translated_captions_wrap_with_full_accessibility(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    captions = {
        "identity_change_role": "Change the selected account role",
        "identity_disable": "Disable the selected user account",
        "identity_enable": "Enable the selected user account",
        "identity_reset": "Reset the selected account password",
        "identity_role": "Configured account authorization role",
        "identity_status": "Current account availability status",
    }

    def translated(key: str, language: Language) -> str:
        return (
            captions.get(key, text(key, language))
            if language == "af"
            else text(key, language)
        )

    monkeypatch.setattr(identity_ui_module, "text", translated)
    host = QWidget()
    apply_theme(host, "dark")
    users = UsersDialog(host)
    users.install_accounts((Account("user", "Account", Role.OPERATOR, True),))
    users.present("af")
    users.show()
    users.resize(850, 500)
    try:
        until(
            lambda: (
                users.controls["identity_create"].mapTo(users, QPoint()).y()
                < users.controls["identity_change_role"].mapTo(users, QPoint()).y()
            )
        )
        for key in ("identity_change_role", "identity_disable", "identity_reset"):
            button = users.controls[key]
            assert_full_caption(button, captions[key])
            assert button.toolTip() == button.accessibleName() == captions[key]
            assert users.rect().contains(
                QRect(button.mapTo(users, QPoint()), button.size())
            )
        for column in (1, 2):
            heading = users.table.horizontalHeaderItem(column)
            assert heading is not None
            assert (
                heading.toolTip()
                == heading.data(Qt.ItemDataRole.AccessibleTextRole)
                == heading.text()
            )
        assert users.table.horizontalScrollBar().maximum() == 0
        users.controls["identity_reset"].setFocus()
        users.present("en")
        until(
            lambda: (
                len({b.mapTo(users, QPoint()).y() for b in users.controls.values()})
                == 1
            )
        )
        assert users.controls["identity_reset"].hasFocus()
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

    def delayed(account_id: str, role: Role) -> Future[Account]:
        accepted.append(original(account_id, role))
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
