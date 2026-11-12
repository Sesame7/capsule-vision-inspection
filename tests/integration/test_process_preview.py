"""Read-only FINS process preview: real loopback wire, isolated definitions."""

import json
from concurrent.futures import CancelledError
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Event

import pytest
from tests.support.application import WAIT
from tests.support.fins_tcp import TcpPeer
from tests.support.startup import (
    TEST_PASSWORD,
    authenticated_startup,
    close,
    provision_account,
    saved_data,
)

from capsule_vision.application.identity_models import (
    AuthorizationError,
    Role,
    StaleSessionError,
)
from capsule_vision.application.process_preview import read_process_preview, signed_dint
from capsule_vision.communication.endpoints import FinsTcpEndpoint
from capsule_vision.communication.fins import FinsPlcError
from capsule_vision.communication.operations import (
    CommunicationRequest,
    CommunicationResponse,
    RequestKind,
)
from capsule_vision.configuration import ConfigurationError
from capsule_vision.configuration.communication_preferences import (
    CommunicationPreferences,
    StatusPreferences,
    load_communication_preferences,
    save_communication_preferences,
)
from capsule_vision.configuration.process_parameters import (
    BUILTIN_PROCESS_CATALOG,
    ProcessCatalog,
    load_process_catalog,
)


def saved_target(root: Path, port: int = 9600) -> Path:
    path = root / "communication.json"
    save_communication_preferences(
        path,
        CommunicationPreferences(
            status=StatusPreferences(endpoint=FinsTcpEndpoint(port=port)),
        ),
    )
    return path


