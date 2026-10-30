"""Deterministic machine ROI batches and atomic persistence, without Qt/GPU."""

from dataclasses import replace
from pathlib import Path

import pytest
from tests.support.configuration import machine

from capsule_vision.configuration import (
    ConfigurationError,
    ConfigurationIOError,
    load_machine_layout,
    save_machine_layout,
)
from capsule_vision.configuration.drafts import MachineLayoutDraft
from capsule_vision.inference import BoundingRect
from capsule_vision.inspection import CameraId
from capsule_vision.inspection.production import ChannelId, InspectionLayout


def test_cam6_template_uses_draft_cam5_with_independent_geometry_and_identities(
    tmp_path: Path,
) -> None:
    original = machine(mirrored=True)
    working = MachineLayoutDraft(original, tmp_path / "machine.tsv")
    working.remove_camera_six()
    assert working.saved == original and working.dirty
    assert working.definition.layout.cameras == original.layout.cameras[:5]
    working.replace_rects(
        CameraId("CAM5"),
        tuple(
            replace(s.rect, x=s.rect.x + s.slot + 2, height=11 + s.slot)
            for s in working.camera(CameraId("CAM5")).slots
        ),
    )
    five = working.definition
    working.restore_camera_six()
    assert working.definition.layout.cameras[:5] == five.layout.cameras
    assert working.saved == original and working.dirty
    template = working.camera(CameraId("CAM6"))
    assert [s.slot for s in template.slots] == list(range(6))
    assert [s.channel_id for s in template.slots] == list(range(31, 37))
    cam5 = working.camera(CameraId("CAM5"))
    for source, target in zip(cam5.slots, template.slots, strict=True):
        assert source.rect == target.rect and source.rect is not target.rect
        assert source is not target and source.channel_id != target.channel_id
    with pytest.raises(ValueError):
        working.restore_camera_six()
    working.replace_rects(
        CameraId("CAM6"),
        (replace(template.slots[0].rect, x=8), *(s.rect for s in template.slots[1:])),
    )
    assert working.camera(CameraId("CAM5")) == cam5
    working.bounds[CameraId("CAM6")] = (60, 8)
    before = working.definition
    with pytest.raises(ValueError, match="outside"):
        working.save()
    assert working.definition == before and working.saved == original
    assert working.path is not None and not working.path.exists()
    working.bounds[CameraId("CAM6")] = (200, 40)
    working.save()
    assert load_machine_layout(working.path) == before and not working.dirty
    working.replace_rects(CameraId("CAM6"), tuple(s.rect for s in template.slots))
    assert working.dirty
    working.discard()
    assert working.definition == before and not working.dirty


@pytest.mark.parametrize("defect", ["camera_order", "channel_mapping"])
def test_machine_rejects_noncanonical_identity_mapping(defect: str) -> None:
    original = machine(mirrored=True)
    # Contradictory identities are rejected before editing or saving geometry.
    cameras = (
        tuple(reversed(original.layout.cameras))
        if defect == "camera_order"
        else tuple(
            replace(
                c,
                slots=tuple(
                    replace(s, channel_id=ChannelId(37 - s.channel_id)) for s in c.slots
                ),
            )
            for c in original.layout.cameras
        )
    )
    with pytest.raises(
        ValueError, match="CAM1" if defect == "camera_order" else "canonical"
    ):
        replace(
            original,
            layout=InspectionLayout(cameras),
        )
    assert MachineLayoutDraft(original, None).definition == original


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
    d = draft(tmp_path / "machine.tsv")
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
    d = draft(tmp_path / "machine.tsv")
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
    # Interpolation changes geometry while the fixed channel mapping stays intact.
    assert [s.channel_id for s in slots] == list(range(13, 19))
    stable = d.definition
    d.distribute(camera)
    assert d.definition == stable


