"""Diagnostic seams reuse real workers, archive writer and maintained UDP target."""

import hashlib
from collections.abc import Callable
from pathlib import Path
from threading import get_ident
from typing import cast

import cv2
import numpy as np
import pytest
from field_benchmark.analyze import load_rows, reconcile
from field_benchmark.archive_check import verify_archive
from field_benchmark.ledger import Ledger
from field_benchmark.observations import Observations
from field_benchmark.target import IsolatedTarget
from numpy.typing import NDArray
from tests.application_support import prepared
from tests.business_support import mixed_result
from tests.cycle_support import image_result
from tests.production_support import source_frame

from vimo_demo.application import ProductionApplication, _basler_sdk
from vimo_demo.application.basler import AcquisitionMetadata
from vimo_demo.application.image_archive import ImageArchive
from vimo_demo.communication.reporting import (
    FirstStationReport,
    FirstStationReportingWorker,
)
from vimo_demo.configuration import ImageArchiveSettings
from vimo_demo.inference import ImageResult
from vimo_demo.inference._validation import uint8_array
from vimo_demo.inspection import CameraId, CycleId
from vimo_demo.runtime import InferenceWorker
from vimo_demo.ui import local_production


def test_existing_worker_engine_owner_observations(tmp_path: Path) -> None:
    owners: list[int] = []
    ledger = Ledger(tmp_path / "engine.jsonl", "r", "backend")
    obs = Observations(ledger, "epoch")

    class Engine:
        def __init__(self) -> None:
            owners.append(get_ident())

        def infer(self, image_bgr: NDArray[np.generic]) -> ImageResult:
            owners.append(get_ident())
            return image_result(uint8_array(image_bgr))

        def close(self) -> None:
            owners.append(get_ident())

    config = prepared(tmp_path)
    factory = obs.engine(lambda _: Engine())
    worker = InferenceWorker(lambda: factory(config))
    worker.start().result(3)
    try:
        worker.submit(np.zeros((20, 60, 3), dtype=np.uint8)).result(3)
    finally:
        worker.request_stop()
        assert worker.join(3)
        obs.owner("inference_worker", True, True)
        assert ledger.close()
    assert len(owners) == 3 and len(set(owners)) == 1 and owners[0] != get_ident()
    rows = load_rows([ledger.path])
    assert [r["kind"] for r in rows] == [
        "clock",
        "engine_initializing",
        "engine_created",
        "inference_finished",
        "engine_closed",
        "owner_stopped",
    ]
    assert cast(float, rows[3]["duration_ms"]) >= 0


def test_scoped_sdk_reporting_and_archive_observers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = mixed_result()
    cameras = result.layout.camera_ids
    mapping = tuple((c, str(100 + i)) for i, c in enumerate(cameras))
    ledger = Ledger(tmp_path / "backend.jsonl", "r", "backend")
    obs = Observations(ledger, "connection-e", mapping)
    target = IsolatedTarget(obs, 2)
    original_begin = ProductionApplication.begin_cycle
    original_backend = _basler_sdk.PylonBackend

    def retrieve(
        _backend: object, serial: str
    ) -> tuple[AcquisitionMetadata, NDArray[np.uint8]]:
        return AcquisitionMetadata(
            serial, 1, True, 0, "", 60, 20, 1, 3600, 9, 7, 123456, 0
        ), np.zeros((20, 60, 3), dtype=np.uint8)

    monkeypatch.setattr(_basler_sdk.PylonBackend, "retrieve", retrieve)
    settings = ImageArchiveSettings(str(tmp_path / "archive"), True, True, True, True)
    try:
        with obs.composition_scope():
            # No camera construction/Open: exercise only the transparent retrieve seam.
            backend = object.__new__(_basler_sdk.PylonBackend)
            metadata, pixels = backend.retrieve(mapping[0][1])
            assert metadata.image_number == 7 and pixels is not None
            reporter_type = cast(
                type[FirstStationReportingWorker],
                vars(local_production)["FirstStationReportingWorker"],
            )
            archive_factory = cast(
                Callable[
                    [ImageArchiveSettings, int, tuple[CameraId, ...]], ImageArchive
                ],
                vars(local_production)["ImageArchive"],
            )
            reporter = reporter_type(1, target.settings)
            archive = archive_factory(settings, 1, cameras)
            reporter.start()
            archive.start()
            try:
                archive.begin(CycleId(1))
                for camera in cameras:
                    archive.retain(source_frame(camera))
                archive.complete(CycleId(1), result)
                for cycle in (1, 2):
                    observation = reporter.submit(
                        FirstStationReport.from_result(1, mixed_result(cycle))
                    ).result(3)
                    assert observation.sequence == cycle
                    assert (
                        observation.reject_words
                        == FirstStationReport.from_result(1, result).reject_words
                    )
            finally:
                reporter.request_stop(drain=True)
                archive.request_stop()
                assert reporter.join(3) and archive.join(3)
        assert ProductionApplication.begin_cycle is original_begin
        assert _basler_sdk.PylonBackend is original_backend
    finally:
        assert target.close()
        assert ledger.close()
    rows = load_rows([ledger.path])
    assert len([r for r in rows if r["kind"] == "target_commit"]) == 2
    assert [r["sequence"] for r in rows if r["kind"] == "report_outcome"] == [1, 2]
    written = [r for r in rows if r["kind"] == "archive_written"]
    files = [r for r in rows if r["kind"] == "archive_file"]
    assert len(written) == len(files) == 6
    for row in files:
        path = Path(cast(str, row["path"]))
        decoded = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
        assert decoded is not None
        expected = next(r for r in written if r["camera"] == row["camera"])
        assert hashlib.sha256(decoded).hexdigest() == expected["pixel_sha256"]
        assert path.stat().st_size == row["bytes"]
    assert not verify_archive(rows, tmp_path / "archive")
    # Component observations do not pretend an actual triggered Production run occurred.
    summary = reconcile(
        rows, "r", reports=True, archives=True, required_owners=("source", "session")
    )
    assert summary["evidence_complete"] is False
    assert "missing finite trigger plan" in cast(list[str], summary["issues"])
    Path(cast(str, files[0]["path"])).write_bytes(b"controlled corrupt PNG")
    assert verify_archive(rows, tmp_path / "archive")
