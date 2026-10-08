"""Regression tests for failure-handling bugs found in code review. One section per issue."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from unittest.mock import AsyncMock

import aiohttp
import pytest
from homeassistant.const import STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir

from custom_components.paxton10.api import (
    RECORD_SEPARATOR,
    PaxtonError,
    RemoteTransport,
)
from custom_components.paxton10.const import (
    DOMAIN,
    EVENT_PAXTON10,
    OPT_ALLOW_DOOR_CONTROL,
    OPT_DEVICE_INTERVAL,
    OPT_EVENT_INTERVAL,
    OPT_FALLBACK,
    OPT_FALLBACK_TARGET,
    OPT_INCLUDE_USER_NAMES,
)
from custom_components.paxton10.diagnostics import async_get_config_entry_diagnostics
from custom_components.paxton10.logbook import async_describe_events
from custom_components.paxton10.source import SourceUpdate

from .conftest import SITE_ID, FakeServer, controller, eid, entity_id, event
from .test_api import FakeWS, fake_session
from .test_init import capture, coordinator, setup, source, state

DEVICES = "/api/v1/Devices/1/false?page=0&pageSize=100"


# Issue 1: a reader that dies must still fail pending calls and close the socket.


class RaisingWS(FakeWS):
    """Raises from receive inside `async for`, like aiohttp's ws_receive timeout."""

    async def __anext__(self) -> Any:
        msg = await self._queue.get()
        if isinstance(msg, BaseException):
            raise msg
        if msg is None:
            raise StopAsyncIteration
        return msg


async def test_issue1_reader_crash_fails_pending_and_closes_socket() -> None:
    ws = RaisingWS(reply=False)
    transport = RemoteTransport(fake_session(ws), "abc123")
    await transport.start()
    pending = asyncio.ensure_future(transport.send("GET", "/x", "tok", None, "application/json"))
    await asyncio.sleep(0)
    ws._queue.put_nowait(TimeoutError())
    # Fails at once, not after the 20 s send timeout.
    with pytest.raises(PaxtonError, match="closed"):
        await asyncio.wait_for(pending, timeout=1)
    assert ws.closed
    with pytest.raises(PaxtonError, match="not open"):
        await transport.send("GET", "/y", "tok", None, "application/json")
    await transport.close()


async def test_issue1_close_after_reader_died_still_cleans_up() -> None:
    ws = RaisingWS(reply=False)
    transport = RemoteTransport(fake_session(ws), "abc123")
    await transport.start()
    reader = transport._reader
    assert reader
    # Simulate a reader that ended with an exception before close() runs.
    reader.cancel()
    await asyncio.sleep(0)
    reader2: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    reader2.set_exception(RuntimeError("boom"))
    transport._reader = reader2  # type: ignore[assignment]
    fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    transport._pending["m"] = fut
    ws.closed = False
    await transport.close()
    assert ws.closed
    assert fut.done() and isinstance(fut.exception(), PaxtonError)


async def test_issue1_malformed_reply_is_ignored() -> None:
    ws = FakeWS(reply=False)
    transport = RemoteTransport(fake_session(ws), "abc123")
    await transport.start()
    bad = json.dumps({"type": 1, "target": "CloudApiResponse", "arguments": ["{not json"]})
    ws.push(aiohttp.WSMsgType.TEXT, bad + RECORD_SEPARATOR)
    await asyncio.sleep(0)
    assert transport._open and not ws.closed
    await transport.close()


async def test_issue1_server_close_fails_pending_before_close_handshake() -> None:
    done_at_close: list[bool] = []
    inner: asyncio.Future[Any]

    class SlowCloseWS(FakeWS):
        async def close(self) -> None:
            # By the time the close handshake starts, the waiting call has already failed.
            done_at_close.append(inner.done())
            await super().close()

    ws = SlowCloseWS(reply=False)
    transport = RemoteTransport(fake_session(ws), "abc123")
    await transport.start()
    pending = asyncio.ensure_future(transport.send("GET", "/x", "tok", None, "application/json"))
    await asyncio.sleep(0)
    inner = next(iter(transport._pending.values()))
    ws.push(aiohttp.WSMsgType.TEXT, json.dumps({"type": 7}) + RECORD_SEPARATOR)
    with pytest.raises(PaxtonError):
        await pending
    assert done_at_close[:1] == [True]
    await transport.close()


