"""Existing reporting owner over real localhost Modbus TCP, no physical PLC."""

import struct
from dataclasses import replace
from threading import Event, get_ident
from threading import enumerate as threads
from typing import Literal

import pytest
from tests.support.business import mixed_result
from tests.support.modbus import ModbusPeer, frame
from tests.support.reporting import WAIT

from capsule_vision.communication import (
    Endpoint,
    FinsError,
    FinsUdpEndpoint,
    FirstStationSubmissionError,
    ModbusError,
    ModbusExceptionResponse,
    ModbusSettings,
    ModbusTcpClient,
)
from capsule_vision.communication.modbus_tcp import (
    ModbusProtocolError,
    ModbusTimeoutError,
    ModbusTransportError,
)
from capsule_vision.communication.reporting import (
    FirstStationReport,
    FirstStationReportingWorker,
    ReportingSelection,
    ReportingSettings,
    ReportingStateError,
    ReportOutcome,
    ReportStage,
)


def report(cycle: int, count: int = 36) -> FirstStationReport:
    item = FirstStationReport.from_result(7, mixed_result(cycle))
    return replace(item, statuses=item.statuses[:count])


def stop(worker: FirstStationReportingWorker) -> None:
    worker.request_stop(drain=True)
    assert worker.join(WAIT) and worker.cleanup_safe
    assert not any(t.name == worker.thread_name for t in threads())


@pytest.mark.parametrize("base", [0, 65532])
@pytest.mark.parametrize("count", [30, 36])
def test_exact_modbus_reporting_bytes_addresses_and_rollover(
    base: int, count: int
) -> None:
    with ModbusPeer() as peer:
        peer.words.update({base: 65534, base + 3: 0xABCD})
        worker = FirstStationReportingWorker(
            7,
            ReportingSettings(
                base_address=base, endpoint=ModbusSettings(port=peer.port, unit_id=17)
            ),
        )
        worker.start()
        try:
            words = (0xEFBE, 0x3EFB) if count == 30 else (0xEFBE, 0xBEFB, 15)
            for cycle, sequence in ((1, 65535), (2, 0), (3, 1)):
                result = worker.submit(report(cycle, count)).result(WAIT)
                assert result.outcome is ReportOutcome.ACKNOWLEDGED
                assert result.sequence == sequence and result.reject_words == words
                assert tuple(peer.words.get(base + i, 0) for i in range(4)) == (
                    sequence,
                    *words,
                    *((0xABCD,) if count == 30 else ()),
                )
            expected = [frame(struct.pack("!BHH", 3, base, 1), transaction=1, unit=17)]
            for cycle, sequence in ((1, 65535), (2, 0), (3, 1)):
                bitmap = struct.pack(
                    "!BHHB", 16, base + 1, len(words), len(words) * 2
                ) + struct.pack(f"!{len(words)}H", *words)
                expected.extend(
                    (
                        frame(bitmap, transaction=cycle * 2, unit=17),
                        frame(
                            struct.pack("!BHH", 6, base, sequence),
                            transaction=cycle * 2 + 1,
                            unit=17,
                        ),
                    )
                )
            assert peer.requests == expected
            assert peer.accepted == 1 and worker.snapshot().next_sequence == 2
        finally:
            stop(worker)
    assert peer.disconnected == 1


@pytest.mark.parametrize("base", [-1, 65533, 65536, True])
def test_invalid_modbus_reporting_window_fails_before_owner(base: int) -> None:
    with pytest.raises(ValueError):
        ReportingSettings(base_address=base, endpoint=ModbusSettings())


def test_missing_modbus_base_never_becomes_fins_default() -> None:
    missing = ReportingSettings(endpoint=ModbusSettings())
    assert isinstance(missing.endpoint, ModbusSettings)
    assert (
        missing.base_address is None
        and missing.endpoint.port == 502
        and missing.endpoint.unit_id == 255
    )
    assert ReportingSettings(endpoint=FinsUdpEndpoint()).base_address == 49
    ReportingSelection(False, missing)
    with pytest.raises(ValueError, match="explicit base_address"):
        ReportingSelection(True, missing)
    with pytest.raises(ValueError, match="explicit base_address"):
        FirstStationReportingWorker(7, missing)


