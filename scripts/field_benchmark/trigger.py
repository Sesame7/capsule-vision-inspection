"""Independent serial command owner and finite controller lease.

P0B-1 is deliberately loopback-only. No field endpoint can be contacted by this
tool until a subsequent readiness review enables live operation. The controller
never owns a PLC socket: revocation and release cannot race another writer here.
"""

import hashlib
import json
import math
import multiprocessing as mp
import os
from dataclasses import dataclass
from ipaddress import IPv4Address
from multiprocessing.process import BaseProcess
from pathlib import Path
from threading import Event
from time import monotonic
from typing import Protocol

from vimo_demo.communication.fins import FinsProtocolError
from vimo_demo.communication.fins_tcp import FinsTcpClient

from .ledger import Ledger
from .schedule import BurstPlan, Slot

PHOTO = bytes.fromhex("31 00 0a 07 00 01")  # W342 WR bit, W10.7, one bit.


class Channel(Protocol):
    def send(self, obj: object) -> None: ...
    def recv(self) -> object: ...
    def poll(self, timeout: float = 0.0) -> bool: ...
    def close(self) -> None: ...


class PhotoClient(FinsTcpClient):
    def __init__(self, host: str, *, port: int, timeout: float) -> None:
        if not IPv4Address(host).is_loopback:
            raise ValueError("P0B-1 permits controlled loopback peers only")
        super().__init__(host, port=port, timeout=timeout)

    def __enter__(self) -> PhotoClient:
        super().__enter__()
        return self

    def read_photo(self) -> int:
        data = self._exchange(b"\x01\x01", PHOTO, 1)
        if data not in (b"\x00", b"\x01"):
            raise FinsProtocolError("Invalid W10.7 bit response")
        return data[0]

    def write_photo(self, value: int) -> None:
        if type(value) is not int or value not in (0, 1):
            raise ValueError("Only one W10.7 bit value 0/1 is permitted")
        self._exchange(b"\x01\x02", PHOTO + bytes((value,)), 0)


@dataclass(frozen=True, slots=True)
class Readiness:
    """Explicit receipt, not discovery/old ARMED timestamps or inferred safety.

    Local tests supply controlled evidence. A future live receipt must be based
    on fresh manual/stopped/low/no-competing-writer and attached RUNNING evidence.
    """

    run: str
    generation: int
    source_epoch: str
    configuration_sha256: str
    expires_at: float
    manual_stopped: bool
    exclusive_photo_writer: bool
    inspection_attached: bool
    release_qualified: bool

    def validate(self, run: str, now: float) -> None:
        if (
            self.run != run
            or self.generation < 1
            or not self.source_epoch
            or len(self.configuration_sha256) != 64
            or not math.isfinite(self.expires_at)
            or now >= self.expires_at
            or not all(
                (
                    self.manual_stopped,
                    self.exclusive_photo_writer,
                    self.inspection_attached,
                    self.release_qualified,
                )
            )
        ):
            raise ValueError("Missing, mismatched or expired readiness receipt")


@dataclass(frozen=True, slots=True)
class SupervisorSettings:
    run: str
    host: str
    port: int
    plan: BurstPlan
    readiness: Readiness
    hold: float  # Explicit experiment input; no invented electrical pulse width.
    lease: float = 1.0
    timeout: float = 0.25

    def validate(self) -> None:
        if not IPv4Address(self.host).is_loopback:
            raise ValueError("P0B-1 permits controlled loopback peers only")
        if not self.run or not 1 <= self.port <= 65535:
            raise ValueError("Explicit run/endpoint required")
        if not all(math.isfinite(v) for v in (self.hold, self.lease, self.timeout)):
            raise ValueError("Nonfinite control bound")
        if not 0 < self.hold < 0.060 or not 0 < self.timeout <= 1:
            raise ValueError("Hold must fit the 60ms slots; timeout is bounded")
        if not self.hold < self.lease <= 5:
            raise ValueError("Finite lease must exceed hold")
        self.readiness.validate(self.run, monotonic())


