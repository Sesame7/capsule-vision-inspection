"""Engineering command boundaries use real temporary accounts and isolated IO."""

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Event

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox
from tests.support.application import WAIT, ReplayEngine
from tests.support.capsule_images import SETTINGS
from tests.support.qt import close_window, until
from tests.support.startup import (
    TEST_PASSWORD,
    authenticated_startup,
    close,
    saved_data,
)

from capsule_vision.application.basler import BaslerDevice
from capsule_vision.application.identity import LocalIdentity
from capsule_vision.application.identity_models import (
    AuthorizationError,
    Role,
    StaleSessionError,
)
from capsule_vision.application.model_preparation import ModelPreparationRequest
from capsule_vision.application.startup import Startup
from capsule_vision.communication import FinsMemoryArea
from capsule_vision.communication.operations import (
    CommunicationRequest,
    CommunicationResponse,
    RegisterWatchGroup,
    RequestKind,
)
from capsule_vision.configuration import load_recipe, save_recipe
from capsule_vision.configuration.communication_preferences import (
    CommunicationPreferences,
    WebPreferences,
    save_communication_preferences,
)
from capsule_vision.inference import SegmentationModuleInfo, read_model_labels
from capsule_vision.localization.catalog import text
from capsule_vision.ui.bootstrap import create_window
from capsule_vision.ui.communication_page import CommunicationPage
from capsule_vision.ui.future_delivery import FutureDelivery
from capsule_vision.ui.recipe_tuning import PreviewMeasurement
from capsule_vision.web import ProductionSnapshotService
from capsule_vision.web.runtime import WebRuntime, WebState


@pytest.fixture
def engineering(tmp_path: Path) -> Iterator[Startup]:
    data = saved_data(tmp_path)
    save_recipe(
        data.recipes / "spare.json",
        replace(load_recipe(data.recipes / "local.json"), recipe_id="spare"),
    )
    startup = authenticated_startup(data)
    try:
        yield startup
    finally:
        close(startup)


def sign_in(owner: LocalIdentity, role: Role) -> None:
    if role is Role.GUEST:
        owner.logout()
    elif role is not Role.ADMINISTRATOR:
        owner.create_account("Actor", role, TEST_PASSWORD).result(WAIT)
        owner.login("Actor", TEST_PASSWORD).result(WAIT)
    assert owner.snapshot().role is role


