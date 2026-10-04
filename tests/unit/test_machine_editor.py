"""Deterministic machine ROI batches and atomic persistence, without Qt/GPU."""

from dataclasses import replace
from pathlib import Path

import pytest
from tests.unit.test_configuration import machine

from vimo_demo.configuration import ConfigurationIOError, load_machine_layout
from vimo_demo.inference import BoundingRect
from vimo_demo.inspection import CameraId
from vimo_demo.inspection.production import InspectionLayout
from vimo_demo.ui.machine_editor import MachineLayoutDraft


def draft(path: Path) -> MachineLayoutDraft:
    base = machine(mirrored=True)
    cameras = tuple(
        replace(
            c,
            slots=tuple(
                replace(
                    s,
                    rect=BoundingRect(
                        10 + s.slot * 20, 10 + s.slot * 2, 8 + s.slot % 2, 10
                    ),
                )
                for s in c.slots
            ),
        )
        for c in base.layout.cameras
    )
    result = MachineLayoutDraft(replace(base, layout=InspectionLayout(cameras)), path)
    result.bounds[CameraId("CAM3")] = (160, 100)
    return result


def test_common_size_preserves_centers_and_other_cameras(tmp_path: Path) -> None:
    d = draft(tmp_path / "machine.json")
    before = d.definition
    camera = CameraId("CAM3")
    old = d.camera(camera)
    d.common_size(camera, 11, 7)
    for a, b in zip(old.slots, d.camera(camera).slots, strict=True):
        assert (b.rect.width, b.rect.height) == (11, 7)
        assert abs((a.rect.x + a.rect.width / 2) - (b.rect.x + b.rect.width / 2)) <= 0.5
        assert (
            abs((a.rect.y + a.rect.height / 2) - (b.rect.y + b.rect.height / 2)) <= 0.5
        )
        assert a.slot == b.slot and a.channel_id == b.channel_id
    assert before.layout.cameras[:2] == d.definition.layout.cameras[:2]
    d.save()
    assert load_machine_layout(d.path) == d.saved if d.path else False
    assert not d.dirty


def test_distribution_uses_slot_centers_both_axes_and_retains_sizes(
    tmp_path: Path,
) -> None:
    d = draft(tmp_path / "machine.json")
    camera = CameraId("CAM3")
    rects = [s.rect for s in d.camera(camera).slots]
    rects[0], rects[-1] = BoundingRect(10, 10, 8, 10), BoundingRect(110, 35, 8, 10)
    d.replace_rects(camera, tuple(rects))
    d.distribute(camera)
    slots = d.camera(camera).slots
    assert slots[0].rect == rects[0] and slots[-1].rect == rects[-1]
    assert [s.rect.y for s in slots] == [10, 15, 20, 25, 30, 35]
    assert [s.rect.x for s in slots] == [10, 30, 50, 70, 90, 110]
    assert [s.rect.width for s in slots] == [r.width for r in rects]
    # Mirrored channel IDs do not define interpolation order.
    assert slots[0].channel_id > slots[-1].channel_id
    stable = d.definition
    d.distribute(camera)
    assert d.definition == stable


@pytest.mark.parametrize("size", [(500, 5), (5, 500), (0, 5), (-1, 4)])
def test_invalid_common_size_rejected_atomically(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    d = draft(tmp_path / "machine.json")
    before = d.definition
    with pytest.raises(ValueError):
        d.common_size(CameraId("CAM3"), *size)
    assert d.definition == before and not d.dirty


def test_distribution_rejects_intermediate_outside_bounds(tmp_path: Path) -> None:
    d = draft(tmp_path / "machine.json")
    camera = CameraId("CAM3")
    rects = [
        BoundingRect(0, 0, 1, 1),
        *[BoundingRect(0, 0, 100, 10)] * 4,
        BoundingRect(10, 0, 1, 1),
    ]
    d.replace_rects(camera, tuple(rects))
    before = d.definition
    with pytest.raises(ValueError):
        d.distribute(camera)
    assert d.definition == before


def test_save_failure_keeps_draft_and_baseline(tmp_path: Path) -> None:
    d = draft(tmp_path / "missing" / "machine.json")
    before = d.saved
    d.common_size(CameraId("CAM3"), 10, 10)
    with pytest.raises(ConfigurationIOError):
        d.save()
    assert d.dirty and d.saved == before
    d.discard()
    assert d.definition == before