@pytest.mark.parametrize(
    "stage,fault",
    [
        (ReportStage.INITIALIZATION, "exception"),
        (ReportStage.INITIALIZATION, "disconnect"),
        (ReportStage.INITIALIZATION, "timeout_before"),
        (ReportStage.BITMAP, "exception"),
        (ReportStage.BITMAP, "disconnect"),
        (ReportStage.BITMAP, "timeout_before"),
        (ReportStage.BITMAP, "timeout_after"),
        (ReportStage.BITMAP, "malformed_after"),
        (ReportStage.SEQUENCE, "exception"),
        (ReportStage.SEQUENCE, "disconnect"),
        (ReportStage.SEQUENCE, "timeout_before"),
        (ReportStage.SEQUENCE, "timeout_after"),
        (ReportStage.SEQUENCE, "malformed_after"),
    ],
)
def test_modbus_failures_preserve_stage_cause_sequence_gap_and_no_replay(
    stage: ReportStage,
    fault: Literal[
        "exception", "disconnect", "timeout_before", "timeout_after", "malformed_after"
    ],
) -> None:
    with ModbusPeer() as peer:
        base = 120
        peer.words[base] = 200
        fired = False
        function = {
            ReportStage.INITIALIZATION: 3,
            ReportStage.BITMAP: 16,
            ReportStage.SEQUENCE: 6,
        }[stage]

        def respond(request: bytes) -> tuple[bytes, ...] | None:
            nonlocal fired
            if request[7] != function or fired:
                return tuple(peer.normal_response(request))
            fired = True
            transaction = int.from_bytes(request[:2], "big")
            if fault == "exception":
                return (frame(bytes((function | 0x80, 4)), transaction=transaction),)
            if fault == "disconnect":
                return ()
            if fault == "timeout_before":
                return None
            response = peer.normal_response(request)[
                0
            ]  # Applied, but not acknowledged.
            if fault == "timeout_after":
                return None
            return (response[:-1] + bytes((response[-1] ^ 1,)),)

        peer.respond = respond
        worker = FirstStationReportingWorker(
            7,
            ReportingSettings(
                base_address=base, endpoint=ModbusSettings(port=peer.port, timeout=0.1)
            ),
        )
        worker.start()
        try:
            first = worker.submit(report(1)).result(WAIT)
            initialized = stage is not ReportStage.INITIALIZATION
            assert first.outcome is ReportOutcome.FAILED and first.stage is stage
            assert first.sequence == (201 if initialized else None)
            assert first.error is not None and not isinstance(first.error, FinsError)
            cause = (
                first.error.__cause__
                if isinstance(first.error, FirstStationSubmissionError)
                else first.error
            )
            expected = (
                ModbusExceptionResponse
                if fault == "exception"
                else ModbusTransportError
                if fault == "disconnect"
                else ModbusProtocolError
                if fault == "malformed_after"
                else ModbusTimeoutError
            )
            assert isinstance(cause, expected)
            assert first.issue is not None
            assert first.issue.end_code is None
            assert (
                first.issue.cause_type if initialized else first.issue.exception_type
            ) == expected.__name__
            if initialized and fault != "exception":
                assert (
                    first.issue.cause_message is not None
                    and "uncertain" in first.issue.cause_message
                )
            captured = len(peer.requests)
            assert (
                captured
                == {
                    ReportStage.INITIALIZATION: 1,
                    ReportStage.BITMAP: 2,
                    ReportStage.SEQUENCE: 3,
                }[stage]
            )
            applied_sequence = (
                201
                if stage is ReportStage.SEQUENCE
                and fault in ("timeout_after", "malformed_after")
                else 200
            )
            assert peer.words[base] == applied_sequence
            with pytest.raises(ReportingStateError):
                worker.submit(report(1))
            assert len(peer.requests) == captured
            peer.release.set()
            second = worker.submit(report(2)).result(WAIT)
            assert second.outcome is ReportOutcome.ACKNOWLEDGED
            assert second.sequence == (202 if initialized else 201)
            assert peer.accepted == 2  # Failed Modbus connections are never reused.
            assert [r[7] for r in peer.requests[captured:]] == (
                [16, 6] if initialized else [3, 16, 6]
            )
            assert (
                worker.snapshot().failed_count
                == worker.snapshot().acknowledged_count
                == 1
            )
        finally:
            peer.release.set()
            stop(worker)


def test_modbus_attribution_fence_prevents_sequence_without_losing_attempt() -> None:
    with ModbusPeer() as peer:
        allowed = Event()
        allowed.set()
        peer.words[100] = 2
        first_bitmap = True

        def respond(request: bytes) -> tuple[bytes, ...]:
            nonlocal first_bitmap
            result = tuple(peer.normal_response(request))
            if request[7] == 16 and first_bitmap:
                allowed.clear()
                first_bitmap = False
            return result

        peer.respond = respond
        worker = FirstStationReportingWorker(
            7,
            ReportingSettings(
                base_address=100, endpoint=ModbusSettings(port=peer.port)
            ),
            attributable=lambda _: allowed.is_set(),
        )
        worker.start()
        try:
            result = worker.submit(report(1)).result(WAIT)
            assert (
                result.stage is ReportStage.SEQUENCE
                and result.outcome is ReportOutcome.FAILED
            )
            assert result.sequence == 3 and isinstance(
                result.error, FirstStationSubmissionError
            )
            assert isinstance(result.error.__cause__, ModbusError)
            assert [r[7] for r in peer.requests] == [3, 16] and peer.words[100] == 2
            allowed.set()
            assert worker.submit(report(2)).result(WAIT).sequence == 4
            assert peer.accepted == 1 and [r[7] for r in peer.requests] == [
                3,
                16,
                16,
                6,
            ]
        finally:
            stop(worker)


def test_modbus_connect_failure_is_observed_and_new_cycle_can_recover() -> None:
    with ModbusPeer() as peer:
        calls: list[int] = []

        def factory(settings: Endpoint) -> ModbusTcpClient:
            assert isinstance(settings, ModbusSettings)
            calls.append(get_ident())
            if len(calls) == 1:
                raise ModbusTransportError("Controlled connect failure")
            return ModbusTcpClient(settings.host, port=settings.port)

        worker = FirstStationReportingWorker(
            7,
            ReportingSettings(
                base_address=100, endpoint=ModbusSettings(port=peer.port)
            ),
            client_factory=factory,
        )
        worker.start()
        try:
            failed = worker.submit(report(1)).result(WAIT)
            assert failed.stage is ReportStage.INITIALIZATION and isinstance(
                failed.error, ModbusTransportError
            )
            assert len(calls) == 1 and not peer.requests
            assert (
                worker.submit(report(2)).result(WAIT).outcome
                is ReportOutcome.ACKNOWLEDGED
            )
            assert len(set(calls)) == 1 and calls[0] != get_ident()
        finally:
            stop(worker)