@dataclass(frozen=True, slots=True)
class Request:
    run: str
    kind: str
    slot: Slot | None = None


@dataclass(frozen=True, slots=True)
class Reply:
    run: str
    kind: str
    released: bool = False
    issue: str | None = None


class EndpointLock:
    """OS-released endpoint exclusion; all supervisors share this lock directory.

    Lock files persist as receipts. Never unlink a locked pathname and permit a
    second inode/owner. Windows process termination releases the byte-range lock.
    """

    def __init__(self, root: Path, host: str, port: int) -> None:
        root.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha256(f"{host}:{port}".encode()).hexdigest()
        self._file = (root / f"{key}.lock").open("a+b")
        self._file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if not self._file.read(1):
                self._file.write(b"0")
                self._file.flush()
        except BaseException:
            self._file.close()
            raise

    def close(self) -> None:
        self._file.close()  # OS releases ownership, including after process death.


class _Supervisor:
    def __init__(
        self, settings: SupervisorSettings, connection: Channel, ledger: Ledger
    ) -> None:
        self.settings, self.connection, self.ledger = settings, connection, ledger
        self.client: PhotoClient | None = None
        self.revoked = False
        self.released = False
        self.last_pulse = -1
        self.lease_until = 0.0

    def emit(
        self, kind: str, pulse: int | None = None, **fields: str | float | bool | None
    ) -> None:
        self.ledger.emit(kind, pulse=pulse, **fields)

    def connect(self) -> PhotoClient:
        s = self.settings
        return PhotoClient(s.host, port=s.port, timeout=s.timeout)

    def release(self, pulse: int | None, *, recovery: bool) -> bool:
        # Recovery uses a new serial connection ONLY for zero; no activation retry.
        try:
            if recovery or self.client is None or self.client.closed:
                if self.client is not None:
                    self.client.close()
                self.client = self.connect()
            self.emit("release_send", pulse, recovery=recovery)
            self.client.write_photo(0)
            self.emit("release_ack", pulse)
            value = self.client.read_photo()
            self.emit("release_readback", pulse, low=value == 0)
            self.released = value == 0
            if not self.released:
                raise RuntimeError("W10.7 remains asserted")
        except Exception as exc:
            self.released = False
            self.emit("unresolved_release", pulse, issue=f"{type(exc).__name__}: {exc}")
        return self.released

    def pulse(self, slot: Slot) -> Reply:
        s = self.settings
        now = monotonic()
        s.readiness.validate(s.run, now)
        expected = s.plan.start + slot.group * 0.5 + slot.offset * 0.060
        if (
            self.revoked
            or self.ledger.abort.is_set()
            or now >= self.lease_until
            or now >= s.plan.end
            or now < slot.planned_at
            or now - slot.planned_at > s.plan.max_lateness
            or slot.pulse != self.last_pulse + 1
            or not 0 <= slot.pulse < s.plan.pulse_budget
            or (slot.group, slot.offset) != divmod(slot.pulse, 4)
            or slot.planned_at != expected
        ):
            raise RuntimeError("Lease/budget/slot admission failed; no catch-up")
        self.last_pulse = slot.pulse
        self.released = False
        issue: str | None = None
        try:
            assert self.client is not None
            self.emit("activation_send", slot.pulse, planned_at=slot.planned_at)
            self.client.write_photo(1)
            self.emit("activation_ack", slot.pulse)
            if self.client.read_photo() != 1:
                raise RuntimeError("High readback absent; no repeated activation")
            self.emit("activation_readback", slot.pulse, high=True)
            # A pipe event (cancel/EOF/anything unexpected) shortens high time.
            # Commands queued during high are NOT executed; revoke instead.
            remaining = min(now + s.hold, self.lease_until, s.plan.end) - monotonic()
            if remaining > 0 and self.connection.poll(remaining):
                raise RuntimeError("Controller interrupted pulse; lease revoked")
        except Exception as exc:
            issue = f"{type(exc).__name__}: {exc}"
            self.revoked = True
            self.emit("activation_uncertain", slot.pulse, issue=issue)
        self.release(slot.pulse, recovery=issue is not None)
        if not self.released or monotonic() >= self.lease_until:
            self.revoked = True
            issue = issue or "Release/lease deadline not satisfied"
        return Reply(s.run, "pulse_finished", self.released, issue)

    def serve(self) -> str:
        s = self.settings
        self.client = self.connect()
        if self.client.read_photo() != 0:
            raise RuntimeError("W10.7 initially high; no activation admitted")
        self.released = True
        self.lease_until = min(
            monotonic() + s.lease, s.plan.end, s.readiness.expires_at
        )
        self.ledger.emit(
            "supervisor_ready", lease_until=self.lease_until, pid=os.getpid()
        )
        self.connection.send(Reply(s.run, "ready", True))
        while not self.revoked:
            remaining = (
                min(self.lease_until, s.plan.end, s.readiness.expires_at) - monotonic()
            )
            if remaining <= 0 or self.ledger.abort.is_set():
                return "lease_expired"
            if not self.connection.poll(min(remaining, 0.05)):
                continue
            request = self.connection.recv()
            if monotonic() >= min(self.lease_until, s.plan.end, s.readiness.expires_at):
                return "lease_expired"
            if not isinstance(request, Request) or request.run != s.run:
                raise RuntimeError("Wrong run/invalid IPC request")
            if request.kind == "stop":
                return "stop"
            if request.kind == "heartbeat":
                self.lease_until = min(
                    monotonic() + s.lease, s.plan.end, s.readiness.expires_at
                )
                self.ledger.emit("heartbeat", lease_until=self.lease_until)
                self.connection.send(Reply(s.run, "heartbeat", self.released))
            elif request.kind == "pulse" and request.slot is not None:
                reply = self.pulse(request.slot)
                self.connection.send(reply)
                if reply.issue is not None:
                    self.revoked = True
            else:
                raise RuntimeError("Unexpected IPC command")
        return "revoked"


