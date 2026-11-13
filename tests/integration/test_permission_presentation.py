"""Permission presentation with real local sessions and isolated saved Recipes."""

import hashlib
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from threading import Event

import pytest
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox
from tests.support.application import WAIT, ReplayEngine
from tests.support.qt import close_window, until
from tests.support.startup import (
    TEST_PASSWORD,
    authenticated_startup,
    close,
    provision_account,
    saved_data,
)

from capsule_vision.application import ApplicationState
from capsule_vision.application.identity_models import AuthorizationError, Role
from capsule_vision.application.production_application import (
    SourceProductionApplication,
)
from capsule_vision.application.source_control import SourceControlState
from capsule_vision.application.startup import load_startup
from capsule_vision.configuration import (
    load_camera_sources,
    load_model_definition,
    load_recipe,
    save_camera_sources,
    save_model_definition,
    save_recipe,
)
from capsule_vision.configuration.preferences import load_preferences
from capsule_vision.localization.catalog import text
from capsule_vision.ui.bootstrap import create_window
from capsule_vision.ui.future_delivery import FutureDelivery
from capsule_vision.ui.main_window import MainWindow


@pytest.fixture
def screen(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[MainWindow]:
    data = saved_data(tmp_path)
    sources = load_camera_sources(data.sources)
    save_camera_sources(data.sources, replace(sources, loop=True))
    original = load_recipe(data.recipes / "local.json")
    model = load_model_definition(data.models / "general/model.json")
    directory = data.models / "alternate"
    directory.mkdir()
    artifact = directory / "model.vimosln"
    artifact.write_bytes(b"second synthetic model")
    save_model_definition(
        directory / "model.json",
        replace(
            model,
            model_id="alternate",
            display_name="Alternate Model",
            model_path=str(artifact),
            expected_sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(),
        ),
    )
    other = replace(
        original,
        model_id="alternate",
        recipe_id="other",
        display_name="Second saved",
        product_code="SECOND",
        capsule=replace(
            original.capsule,
            size=replace(original.capsule.size, enabled=True, target_length=47),
        ),
        defaults=replace(
            original.defaults,
            vimo=replace(original.defaults.vimo, disabled_labels=("Dent",)),
        ),
    )
    save_recipe(data.recipes / "other.json", other)
    startup = authenticated_startup(data, engine_factory=lambda _: ReplayEngine())
    owner = startup.application.identity
    assert owner is not None
    for role in (Role.OPERATOR, Role.ENGINEER):
        provision_account(owner, role.name, role, TEST_PASSWORD)

    def labels(path: Path, **_kwargs: object) -> tuple[str, ...]:
        return (
            ("Scratch", "Dent", "dent")
            if path.parent.name == "alternate"
            else ("Chip", "Dent", "dent")
        )

    monkeypatch.setattr("capsule_vision.ui.recipe_page.read_model_labels", labels)
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    window.show()
    try:
        yield window
    finally:
        if startup.editor is not None:
            startup.editor.discard()
            startup.editor.machine_editor.discard()
            if startup.editor.camera_sources is not None:
                startup.editor.camera_sources.discard()
        close_window(window)
        delivery.detach()


def login(window: MainWindow, role: Role) -> None:
    owner = window.bridge.application.identity
    assert owner is not None
    if role is Role.GUEST:
        owner.logout()
    else:
        owner.login(
            "TestAdmin" if role is Role.ADMINISTRATOR else role.name, TEST_PASSWORD
        ).result(WAIT)
    window.identity_controls.refresh()


@pytest.mark.parametrize("role", list(Role))
@pytest.mark.parametrize("protocol", [0, 1])
def test_page_controls_combine_role_and_state(
    screen: MainWindow, role: Role, protocol: int
) -> None:
    window = screen
    # Select each FINS protocol as Administrator before testing the observed role.
    window.communication.protocol.setCurrentIndex(protocol)
    login(window, role)
    engineer, operator = role >= Role.ENGINEER, role >= Role.OPERATOR
    assert window.pages.count() == 5
    assert window.buttons["operation"].isVisible()
    assert window.buttons["operation"].isEnabled() == operator
    if not operator:
        assert window.buttons["operation"].toolTip() == text(
            "permission_operator", window.language
        )
    assert window.buttons["batch"].isEnabled() == operator
    assert window.production_recipes.recipes.isEnabled() == operator
    window.navigate(1)
    page = window.recipe_page
    until(lambda: not page.labels_loading)
    assert page.recipes.isEnabled() and tuple(page.label_checks) == (
        "Chip",
        "Dent",
        "dent",
    )
    assert page.metadata["display_name"].isReadOnly() != engineer
    for key in ("save", "new_recipe", "model_import", "model_rename", "cache_prepare"):
        assert page.buttons[key].isEnabled() == engineer
    assert page.model.isEnabled() == engineer
    window.navigate(2)
    comm = window.communication
    assert comm.reporting_panel is not None
    assert comm.reporting_panel.enabled.isEnabled() == engineer
    assert comm.write_button.isEnabled() == engineer
    assert comm.save_settings.isEnabled() == engineer
    assert comm.manual_button.isEnabled()
    assert comm.advanced_button.isEnabled() == (protocol == 0)
    assert comm.advanced_button.isVisible() == (protocol == 0)
    assert comm.web_panel.port.isEnabled() == engineer
    window.navigate(3)
    window.navigate(4)
    vision = window.vision_workspace
    assert vision is not None
    until(lambda: not window.editing.busy)
    assert vision.buttons["save"].isEnabled() == engineer
    assert vision.coordinates["X"].isReadOnly() != engineer
    assert vision.channel.isEnabled() and vision.buttons["vision_import"].isEnabled()
    assert vision.folder.isReadOnly() != engineer


def test_switch_keeps_engineering_controls_until_successful_replacement(
    screen: MainWindow,
) -> None:
    window = screen
    owner = window.bridge.application.identity
    assert owner is not None
    ui = window.identity_controls
    before = owner.snapshot()
    production = window.bridge.application.snapshot()
    ui.controls["identity_switch"].trigger()
    dialog = ui.form
    assert dialog is not None

    def assert_engineer() -> None:
        assert owner.snapshot() == before
        assert window.communication.write_button.isEnabled()
        assert window.communication.save_settings.isEnabled()
        assert not window.recipe_page.metadata["display_name"].isReadOnly()

    assert_engineer()
    dialog.fields["identity_username"].setText("OPERATOR")
    dialog.fields["identity_password"].setText("wrong")
    dialog.submit.click()
    assert_engineer()
    until(lambda: not dialog.busy)
    assert_engineer()
    dialog.reject()
    assert_engineer()
    ui.switch_user()
    retry = ui.form
    assert retry is not None
    retry.fields["identity_username"].setText("OPERATOR")
    retry.fields["identity_password"].setText(TEST_PASSWORD)
    retry.submit.click()
    until(lambda: ui.form is None)
    assert owner.snapshot().role is Role.OPERATOR
    assert not window.communication.write_button.isEnabled()
    assert not window.communication.save_settings.isEnabled()
    assert window.recipe_page.metadata["display_name"].isReadOnly()
    assert window.buttons["batch"].isEnabled()
    assert window.bridge.application.snapshot() == production


@pytest.mark.parametrize("role", [Role.GUEST, Role.OPERATOR])
def test_browsing_saved_recipes_preserves_dirty_engineering_state(
    screen: MainWindow, tmp_path: Path, role: Role
) -> None:
    window = screen
    window.navigate(1)
    page, session = window.recipe_page, window.editing.session
    assert session is not None and session.entry is not None
    entry = session.entry
    session.draft.display_name = "Unsaved engineer edit"
    draft = session.draft
    definition = draft.to_recipe()
    page.show_draft()
    before = window.bridge.application.snapshot()
    files = {p: p.read_bytes() for p in (tmp_path / "data").rglob("*.json")}
    login(window, role)
    assert page.metadata["display_name"].text() == entry.recipe.display_name
    idx = page.recipes.findText("Second saved")
    page.recipes.activated.emit(idx)
    until(lambda: not page.labels_loading)
    assert page.recipes.currentIndex() == idx
    assert page.metadata["display_name"].text() == "Second saved"
    assert page.metadata["product_code"].text() == "SECOND"
    assert page.model.currentText().startswith("Alternate Model")
    assert tuple(page.label_checks) == ("Scratch", "Dent", "dent")
    assert (
        page.capsule_enabled["size"].isChecked()
        and "47" in page.expand_tuning["size"].text()
    )
    assert (
        not page.label_checks["Dent"].isChecked()
        and page.label_checks["dent"].isChecked()
    )
    page.buttons["save"].click()
    page.buttons["recipe_discard"].click()
    page.buttons["reload"].click()
    assert all(path.read_bytes() == value for path, value in files.items())
    window.set_language("ja")
    window.navigate(0)
    window.navigate(1)
    assert page.recipes.currentIndex() == idx
    assert (
        session.entry is entry
        and session.draft is draft
        and draft.to_recipe() == definition
    )
    assert window.bridge.application.snapshot() == before
    assert all(
        path.read_bytes() == value
        for path, value in files.items()
        if path.name != "app.json"
    )
    # Language has its own explicit preference write; browsing leaves the Recipe ID intact.
    assert (
        load_preferences(tmp_path / "data/app.json").last_applied_recipe_id == "local"
    )
    login(window, Role.ENGINEER)
    assert page.recipes.currentText() == entry.recipe.display_name
    assert (
        page.metadata["display_name"].text() == "Unsaved engineer edit"
        and session.dirty
    )


def test_invalid_or_missing_browsed_definition_keeps_draft(
    screen: MainWindow, tmp_path: Path
) -> None:
    session = screen.editing.session
    assert session is not None
    entry, draft = session.entry, session.draft
    login(screen, Role.GUEST)
    screen.navigate(1)
    page = screen.recipe_page
    idx = page.recipes.findText("Second saved")
    target = tmp_path / "data/recipes/other.json"
    target.write_text("{", encoding="utf-8")
    page.recipes.activated.emit(idx)
    assert (
        page.recipes.currentIndex() == -1 and not page.metadata["display_name"].text()
    )
    assert page.status.toolTip() and session.entry is entry and session.draft is draft
    target.unlink()
    page.recipes.activated.emit(idx)
    assert page.recipes.currentIndex() == -1 and session.entry is entry


def test_guest_can_observe_and_stop_running_replay(screen: MainWindow) -> None:
    app = screen.bridge.application
    assert isinstance(app, SourceProductionApplication)
    login(screen, Role.OPERATOR)
    app.start().result(WAIT)
    screen.bridge.refresh()
    login(screen, Role.GUEST)
    running = app.snapshot()
    assert running.state is ApplicationState.RUNNING
    assert screen.buttons["stop"].isEnabled()
    assert not screen.buttons["pause"].isEnabled()
    assert screen.buttons["pause"].toolTip() == text(
        "permission_operator", screen.language
    )
    until(
        lambda: (
            app.snapshot().counters.cycles_completed > running.counters.cycles_completed
        )
    )
    login(screen, Role.OPERATOR)
    app.pause_source().result(WAIT)
    screen.bridge.refresh()
    assert (
        screen.buttons["operation"].isEnabled() and screen.buttons["pause"].isEnabled()
    )
    login(screen, Role.GUEST)
    for key in ("operation", "pause"):
        button = screen.buttons[key]
        assert button.isVisible() and not button.isEnabled()
        assert button.toolTip() == text("permission_operator", screen.language)
    assert screen.buttons["stop"].isEnabled()
    screen.buttons["stop"].click()
    until(lambda: app.snapshot().state is ApplicationState.READY)
    screen.bridge.refresh()
    assert not screen.buttons["operation"].isEnabled()
    assert screen.buttons["operation"].toolTip() == text(
        "permission_operator", screen.language
    )


def test_auto_login_and_manual_login_grant_the_same_production_controls(
    qapp: QApplication, tmp_path: Path
) -> None:
    data = saved_data(tmp_path)
    save_camera_sources(
        data.sources, replace(load_camera_sources(data.sources), loop=True)
    )
    saved = authenticated_startup(data)
    try:
        owner = saved.application.identity
        assert owner is not None
        operator = provision_account(
            owner, "Shared Shift", Role.OPERATOR, TEST_PASSWORD
        )
        owner.set_auto_login(operator.account_id, True).result(WAIT)
    finally:
        close(saved)

    engine = ReplayEngine()
    startup = load_startup(data, engine_factory=lambda _: engine)
    app = startup.application
    assert isinstance(app, SourceProductionApplication) and app.identity is not None
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    try:
        window.show()
        primary, middle = window.buttons["operation"], window.buttons["pause"]
        operator_detail = (
            f"{operator.username} · {text('identity_operator', window.language)}"
        )
        until(
            lambda: (
                window.navbar.user.accessibleName() == operator_detail
                and primary.isEnabled()
            )
        )
        assert app.identity.snapshot().role is Role.OPERATOR
        assert app.snapshot().state is ApplicationState.READY and not engine.calls

        primary.click()
        until(lambda: middle.isEnabled())
        middle.click()
        until(
            lambda: primary.accessibleName() == text("replay_continue", window.language)
        )
        paused = app.control_snapshot()
        assert paused is not None and paused.state is SourceControlState.PAUSED
        running = app.snapshot()
        window.identity_controls.controls["identity_logout"].trigger()
        assert window.navbar.user.text() == "Guest"
        assert not primary.isEnabled() and not middle.isEnabled()
        assert window.buttons["stop"].isEnabled() and app.control_snapshot() == paused
        assert app.snapshot().run_id == running.run_id

        window.identity_controls.controls["identity_login"].trigger()
        form = window.identity_controls.form
        assert form is not None
        form.fields["identity_username"].setText(operator.username)
        form.fields["identity_password"].setText(TEST_PASSWORD)
        form.submit.click()
        until(lambda: window.identity_controls.form is None and primary.isEnabled())
        assert middle.isEnabled() and app.identity.snapshot().role is Role.OPERATOR
        assert (
            app.snapshot().run_id == running.run_id and app.control_snapshot() == paused
        )
        window.identity_controls.controls["identity_logout"].trigger()
        assert app.snapshot().state is ApplicationState.RUNNING
    finally:
        close_window(window)
        delivery.detach()
    assert engine.closed and app.identity.join(0)


def test_stale_reader_never_fills_browsed_recipe(
    screen: MainWindow, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    entered, release = Event(), Event()

    def held(*_args: object, **_kwargs: object) -> tuple[str, ...]:
        entered.set()
        assert release.wait(WAIT)
        return ("Old model",)

    monkeypatch.setattr("capsule_vision.ui.recipe_page.read_model_labels", held)
    page = screen.recipe_page
    target = tmp_path / "data/recipes/other.json"
    save_recipe(target, replace(load_recipe(target), model_id=None))
    screen.navigate(1)
    try:
        until(entered.is_set)
        login(screen, Role.GUEST)
        page.recipes.activated.emit(page.recipes.findText("Second saved"))
    finally:
        release.set()
    until(lambda: not page.labels_loading)
    assert not page.label_checks and page.selected_model() is None
    assert page.metadata["display_name"].text() == "Second saved"


def test_device_and_authorization_errors_stay_distinct(screen: MainWindow) -> None:
    screen.bridge.command_failed.emit("start", AuthorizationError("Operator required"))
    assert text("permission_operator", screen.language) in screen.event_label.toolTip()
    screen.bridge.command_failed.emit("start", ValueError("bad configuration"))
    assert "ValueError" in screen.event_label.toolTip()


def test_fault_reset_is_operator_only_and_close_remains_available(
    screen: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    init = ReplayEngine.__init__

    def failing(engine: ReplayEngine) -> None:
        init(engine, fail="fatal")

    monkeypatch.setattr(ReplayEngine, "__init__", failing)
    app = screen.bridge.application
    assert isinstance(app, SourceProductionApplication)
    login(screen, Role.OPERATOR)
    app.step_source()
    until(
        lambda: (
            app.snapshot().state is ApplicationState.FAULT
            and not app.snapshot().runtime_owned
        )
    )
    screen.bridge.refresh()
    login(screen, Role.GUEST)
    assert not screen.buttons["secondary"].isEnabled()
    login(screen, Role.OPERATOR)
    assert screen.buttons["secondary"].isEnabled()
    screen.buttons["secondary"].click()
    assert app.snapshot().state is ApplicationState.READY


def test_disabling_current_account_closes_draft_form_and_keeps_unsaved_data(
    screen: MainWindow,
) -> None:
    session, owner = screen.editing.session, screen.bridge.application.identity
    assert session is not None and owner is not None and session.entry is not None
    author = next(
        a for a in owner.list_accounts().result(WAIT) if a.role is Role.ENGINEER
    )
    login(screen, Role.ENGINEER)
    screen.navigate(1)
    page = screen.recipe_page
    entry = session.entry
    session.draft.display_name = "Unpublished"
    page.show_draft()
    page.create_dialog(False)
    dialog = page.findChild(QDialog)
    assert dialog is not None and dialog.isVisible()
    owner.login("TestAdmin", TEST_PASSWORD).result(WAIT)
    owner.set_enabled(author.account_id, False).result(WAIT)
    owner.logout()
    until(lambda: not any(d.isVisible() for d in page.findChildren(QDialog)))
    assert page.metadata["display_name"].isReadOnly()
    assert page.metadata["display_name"].text() == entry.recipe.display_name
    assert session.entry is entry and session.draft.display_name == "Unpublished"
    owner.login("TestAdmin", TEST_PASSWORD).result(WAIT)
    until(lambda: not page.metadata["display_name"].isReadOnly())
    assert page.metadata["display_name"].text() == "Unpublished"


def test_guest_navigation_and_cancelled_close_preserve_vision_draft(
    screen: MainWindow, tmp_path: Path
) -> None:
    screen.navigate(4)
    vision = screen.vision_workspace
    assert vision is not None and vision.sources is not None
    until(lambda: not screen.editing.busy)
    sources = vision.sources
    before = (tmp_path / "data/camera_sources.json").read_bytes()
    vision.loop_changed(not sources.saved.loop)
    draft = sources.definition
    assert sources.dirty
    login(screen, Role.GUEST)
    screen.navigate(1)
    assert screen.pages.currentWidget() is screen.recipe_page
    assert sources.dirty and sources.definition == draft
    assert not screen.close()
    until(
        lambda: (
            screen.vision_workspace is not None
            and any(
                d.isVisible() for d in screen.vision_workspace.findChildren(QMessageBox)
            )
        )
    )
    workspace = screen.vision_workspace
    assert workspace is not None
    box = next(d for d in workspace.findChildren(QMessageBox) if d.isVisible())
    save = next(
        b
        for b in box.buttons()
        if box.buttonRole(b) == QMessageBox.ButtonRole.AcceptRole
    )
    assert not save.isEnabled()
    box.reject()
    assert sources.dirty and sources.definition == draft
    assert (tmp_path / "data/camera_sources.json").read_bytes() == before
    login(screen, Role.ENGINEER)
    assert workspace.buttons["save"].isEnabled()
