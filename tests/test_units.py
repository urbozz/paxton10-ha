"""Edge cases in parsing, discovery, the connection, and the source."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from custom_components.paxton10.api import (
    PaxtonAuthError,
    PaxtonBlockedRequest,
    PaxtonError,
    password_hash,
)
from custom_components.paxton10.connection import PaxtonConnection
from custom_components.paxton10.discovery import discover_site, read_doors, read_server
from custom_components.paxton10.models import (
    KIND_CONTROLLER,
    KIND_ENTRY_PANEL,
    Device,
    Door,
    name_hardware,
    offset_suffix,
    parse_devices,
    parse_event,
    parse_summary,
    parse_time,
)
from custom_components.paxton10.source import PollingSource

from .conftest import PASSWORD, USERNAME, FakeServer, make_entry


def conn(**kwargs: Any) -> PaxtonConnection:
    return PaxtonConnection(MagicMock(), "direct", "192.0.2.1", USERNAME, password_hash(PASSWORD), **kwargs)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-01-02T03:04:05.7697139+00:00", datetime(2026, 1, 2, 3, 4, 5, 769713, tzinfo=timezone.utc)),
        ("2026-01-02T03:04:05Z", datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)),
        ("2026-01-02T03:04:05", datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)),
        ("yesterday", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_time(value: Any, expected: datetime | None) -> None:
    assert parse_time(value) == expected


def test_offset_suffix() -> None:
    assert offset_suffix(60) == "+01:00"
    assert offset_suffix(0) == "+00:00"
    assert offset_suffix(-330) == "-05:30"


def test_parse_odd_shapes() -> None:
    assert parse_summary("nope") == {}
    assert parse_summary([{"Data": [{"Description": "Active users", "Value": "x"}]}]) == {}
    assert parse_devices({"not": "a list"}, KIND_CONTROLLER) == {}
    assert parse_devices([{"EntityId": "x"}, {"EntityId": 5}], KIND_CONTROLLER)[5].name == "Device 5"
    assert Device(1, KIND_CONTROLLER, "c", "m", None, None, None, None, None).online is None
    for bad in (None, "", True, 1.5):
        assert parse_event({"EventId": bad}, False) is None
    plain = parse_event({"EventId": 1, "EventTypeId": None, "ApplianceIds": None, "ApplianceData": None}, True)
    assert plain and plain.event_id == "1" and plain.event_type == "other" and plain.door_ids == ()
    assert plain.user_name is None
    # The door can come from either field, without duplicates.
    both = parse_event({"EventId": "a" * 24, "ApplianceIds": [5, "x"], "ApplianceData": {"ApplianceId": 5}}, False)
    assert both and both.door_ids == (5,)
    data = parse_event({"EventId": "b" * 24, "ApplianceIds": [], "ApplianceData": {"ApplianceId": "x"}}, False)
    assert data and data.door_ids == ()


@pytest.mark.parametrize(
    ("user", "name"),
    [
        ({"Name": "A B"}, "A B"),
        ([{"UserName": "C"}], "C"),
        ([], None),
        ("D E", "D E"),
        ("", None),
        ({"FirstName": "F"}, "F"),
        ({}, None),
        (42, None),
    ],
)
def test_user_name_shapes(user: Any, name: str | None) -> None:
    parsed = parse_event({"EventId": 1, "UserData": user}, True)
    assert parsed and parsed.user_name == name


async def test_discovery_edges(server: FakeServer) -> None:
    c = conn()
    # A door whose entity record has no parent is skipped; a non-dict group is ignored.
    server.groups[3001]["Appliances"].append({"EntityId": 1777, "Name": "Orphan", "ApplianceType": 1})
    server.groups[4]["Groups"].append({"GroupEntityId": 1400, "GroupName": "Weird", "HasChildren": True})
    server.status[
        "/api/v3/groups/1400/Children?includeEmptyGroups=true&page=0&pageSize=100&sortBy=null&sortDirection=null"
    ] = 200
    doors = await read_doors(c)
    assert set(doors) == {2001, 2002}
    # A controller with no door is named by its Paxton entity id, never its serial.
    server.controllers[0]["Connectors"] = []
    site = await discover_site(c)
    assert site.devices[4001].name == "Controller 4001"


async def test_server_without_site_id(server: FakeServer) -> None:
    c = conn()
    server.status["/api/v2/System/Parameters/All"] = 200  # empty body
    with pytest.raises(PaxtonError, match="SiteId"):
        await read_server(c)


async def test_connection_errors(server: FakeServer) -> None:
    c = conn()
    with pytest.raises(PaxtonBlockedRequest):
        await c.post("/api/v2/System/ActivateAppliances", [])
    # A network error drops the client; the next call signs in again.
    await c.get("/api/v1/System/Software/Version")
    before = server.tokens
    server.down.add("192.0.2.1")
    with pytest.raises(PaxtonError):
        await c.get("/api/v1/System/Software/Version")
    assert c.active_route is None
    server.down.clear()
    await c.get("/api/v1/System/Software/Version")
    assert server.tokens == before + 1
    # Two 401s in a row are real.
    server.expire_tokens = 5
    with pytest.raises(PaxtonAuthError):
        await c.get("/api/v1/System/Software/Version")
    server.expire_tokens = 0
    server.status["/missing"] = 500
    with pytest.raises(PaxtonError, match="HTTP 500"):
        await c.get("/missing")
    await c.close()


async def test_connection_auth_error_on_every_route(server: FakeServer) -> None:
    c = conn(fallback_target="abc123")
    server.password_hash = "changed"
    with pytest.raises(PaxtonAuthError):
        await c.connect()
    # Auth errors don't fall through to the other route.
    assert [t for t, _, p, _ in server.calls if p == "/token"] == ["192.0.2.1"]


async def test_connection_all_routes_down(server: FakeServer) -> None:
    c = conn(fallback_target="abc123")
    server.down.update({"192.0.2.1", "abc123"})
    with pytest.raises(PaxtonError, match="abc123 unreachable"):
        await c.connect()


async def test_connection_close_ignores_errors(server: FakeServer) -> None:
    c = conn()
    await c.connect()
    assert c._client
    c._client.close = AsyncMock(side_effect=RuntimeError)  # type: ignore[method-assign]
    await c.close()
    assert c.active_route is None


async def test_source_first_poll(server: FakeServer) -> None:
    c = conn()
    site = await discover_site(c)
    # A failed first poll doesn't stop the source; the loop retries.
    src = PollingSource(c, site, 30, 10, False)
    server.status["/api/v2/Events/?page=0&pageSize=50"] = 200  # body None: no Result list
    await src.async_start(AsyncMock())
    assert src.last_event_id is None
    await src.async_stop()
    # An auth failure on the first poll is raised to setup.
    server.status.clear()
    server.password_hash = "changed"
    server.expire_tokens = 1
    src = PollingSource(c, site, 30, 10, False)
    with pytest.raises(PaxtonAuthError):
        await src.async_start(AsyncMock())


async def test_setup_auth_failure_on_first_event_poll(hass: HomeAssistant, server: FakeServer) -> None:
    entry = make_entry()
    entry.add_to_hass(hass)
    with patch("custom_components.paxton10.source.PollingSource.poll_events", side_effect=PaxtonAuthError("x")):
        await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR


def test_name_hardware() -> None:
    doors = {7: Door(7, "Rear Door", 1, 1, 1, None)}

    def dev(entity_id: int, kind: str, name: str, serial: str | None, door_ids: tuple[int, ...] = ()) -> Device:
        return Device(entity_id, kind, name, "m", serial, None, None, 1, None, door_ids=door_ids)

    devices = {
        1: dev(1, KIND_CONTROLLER, "Paxton10 Door Controller", "111", (7,)),
        2: dev(2, KIND_CONTROLLER, "Paxton10 Door Controller", "222"),
        3: dev(3, KIND_ENTRY_PANEL, "Rear", "333", (7,)),
        4: dev(4, KIND_ENTRY_PANEL, "7507256", "7507256"),  # unnamed: Paxton reports the serial
        5: dev(5, KIND_ENTRY_PANEL, "8075329", None),
        6: dev(6, KIND_ENTRY_PANEL, "Reception", "666"),
    }
    name_hardware(devices, doors)
    assert {k: d.name for k, d in devices.items()} == {
        1: "Rear Door controller",
        2: "Controller 2",
        3: "Rear Door entry panel",
        4: "Entry panel 4",
        5: "Entry panel 5",
        6: "Reception",
    }


def test_unknown_battery_codes_are_unknown() -> None:
    from custom_components.paxton10.sensor import DEVICE_SENSORS

    sensors = {d.key: d for d in DEVICE_SENSORS}
    # GEN2 without a battery: Charge 0 and State 0.
    device = Device(1, KIND_CONTROLLER, "c", "m", None, None, None, 1, None, 0, 0, 0)
    assert sensors["battery"].value(device) == "not_connected"
    assert sensors["battery_state"].value(device) is None
    assert sensors["power_supply"].value(device) is None  # 0 is Unknown in the web app
    # A fitted battery can report Charge 0 with a State. The web app reads the State as a charge.
    device.battery_state = 2
    assert sensors["battery"].value(device) == "low"
    assert sensors["battery_state"].value(device) == "charging"
    device.battery_state = 1
    assert sensors["battery"].value(device) == "critical"
    device.battery_charge, device.battery_state = 3, 0
    assert sensors["battery_state"].value(device) is None
    device.battery_charge, device.psu_state = 9, None
    assert sensors["battery"].value(device) is None
    assert sensors["power_supply"].value(device) is None


@pytest.mark.parametrize(
    ("status", "online", "name"),
    [
        (1, True, "online"),
        (2, True, "online_on_battery"),  # mains failed: still connected, which matters most in a power cut
        (3, True, "updating"),
        (4, False, "offline"),
        (5, True, "refreshing"),
        (6, False, "reinstating"),
        (7, False, "rebooting"),
        (0, None, None),
        (99, None, None),
        (None, None, None),
    ],
)
def test_device_status(status: int | None, online: bool | None, name: str | None) -> None:
    from custom_components.paxton10.sensor import DEVICE_SENSORS

    device = Device(1, KIND_CONTROLLER, "c", "m", None, None, None, status, None)
    assert device.online is online
    assert {d.key: d for d in DEVICE_SENSORS}["status"].value(device) == name


@pytest.mark.parametrize("name", ["Keyfob", "HandsFreeCredential-3", "alex.smith@example.com", "Replacement fob"])
def test_credential_name_is_never_a_type(name: str) -> None:
    """The name is free text, even when it looks like a type, so it's passed on as entered and never typed."""
    row = {"EventId": "a" * 24, "CredentialData": {"CredentialId": 22, "Credential": f" {name} ", "CredentialValue": "x"}}
    parsed = parse_event(row, True, True)
    assert parsed is not None and parsed.credential_name == name
    assert not hasattr(parsed, "credential")
    assert parse_event(row, True, False).credential_name is None  # type: ignore[union-attr]


def test_credential_sources() -> None:
    # Older rows put it in UserData; a blank or missing block gives None.
    assert parse_event({"EventId": "b", "UserData": {"Credential": "PIN"}}, False, True).credential_name == "PIN"  # type: ignore[union-attr]
    assert parse_event({"EventId": "c", "CredentialData": {"Credential": " "}}, False, True).credential_name is None  # type: ignore[union-attr]
    assert parse_event({"EventId": "d", "CredentialData": None}, False, True).credential_name is None  # type: ignore[union-attr]
