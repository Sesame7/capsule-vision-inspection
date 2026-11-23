"""Reconcile recorded effective boxes with measured replay regions, one channel/view/label at a time."""

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from scripts.area_explorer.evidence import Frame

from capsule_vision.application.history_findings import (
    FindingKey,
    HistoryFinding,
    display_winners,
    finding_key,
    selected_findings,
)
from capsule_vision.inference import BoundingRect
from capsule_vision.inspection import CycleId, FrameId
from capsule_vision.inspection.production import CameraLayout, ViMoFinding
from capsule_vision.inspection.production.evidence import (
    assign_vimo_findings,
    representative_findings,
)


def coordinates(rect: BoundingRect) -> tuple[int, int, int, int]:
    return rect.x, rect.y, rect.width, rect.height


def encode(finding: HistoryFinding) -> list[object]:
    row: list[object] = [
        finding.label,
        list(coordinates(finding.bounding_rect)),
        list(finding.channels),
    ]
    if finding.area is not None:
        row.append(finding.area)
    return row


@dataclass
class Comparison:
    findings: list[HistoryFinding] = field(default_factory=list[HistoryFinding])
    counts: Counter[str] = field(default_factory=Counter[str])
    issues: list[dict[str, object]] = field(default_factory=list[dict[str, object]])


def reconcile(
    old: Sequence[HistoryFinding],
    frame: Frame,
    cycle: int,
    layout: CameraLayout,
    module: str,
    disabled: frozenset[str],
    minima: Mapping[str, int],
) -> Comparison:
    if frame.regions is None:
        raise ValueError("Missing replay measurements")
    layout.validate_shape((frame.height, frame.width, 3))
    # One local replay frame. Its sequence is never written as an acquisition identity.
    identity = FrameId(CycleId(cycle), layout.camera_id, 0)
    raw = tuple(
        ViMoFinding(identity, r.number - 1, r.label, r.area, r.box, module)
        for r in frame.regions
    )
    if any(
        f.bounding_rect.x + f.bounding_rect.width > frame.width
        or f.bounding_rect.y + f.bounding_rect.height > frame.height
        for f in raw
    ):
        raise ValueError("SDK region outside original camera image")
    assigned, _ = assign_vimo_findings(layout, raw)
    effective = tuple(
        a
        for a in assigned
        if a.finding.label not in disabled and frame.gate(a.channel_id) == "PRESENT"
    )
    channels: dict[ViMoFinding, list[int]] = defaultdict(list)
    for a in effective:
        channels[a.finding].append(a.channel_id)
    new = [
        HistoryFinding(f.label, f.bounding_rect, tuple(ch), f.area)
        for f, ch in channels.items()
    ]
    indices = {f: i for i, f in enumerate(channels)}
    rois = {int(s.channel_id): s.rect for s in layout.slots}
    winners: dict[FindingKey, int] = {}
    for slot in layout.slots:
        for assignment in representative_findings(slot, effective, minima)[0]:
            index = indices[assignment.finding]
            winners[finding_key(new[index], slot.channel_id, slot.rect)] = index

    historical = display_winners(old, rois)
    buckets: dict[FindingKey, list[int]] = defaultdict(list)
    for i, f in enumerate(new):
        for ch in f.channels:
            buckets[finding_key(f, ch, rois[ch])].append(i)
    result = Comparison()
    supported = {key: i for key, i in winners.items() if key in historical}
    fallback = {key: i for key, i in historical.items() if key not in supported}
    result.counts["measured_assignments"] = len(supported)
    result.counts["subordinate_assignments"] = sum(len(f.channels) for f in old) - len(
        historical
    )
    for key in sorted(fallback.keys() | (buckets.keys() - historical.keys())):
        ch, label, lower = key
        if key not in historical:
            reason = "replay_only"
        elif frame.gate(ch) == "Unknown":
            reason = "occupancy_unavailable"
        elif key in buckets:
            reason = "below_frozen_minimum"
        else:
            reason = "historical_only"
        result.counts[reason] += 1
        result.issues.append(
            dict(
                channel=ch,
                label=label,
                view="lower" if lower else "upper",
                reason=reason,
                kept=encode(old[fallback[key]]) if key in fallback else None,
                replay=[encode(new[i]) for i in buckets.get(key, ())],
            )
        )
    result.findings.extend(selected_findings(old, fallback))
    result.findings.extend(selected_findings(new, supported))
    # Reusing the original ordering avoids a write for an already-correct exposure.
    if Counter(result.findings) == Counter(old):
        result.findings = list(old)
    return result
