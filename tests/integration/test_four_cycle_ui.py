"""Qt and Web consume the same scalar lamps independently of fourth pixels."""

import json
from dataclasses import replace
from pathlib import Path

from PySide6.QtWidgets import QApplication
from tests.support.application import prepared
from tests.support.business import mixed_result
from tests.support.production import source_frame
from tests.unit.test_four_cycle_presentation import completed
from tests.unit.test_web_snapshots import Source

from capsule_vision.communication.reporting import ReportingGroupProgress
from capsule_vision.display import LatestDisplayFrameStore
from capsule_vision.inspection import CycleId
from capsule_vision.ui.camera_card import CameraCard
from capsule_vision.ui.style import Style
from capsule_vision.web.publisher import SnapshotPublisher


def test_lamps_do_not_depend_on_camera_image_and_keep_fourth_evidence(
    qapp: QApplication, tmp_path: Path
) -> None:
    result = mixed_result(4)
    batch = completed(1)
    store = LatestDisplayFrameStore(result.layout.camera_ids)
    store.begin_generation(1, four_cycle=True)
    store.select_fourth(1, CycleId(4))
    store.retain_fourth_result(1, result)
    store.publish(source_frame(result.cameras[0].camera_id, cycle=4), generation=1)
    store.complete_group(batch)
    progress = ReportingGroupProgress(
        batch.identity,
        tuple(r.identity.cycle_id for r in batch.reports),
        batch.reports,
        eligible=True,
    )
    source = Source()
    source.value = replace(
        source.value,
        pilot_counting=True,
        group_progress=progress,
        active_prepared=prepared(tmp_path),
    )
    publisher = SnapshotPublisher(source, store)
    card = CameraCard(result.layout.cameras[0])
    card.resize(340, 240)
    card.show()
    try:
        for language in ("zh", "en"):
            card.present(store.snapshot().frames[0], source.value, language)
            QApplication.processEvents()
            assert card.result == result.cameras[0]
            assert card.indicators[0].states == ("ok",) * 4
            assert card.indicators[1].states == ("empty",) * 4
            assert card.indicators[2].states == ("ng",) * 4
            for slot, button in zip(
                card.camera_layout.slots, card.indicators, strict=True
            ):
                description = button.toolTip()
                assert description.startswith(
                    f"{card.camera_layout.camera_id} CH{slot.channel_id} ·"
                )
                assert all(f"#{cycle}:" in description for cycle in range(1, 5))
                assert button.accessibleName() == description
            height = card.indicator_group.height()
            # New incomplete progress leaves image evidence at Cycle 4.
            new = completed(5)
            pending = ReportingGroupProgress(
                new.identity, (CycleId(5),), new.reports[:1]
            )
            source.value = replace(
                source.value,
                group_progress=pending,
                latest_production_result=mixed_result(5),
            )
            card.present(store.snapshot().frames[0], source.value, language)
            assert card.result == result.cameras[0]
            assert card.indicators[0].states == ("ok", "pending", "pending", "pending")
            # Blank camera at new eligibility keeps valid group progress.
            store.select_fourth(1, CycleId(8))
            store.retain_fourth_result(1, mixed_result(8))
            store.complete_group(new)
            source.value = replace(
                source.value,
                group_progress=replace(
                    pending,
                    cycles=tuple(r.identity.cycle_id for r in new.reports),
                    reports=new.reports,
                    eligible=True,
                ),
            )
            card.present(None, source.value, language)
            assert card.viewport.image.isNull() and not card.viewport.boxes
            assert card.indicator_group.height() == height
            publisher.publish()
            publication = publisher.latest()
            assert publication is not None
            row = json.loads(publication.metadata)["cameras"][0]
            assert row["image_identity"] is None
            assert [list(button.states or ()) for button in card.indicators] == [
                channel["states"] for channel in row["group_channels"]
            ]
            # Reset this isolated display owner for the next language.
            store = LatestDisplayFrameStore(result.layout.camera_ids)
            store.begin_generation(1, four_cycle=True)
            store.select_fourth(1, CycleId(4))
            store.retain_fourth_result(1, result)
            store.publish(
                source_frame(result.cameras[0].camera_id, cycle=4), generation=1
            )
            store.complete_group(batch)
            source.value = replace(source.value, group_progress=progress)
    finally:
        card.close()


def test_four_cycle_header_releases_height_to_viewport(qapp: QApplication) -> None:
    card = CameraCard(mixed_result(4).layout.cameras[0])
    card.resize(522, 240)
    snapshot = Source().value
    card.show()
    try:
        card.present(None, snapshot, "en")
        qapp.processEvents()
        original_size = card.size()
        original_viewport = card.viewport.geometry()
        original_header = card.indicator_group.height()
        for four_cycle in (True, False, True):
            card.present(None, replace(snapshot, pilot_counting=four_cycle), "en")
            qapp.processEvents()
            group = card.indicator_group.geometry()
            viewport = card.viewport.geometry()
            assert card.size() == original_size
            assert viewport.bottom() == original_viewport.bottom()
            assert (
                viewport.height() - original_viewport.height()
                == original_header - group.height()
            )
            if four_cycle:
                assert 30 <= group.height() <= 32
                assert card.identity_group.geometry().right() < group.left()
                assert group.right() < card.timing.geometry().left()
                assert group.center().y() == card.timing.geometry().center().y()
            else:
                assert viewport == original_viewport
            size = 28 if four_cycle else Style.touch
            assert all(
                button.size().toTuple() == (size, size) for button in card.indicators
            )
    finally:
        card.close()
