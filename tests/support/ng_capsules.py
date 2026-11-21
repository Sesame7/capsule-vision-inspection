"""A closed synthetic History package with real PNGs and deliberately sparse observations."""

import json
import sqlite3
import zlib
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path
from time import time

import cv2
import numpy as np

from capsule_vision.application import ConfigurationIdentity
from capsule_vision.application.archive_links import PublishedImage, create, insert
from capsule_vision.application.history import ProductionHistory
from capsule_vision.application.run_configuration import capture_run
from capsule_vision.communication.endpoints import FinsTcpEndpoint
from capsule_vision.communication.reporting import (
    ReportingMode,
    ReportingSelection,
    ReportingSettings,
)
from capsule_vision.inference import BoundingRect
from tests.support.application import WAIT, prepared

MEMBERS = (101, 131, 117, 190)
RUN = "synthetic-gallery"
RECT = BoundingRect(1010, 12, 230, 165)


@dataclass(frozen=True, slots=True)
class Sample:
    root: Path
    batch: str
    observed: int
    channel: int
    camera: str


def add_group(
    db: sqlite3.Connection,
    run: str,
    members: tuple[int, ...],
    observed: float,
    final: tuple[int, ...],
    count: int,
) -> None:
    # Reverse storage order and repeat the PLC counter across groups.
    states = [(i, "NG" if i in final else "OK") for i in range(count, 0, -1)]
    first = str(members[0])
    db.execute(
        "INSERT INTO plc_groups VALUES(?,?,?,7,20,?)",
        (run, first, observed, json.dumps(states)),
    )
    db.executemany(
        "INSERT INTO group_members VALUES(?,?,?,?)",
        ((run, str(cycle), first, slot) for slot, cycle in enumerate(members, 1)),
    )


def _png(path: Path, index: int) -> None:
    # Two distinct synthetic optical bands in a full six-position camera frame.
    h, w = 196, 1520
    frame = np.full((h, w, 3), (33, 32, 30), dtype=np.uint8)
    yy, xx = np.mgrid[:h, :w]
    for slot in range(6):
        for view, center_y in enumerate((54, 136)):
            center = 126 + slot * 250
            dx = np.maximum(abs(xx - center) - 59, 0)
            dy = yy - center_y
            radius = np.hypot(dx, dy)
            mask = radius < 27
            shade = np.sqrt(np.maximum(0, 1 - (radius / 27) ** 2)) * 0.5 + 0.5
            cap = xx < center + 3 + 2 * index - 4 * view
            for c, (left, right) in enumerate(((58, 180), (139, 207), (196, 224))):
                layer = np.where(cap, left, right) * shade
                frame[:, :, c][mask] = layer[mask].astype(np.uint8)
            if index == 0 and slot == 4:
                cv2.ellipse(
                    frame,
                    (center + 27, center_y - 4),
                    (7, 4),
                    22,
                    0,
                    360,
                    (38, 61, 79),
                    -1,
                )
    path.parent.mkdir(parents=True, exist_ok=True)
    assert cv2.imwrite(str(path), frame)


def sample(
    root: Path,
    cameras: int = 5,
    *,
    roi_width: int = RECT.width,
    recipe_name: str | None = None,
) -> Sample:
    root.mkdir(parents=True, exist_ok=True)
    database = root / "history.sqlite3"
    observed = int(time()) - 600
    ch = cameras * 6 - 1
    config = prepared(root)
    configured = config.configuration.layout.cameras[:cameras]
    camera_ids = [c.camera_id for c in configured]
    if cameras == 6:
        # CH35 belongs to CAM2 in this Run. Channel numbers do not identify a camera.
        camera_ids[1], camera_ids[-1] = camera_ids[-1], camera_ids[1]
    camera = camera_ids[-1]
    layouts = tuple(
        replace(
            c,
            camera_id=camera_id,
            slots=tuple(
                replace(
                    slot, rect=BoundingRect(10 + 250 * slot.slot, 12, roi_width, 165)
                )
                for slot in c.slots
            ),
        )
        for c, camera_id in zip(configured, camera_ids, strict=True)
    )
    machine = replace(config.configuration.layout, cameras=layouts)
    reporting = ReportingSettings(
        endpoint=FinsTcpEndpoint(), mode=ReportingMode.FOUR_CYCLE
    )
    frozen = capture_run(
        replace(
            config,
            configuration=replace(
                config.configuration,
                layout=machine,
                display_name=config.configuration.display_name
                if recipe_name is None
                else recipe_name,
            ),
        ),
        ReportingSelection(True, reporting),
        None,
        None,
    )
    owner = ProductionHistory(database, clock=lambda: float(observed))
    try:
        batch = owner.select_batch("Training lot").result(WAIT)
        assert batch
        owner.start_run(
            batch.uuid,
            RUN,
            ConfigurationIdentity("recipe", "model", "0" * 64),
            reporting,
            configuration=frozen,
        ).result(WAIT)
        owner.finish_run(RUN, "stopped", False).result(WAIT)
    finally:
        owner.request_stop()
        assert owner.join(WAIT)
    with closing(sqlite3.connect(database)) as db, db:
        create(db)
        add_group(db, RUN, MEMBERS, observed, (ch, ch + 1), cameras * 6)
        for index, (cycle, business) in enumerate(
            zip(MEMBERS, ("NG", "OK", "EMPTY", "UNKNOWN"), strict=True)
        ):
            channel = [
                camera,
                4,
                ch,
                business,
                "ERROR" if index == 3 else "SUCCESS",
                "UNKNOWN" if index == 3 else "NG" if index == 0 else "OK",
                ["defect_region"]
                if index == 0
                else ["execution_error"]
                if index == 3
                else [],
            ]
            other: list[object] = [
                camera,
                5,
                ch + 1,
                "NG" if index == 0 else "UNKNOWN",
                "SUCCESS",
                "NG" if index == 0 else "UNKNOWN",
                [],
            ]
            evidence: list[object] = [camera, "SUCCESS", "OK", [], ["Unassigned"]]
            if index == 0:
                evidence.append(
                    [
                        [1520, 196],
                        [
                            ["Spot", [1144, 40, 19, 17], [ch]],
                            ["Spot", [1144, 122, 19, 17], [ch]],
                            ["Edge", [1005, 92, 12, 5], [ch, ch + 1]],
                            ["Neighbour", [1270, 36, 8, 6], [ch + 1]],
                        ],
                    ]
                )
            blob = zlib.compress(
                json.dumps([[channel, other], [evidence], None]).encode()
            )
            db.execute(
                """INSERT INTO cycles(completed_at,batch_segment,batch_label,run_id,cycle_id,recipe_id,model_id,model_sha,
                status,technical,judgment,empty_count,unknown_count,detail) VALUES(?,?,?,?,?,'recipe','model',?,'COMPLETE','SUCCESS','OK',0,0,?)""",
                (
                    observed - 86401 + index,
                    batch.uuid,
                    batch.label,
                    RUN,
                    str(cycle),
                    "0" * 64,
                    blob,
                ),
            )
            if index != 2:
                path = root / "2026-01-15" / camera / f"shot-{cycle}.png"
                _png(path, index)
                insert(db, PublishedImage(RUN, cycle, camera, path, float(observed)))
    return Sample(root, batch.uuid, observed, ch, camera)