# Issue 2: one loop's success must not clear the other loop's failure.


async def test_issue2_event_poll_does_not_clear_device_failure(hass: HomeAssistant, server: FakeServer) -> None:
    entry = await setup(hass)
    server.status[DEVICES] = 500
    with pytest.raises(PaxtonError):
        await source(entry).poll_devices()
    assert source(entry)._callback
    await source(entry)._callback(SourceUpdate("devices", error=PaxtonError("HTTP 500")))
    await hass.async_block_till_done()
    assert state(hass, "binary_sensor", 4001, "connectivity") == STATE_UNAVAILABLE

    # Successful event polls, empty and not, leave the device failure in place.
    await source(entry).poll_events()
    server.events.append(event(101))
    await source(entry).poll_events()
    await hass.async_block_till_done()
    assert state(hass, "binary_sensor", 4001, "connectivity") == STATE_UNAVAILABLE
    assert not coordinator(entry).last_update_success

    # Only a successful device poll brings entities back.
    server.status.clear()
    await source(entry).poll_devices()
    await hass.async_block_till_done()
    assert state(hass, "binary_sensor", 4001, "connectivity") == STATE_ON


async def test_issue2_device_data_held_while_events_fail(hass: HomeAssistant, server: FakeServer) -> None:
    entry = await setup(hass)
    assert source(entry)._callback
    await source(entry)._callback(SourceUpdate("events", error=PaxtonError("event log down")))
    await hass.async_block_till_done()

    # A device poll succeeds and finds a controller offline, but the event read is still failing.
    server.controllers = [controller(4001, 2001, status=4)]
    await source(entry).poll_devices()
    await hass.async_block_till_done()
    assert state(hass, "binary_sensor", 4001, "connectivity") == STATE_UNAVAILABLE
    assert coordinator(entry).data.devices[4001].online is False

    # When the event read recovers, entities show the newest device data.
    await source(entry).poll_events()
    await hass.async_block_till_done()
    assert state(hass, "binary_sensor", 4001, "connectivity") == "off"


async def test_issue2_empty_event_page_does_not_republish(hass: HomeAssistant, server: FakeServer) -> None:
    entry = await setup(hass)
    coord = coordinator(entry)
    calls: list[Any] = []
    coord.async_set_updated_data = calls.append  # type: ignore[method-assign]
    await source(entry).poll_events()
    assert calls == []


# Issue 3: the hourly probe must not tear down a working fallback session.


async def test_issue3_probe_keeps_fallback_when_primary_down(hass: HomeAssistant, server: FakeServer) -> None:
    server.down.add("192.0.2.1")
    entry = await setup(hass, {OPT_FALLBACK: True, OPT_FALLBACK_TARGET: "abc123"})
    conn = coordinator(entry).conn
    fallback_client = conn._client
    assert conn.active_route == "remote"
    await coordinator(entry)._async_rediscover()
    assert conn._client is fallback_client and conn.active_route == "remote"
    assert coordinator(entry).last_update_success
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"using_fallback_route_{entry.entry_id}")


async def test_issue3_probe_swaps_only_after_sign_in(hass: HomeAssistant, server: FakeServer) -> None:
    server.down.add("192.0.2.1")
    entry = await setup(hass, {OPT_FALLBACK: True, OPT_FALLBACK_TARGET: "abc123"})
    conn = coordinator(entry).conn
    assert await conn.try_primary() is False
    server.down.clear()
    old = conn._client
    assert old
    old.close = AsyncMock(side_effect=RuntimeError)  # type: ignore[method-assign]
    assert await conn.try_primary() is True
    assert conn.active_route == "direct" and conn._client is not old
    assert await conn.try_primary() is False  # already on the primary


# Issue 4: a device that's removed and comes back gets its entities again.


