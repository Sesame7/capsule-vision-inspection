"""S1-E2A fixed photo bit only; no production communication API changes.

W342 5-2-1/2 and 5-3-2/3: WR bit code 31, W10 bit7, one byte 00/01.
The existing TCP transport validates envelopes/ACKs. Private exchange reuse is
restricted to this isolated diagnostic, as in the verified S1-D2A diagnostic.
"""

import math
from collections.abc import Callable
from dataclasses import dataclass
from threading import Event
from time import perf_counter, sleep
from typing import Protocol

from vimo_demo.communication import FinsMemoryArea, FinsTcpClient

HOST = "192.168.251.1"
PHOTO = bytes.fromhex("31 00 0a 07 00 01")


@dataclass(frozen=True)
class PlcState:
    words: tuple[int, ...]  # W10..W18; one contiguous protocol read.

    @property
    def photo(self) -> int:
        return (self.words[0] >> 7) & 1

    def require_idle(self, original: PlcState | None = None) -> None:
        if len(self.words) != 9:
            raise RuntimeError("Invalid PLC status length")
        if not self.words[5] & 1 or self.words[6] & 6 or self.photo:
            raise RuntimeError(f"PLC not stopped/manual/released: {self.words}")
        if original is not None and (
            self.words[0] & ~128 != original.words[0] & ~128
            or self.words[5:9] != original.words[5:9]
        ):
            raise RuntimeError(f"PLC state changed: {original.words} -> {self.words}")


class PhotoClient(FinsTcpClient):
    def snapshot(self) -> PlcState:
        return PlcState(self.read_words(10, 9, area=FinsMemoryArea.W))

    def read_photo(self) -> int:
        value = self._exchange(b"\x01\x01", PHOTO, 1)  # pyright: ignore[reportPrivateUsage]
        if value not in (b"\x00", b"\x01"):
            raise ValueError("Invalid photo bit read response")
        return value[0]

    def write_photo(self, value: int) -> None:
        if type(value) is not int or value not in (0, 1):
            raise ValueError("Only integer 0/1 may be written to W10.7")
        self._exchange(b"\x01\x02", PHOTO + bytes((value,)), 0)  # pyright: ignore[reportPrivateUsage]


class PhotoConnection(Protocol):
    @property
    def closed(self) -> bool: ...
    def write_photo(self, value: int) -> None: ...
    def read_photo(self) -> int: ...


def wait_until(deadline: float, stop: Event) -> None:
    """Existing local-source coarse/fine waiting policy; no timer/GPU settings."""
    while not stop.is_set():
        remaining = deadline - perf_counter()
        if remaining <= 0:
            return
        if remaining > 0.012:
            stop.wait(remaining - 0.012)
        else:
            sleep(min(0.0005, remaining))


def release(
    client: PhotoConnection,
    recovery: Callable[[], PhotoConnection],
) -> tuple[bool, int]:
    """One explicit release action; a retired socket uses a separate 0-only recovery.

    No activation is replayed. Caller owns recovery close via its supplied factory.
    Failure to verify low remains an error, never inferred from a timeout.
    """
    fresh = client.closed
    connection = recovery() if fresh else client
    connection.write_photo(0)
    value = connection.read_photo()
    if value != 0:
        raise RuntimeError("UNRESOLVED RELEASE: W10.7 not zero")
    return fresh, value


def pulse(
    client: PhotoConnection,
    *,
    hold_ms: float,
    full_readback: bool,
    stop: Event,
    recovery: Callable[[], PhotoConnection],
    clock: Callable[[], float] = perf_counter,
    wait: Callable[[float, Event], None] = wait_until,
    trace: dict[str, object] | None = None,
) -> dict[str, object]:
    if not math.isfinite(hold_ms) or not 5 <= hold_ms <= 1000:
        raise ValueError("Experimental hold must be 5..1000 ms")
    if stop.is_set():
        raise RuntimeError("Trial stopped before activation")
    start = clock()
    row = trace if trace is not None else {}
    row.update(
        activation_send=start, hold_requested_ms=hold_ms, activation_attempted=True
    )
    try:
        client.write_photo(1)  # Exactly once. Any missing ACK ends activation trials.
        row["activation_ack"] = clock()
        if full_readback:
            if client.read_photo() != 1:
                raise RuntimeError("Unexpected asserted-bit readback")
            row["activation_readback"] = clock()
        wait(start + hold_ms / 1000, stop)
    except BaseException as exc:
        row["activation_error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        row["deactivation_send"] = clock()
        try:
            if client.closed:
                row["recovery_connection"], row["release_readback"] = release(
                    client, recovery
                )
            else:
                client.write_photo(0)
                row["deactivation_ack"] = clock()
                if full_readback:
                    if client.read_photo() != 0:
                        raise RuntimeError("UNRESOLVED RELEASE: nonzero low readback")
                    row["release_readback"] = 0
            row["release_complete"] = clock()
        except BaseException as exc:
            row["release_error"] = f"{type(exc).__name__}: {exc}"
            raise
        # A diagnostic error carries the partial trace via exception notes below
        # in the owner; successful rows contain every measured command boundary.
    return row