@pytest.mark.parametrize("size", [(500, 5), (5, 500), (0, 5), (-1, 4)])
def test_invalid_common_size_rejected_atomically(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    d = draft(tmp_path / "machine.tsv")
    before = d.definition
    with pytest.raises(ValueError):
        d.common_size(CameraId("CAM3"), *size)
    assert d.definition == before and not d.dirty


def test_distribution_rejects_intermediate_outside_bounds(tmp_path: Path) -> None:
    d = draft(tmp_path / "machine.tsv")
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


def test_save_failure_keeps_draft_and_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = draft(tmp_path / "missing" / "machine.tsv")
    before = d.saved
    d.common_size(CameraId("CAM3"), 10, 10)

    def denied(*_args: object) -> None:
        raise ConfigurationIOError("save denied")

    monkeypatch.setattr(
        "capsule_vision.configuration.drafts.save_machine_layout", denied
    )
    with pytest.raises(ConfigurationIOError):
        d.save()
    assert d.dirty and d.saved == before
    d.discard()
    assert d.definition == before


@pytest.mark.parametrize("width,height", [(60, 20), (61, 19), (7, 3)])
def test_first_run_geometry_uses_real_dimensions_once(
    tmp_path: Path, width: int, height: int
) -> None:
    d = MachineLayoutDraft(None, tmp_path / "data/machine.tsv")
    assert d.camera_ids == tuple(f"CAM{i}" for i in range(1, 7))
    assert not d.dirty and not d.complete and not d.persisted
    assert all(d.rois(c) == () for c in d.camera_ids)
    assert d.initialize(CameraId("CAM1"), width, height)
    slots = d.rois(CameraId("CAM1"))
    assert [(s.slot, s.channel_id) for s in slots] == [(i, i + 1) for i in range(6)]
    assert [s.rect for s in slots] == [
        BoundingRect(i * width // 6, 0, (i + 1) * width // 6 - i * width // 6, height)
        for i in range(6)
    ]
    assert sum(s.rect.width for s in slots) == width
    assert all(
        a.rect.x + a.rect.width == b.rect.x
        for a, b in zip(slots, slots[1:], strict=False)
    )
    changed = (replace(slots[0].rect, height=height - 1), *(s.rect for s in slots[1:]))
    d.replace_rects(CameraId("CAM1"), changed)
    assert not d.initialize(CameraId("CAM1"), width + 20, height + 20)
    assert tuple(s.rect for s in d.rois(CameraId("CAM1"))) == changed
    with pytest.raises(ValueError, match="initialized"):
        d.save()
    assert d.path is not None and not d.path.exists()
    d.remove_camera_six()
    assert d.camera_ids == tuple(f"CAM{i}" for i in range(1, 6))
    d.restore_camera_six()
    assert not d.rois(CameraId("CAM6"))  # No copied/fabricated first-run geometry.
    for camera in d.camera_ids[1:]:
        d.initialize(camera, width, height)
    assert d.complete
    d.save()
    assert d.persisted and not d.dirty and load_machine_layout(d.path) == d.definition
    saved = d.definition
    assert not d.initialize(CameraId("CAM2"), 1000, 1000)
    assert d.definition == saved


@pytest.mark.parametrize("width,height", [(0, 10), (5, 10), (6, 0), (True, 10)])
def test_first_run_rejects_dimensions_that_cannot_form_valid_rois(
    width: int, height: int
) -> None:
    d = MachineLayoutDraft(None, None)
    with pytest.raises(ValueError):
        d.initialize(CameraId("CAM1"), width, height)
    assert not d.rois(CameraId("CAM1")) and not d.complete and not d.dirty


@pytest.mark.parametrize("present", [False, True])
def test_first_machine_save_preserves_invalid_or_new_external_file(
    tmp_path: Path, present: bool
) -> None:
    path = tmp_path / "machine.tsv"
    if present:
        path.write_text("invalid saved geometry", encoding="utf-8")
    working = MachineLayoutDraft(None, path)
    for camera in working.camera_ids:
        working.initialize(camera, 60, 20)
    if not present:
        save_machine_layout(path, machine())
    original = path.read_bytes()
    with pytest.raises(ConfigurationError):
        working.save()
    assert path.read_bytes() == original and working.dirty and not working.persisted