def supervise(
    settings: SupervisorSettings, connection: Channel, directory: Path, locks: Path
) -> None:
    """Process entry. Bounded lease watchdog is independent of controller finally."""
    settings.validate()  # Hard loopback gate precedes sockets or file ownership.
    lock = EndpointLock(locks, settings.host, settings.port)
    ledger = Ledger(directory / "supervisor.jsonl", settings.run, "supervisor")
    owner = _Supervisor(settings, connection, ledger)
    reason = "failure"
    try:
        reason = owner.serve()
    except EOFError:
        reason = "controller_eof"
    except Exception as exc:
        ledger.emit("supervisor_failure", issue=f"{type(exc).__name__}: {exc}")
    finally:
        owner.revoked = True  # Irreversible; no activation while recovering.
        ledger.emit("lease_revoked", reason=reason)
        # One independent zero-only recovery, even if controller died mid-command.
        owner.release(None, recovery=True)
        if owner.client is not None:
            owner.client.close()
        ledger.emit(
            "owner_stopped", owner="photo_socket", terminated=True, cleanup_safe=True
        )
        ledger_ok = ledger.close()
        with (directory / "supervisor-final.json").open(
            "x", encoding="utf-8"
        ) as stream:
            json.dump(
                {
                    "run": settings.run,
                    "reason": reason,
                    "release_verified": owner.released,
                    "ledger_ok": ledger_ok,
                    "activations": owner.last_pulse + 1,
                },
                stream,
            )
        connection.close()
        lock.close()


