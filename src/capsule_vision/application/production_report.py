"""Manual report publication from one immutable History snapshot."""

import sqlite3
from collections.abc import Callable
from concurrent.futures import CancelledError
from contextlib import AbstractContextManager, closing, nullcontext
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from threading import Event

from capsule_vision.localization.languages import Language

from .alarm_report import AlarmReportRequest
from .audit_report import AuditReportRequest
from .audit_report import capture as capture_audit
from .history import ProductionHistory
from .identity import LocalIdentity
from .report_identification import pdf_language
from .report_scope import ReportRequest


def _check_cancel(cancel: Event) -> None:
    if cancel.is_set():
        raise CancelledError()


def write_report(
    history: ProductionHistory | None,
    batch: str | ReportRequest | AuditReportRequest | AlarmReportRequest,
    destination: Path,
    language: Language,
    cancel: Event,
    admit_publication: Callable[[], AbstractContextManager[None] | None],
    *,
    identity: LocalIdentity | None = None,
    revision: int | None = None,
) -> None:
    _check_cancel(cancel)
    if destination.suffix.lower() != ".pdf":
        raise ValueError("Select a .pdf file")
    # Same-directory replacement also preserves an existing report on failure.
    with NamedTemporaryFile(
        dir=destination.parent, suffix=".tmp", delete=False
    ) as handle:
        staged = Path(handle.name)
    try:
        with TemporaryDirectory(prefix="capsule-report-") as directory:
            snapshot = Path(directory) / "snapshot.sqlite3"
            if isinstance(batch, AuditReportRequest):
                if identity is None or revision is None:
                    raise ValueError("Audit export requires an authorized session")
                capture_audit(identity, history, batch, snapshot, cancel, revision)
            elif isinstance(batch, AlarmReportRequest):
                if history is None:
                    raise ValueError("History unavailable")
                history.capture_alarm_report(batch, snapshot, cancel, revision).result()
            else:
                if history is None:
                    raise ValueError("History unavailable")
                history.capture_report(
                    batch, snapshot, cancel
                ).result()  # Export thread only.
            _check_cancel(cancel)
            with closing(
                sqlite3.connect(snapshot.as_uri() + "?mode=ro", uri=True)
            ) as db:
                db.set_progress_handler(lambda: int(cancel.is_set()), 4096)
                try:
                    from .report_pdf import write_pdf

                    write_pdf(db, staged, pdf_language(language), cancel)
                except sqlite3.OperationalError:
                    _check_cancel(cancel)
                    raise
        _check_cancel(cancel)
        with admit_publication() or nullcontext():
            _check_cancel(cancel)
            staged.replace(destination)
    finally:
        staged.unlink(missing_ok=True)
