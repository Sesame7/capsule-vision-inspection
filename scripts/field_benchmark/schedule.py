"""Absolute host-monotonic slots, with no catch-up or replacement pulses."""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from math import isfinite
from threading import Event
from time import monotonic


@dataclass(frozen=True, slots=True)
class Slot:
    pulse: int
    group: int
    offset: int
    planned_at: float


@dataclass(frozen=True, slots=True)
class BurstPlan:
    start: float
    groups: int
    pulse_budget: int
    end: float
    max_lateness: float = 0.010

    def __post_init__(self) -> None:
        if (
            type(self.groups) is not int
            or not 1 <= self.groups <= 10000
            or type(self.pulse_budget) is not int
            or not 1 <= self.pulse_budget <= 4 * self.groups
            or not all(isfinite(v) for v in (self.start, self.end, self.max_lateness))
            or not self.start < self.end <= self.start + self.groups * 0.5
            or not 0 <= self.max_lateness < 0.060
        ):
            raise ValueError("Invalid finite burst budget/deadline/lateness guard")

    def slots(self) -> Iterator[Slot]:
        for pulse in range(self.pulse_budget):
            group, offset = divmod(pulse, 4)
            planned = self.start + group * 0.5 + offset * 0.060
            if planned >= self.end:
                return
            yield Slot(pulse, group, offset, planned)


def drive(
    plan: BurstPlan,
    send: Callable[[Slot], bool],
    observe: Callable[[str, Slot], None],
    stop: Event,
    *,
    clock: Callable[[], float] = monotonic,
    wait: Callable[[float], bool] | None = None,
) -> str:
    """send includes verified release. A late/failed slot ends this run, no retry."""
    wait = wait or stop.wait
    for slot in plan.slots():
        observe("trigger_plan", slot)
        if stop.is_set() or wait(max(0.0, slot.planned_at - clock())):
            observe("trigger_cancelled", slot)
            return "cancelled"
        now = clock()
        if now >= plan.end or now - slot.planned_at > plan.max_lateness:
            observe("trigger_late", slot)
            return "late"
        if not send(slot):
            return "failed"
    return "budget_complete"