@pytest.mark.parametrize("role", list(Role))
def test_engineering_files_and_native_commands(
    engineering: Startup, role: Role, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, editor, data = engineering.application, engineering.editor, engineering.data
    assert (
        editor is not None
        and editor.managed_models is not None
        and editor.camera_sources is not None
    )
    owner, models = app.identity, editor.managed_models
    assert owner is not None and engineering.prepared is not None
    model = engineering.prepared.configuration.model
    calls: list[Path] = []

    def inspect(path: Path, **_kwargs: object) -> tuple[SegmentationModuleInfo, ...]:
        calls.append(path)
        return (SegmentationModuleInfo("2", "Segment", ("defect",)),)

    monkeypatch.setattr(
        "capsule_vision.configuration.managed_models.read_model_modules", inspect
    )
    engine = ReplayEngine()

    def prepare(_request: ModelPreparationRequest) -> ReplayEngine:
        return engine

    monkeypatch.setattr(app, "_preparation_factory", prepare)
    source = data.root.parent / "test.vimosln"
    source.write_bytes(b"isolated new model")
    cache = ModelPreparationRequest(model, Path(model.model_path))
    spare = next(
        entry
        for entry in editor.catalog.discover().entries
        if entry.recipe.recipe_id == "spare"
    )
    editor.load(spare)
    editor.draft.display_name = "Changed"
    editor.camera_sources.set_loop(not editor.camera_sources.saved.loop)
    before = tuple(
        path.read_bytes() for path in (spare.path, data.machine, data.sources)
    )
    sign_in(owner, role)

    if role < Role.ENGINEER:
        for command in (
            editor.save,
            lambda: editor.delete(protected_ids=frozenset()),
            lambda: editor.save_vision(None, None),
            lambda: models.inspect_import(source),
            lambda: models.rename(model, "Denied"),
            lambda: models.delete(model, data.recipes),
            lambda: app.prepare_model_cache(cache),
        ):
            with pytest.raises(AuthorizationError):
                command()
        with pytest.raises(AuthorizationError), app.model_management():
            pytest.fail("denied native slot entered")
        assert not calls and not tuple(data.root.glob(".model-import-*"))
        assert not engine.closed and not app.preparation_snapshot().owns_resources
        assert before == tuple(
            path.read_bytes() for path in (spare.path, data.machine, data.sources)
        )
    else:
        editor.save()
        assert load_recipe(spare.path).display_name == "Changed"
        editor.delete(protected_ids=frozenset())
        assert not spare.path.exists()
        outcome = editor.save_vision(None, None)
        assert outcome.machine_saved and outcome.sources_saved and not outcome.errors
        with app.model_management():
            pending = models.inspect_import(source)
        imported = models.publish_import(pending, "2", "Imported")
        updated = models.rename(imported, "Revised")
        models.delete(updated, data.recipes)
        assert len(calls) == 1 and not (data.models / imported.model_id).exists()
        assert app.prepare_model_cache(cache).result(WAIT).initialized and engine.closed


@pytest.mark.parametrize("transition", ["switch", "disable", "demote", "logout"])
def test_staged_import_cannot_publish_under_a_later_session(
    engineering: Startup, transition: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, editor = engineering.application.identity, engineering.editor
    assert (
        owner is not None and editor is not None and editor.managed_models is not None
    )
    admin = owner.snapshot().account
    assert admin is not None
    owner.create_account("Backup", Role.ADMINISTRATOR, TEST_PASSWORD).result(WAIT)
    models = editor.managed_models
    source = engineering.data.root.parent / "test.vimosln"
    source.write_bytes(b"new model")

    def inspect(_path: Path, **_kwargs: object) -> tuple[SegmentationModuleInfo, ...]:
        return (SegmentationModuleInfo("2", "S", ()),)

    monkeypatch.setattr(
        "capsule_vision.configuration.managed_models.read_model_modules", inspect
    )
    pending = models.inspect_import(source)
    if transition == "switch":
        owner.login("Backup", TEST_PASSWORD).result(WAIT)
    elif transition == "disable":
        owner.set_enabled(admin.account_id, False).result(WAIT)
    elif transition == "demote":
        owner.set_role(admin.account_id, Role.ENGINEER).result(WAIT)
    else:
        owner.logout()
    with pytest.raises(StaleSessionError):
        models.publish_import(pending, "2", "Stale")
    assert {p.name for p in models.directory.iterdir()} == {"general"}
    models.cancel_import(pending)
    assert not pending.directory.exists()


def reply(request: CommunicationRequest) -> CommunicationResponse:
    values = (0,) * request.count if request.kind is RequestKind.READ else None
    return CommunicationResponse(request, values, 0, datetime.now(UTC))


def close_page(page: CommunicationPage) -> None:
    page.request_close(lambda: None)
    until(lambda: page.closed)
    page.dispose()
    destroyed = Event()
    page.destroyed.connect(destroyed.set)
    page.deleteLater()
    until(destroyed.is_set)


@pytest.mark.parametrize("role", list(Role))
def test_communication_and_injected_web_commands(
    qapp: QApplication, engineering: Startup, role: Role
) -> None:
    app = engineering.application
    owner = app.identity
    assert owner is not None
    sign_in(owner, role)
    calls: list[RequestKind] = []

    def execute(request: CommunicationRequest) -> CommunicationResponse:
        calls.append(request.kind)
        return reply(request)

    page = CommunicationPage(application=app, execute=execute)
    page.configure_preferences(
        engineering.data.communication, engineering.communication
    )
    runtime = WebRuntime(
        lambda settings: ProductionSnapshotService(
            app, engineering.frames, host="127.0.0.1", port=0
        )
    )
    page.bind_web_runtime(runtime)
    page.show()
    try:
        engineer = role >= Role.ENGINEER
        assert page.web_panel.power.isEnabled() == engineer
        assert page.write_button.isEnabled() == engineer
        page.write_values.setText("123")
        page.persist_settings()
        if not engineer:
            assert page.preferences_status.text() == text(
                "permission_engineer", page.language
            )
        assert engineering.data.communication.exists() == (role >= Role.ENGINEER)
        assert page.add_watch_group(RegisterWatchGroup(FinsMemoryArea.DM, 0))
        for dispatch in (
            lambda: page.dispatch(RequestKind.READ),
            lambda: page.dispatch(RequestKind.WRITE),
            page.read_watch_once,
            page.start_monitoring,
        ):
            assert dispatch() == (role >= Role.ENGINEER)
            until(lambda: not page.busy)
        page.stop_monitoring()
        assert calls == (
            [RequestKind.READ, RequestKind.WRITE, RequestKind.READ, RequestKind.READ]
            if role >= Role.ENGINEER
            else []
        )
        settings = WebPreferences(enabled=True)
        if role < Role.ENGINEER:
            for action in (
                lambda: runtime.start(settings),
                lambda: runtime.apply(settings),
                runtime.stop,
            ):
                with pytest.raises(AuthorizationError):
                    action()
            page.web_panel.power.click()
            assert (
                runtime.snapshot().state is WebState.STOPPED
                and not engineering.data.communication.exists()
            )
        else:
            assert runtime.start(settings).result(WAIT).state is WebState.RUNNING
            assert runtime.apply(settings).result(WAIT).state is WebState.RUNNING
            assert runtime.stop().result(WAIT).state is WebState.STOPPED
    finally:
        owner.logout()
        close_page(page)


@pytest.mark.parametrize("between_groups", [True, False])
def test_monitoring_rechecks_between_reads_and_timer_passes(
    qapp: QApplication, engineering: Startup, between_groups: bool
) -> None:
    app = engineering.application
    owner = app.identity
    assert owner is not None
    owner.create_account("OtherEngineer", Role.ENGINEER, TEST_PASSWORD).result(WAIT)
    entered, release = Event(), Event()
    calls: list[int] = []

    def execute(request: CommunicationRequest) -> CommunicationResponse:
        calls.append(request.start)
        entered.set()
        assert release.wait(WAIT)
        return reply(request)

    page = CommunicationPage(application=app, execute=execute)
    page.show()
    try:
        assert page.add_watch_group(RegisterWatchGroup(FinsMemoryArea.DM, 0))
        if between_groups:
            assert page.add_watch_group(RegisterWatchGroup(FinsMemoryArea.DM, 10))
        assert page.start_monitoring() and entered.wait(WAIT)
        owner.logout()
        page.identity_changed()
        assert page.watch_auto.isEnabled() and not page.write_button.isEnabled()
        owner.login("OtherEngineer", TEST_PASSWORD).result(WAIT)
        release.set()
        until(lambda: not page.auto_refresh and not page.busy)
        assert calls == [0] and page.last_response is not None
        assert "StaleSessionError" in page.feedback_error
    finally:
        release.set()
        close_page(page)


def test_pending_recipe_delete_does_not_follow_a_role_change(
    qapp: QApplication, engineering: Startup
) -> None:
    owner, editor = engineering.application.identity, engineering.editor
    assert owner is not None and editor is not None
    admin = owner.snapshot().account
    assert admin is not None
    owner.create_account("Backup", Role.ADMINISTRATOR, TEST_PASSWORD).result(WAIT)
    delivery = FutureDelivery()
    window = create_window(engineering, delivery)
    try:
        spare = next(
            entry
            for entry in editor.catalog.discover().entries
            if entry.recipe.recipe_id == "spare"
        )
        editor.load(spare)
        page = window.recipe_page
        page.show_draft()
        page.request_delete()
        dialog = page.findChild(QMessageBox)
        assert dialog is not None
        owner.set_role(admin.account_id, Role.ENGINEER).result(WAIT)
        button = next(
            b
            for b in dialog.buttons()
            if dialog.buttonRole(b) == QMessageBox.ButtonRole.AcceptRole
        )
        button.click()
        assert spare.path.exists() and editor.entry == spare
        assert "superseded" in page.status.toolTip()
    finally:
        close_window(window)
        delivery.detach()


def test_web_power_rechecks_actor_between_save_and_service_admission(
    qapp: QApplication, engineering: Startup, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, owner = engineering.application, engineering.application.identity
    assert owner is not None

    def save_then_logout(path: Path, value: CommunicationPreferences) -> None:
        save_communication_preferences(path, value)
        owner.logout()

    monkeypatch.setattr(
        "capsule_vision.ui.communication_page.save_communication_preferences",
        save_then_logout,
    )

    def forbidden(_settings: WebPreferences) -> ProductionSnapshotService:
        pytest.fail("unauthorized Web service construction")

    runtime = WebRuntime(forbidden)
    page = CommunicationPage(application=app)
    page.configure_preferences(
        engineering.data.communication, engineering.communication
    )
    page.bind_web_runtime(runtime)
    try:
        page.web_panel.power.click()
        assert engineering.data.communication.exists() and page.web_panel.saved.enabled
        assert runtime.snapshot().state is WebState.STOPPED
        assert "StaleSessionError" in page.web_panel.operation_error
    finally:
        close_page(page)


@pytest.mark.parametrize("role", list(Role))
def test_engineering_readers_check_before_native_calls(
    qapp: QApplication,
    engineering: Startup,
    role: Role,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, editor = engineering.application, engineering.editor
    assert editor is not None and app.identity is not None
    sign_in(app.identity, role)
    calls: list[str] = []

    def labels(*_args: object, **_kwargs: object) -> tuple[str, ...]:
        calls.append("labels")
        return ("defect",)

    def discover() -> tuple[BaslerDevice, ...]:
        calls.append("discovery")
        return ()

    def measure(*_args: object) -> tuple[PreviewMeasurement, ...]:
        calls.append("measure")
        return ()

    monkeypatch.setattr("capsule_vision.ui.recipe_page.read_model_labels", labels)
    monkeypatch.setattr(
        "capsule_vision.ui.vision_page.discover_basler_devices", discover
    )
    monkeypatch.setattr("capsule_vision.ui.recipe_tuning.measure_reference", measure)
    delivery = FutureDelivery()
    window = create_window(engineering, delivery)
    window.show()
    try:
        window.navigate(1)
        page = window.recipe_page
        until(lambda: not page.labels_loading)
        editor.draft.capsule = SETTINGS
        page.open_preview("size")
        preview = page.preview
        if role >= Role.ENGINEER:
            assert preview is not None
            until(lambda: not preview.busy)
            preview.load_image(
                next((engineering.data.root.parent / "frames").glob("*.png"))
            )
            until(lambda: not preview.busy)
        else:
            assert preview is None and not page.expand_tuning["size"].isEnabled()
        editor.discard()
        window.navigate(4)
        vision = window.vision_workspace
        assert vision is not None
        until(lambda: not window.editing.busy)
        vision.discover_devices()
        until(lambda: not window.editing.busy)
        assert set(calls) == (
            {"labels", "measure", "discovery"} if role >= Role.ENGINEER else {"labels"}
        )
    finally:
        editor.discard()
        close_window(window)
        delivery.detach()


@pytest.mark.parametrize("role", list(Role))
def test_recipe_labels_show_saved_policy_and_only_engineers_can_edit(
    qapp: QApplication,
    engineering: Startup,
    role: Role,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, editor = engineering.application.identity, engineering.editor
    assert owner is not None and editor is not None and editor.entry is not None
    path = editor.entry.path
    defaults = editor.draft.defaults
    editor.draft.defaults = replace(
        defaults, vimo=replace(defaults.vimo, disabled_labels=("Dent", "Retired"))
    )
    editor.save()
    original = path.read_bytes()
    sign_in(owner, role)
    catalog = ("Chip", "Dent", "dent")
    calls: list[Path] = []

    def labels(path: Path, *, module_id: str, expected_sha256: str) -> tuple[str, ...]:
        calls.append(path)
        return catalog

    monkeypatch.setattr("capsule_vision.ui.recipe_page.read_model_labels", labels)
    delivery = FutureDelivery()
    window = create_window(engineering, delivery)
    window.show()
    try:
        window.navigate(1)
        page = window.recipe_page
        until(lambda: not page.labels_loading)
        assert tuple(page.label_checks) == catalog, page.status.toolTip()
        assert len(calls) == 1
        assert [check.isChecked() for check in page.label_checks.values()] == [
            True,
            False,
            True,
        ]
        assert "Retired" in page.label_choices.toolTip()
        allowed = role >= Role.ENGINEER
        assert all(check.isEnabled() == allowed for check in page.label_checks.values())
        dent = page.label_checks["Dent"]
        dent.setChecked(
            True
        )  # Also exercise the mutation boundary for a disabled widget.
        assert dent.isChecked() == allowed
        assert editor.dirty == allowed
        if allowed:
            page.save()
            assert load_recipe(path).defaults.vimo.disabled_labels == ("Retired",)
        else:
            with pytest.raises(AuthorizationError):
                editor.save()
            assert path.read_bytes() == original
    finally:
        editor.discard()
        close_window(window)
        delivery.detach()


def test_label_catalog_survives_identity_changes_and_preserves_engineering_draft(
    qapp: QApplication, engineering: Startup, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, editor = engineering.application.identity, engineering.editor
    assert owner is not None and editor is not None and editor.entry is not None
    assert not editor.draft.defaults.vimo.disabled_labels
    owner.create_account("Engineer", Role.ENGINEER, TEST_PASSWORD).result(WAIT)
    entered, release = Event(), Event()
    reads: list[Path] = []

    def labels(path: Path, *, module_id: str, expected_sha256: str) -> tuple[str, ...]:
        reads.append(path)
        entered.set()
        assert release.wait(WAIT)
        return ("Dent",)

    monkeypatch.setattr("capsule_vision.ui.recipe_page.read_model_labels", labels)
    delivery = FutureDelivery()
    window = create_window(engineering, delivery)
    window.show()
    try:
        page = window.recipe_page
        window.navigate(1)
        until(entered.is_set)
        window.identity_controls.logout()
        release.set()
        until(lambda: not page.labels_loading)
        dent = page.label_checks["Dent"]
        assert dent.isChecked() and not dent.isEnabled()
        owner.login("Engineer", TEST_PASSWORD).result(WAIT)
        until(dent.isEnabled)
        dent.click()
        assert editor.draft.defaults.vimo.disabled_labels == ("Dent",)
        owner.logout()
        # A toggle delivered before the shell observes logout must also be denied.
        dent.setChecked(True)
        assert editor.draft.defaults.vimo.disabled_labels == ("Dent",)
        assert dent.isChecked() and not dent.isEnabled()
        window.identity_controls.refresh()
        owner.login("Engineer", TEST_PASSWORD).result(WAIT)
        until(dent.isEnabled)
        assert not dent.isChecked() and len(reads) == 1
    finally:
        release.set()
        editor.discard()
        close_window(window)
        delivery.detach()


@pytest.mark.parametrize("failure", ["earlier_denial", "missing", "corrupt"])
def test_guest_label_read_failure_and_explicit_retry(
    qapp: QApplication,
    engineering: Startup,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    owner, editor = engineering.application.identity, engineering.editor
    assert owner is not None and editor is not None and engineering.prepared is not None
    owner.logout()
    artifact = engineering.prepared.model_path
    if failure == "missing":
        artifact.unlink()
    elif failure == "corrupt":
        artifact.write_bytes(b"corrupt synthetic artifact")
    calls: list[Path] = []

    def labels(path: Path, *, module_id: str, expected_sha256: str) -> tuple[str, ...]:
        calls.append(path)
        if failure == "earlier_denial":
            if len(calls) == 1:
                raise AuthorizationError("Earlier label read was denied")
            return ("Recovered",)
        # Both artifact failures precede SDK loading in the actual adapter.
        return read_model_labels(
            path, module_id=module_id, expected_sha256=expected_sha256
        )

    monkeypatch.setattr("capsule_vision.ui.recipe_page.read_model_labels", labels)
    delivery = FutureDelivery()
    window = create_window(engineering, delivery)
    window.show()
    try:
        page = window.recipe_page
        window.navigate(1)
        until(lambda: not page.labels_loading)
        assert not page.label_checks and not editor.dirty
        detail = page.status.toolTip()
        assert {
            "earlier_denial": "Earlier label read was denied",
            "missing": "[Errno 2]",
            "corrupt": "SHA-256 differs",
        }[failure] in detail
        page.refresh_state()
        QApplication.processEvents()
        assert len(calls) == 1 and not page.labels_loading
        if failure == "earlier_denial":
            page.buttons["reload"].click()
            until(lambda: "Recovered" in page.label_checks)
            assert not page.label_checks["Recovered"].isEnabled() and len(calls) == 2
    finally:
        close_window(window)
        delivery.detach()