def extended_catalog(root: Path) -> tuple[Path, ProcessCatalog]:
    path = root / "process_parameters.json"
    path.write_text(
        json.dumps(
            {
                "parameters": [
                    {
                        "id": "extra_phase",
                        "group": "photo",
                        "labels": {"en": "Extra phase"},
                        "address": 1200,
                        "minimum": -100,
                        "maximum": 100,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return path, load_process_catalog(path)


@pytest.mark.parametrize(
    "value", (0, 1, 65535, 65536, 0x12345678, -1, -65536, -(2**31), 2**31 - 1)
)
def test_signed_low_word_first_dint(value: int) -> None:
    raw = value & 0xFFFFFFFF
    assert signed_dint(raw & 65535, raw >> 16) == value


def test_explicit_mapping_grouped_wire_reads_and_no_writes(tmp_path: Path) -> None:
    catalog_path, catalog = extended_catalog(tmp_path)
    with TcpPeer() as peer:
        path = saved_target(tmp_path, peer.port)
        expected: dict[str, int | None] = {}
        for index, parameter in enumerate(catalog.parameters):
            address = parameter.address
            value = (index + 1) * (-65537 if index % 2 else 65537)
            expected[parameter.id] = value
            raw = value & 0xFFFFFFFF
            peer.set_words(address, (raw & 65535, raw >> 16), area=0x82)
        before = path.read_bytes(), catalog_path.read_bytes()
        result = read_process_preview(
            path, catalog, catalog_path, Event(), identity=None, revision=None
        )
        assert dict(result.values) == expected
        assert result.endpoint.port == peer.port
        assert (path.read_bytes(), catalog_path.read_bytes()) == before
        packets = peer.packets
        assert len(packets) == 2
        assert all(p[10:13] == b"\x01\x01\x82" for p in packets)
        assert int.from_bytes(packets[0][13:15]) == 102
        assert int.from_bytes(packets[0][16:18]) == 52
        assert not load_communication_preferences(path).status.enabled


def test_failed_group_does_not_publish_partial_values_or_retry(tmp_path: Path) -> None:
    catalog_path, catalog = extended_catalog(tmp_path)
    with TcpPeer() as peer:
        path = saved_target(tmp_path, peer.port)
        peer.fail_next("read", "plc", start=1200)
        with pytest.raises(FinsPlcError):
            read_process_preview(
                path, catalog, catalog_path, Event(), identity=None, revision=None
            )
        assert len(peer.packets) == 2
        assert all(p[10:12] == b"\x01\x01" for p in peer.packets)


@pytest.mark.parametrize(
    "raw",
    (
        {"process_station": 0},
        {"process_station": 1},
        {"process_station": 2},
        {"process_station": 3},
        {"process_station": True},
        {"process_station": "1"},
        {"process_station": None},
        {"status": {"protocol": "fins_udp", "fault_address": 47}},
        {"status": {"host": "0.0.0.0", "fault_address": 47}},
    ),
)
def test_invalid_target_rejects_before_transport(tmp_path: Path, raw: object) -> None:
    path = tmp_path / "communication.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    calls: list[CommunicationRequest] = []

    def forbidden(request: CommunicationRequest) -> CommunicationResponse:
        calls.append(request)
        raise AssertionError("Invalid target reached transport")

    with pytest.raises(ConfigurationError):
        read_process_preview(
            path,
            BUILTIN_PROCESS_CATALOG,
            None,
            Event(),
            identity=None,
            revision=None,
            execute=forbidden,
        )
    assert not calls


def test_missing_settings_changed_catalog_and_invalid_dm_do_not_read(
    tmp_path: Path,
) -> None:
    path = tmp_path / "communication.json"
    with pytest.raises(ConfigurationError):
        read_process_preview(
            path, BUILTIN_PROCESS_CATALOG, None, Event(), identity=None, revision=None
        )
    saved_target(tmp_path)
    catalog_path, catalog = extended_catalog(tmp_path)
    with pytest.raises(ConfigurationError, match="catalog changed"):
        read_process_preview(
            path,
            BUILTIN_PROCESS_CATALOG,
            catalog_path,
            Event(),
            identity=None,
            revision=None,
        )
    catalog_path.write_text(catalog_path.read_text().replace("1200", "65534"))
    catalog = load_process_catalog(catalog_path)
    with pytest.raises(ValueError, match="start"):
        read_process_preview(
            path, catalog, catalog_path, Event(), identity=None, revision=None
        )


@pytest.mark.parametrize("role", list(Role))
def test_real_role_admission_even_for_injected_executor(
    tmp_path: Path, role: Role
) -> None:
    startup = authenticated_startup(saved_data(tmp_path))
    identity = startup.application.identity
    assert identity is not None
    calls: list[CommunicationRequest] = []

    def execute(request: CommunicationRequest) -> CommunicationResponse:
        calls.append(request)
        return CommunicationResponse(
            request, (0,) * request.count, 0, datetime.now(UTC)
        )

    try:
        if role is Role.GUEST:
            identity.logout()
        elif role is not Role.ADMINISTRATOR:
            provision_account(identity, role.name, role, TEST_PASSWORD)
            identity.login(role.name, TEST_PASSWORD).result(WAIT)
        path = saved_target(startup.data.root)
        if role < Role.ENGINEER:
            with pytest.raises(AuthorizationError):
                read_process_preview(
                    path,
                    BUILTIN_PROCESS_CATALOG,
                    None,
                    Event(),
                    identity=identity,
                    revision=identity.snapshot().revision,
                    execute=execute,
                )
            assert not calls
        else:
            read_process_preview(
                path,
                BUILTIN_PROCESS_CATALOG,
                None,
                Event(),
                identity=identity,
                revision=identity.snapshot().revision,
                execute=execute,
            )
            assert len(calls) == 1 and calls[0].kind is RequestKind.READ
    finally:
        close(startup)


@pytest.mark.parametrize("cancel", (False, True))
def test_cancellation_or_new_identity_stops_subsequent_groups(
    tmp_path: Path, cancel: bool
) -> None:
    startup = authenticated_startup(saved_data(tmp_path))
    identity = startup.application.identity
    assert identity is not None
    stopped = Event()
    calls: list[CommunicationRequest] = []

    def execute(request: CommunicationRequest) -> CommunicationResponse:
        calls.append(request)
        if cancel:
            stopped.set()
        else:
            identity.logout()
        return CommunicationResponse(
            request, (0,) * request.count, 0, datetime.now(UTC)
        )

    try:
        path = saved_target(startup.data.root)
        catalog_path, catalog = extended_catalog(startup.data.root)
        with pytest.raises(CancelledError if cancel else StaleSessionError):
            read_process_preview(
                path,
                catalog,
                catalog_path,
                stopped,
                identity=identity,
                revision=identity.snapshot().revision,
                execute=execute,
            )
        assert len(calls) == 1
    finally:
        close(startup)


def test_endpoint_round_trip_and_unrelated_preference_change(tmp_path: Path) -> None:
    path = saved_target(tmp_path, port=9876)
    saved = load_communication_preferences(path)
    assert saved.status.endpoint.port == 9876
    changed = replace(saved, web=replace(saved.web, port=8766))
    save_communication_preferences(path, changed)
    assert load_communication_preferences(path) == changed
    assert "process_station" not in json.loads(path.read_text())