async def test_issue4_device_that_returns_is_re_added(hass: HomeAssistant, server: FakeServer) -> None:
    entry = await setup(hass, {OPT_ALLOW_DOOR_CONTROL: True})
    gate = server.groups[3002]["Appliances"]
    server.groups[3002]["Appliances"] = []
    await coordinator(entry)._async_rediscover()
    await hass.async_block_till_done()
    assert entity_id(hass, "button", 2002, "open") is None

    server.groups[3002]["Appliances"] = gate
    await coordinator(entry)._async_rediscover()
    await hass.async_block_till_done()
    assert entity_id(hass, "button", 2002, "open")
    assert entity_id(hass, "event", 2002, "door_event")


# Issue 5: unloading removes the fallback repair.


async def test_issue5_unload_deletes_fallback_issue(hass: HomeAssistant, server: FakeServer) -> None:
    server.down.add("192.0.2.1")
    entry = await setup(hass, {OPT_FALLBACK: True, OPT_FALLBACK_TARGET: "abc123"})
    issue_id = f"using_fallback_route_{entry.entry_id}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


# Issue 6: serials don't leak through names in diagnostics or logs.


async def test_issue6_no_serial_in_names_diagnostics_or_log(
    hass: HomeAssistant, server: FakeServer, caplog: pytest.LogCaptureFixture
) -> None:
    server.controllers = [controller(4001, 2001), controller(1300, 9999)]  # 1300 drives no known door
    entry = await setup(hass)
    caplog.set_level(logging.INFO)
    assert coordinator(entry).data.devices[1300].name == "Controller 1300"
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert "C1300" not in str(diag) and "C4001" not in str(diag)

    server.controllers = [controller(4001, 2001)]
    await coordinator(entry)._async_rediscover()
    assert f"{SITE_ID}_1300" in caplog.text
    assert "C1300" not in caplog.text


# Issue 7: a delivery failure doesn't lose events.


async def test_issue7_failed_delivery_is_retried(hass: HomeAssistant, server: FakeServer) -> None:
    entry = await setup(hass)
    src = source(entry)
    good = src._callback
    server.events.append(event(101))

    async def broken(update: Any) -> None:
        raise RuntimeError("listener failed")

    src._callback = broken
    with pytest.raises(RuntimeError):
        await src.poll_events()
    assert src.last_event_id == eid(100)

    fired = capture(hass)
    src._callback = good
    await src.poll_events()
    await hass.async_block_till_done()
    assert [e.data["event_id"] for e in fired] == [eid(101)]
    assert src.last_event_id == eid(101)


# Issue 8: the fallback target is stored trimmed, and dropped when the fallback is off.


@pytest.mark.parametrize(("fallback", "expected"), [(True, "abc123"), (False, None)])
async def test_issue8_fallback_target_trimmed(
    hass: HomeAssistant, server: FakeServer, fallback: bool, expected: str | None
) -> None:
    entry = await setup(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            OPT_ALLOW_DOOR_CONTROL: False,
            OPT_DEVICE_INTERVAL: 30,
            OPT_EVENT_INTERVAL: 10,
            OPT_FALLBACK: fallback,
            OPT_FALLBACK_TARGET: " abc123 ",
            OPT_INCLUDE_USER_NAMES: False,
        },
    )
    await hass.async_block_till_done()
    assert entry.options.get(OPT_FALLBACK_TARGET) == expected


# Issue 9: the device registry follows firmware and name changes.


async def test_issue9_device_registry_follows_changes(hass: HomeAssistant, server: FakeServer) -> None:
    entry = await setup(hass)
    devices = dr.async_get(hass)
    server.controllers[0]["FirmwareVersion"] = "3.01.0"
    await source(entry).poll_devices()
    await hass.async_block_till_done()
    ctrl = devices.async_get_device_by_identifier((DOMAIN, f"{SITE_ID}_4001"), entry.entry_id)
    assert ctrl and ctrl.sw_version == "3.01.0"

    server.groups[3001]["Appliances"][0]["Name"] = "Main Front Door"
    await coordinator(entry)._async_rediscover()
    await hass.async_block_till_done()
    door = devices.async_get_device_by_identifier((DOMAIN, f"{SITE_ID}_2001"), entry.entry_id)
    assert door and door.name == "Main Front Door"
    ctrl = devices.async_get_device_by_identifier((DOMAIN, f"{SITE_ID}_4001"), entry.entry_id)
    assert ctrl and ctrl.name == "Main Front Door controller"


