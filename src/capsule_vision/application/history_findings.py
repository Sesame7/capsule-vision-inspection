"""Display representatives within one historical camera exposure."""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace

from capsule_vision.inference import BoundingRect


@dataclass(frozen=True, slots=True)
class HistoryFinding:
    label: str
    bounding_rect: BoundingRect
    channels: tuple[int, ...]
    area: int | None = None
    filtered: bool = False


type FindingKey = tuple[int, str, bool | None]


def lower_view(box: BoundingRect, roi: BoundingRect | None) -> bool | None:
    return (
        None if roi is None else 2 * box.y + box.height >= 2 * (roi.y + roi.height // 2)
    )


def finding_key(
    finding: HistoryFinding, channel: int, roi: BoundingRect | None
) -> FindingKey:
    return channel, finding.label, lower_view(finding.bounding_rect, roi)


def display_winners(
    findings: Sequence[HistoryFinding], rois: Mapping[int, BoundingRect | None]
) -> dict[FindingKey, int]:
    """Prefer a measurement, then largest area. Equal ranks retain stored order."""
    winners: dict[FindingKey, int] = {}
    ranks: dict[FindingKey, tuple[bool, int]] = {}
    for index, finding in enumerate(findings):
        rect = finding.bounding_rect
        rank = (
            finding.area is not None,
            finding.area if finding.area is not None else rect.width * rect.height,
        )
        for channel in finding.channels:
            key = finding_key(finding, channel, rois.get(channel))
            if key not in ranks or rank > ranks[key]:
                winners[key], ranks[key] = index, rank
    return winners


def selected_findings(
    findings: Sequence[HistoryFinding], winners: Mapping[FindingKey, int]
) -> tuple[HistoryFinding, ...]:
    """Keep a shared rectangle once, with only its winning channel associations."""
    return _with_channels(findings, ((key[0], index) for key, index in winners.items()))


def display_findings(
    findings: Sequence[HistoryFinding], rois: Mapping[int, BoundingRect | None]
) -> tuple[HistoryFinding, ...]:
    winners = display_winners(findings, rois)
    selections = [(key[0], i) for key, i in winners.items()]
    # Both area-writing formats store only measured winners. An older package can
    # lack the Run manifest while still carrying that authoritative pair.
    for (ch, label, view), _ in winners.items():
        if view is not None:
            continue
        pair = [
            (i, f)
            for i, f in enumerate(findings)
            if ch in f.channels and f.label == label
        ]
        if len(pair) == 2 and all(f.area is not None for _, f in pair):
            centers = {2 * f.bounding_rect.y + f.bounding_rect.height for _, f in pair}
            if len(centers) == 2:
                selections.extend((ch, i) for i, _ in pair)
    return _with_channels(findings, selections)


def _with_channels(
    findings: Sequence[HistoryFinding], selections: Iterable[tuple[int, int]]
) -> tuple[HistoryFinding, ...]:
    selected: dict[int, set[int]] = {}
    for channel, index in selections:
        selected.setdefault(index, set()).add(channel)
    return tuple(
        replace(
            f,
            channels=tuple(ch for ch in dict.fromkeys(f.channels) if ch in selected[i]),
        )
        for i, f in enumerate(findings)
        if i in selected
    )