class Controller:
    """One request at a time, no background request queue or retry."""

    def __init__(self, settings: SupervisorSettings, connection: Channel) -> None:
        self.settings, self.connection = settings, connection
        self.ready = False
        self.failed = False

    def receive(self, kind: str) -> Reply:
        # Includes recovery handshake/write/read plus the uncertain old exchange.
        if not self.connection.poll(8 * self.settings.timeout + self.settings.hold + 1):
            self.failed = True
            raise TimeoutError("Supervisor response missing; no activation retry")
        reply = self.connection.recv()
        if (
            not isinstance(reply, Reply)
            or reply.run != self.settings.run
            or reply.kind != kind
        ):
            self.failed = True
            raise RuntimeError("Mismatched supervisor acknowledgment")
        return reply

    def acknowledge_ready(self) -> None:
        reply = self.receive("ready")
        self.ready = reply.released and reply.issue is None

    def heartbeat(self) -> None:
        if not self.ready or self.failed:
            raise RuntimeError("Supervisor not acknowledged or already failed")
        self.connection.send(Request(self.settings.run, "heartbeat"))
        self.receive("heartbeat")

    def pulse(self, slot: Slot) -> bool:
        if not self.ready or self.failed:
            raise RuntimeError("Supervisor not acknowledged or already failed")
        self.connection.send(Request(self.settings.run, "pulse", slot))
        reply = self.receive("pulse_finished")
        self.failed = reply.issue is not None or not reply.released
        return not self.failed

    def stop(self) -> None:
        self.connection.send(Request(self.settings.run, "stop"))

    def run(self, ledger: Ledger, stop: Event) -> str:
        """Field-local schedule; logging pressure/cancellation ends admission.

        The caller still owns Production Stop/drain/Disconnect. This method does
        not create an application, SDK, reporting target or archive owner.
        """
        plan = self.settings.plan
        if ledger.run != self.settings.run:
            raise ValueError("Controller ledger belongs to another run")
        for slot in plan.slots():
            ledger.emit(
                "trigger_plan",
                pulse=slot.pulse,
                group=slot.group,
                offset=slot.offset,
                planned_at=slot.planned_at,
            )
        result = "budget_complete"
        try:
            self.acknowledge_ready()
            for slot in plan.slots():
                while True:
                    if stop.is_set() or ledger.abort.is_set():
                        ledger.emit("trigger_cancelled", pulse=slot.pulse)
                        return "cancelled"
                    self.heartbeat()
                    remaining = slot.planned_at - monotonic()
                    if remaining <= 0:
                        break
                    stop.wait(min(remaining, self.settings.lease / 3))
                if (
                    monotonic() >= plan.end
                    or monotonic() - slot.planned_at > plan.max_lateness
                ):
                    ledger.emit("trigger_late", pulse=slot.pulse)
                    return "late"
                if not self.pulse(slot):
                    return "failed"
        except Exception as exc:
            ledger.emit(
                "controller_failure", issue=f"{type(exc).__name__}: {exc}"[:1200]
            )
            self.failed = True
            result = "failed"
        finally:
            ledger.emit("trigger_stop_requested")
            try:
                self.stop()
            except OSError, EOFError:
                pass  # Supervisor lease/EOF recovery remains independently owned.
        return result


def start_supervisor(
    settings: SupervisorSettings, directory: Path, locks: Path
) -> tuple[BaseProcess, Controller]:
    settings.validate()
    parent, child = mp.get_context("spawn").Pipe()
    process = mp.get_context("spawn").Process(
        target=supervise,
        args=(settings, child, directory, locks),
        name="PhotoReleaseSupervisor",
    )
    process.start()
    child.close()
    return process, Controller(settings, parent)


def join_supervisor(process: BaseProcess, ledger: Ledger, timeout: float) -> bool:
    process.join(timeout)
    terminated = not process.is_alive()
    ledger.emit(
        "owner_stopped",
        owner="supervisor_process",
        terminated=terminated,
        cleanup_safe=terminated and process.exitcode == 0,
        exitcode=process.exitcode,
    )
    return terminated and process.exitcode == 0
