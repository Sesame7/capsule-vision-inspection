"""Local B12 waiting contracts use simulated time, not Windows wake-up precision."""

from collections.abc import Callable
from threading import Event

import pytest

from vimo_demo.ui import local_production as local

# Exercise the private wait contract without adding a public timing API.
wait_for_interval = local._wait_for_source_interval  # pyright: ignore[reportPrivateUsage]


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now
        self.sleeps: list[float] = []
        self.on_sleep: Callable[[], None] = lambda: None

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.on_sleep()
        self.now += seconds


class StopEvent(Event):
    def __init__(self, clock: Clock) -> None:
        super().__init__()
        self.clock = clock
        self.waits: list[float] = []
        self.on_wait: Callable[[], None] = lambda: None
        self.overshoot = 0.0

    def wait(self, timeout: float | None = None) -> bool:
        assert timeout is not None and timeout > 0
        self.waits.append(timeout)
        self.on_wait()
        if not self.is_set():
            self.clock.now += timeout + self.overshoot
        return self.is_set()


def install_clock(monkeypatch: pytest.MonkeyPatch, clock: Clock) -> StopEvent:
    monkeypatch.setattr(local, "monotonic", clock.monotonic)
    monkeypatch.setattr(local, "sleep", clock.sleep)
    return StopEvent(clock)


@pytest.mark.parametrize("work", [0.040, 0.080, 0.115])
def test_previous_start_deadline_and_coarse_to_fine(
    monkeypatch: pytest.MonkeyPatch, work: float
) -> None:
    clock = Clock(10 + work)
    stop = install_clock(monkeypatch, clock)
    assert not wait_for_interval(stop, 10, 8)
    assert clock.now == pytest.approx(10.125)
    assert stop.waits == (pytest.approx([0.125 - work - 0.012]) if work < 0.113 else [])
    assert clock.sleeps and all(0 < seconds <= 0.0005 for seconds in clock.sleeps)


def test_early_coarse_return_still_waits_until_absolute_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Clock(10.08)
    stop = install_clock(monkeypatch, clock)
    stop.overshoot = -0.010
    assert not wait_for_interval(stop, 10, 8)
    assert clock.now == pytest.approx(10.125)


def test_coarse_overshoot_does_not_add_fine_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Clock(10.08)
    stop = install_clock(monkeypatch, clock)
    stop.overshoot = 0.020
    assert not wait_for_interval(stop, 10, 8)
    assert clock.now == pytest.approx(10.133)
    assert clock.sleeps == []


@pytest.mark.parametrize("now", [10.125, 10.130, 10.500])
def test_zero_or_overdue_budget_introduces_no_wait(
    monkeypatch: pytest.MonkeyPatch, now: float
) -> None:
    clock = Clock(now)
    stop = install_clock(monkeypatch, clock)
    assert not wait_for_interval(stop, 10, 8)
    assert clock.now == now and not stop.waits and not clock.sleeps


@pytest.mark.parametrize("phase", ["before", "coarse", "fine", "expired"])
def test_stop_is_authoritative_at_each_wait_phase(
    monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    clock = Clock(10.130 if phase == "expired" else 10.080)
    stop = install_clock(monkeypatch, clock)
    if phase in ("before", "expired"):
        stop.set()
    elif phase == "coarse":
        stop.on_wait = stop.set
    else:
        clock.on_sleep = stop.set
    assert wait_for_interval(stop, 10, 8)
    assert len(clock.sleeps) == (1 if phase == "fine" else 0)
    if phase in ("before", "expired"):
        assert stop.waits == []


def test_repeated_iterations_anchor_to_actual_start_without_catchup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Clock(10)
    stop = install_clock(monkeypatch, clock)
    starts = [clock.now]
    for work in (0.040, 0.130, 0.080):
        previous = clock.now
        clock.now += work
        assert not wait_for_interval(stop, previous, 8)
        starts.append(clock.now)
    assert starts == pytest.approx([10, 10.125, 10.255, 10.380])


def test_configured_fps_preserves_its_original_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Clock(10.08)
    stop = install_clock(monkeypatch, clock)
    assert not wait_for_interval(stop, 10, 4)
    assert clock.now == pytest.approx(10.25)
    assert stop.waits == pytest.approx([0.158])
