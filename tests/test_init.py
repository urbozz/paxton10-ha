"""Setup, entities, door release, events, failure handling, and devices."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from custom_components.paxton10.api import PaxtonAuthError, PaxtonError, password_hash
from custom_components.paxton10.const import (
    CONF_PASSWORD_HASH,
    DOMAIN,
    EVENT_PAXTON10,
    OPT_ALLOW_DOOR_CONTROL,
    OPT_FALLBACK,
    OPT_FALLBACK_TARGET,
    OPT_INCLUDE_USER_NAMES,
)
from custom_components.paxton10.coordinator import Paxton10Coordinator
from custom_components.paxton10.diagnostics import async_get_config_entry_diagnostics
from custom_components.paxton10.source import PollingSource, SourceUpdate

from .conftest import SITE_ID, FakeServer, controller, entity_id, event, make_entry

RELEASE = "/api/v2/System/ActivateAppliances"
EVENTS = "/api/v2/Events/?page=0&pageSize=50"


async def setup(hass: HomeAssistant, options: dict[str, Any] | None = None, **data: Any) -> MockConfigEntry:
    entry = make_entry(options, **data)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def coordinator(entry: MockConfigEntry) -> Paxton10Coordinator:
    return entry.runtime_data  # type: ignore[no-any-return]


def source(entry: MockConfigEntry) -> PollingSource:
    src = coordinator(entry).source
    assert isinstance(src, PollingSource)
    return src


def state(hass: HomeAssistant, platform: str, object_id: str | int, key: str) -> str:
    eid = entity_id(hass, platform, object_id, key)
    assert eid, f"no {platform} {object_id} {key}"
    st = hass.states.get(eid)
    assert st
    return st.state


def capture(hass: HomeAssistant) -> list[Event]:
    # A @callback listener runs in the event loop, in order. A plain function would run in a thread.
    return async_capture_events(hass, EVENT_PAXTON10)


async def test_setup_and_unload(hass: HomeAssistant, server: FakeServer) -> None:
    entry = await setup(hass)
    assert entry.state is ConfigEntryState.LOADED

    assert state(hass, "sensor", "server", "software_version") == "4.11.9753.20528"
    assert state(hass, "sensor", "server", "active_users") == "28"
    assert state(hass, "sensor", "server", "offline_devices") == "0"
    assert state(hass, "binary_sensor", 4001, "connectivity") == STATE_ON
    assert state(hass, "binary_sensor", 4002, "connectivity") == STATE_ON
    assert state(hass, "sensor", 4001, "firmware") == "3.00.17013.672"
    assert state(hass, "sensor", 4001, "battery_charge") == "3"
    assert state(hass, "sensor", 4001, "psu_state") == "2"
    assert state(hass, "event", 2001, "door_event") == STATE_UNKNOWN
    # Entry panels have no battery or PSU, and noisy diagnostics start disabled.
    assert entity_id(hass, "sensor", 4002, "battery_charge") is None
    registry = er.async_get(hass)
    ip = registry.async_get(entity_id(hass, "sensor", 4001, "ip_address") or "")
    assert ip and ip.disabled_by is er.RegistryEntryDisabler.INTEGRATION

    # Doors are de-duplicated, the contact input isn't a door, and areas come from the parent group.
    devices = dr.async_get(hass)
    door = devices.async_get_device_by_identifier((DOMAIN, f"{SITE_ID}_2001"), entry.entry_id)
    assert door and door.suggested_area == "Ground Floor"
    assert devices.async_get_device_by_identifier((DOMAIN, f"{SITE_ID}_1500"), entry.entry_id) is None
    hub = devices.async_get_device_by_identifier((DOMAIN, SITE_ID), entry.entry_id)
    assert hub and door.via_device_id == hub.id
    ctrl = devices.async_get_device_by_identifier((DOMAIN, f"{SITE_ID}_4001"), entry.entry_id)
    assert ctrl and ctrl.name == "Main Entrance Door controller"

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_no_buttons_and_no_writes_by_default(hass: HomeAssistant, server: FakeServer) -> None:
    await setup(hass)
    assert entity_id(hass, "button", 2001, "open") is None
    assert hass.states.async_entity_ids("button") == []
    # Only reads and the two read-only POSTs were sent.
    assert {c[2] for c in server.calls if c[1] == "POST"} <= {"/token", EVENTS}

    # Even if something calls the client directly, it refuses the write.
    with pytest.raises(Exception, match="not on the allowlist"):
        await source_conn(hass).post(RELEASE, [])
    assert server.sent("POST", RELEASE) == []


def source_conn(hass: HomeAssistant) -> Any:
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    return coordinator(entry).conn  # type: ignore[arg-type]


async def test_open_door(hass: HomeAssistant, server: FakeServer, caplog: pytest.LogCaptureFixture) -> None:
    await setup(hass, {OPT_ALLOW_DOOR_CONTROL: True})
    caplog.set_level("INFO")
    for door, payload in (
        (
            2001,
            {
                "State": None,
                "Id": 2001,
                "ApplianceTypeId": 1,
                "EntityTypeId": 26,
                "IsGroup": False,
                "ParentId": 3001,
                "ActorId": 1002,
            },
        ),
        (
            2002,
            {
                "State": None,
                "Id": 2002,
                "ApplianceTypeId": 2,
                "EntityTypeId": 26,
                "IsGroup": False,
                "ParentId": 3002,
                "ActorId": 1002,
            },
        ),
    ):
        await hass.services.async_call(
            "button", "press", {"entity_id": entity_id(hass, "button", door, "open")}, blocking=True
        )
        assert server.sent("POST", RELEASE)[-1] == [payload]
    assert "Opening Vehicle Gate (entity 2002) from Home Assistant" in caplog.text
    assert "tok" not in caplog.text


@pytest.mark.parametrize(("status", "key"), [(403, "door_forbidden"), (405, "door_forbidden"), (500, "door_failed")])
async def test_open_door_errors(hass: HomeAssistant, server: FakeServer, status: int, key: str) -> None:
    await setup(hass, {OPT_ALLOW_DOOR_CONTROL: True})
    server.status[RELEASE] = status
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            "button", "press", {"entity_id": entity_id(hass, "button", 2001, "open")}, blocking=True
        )
    assert err.value.translation_key == key


async def test_open_door_blocked_or_gone(hass: HomeAssistant, server: FakeServer) -> None:
    entry = await setup(hass, {OPT_ALLOW_DOOR_CONTROL: True})
    button = entity_id(hass, "button", 2001, "open")
    coord = coordinator(entry)
    coord.conn.allow_writes = False
    assert coord.conn._client
    coord.conn._client.allow_writes = False
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call("button", "press", {"entity_id": button}, blocking=True)
    assert err.value.translation_key == "door_control_off"

    entity = hass.data["entity_components"]["button"].get_entity(button)
    coord.data.doors.pop(2001)
    with pytest.raises(HomeAssistantError) as err:
        await entity.async_press()
    assert err.value.translation_key == "door_gone"


async def test_events(hass: HomeAssistant, server: FakeServer) -> None:
    fired = capture(hass)
    entry = await setup(hass)
    # Events from before startup are never replayed.
    assert fired == []
    assert source(entry).last_event_id == 100

    server.events += [event(101, 16), event(102, 11, door=2002), event(103, 99), event(104, 7, door=4242)]
    await source(entry).poll_events()
    await hass.async_block_till_done()
    assert [(e.data["event_id"], e.data["event_type"], e.data["door_entity_id"]) for e in fired] == [
        (101, "forced", 2001),
        (102, "closed", 2002),
        (103, "other", 2001),
        (104, "opened_by_software", None),  # unknown door
    ]
    assert fired[0].data["door_name"] == "Main Entrance Door"
    assert fired[0].data["time"] == "2026-10-07T14:00:00.123000+01:00"
    assert "user_name" not in fired[0].data
    eid = entity_id(hass, "event", 2001, "door_event")
    st = hass.states.get(eid or "")
    assert st and st.attributes["event_type"] == "other" and st.attributes["event_id"] == 103
    assert state(hass, "event", 2002, "door_event") != STATE_UNKNOWN

    # The next poll sees the same page and fires nothing new.
    fired.clear()
    await source(entry).poll_events()
    assert fired == []
    # The filter carries the site's UTC offset.
    body = server.sent("POST", EVENTS)[-1]
    assert body["StartTimeWithOffset"] == "2000-01-01T00:00:00.000+01:00"
    assert body["CustomDataIds"] == []


async def test_event_user_names_only_when_allowed(hass: HomeAssistant, server: FakeServer) -> None:
    fired = capture(hass)
    entry = await setup(hass, {OPT_INCLUDE_USER_NAMES: True})
    server.events.append(event(101, 5, user={"FirstName": "Test", "Surname": "Resident"}))
    await source(entry).poll_events()
    await hass.async_block_till_done()
    assert fired[0].data["user_name"] == "Test Resident"
    st = hass.states.get(entity_id(hass, "event", 2001, "door_event") or "")
    assert st and st.attributes["user_name"] == "Test Resident"


async def test_empty_event_log(hass: HomeAssistant, server: FakeServer) -> None:
    server.events = []
    entry = await setup(hass)
    assert source(entry).last_event_id == 0
    fired = capture(hass)
    server.events.append(event(1))
    await source(entry).poll_events()
    await hass.async_block_till_done()
    assert [e.data["event_id"] for e in fired] == [1]


async def test_device_poll_and_unavailable(
    hass: HomeAssistant, server: FakeServer, caplog: pytest.LogCaptureFixture
) -> None:
    entry = await setup(hass)
    server.controllers = [controller(4001, 2001, status=2)]
    await source(entry).poll_devices()
    await hass.async_block_till_done()
    assert state(hass, "binary_sensor", 4001, "connectivity") == STATE_OFF

    # A failed poll marks everything unavailable and logs once.
    server.down.add("192.0.2.1")
    assert source(entry)._callback
    await source(entry)._callback(SourceUpdate("devices", error=PaxtonError("down")))
    await hass.async_block_till_done()
    assert state(hass, "binary_sensor", 4001, "connectivity") == STATE_UNAVAILABLE
    assert state(hass, "sensor", "server", "active_users") == STATE_UNAVAILABLE

    server.down.clear()
    server.controllers = [controller(4001, 2001)]
    await source(entry).poll_devices()
    await hass.async_block_till_done()
    assert state(hass, "binary_sensor", 4001, "connectivity") == STATE_ON


async def test_poll_loop_backoff_and_recovery(
    hass: HomeAssistant, server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from custom_components.paxton10 import source as source_mod

    entry = await setup(hass)
    src = source(entry)
    await src.async_stop()

    delays: list[float] = []
    calls = {"n": 0}

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)
        if len(delays) > 5:
            raise asyncio.CancelledError

    async def flaky() -> None:
        calls["n"] += 1
        if calls["n"] in (1, 2):
            raise source_mod.PaxtonError("down")
        if calls["n"] == 3:
            raise ValueError("bug")

    received: list[Any] = []

    async def cb(update: Any) -> None:
        received.append(update)

    src._callback = cb
    monkeypatch.setattr(source_mod, "_sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        await src._run(flaky, 10, "devices")
    assert delays == [10, 20, 40, 80, 10, 10]
    assert [type(u.error).__name__ for u in received] == ["PaxtonError", "PaxtonError", "PaxtonError"]


async def test_poll_loop_stops_on_auth_failure(
    hass: HomeAssistant, server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    from custom_components.paxton10 import source as source_mod

    entry = await setup(hass)
    src = source(entry)
    await src.async_stop()
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    async def rejected() -> None:
        raise PaxtonAuthError("rejected")

    received: list[Any] = []

    async def cb(update: Any) -> None:
        received.append(update)

    src._callback = cb
    monkeypatch.setattr(source_mod, "_sleep", fake_sleep)
    await src._run(rejected, 10, "devices")  # returns instead of retrying
    assert src.auth_failed and len(received) == 1 and sleeps == [10]
    # The other loop stops too, without polling again.
    await src._run(rejected, 10, "devices")
    assert len(received) == 1


async def test_token_expiry_signs_in_again(hass: HomeAssistant, server: FakeServer) -> None:
    entry = await setup(hass)
    before = server.tokens
    server.expire_tokens = 1
    await source(entry).poll_devices()
    assert server.tokens == before + 1
    assert coordinator(entry).last_update_success


async def test_setup_auth_failure_starts_reauth(hass: HomeAssistant, server: FakeServer) -> None:
    entry = await setup(hass, **{CONF_PASSWORD_HASH: password_hash("wrong")})
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert [f["context"]["source"] for f in flows] == ["reauth"]


async def test_auth_failure_later_starts_reauth(hass: HomeAssistant, server: FakeServer) -> None:
    entry = await setup(hass)
    server.password_hash = "changed"
    server.expire_tokens = 2
    # The poll gets a 401, signs in again, and the server rejects the stored hash.
    with pytest.raises(PaxtonAuthError):
        await source(entry).poll_devices()
    assert source(entry)._callback
    await source(entry)._callback(SourceUpdate("devices", error=PaxtonAuthError("rejected")))
    await hass.async_block_till_done()
    assert [f["context"]["source"] for f in hass.config_entries.flow.async_progress()] == ["reauth"]


async def test_setup_not_ready(hass: HomeAssistant, server: FakeServer) -> None:
    server.down.add("192.0.2.1")
    entry = await setup(hass)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_fallback_route_and_issue(hass: HomeAssistant, server: FakeServer) -> None:
    server.down.add("192.0.2.1")
    entry = await setup(hass, {OPT_FALLBACK: True, OPT_FALLBACK_TARGET: "abc123"})
    assert entry.state is ConfigEntryState.LOADED
    coord = coordinator(entry)
    assert coord.conn.active_route == "remote"
    issue_id = f"using_fallback_route_{entry.entry_id}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id)

    # Rediscovery tries the configured route again and clears the issue.
    server.down.clear()
    await coord._async_rediscover()
    await hass.async_block_till_done()
    assert coord.conn.active_route == "direct"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_rediscovery_adds_and_removes_devices(hass: HomeAssistant, server: FakeServer) -> None:
    entry = await setup(hass, {OPT_ALLOW_DOOR_CONTROL: True})
    devices = dr.async_get(hass)
    assert devices.async_get_device_by_identifier((DOMAIN, f"{SITE_ID}_2002"), entry.entry_id)

    # The gate goes, a new door arrives.
    server.groups[3002]["Appliances"] = []
    server.groups[3001]["Appliances"].append({"EntityId": 1200, "Name": "New Door", "ApplianceType": 1})
    await coordinator(entry)._async_rediscover()
    await hass.async_block_till_done()

    assert devices.async_get_device_by_identifier((DOMAIN, f"{SITE_ID}_2002"), entry.entry_id) is None
    assert entity_id(hass, "button", 1200, "open")
    assert state(hass, "event", 1200, "door_event") == STATE_UNKNOWN


async def test_rediscovery_failure_keeps_layout(hass: HomeAssistant, server: FakeServer) -> None:
    entry = await setup(hass)
    server.down.add("192.0.2.1")
    await coordinator(entry)._async_rediscover()
    assert 2001 in coordinator(entry).data.doors


async def test_remove_device(hass: HomeAssistant, server: FakeServer, hass_ws_client: Any) -> None:
    assert await async_setup_component(hass, "config", {})
    entry = await setup(hass)
    devices = dr.async_get(hass)
    live = devices.async_get_device_by_identifier((DOMAIN, f"{SITE_ID}_2001"), entry.entry_id)
    gone = devices.async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, f"{SITE_ID}_9999")})
    client = await hass_ws_client(hass)
    for device, allowed in ((live, False), (gone, True)):
        assert device
        await client.send_json_auto_id(
            {
                "type": "config/device_registry/remove_config_entry",
                "config_entry_id": entry.entry_id,
                "device_id": device.id,
            }
        )
        response = await client.receive_json()
        assert response["success"] is allowed


async def test_forbidden_reads(hass: HomeAssistant, server: FakeServer) -> None:
    server.status["/api/v1/Devices/1/false?page=0&pageSize=100"] = 403
    server.status["/api/v1/System/Summary"] = 403
    server.status["/api/v1/System/ServerName"] = 405
    entry = await setup(hass)
    assert entry.state is ConfigEntryState.LOADED
    site = coordinator(entry).data
    assert not site.can_read_devices and not site.can_read_summary
    assert entity_id(hass, "sensor", "server", "active_users") is None
    assert entity_id(hass, "binary_sensor", 4001, "connectivity") is None
    assert state(hass, "sensor", "server", "software_version") == "4.11.9753.20528"


async def test_permission_lost_while_running(hass: HomeAssistant, server: FakeServer) -> None:
    entry = await setup(hass)
    server.status["/api/v1/Devices/1/false?page=0&pageSize=100"] = 403
    server.status["/api/v1/System/Summary"] = 403
    await source(entry).poll_devices()
    assert not source(entry)._site.can_read_devices
    assert not source(entry)._site.can_read_summary


async def test_diagnostics(hass: HomeAssistant, server: FakeServer) -> None:
    entry = await setup(hass, {OPT_FALLBACK: True, OPT_FALLBACK_TARGET: "abc123"})
    diag = await async_get_config_entry_diagnostics(hass, entry)
    text = str(diag)
    for secret in (password_hash("test-password"), "ha@example.com", "192.0.2", "abc123", SITE_ID, "C4001"):
        assert secret not in text
    assert diag["active_route"] == "direct"
    assert diag["last_event_id"] == 100
    assert len(diag["doors"]) == 2