# Issue 10: live 4.11 event ids are 24-character strings that don't sort by time,
# and door events name the door in ApplianceData, not ApplianceIds.

LIVE_ROW: dict[str, Any] = {
    "EventId": "6703e1a85d1c2b0f4e9a7c31",
    "EventTime": "2026-10-07T17:28:24+01:00",
    "EventTypeId": 7,
    "CategoryId": 5,
    "ApplianceIds": [],
    "ApplianceData": {"ApplianceTypeId": 0, "ApplianceId": 2001, "Appliance": "Main Entrance", "ApplianceGroupId": 0},
    "UserData": {"UserId": 1015, "UserName": "Test Person", "UserGroup": "Installers"},
    "UserName": None,
    "TranslatableFields": {"Parameters": [{"Value": 545001, "Description": ""}], "InformationTranslationKey": 530006},
    "MyTimeZoneOffsetMins": 0,
    "DeviceSerialNumber": 0,
}


async def test_issue10_live_event_shape(hass: HomeAssistant, server: FakeServer) -> None:
    entry = await setup(hass)
    fired = capture(hass)
    # A new id that sorts before the existing ones is still new.
    server.events.append({**LIVE_ROW, "EventId": "0" * 24})
    await source(entry).poll_events()
    await hass.async_block_till_done()
    assert [(e.data["event_id"], e.data["event_type"], e.data["door_entity_id"]) for e in fired] == [
        ("0" * 24, "opened_by_software", 2001)
    ]
    assert "user_name" not in fired[0].data


async def test_web_app_event_types_and_reader(hass: HomeAssistant, server: FakeServer) -> None:
    """Live 4.11 shapes: an exit reader access, an intercom call, no answer, and release, and a denial."""
    entry = await setup(hass)
    fired = capture(hass)
    exit_reader = {"InformationTranslationKey": 530004, "Parameters": [{"Value": 541135, "Description": ""}]}
    release = {"InformationTranslationKey": 530058, "Parameters": [{"Value": 545000, "Description": "Reception"}]}
    server.events += [
        {**LIVE_ROW, "EventId": "a" * 24, "EventTypeId": 5, "CategoryId": 1, "TranslatableFields": exit_reader},
        {**LIVE_ROW, "EventId": "b" * 24, "EventTypeId": 145, "UserData": None},
        {**LIVE_ROW, "EventId": "c" * 24, "EventTypeId": 142, "UserData": None},
        {**LIVE_ROW, "EventId": "d" * 24, "EventTypeId": 140, "UserData": None, "TranslatableFields": release},
        {**LIVE_ROW, "EventId": "e" * 24, "EventTypeId": 1, "UserData": None},
    ]
    await source(entry).poll_events()
    await hass.async_block_till_done()
    assert {e.data["event_id"][0]: (e.data["event_type"], e.data["reader"]) for e in fired} == {
        "a": ("access_permitted", "exit"),
        "b": ("call_made", None),
        "c": ("call_not_answered", None),
        "d": ("intercom_unlocked", None),
        "e": ("unknown_credential", None),
    }


async def test_issue10_seen_ids_are_bounded(server: FakeServer, monkeypatch: pytest.MonkeyPatch) -> None:
    from collections import deque

    from custom_components.paxton10 import source as source_mod

    src = source_mod.PollingSource(AsyncMock(), AsyncMock(), 30, 10, False)
    src._seen = deque(maxlen=2)
    for n in (1, 2, 2, 3):
        src._remember(eid(n))
    assert list(src._seen) == [eid(2), eid(3)]
    assert src._seen_set == {eid(2), eid(3)}


async def test_issue10_full_page_of_new_events_is_logged(
    hass: HomeAssistant, server: FakeServer, caplog: pytest.LogCaptureFixture
) -> None:
    entry = await setup(hass)
    server.events = [event(n) for n in range(500, 503)]
    with caplog.at_level(logging.DEBUG, logger="custom_components.paxton10.source"):
        await source(entry).poll_events()
    assert "may have been missed" in caplog.text


# Issue 11: doors hang off the controller that drives them.


