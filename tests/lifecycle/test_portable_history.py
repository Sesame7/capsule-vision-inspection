"""Explicit portable launch failures and read-only SQLite ownership."""

import sqlite3
from contextlib import closing
from pathlib import Path
from time import time

import pytest
from PySide6.QtWidgets import QApplication
from tests.support.application import WAIT
from tests.support.qt import delete_widget, ensure_test_fonts, until

from capsule_vision.application import alarm_records
from capsule_vision.application.alarm_records import AlarmEvent, AlarmQuery
from capsule_vision.application.history import ProductionHistory
from capsule_vision.application.history_review import open_history_review
from capsule_vision.application.startup import ApplicationData
from capsule_vision.localization.catalog import text
from capsule_vision.ui import bootstrap
from capsule_vision.ui.history_review_window import HistoryReviewWindow


def forbid_production(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Portable launch reached normal data or services")

    monkeypatch.setattr(ApplicationData, "at_entry", forbidden)
    monkeypatch.setattr(bootstrap, "load_startup", forbidden)
    monkeypatch.setattr(bootstrap, "create_web_runtime", forbidden)
    monkeypatch.setattr(bootstrap, "RuntimeDiagnostics", forbidden)
    monkeypatch.setattr(bootstrap, "QApplication", forbidden)


@pytest.fixture
def history_copy(tmp_path: Path) -> Path:
    directory = tmp_path / "External and separate"
    owner = ProductionHistory(directory / "history.sqlite3")
    try:
        owner.initial_selection.result(WAIT)
    finally:
        owner.request_stop()
        assert owner.join(WAIT)
    return directory


@pytest.mark.parametrize(
    "arguments,expected",
    [
        (["--history"], "expected one argument"),
        (["--history="], "expected a directory path"),
        (
            ["--history-image-root", "unused"],
            "unrecognized arguments: --history-image-root",
        ),
        (
            ["--history", "unused", "--diagnostics", "standard"],
            "--diagnostics is not available",
        ),
    ],
)
def test_portable_cli_rejects_bad_arguments_before_opening_any_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
    expected: str,
) -> None:
    forbid_production(monkeypatch)
    with pytest.raises(SystemExit) as error:
        bootstrap.main(tmp_path, arguments)
    assert error.value.code == 2 and expected in capsys.readouterr().err
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "scenario,expected",
    [
        ("missing-directory", "Cannot open History package"),
        ("not-directory", "Expected a directory"),
        ("missing-database", "Missing history.sqlite3"),
        ("malformed", "not a database"),
    ],
)
def test_portable_launch_rejects_invalid_packages_without_using_local_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    scenario: str,
    expected: str,
) -> None:
    root = tmp_path / "requested package"
    if scenario == "not-directory":
        root.write_bytes(b"wrong type")
    elif scenario in {"missing-database", "malformed"}:
        root.mkdir()
        if scenario == "malformed":
            (root / "history.sqlite3").write_bytes(b"This is not SQLite")
    entry = tmp_path / "entry"
    data = entry / "data"
    data.mkdir(parents=True)
    local = data / "history.sqlite3"
    local.write_bytes(b"local history")
    forbid_production(monkeypatch)
    with pytest.raises(SystemExit) as error:
        bootstrap.main(entry, ["--history", str(root)])
    assert error.value.code == 2
    message = capsys.readouterr().err
    assert str(root) in message and expected in message
    assert list(data.iterdir()) == [local] and local.read_bytes() == b"local history"
    if scenario == "missing-directory":
        assert not root.exists()
    elif scenario == "not-directory":
        assert root.read_bytes() == b"wrong type"
    elif scenario == "missing-database":
        assert not list(root.iterdir())
    else:
        assert (root / "history.sqlite3").read_bytes() == b"This is not SQLite"
        assert len(list(root.iterdir())) == 1


@pytest.mark.parametrize(
    "statement,expected",
    [
        ("PRAGMA user_version=3", "Unsupported History schema version 3"),
        ("DROP TABLE batch_information", "missing table batch_information"),
        ("CREATE TABLE archive_images (path TEXT)", "archive_images lacks"),
        ("CREATE TABLE alarm_events (id INTEGER PRIMARY KEY)", "alarm_events lacks"),
        ("CREATE VIEW alarm_events AS SELECT 1 AS id", "missing table alarm_events"),
        (
            "INSERT INTO run_positions VALUES ('missing-run',NULL)",
            "broken record references",
        ),
    ],
)
def test_portable_launch_rejects_inconsistent_history_without_repair(
    history_copy: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    statement: str,
    expected: str,
) -> None:
    database = history_copy / "history.sqlite3"
    with closing(sqlite3.connect(database)) as db, db:
        db.execute(statement)
    before = database.read_bytes()
    forbid_production(monkeypatch)
    with pytest.raises(SystemExit) as error:
        bootstrap.main(tmp_path / "unused", ["--history", str(history_copy)])
    assert error.value.code == 2 and expected in capsys.readouterr().err
    assert database.read_bytes() == before
    assert list(history_copy.iterdir()) == [database]
    assert not (tmp_path / "unused").exists()


