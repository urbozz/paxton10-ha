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
    Device,
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
    assert parse_event({"EventId": "x"}, False) is None
    plain = parse_event({"EventId": 1, "EventTypeId": None, "ApplianceIds": None}, True)
    assert plain and plain.event_type == "other" and plain.door_ids == () and plain.user_name is None


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