async def test_issue11_door_via_controller(hass: HomeAssistant, server: FakeServer) -> None:
    entry = await setup(hass)
    devices = dr.async_get(hass)
    ctrl = devices.async_get_device_by_identifier((DOMAIN, f"{SITE_ID}_4001"), entry.entry_id)
    srv = devices.async_get_device_by_identifier((DOMAIN, SITE_ID), entry.entry_id)
    door = devices.async_get_device_by_identifier((DOMAIN, f"{SITE_ID}_2001"), entry.entry_id)
    assert ctrl and srv and door
    assert ctrl.via_device_id == srv.id
    assert door.via_device_id == ctrl.id
    # A door no controller drives stays under the server.
    other = devices.async_get_device_by_identifier((DOMAIN, f"{SITE_ID}_2002"), entry.entry_id)
    assert other and other.via_device_id == srv.id

    # Rewiring the door to no controller moves it back under the server on the next device poll.
    server.controllers[0]["Connectors"] = []
    await source(entry).poll_devices()
    await hass.async_block_till_done()
    door = devices.async_get_device_by_identifier((DOMAIN, f"{SITE_ID}_2001"), entry.entry_id)
    assert door and door.via_device_id == srv.id


async def test_intercom_events_name_the_called_user(hass: HomeAssistant, server: FakeServer) -> None:
    """Live 4.11 intercom shapes: the panel and the user are both Value 0 parameters, in template order."""
    entry = await setup(hass, {OPT_INCLUDE_USER_NAMES: True})
    fired = capture(hass)

    def tf(key: int, *params: tuple[str, int]) -> dict[str, Any]:
        return {"InformationTranslationKey": key, "Parameters": [{"Description": d, "Value": v} for d, v in params]}

    server.events += [
        {**LIVE_ROW, "EventId": "a" * 24, "EventTypeId": 140, "UserData": None,
         "TranslatableFields": tf(530058, ("Reception", 545000), ("Alex Smith", 0))},
        {**LIVE_ROW, "EventId": "b" * 24, "EventTypeId": 142, "UserData": None,
         "TranslatableFields": tf(530060, ("Alex Smith", 0), ("7000001", 0))},
        {**LIVE_ROW, "EventId": "c" * 24, "EventTypeId": 145, "UserData": None,
         "TranslatableFields": tf(530083, ("7000001", 0), ("Alex Smith", 0))},
        # A template we don't know, or a short parameter list, names nobody.
        {**LIVE_ROW, "EventId": "d" * 24, "EventTypeId": 145, "UserData": None,
         "TranslatableFields": tf(530083, ("7000001", 0))},
        {**LIVE_ROW, "EventId": "e" * 24, "EventTypeId": 141, "UserData": None,
         "TranslatableFields": tf(539999, ("Alex Smith", 0))},
    ]
    await source(entry).poll_events()
    await hass.async_block_till_done()
    assert {e.data["event_id"][0]: (e.data["event_type"], e.data["user_name"]) for e in fired} == {
        "a": ("intercom_unlocked", "Alex Smith"),
        "b": ("call_not_answered", "Alex Smith"),
        "c": ("call_made", "Alex Smith"),
        "d": ("call_made", None),
        "e": ("other", None),
    }

    describers: dict[tuple[str, str], Any] = {}
    async_describe_events(hass, lambda domain, event_type, fn: describers.__setitem__((domain, event_type), fn))
    describe = describers[(DOMAIN, EVENT_PAXTON10)]
    assert [describe(e)["message"] for e in fired[:3]] == [
        "logged intercom unlocked by Alex Smith",
        "logged call not answered by Alex Smith",
        "logged call made to Alex Smith",
    ]


async def test_intercom_user_only_when_allowed(hass: HomeAssistant, server: FakeServer) -> None:
    entry = await setup(hass)
    fired = capture(hass)
    server.events.append({
        **LIVE_ROW, "EventId": "a" * 24, "EventTypeId": 145, "UserData": None,
        "TranslatableFields": {"InformationTranslationKey": 530083,
                               "Parameters": [{"Description": "7000001", "Value": 0}, {"Description": "Alex Smith", "Value": 0}]},
    })
    await source(entry).poll_events()
    await hass.async_block_till_done()
    assert "user_name" not in fired[0].data