@pytest.mark.parametrize("sidecar", ["-wal", "-shm", "-journal"])
def test_portable_launch_rejects_sidecars_without_recovery(
    history_copy: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    sidecar: str,
) -> None:
    database = history_copy / "history.sqlite3"
    side = database.with_name(database.name + sidecar)
    side.touch()
    before = database.read_bytes()
    forbid_production(monkeypatch)
    with pytest.raises(SystemExit) as error:
        bootstrap.main(tmp_path / "unused", ["--history", str(history_copy)])
    assert error.value.code == 2 and f"Found {side.name}" in capsys.readouterr().err
    assert database.read_bytes() == before and side.read_bytes() == b""
    assert set(history_copy.iterdir()) == {database, side}


def test_empty_current_history_is_reviewable_and_sqlite_itself_refuses_writes(
    history_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = history_copy / "history.sqlite3"
    before = database.read_bytes()
    connect = sqlite3.connect

    def verify_read_only(name: str, *, uri: bool) -> sqlite3.Connection:
        db = connect(name, uri=uri)
        # Verify SQLite refuses writes before the connection-level query guard.
        try:
            assert db.execute("PRAGMA query_only").fetchone() == (0,)
            with pytest.raises(sqlite3.OperationalError, match="readonly database"):
                db.execute("CREATE TABLE unauthorized_write (value TEXT)")
        except BaseException:
            db.close()
            raise
        return db

    monkeypatch.setattr(sqlite3, "connect", verify_read_only)
    with open_history_review(history_copy) as reader:
        assert not reader.query().result(WAIT).records
        assert not reader.batch_page().result(WAIT).rows
        assert not reader.alarm_page(AlarmQuery()).result(WAIT)
    assert database.read_bytes() == before
    assert list(history_copy.iterdir()) == [database]


@pytest.mark.parametrize("has_table", [False, True])
def test_portable_alarms_show_an_empty_page_without_creating_storage(
    qapp: QApplication, history_copy: Path, has_table: bool
) -> None:
    ensure_test_fonts()
    database = history_copy / "history.sqlite3"
    if has_table:
        with closing(sqlite3.connect(database)) as db, db:
            alarm_records.create(db, legacy=False)
    before = database.read_bytes()
    with open_history_review(history_copy) as reader:
        window = HistoryReviewWindow(reader)
        try:
            window.show()
            window.tabs.setCurrentWidget(window.alarms)
            window.set_language("en")
            page = window.alarms
            until(lambda: page.message.text() == text("history_none", "en"))
            assert window.tabs.isTabEnabled(3)
            assert page.message.isVisible() and not page.message.toolTip()
            assert page.table.rowCount() == 0 and page.details.isHidden()
            assert not page.newer.isEnabled() and not page.older.isEnabled()
        finally:
            window.close()
            delete_widget(window)
    assert database.read_bytes() == before
    assert list(history_copy.iterdir()) == [database]


def test_portable_legacy_alarms_use_the_bilingual_evidence_without_altering_schema(
    history_copy: Path,
) -> None:
    database = history_copy / "history.sqlite3"
    stamp = time() - 365 * 86400
    event = AlarmEvent(
        stamp,
        "guard",
        "recovered",
        "interlock",
        "护罩关闭",
        "Guard closed",
        "PLC",
        1,
        7,
        3,
        "legacy evidence",
        None,
        None,
        "Recorded recovery",
    )
    with closing(sqlite3.connect(database)) as db, db:
        alarm_records.create(db, legacy=False)
        alarm_records.append(db, (event,))
        db.execute("ALTER TABLE alarm_events DROP COLUMN translations")
    before = database.read_bytes()
    with open_history_review(history_copy) as reader:
        (row,) = reader.alarm_page(AlarmQuery()).result(WAIT)
        assert row.event == event
        assert row.event.label("zh") == "护罩关闭"
        assert row.event.label("de") == "Guard closed"
        assert not reader.alarm_page(AlarmQuery(since=stamp + 1)).result(WAIT)
    assert database.read_bytes() == before
    assert list(history_copy.iterdir()) == [database]
