"""Real account service, masked forms and queued Qt delivery; isolated data only."""

from collections.abc import Iterator
from concurrent.futures import Future
from pathlib import Path
from threading import Event, current_thread, main_thread

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtGui import QColor
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication, QLineEdit, QWidget
from tests.support.application import FakeSession, prepared
from tests.support.qt import close_window, ensure_test_fonts, until

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
from capsule_vision.ui.identity_controls import (
    CredentialDialog,
    IdentityControls,
    UsersDialog,
)
from capsule_vision.ui.main_window import MainWindow
from capsule_vision.ui.production_bridge import QtProductionBridge
from capsule_vision.ui.shell import NavigationBar
from capsule_vision.ui.style import theme_colors

SECRET = "isolated UI test credential"
NEW_SECRET = "replacement UI test credential"


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
    users.controls["identity_disable"].click()
    until(lambda: not users.busy)
    assert users.status.text() == text("identity_last_admin", "en")
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
    until(lambda: not users.busy)
    assert not select(users, "Worker").enabled
    users.controls["identity_disable"].click()
    until(lambda: not users.busy)
    assert select(users, "Worker").enabled
    users.role.setCurrentIndex(users.role.findData(Role.ENGINEER))
    users.controls["identity_apply"].click()
    until(lambda: not users.busy)
    assert select(users, "Worker").role is Role.ENGINEER
    users.controls["identity_reset"].click()
    submit(ui, new_password=NEW_SECRET, confirm=NEW_SECRET)
    until(lambda: ui.form is None and not users.busy)
    assert worker.account_id == select(users, "Worker").account_id
    select(users, "Admin")
    users.role.setCurrentIndex(users.role.findData(Role.ENGINEER))
    users.controls["identity_apply"].click()
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
        assert dialog.status.text() == text(
            "identity_storage" if corrupt else "identity_missing", "en"
        )
        window.set_language("zh")
        assert dialog.status.text() == text(
            "identity_storage" if corrupt else "identity_missing", "zh"
        )
        window.set_language("af")
        assert dialog.status.text() == text(
            "identity_storage" if corrupt else "identity_missing", "en"
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
