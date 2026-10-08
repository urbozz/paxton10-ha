"""Live pushes on Remote: hub calls and pushes on the relay websocket RemoteTransport holds."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import patch

import aiohttp
import pytest
from homeassistant.core import HomeAssistant

from custom_components.paxton10.api import (
    RECORD_SEPARATOR,
    PaxtonBlockedRequest,
    PaxtonClient,
    PaxtonError,
    RemoteTransport,
)
from custom_components.paxton10.connection import PaxtonConnection
from custom_components.paxton10.const import ROUTE_DIRECT, ROUTE_REMOTE
from custom_components.paxton10.hub import (
    METHOD_SUBSCRIBE_EVENTS,
    HubDisconnected,
    HubMessage,
    RemoteFeed,
    event_rows,
)
from custom_components.paxton10.models import live_event_filter
from custom_components.paxton10.source import MODE_LIVE, LiveSource

from .conftest import FakeServer, eid, event
from .test_api import FakeWS, fake_session
from .test_init import capture, setup, source
from .test_live import fast_sleep, until  # noqa: F401  (fixture)


def push_frame(method: str, parameters: Any) -> str:
    body = json.dumps({"methodName": method, "parameters": parameters})
    return json.dumps({"type": 1, "target": "ServerSignalrMessage", "arguments": [body]}) + RECORD_SEPARATOR


async def started(ws: FakeWS) -> tuple[RemoteTransport, PaxtonClient]:
    transport = RemoteTransport(fake_session(ws), "abc123")
    client = PaxtonClient(transport)
    await client.sign_in("u", "p")
    return transport, client


async def test_push_and_rest_share_the_socket() -> None:
    ws = FakeWS()
    transport, client = await started(ws)
    queue = transport.listen()
    # A malformed push, a push for no one, then a real one, all on the REST socket.
    ws.push(aiohttp.WSMsgType.TEXT, json.dumps({"type": 1, "target": "ServerSignalrMessage", "arguments": ["{nope"]}))
    ws.push(aiohttp.WSMsgType.TEXT, json.dumps({"type": 1, "target": "ServerSignalrMessage", "arguments": []}))
    ws.push(aiohttp.WSMsgType.TEXT, push_frame(5, "not a list"))  # type: ignore[arg-type]
    ws.push(aiohttp.WSMsgType.TEXT, push_frame("newLiveEventNotification", [[event(101)]]))
    method, parameters = await asyncio.wait_for(queue.get(), 1)
    assert method == "newLiveEventNotification"
    assert event_rows(HubMessage("System", method, parameters))[0]["EventId"] == eid(101)
    # The REST call on the same socket still completes.
    assert (await client.get("/api/v1/System/Software/Version")).body == "4.11"
    assert queue.empty()
    # A dropped socket ends the queue, so the feed reconnects.
    await transport.close()
    assert await asyncio.wait_for(queue.get(), 1) is None


async def test_hub_call_is_wrapped_like_the_web_app() -> None:
    ws = FakeWS()
    transport, client = await started(ws)
    feed = RemoteFeed(transport, lambda: client.token)
    await feed.connect()
    assert feed.connected
    await feed.invoke(METHOD_SUBSCRIBE_EVENTS, {"filter": 1})
    calls = [f for f in ws.sent if f.get("target") == "UiSignalrMessage"]
    assert len(calls) == 1
    call = json.loads(calls[0]["arguments"][0])
    # parameters is the argument list, and bearerToken is the raw token, without "Bearer ".
    assert call == {"methodName": METHOD_SUBSCRIBE_EVENTS, "parameters": [{"filter": 1}], "bearerToken": "tok", "remoteId": "abc123"}

    ws.hub_error = "There was an error invoking Hub method"
    with pytest.raises(PaxtonError, match="hub call failed"):
        await feed.invoke(METHOD_SUBSCRIBE_EVENTS, {})
    with pytest.raises(PaxtonBlockedRequest):
        await feed.invoke("ActivateAppliances", [1])
    with pytest.raises(PaxtonBlockedRequest):
        await transport.hub_invoke("ActivateAppliances", [1], "tok")

    # Pushes arrive as hub messages; a close after them is reported on the next poll.
    ws.push(aiohttp.WSMsgType.TEXT, push_frame("newLiveEventNotification", [[event(102)]]))
    await asyncio.sleep(0)
    messages = await feed.poll()
    assert [m.method for m in messages] == ["newLiveEventNotification"]
    ws.push(aiohttp.WSMsgType.TEXT, push_frame("applianceStateNotification", [[{"EntityId": 2001}]]))
    await asyncio.sleep(0)
    await transport.close()
    assert [m.method for m in await feed.poll()] == ["applianceStateNotification"]
    with pytest.raises(HubDisconnected):
        await feed.poll()
    assert not feed.connected
    with pytest.raises(HubDisconnected):
        await feed.invoke(METHOD_SUBSCRIBE_EVENTS, {})
    await feed.close()


async def test_feed_edges() -> None:
    ws = FakeWS()
    transport, client = await started(ws)
    feed = RemoteFeed(transport, lambda: client.token)
    with pytest.raises(HubDisconnected):
        await feed.poll()
    await feed.connect()
    await feed.close()
    assert not feed.connected
    # Pushes with nobody listening are dropped.
    ws.push(aiohttp.WSMsgType.TEXT, push_frame("newLiveEventNotification", [[event(1)]]))
    await asyncio.sleep(0)
    # A closed transport can't be listened to, and a closed socket refuses calls.
    await transport.close()
    with pytest.raises(HubDisconnected):
        await RemoteFeed(transport, lambda: "tok").connect()
    assert await asyncio.wait_for(transport.listen().get(), 1) is None
    with pytest.raises(PaxtonError, match="not open"):
        await transport.hub_invoke(METHOD_SUBSCRIBE_EVENTS, [], "tok")


async def test_hub_call_timeout_and_close_fail_the_call(monkeypatch: pytest.MonkeyPatch) -> None:
    ws = FakeWS(reply=False)
    transport = RemoteTransport(fake_session(ws), "abc123")
    await transport.start()
    real_wait_for = asyncio.wait_for

    async def short_wait(fut: Any, timeout: float) -> Any:
        return await real_wait_for(fut, 0.01)

    monkeypatch.setattr("custom_components.paxton10.api.asyncio.wait_for", short_wait)
    with pytest.raises(PaxtonError, match="no reply from the relay"):
        await transport.hub_invoke(METHOD_SUBSCRIBE_EVENTS, [], "tok")
    monkeypatch.undo()
    call = asyncio.ensure_future(transport.hub_invoke(METHOD_SUBSCRIBE_EVENTS, [], "tok"))
    await asyncio.sleep(0)
    # A completion for another invocation, and a late one, change nothing.
    ws.push(aiohttp.WSMsgType.TEXT, json.dumps({"type": 3, "invocationId": "999"}))
    await asyncio.sleep(0)
    assert not call.done()
    await transport.close()
    with pytest.raises(PaxtonError, match="remote connection closed"):
        await call


async def test_connection_remote_hub(server: FakeServer) -> None:
    session = aiohttp.ClientSession()
    try:
        from custom_components.paxton10.api import password_hash

        from .conftest import PASSWORD, USERNAME

        conn = PaxtonConnection(session, ROUTE_REMOTE, "abc123", USERNAME, password_hash(PASSWORD))
        # The fake transport isn't the relay, so there's no remote feed.
        assert await conn.remote_hub() is None
        transport = RemoteTransport(session, "abc123")
        client = PaxtonClient(transport)
        client._token = "t1"
        conn._client, conn.active_route = client, ROUTE_REMOTE
        target = await conn.remote_hub()
        assert target and target[0] is transport and target[1]() == "t1"
        conn.active_route = ROUTE_DIRECT
        assert await conn.remote_hub() is None
        await conn.renew()
        assert target[1]() is None
    finally:
        await session.close()


class FakeRelay:
    """A RemoteTransport stand-in for LiveSource: records hub calls and queues pushes."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[Any], str | None]] = []
        self.queue: asyncio.Queue[Any] | None = None
        self.is_open = True

    def listen(self) -> asyncio.Queue[Any]:
        self.queue = asyncio.Queue()
        return self.queue

    def stop_listening(self, queue: asyncio.Queue[Any]) -> None:
        self.queue = None

    async def hub_invoke(self, method: str, parameters: list[Any], token: str | None) -> Any:
        self.calls.append((method, parameters, token))
        return None


async def test_live_source_on_remote(
    hass: HomeAssistant, server: FakeServer, fast_sleep: list[float]  # noqa: F811
) -> None:
    relay = FakeRelay()

    async def no_direct(self: PaxtonConnection) -> None:
        return None

    async def remote_hub(self: PaxtonConnection) -> Any:
        await self.get("/api/v1/System/Software/Version")
        return relay, lambda: "tok"

    with patch.object(PaxtonConnection, "hub_target", no_direct), patch.object(PaxtonConnection, "remote_hub", remote_hub):
        entry = await setup(hass)
        src = source(entry)
        assert isinstance(src, LiveSource)
        await until(lambda: src.mode == MODE_LIVE)
        # Remote subscribes to live events, with the same filter as Direct, and door state. Nothing else.
        assert relay.calls == [
            (METHOD_SUBSCRIBE_EVENTS, [live_event_filter(src._site.server.utc_offset_minutes)], "tok"),
            ("SubscribeToApplianceStateNotifications", [[2001, 2002]], "tok"),
        ]
        fired = capture(hass)
        assert relay.queue
        relay.queue.put_nowait(("newLiveEventNotification", [[event(101, 16)]]))
        await until(lambda: len(fired) == 1, hass)
        assert fired[0].data["event_type"] == "forced"
        # The socket drops: the feed subscribes again on the new connection.
        relay.queue.put_nowait(None)
        await until(lambda: len(relay.calls) == 4, hass)
        await hass.config_entries.async_unload(entry.entry_id)


async def test_quiet_remote_feed_still_reconciles(
    hass: HomeAssistant, server: FakeServer, fast_sleep: list[float], monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """Regression (v0.7.1 review): a quiet Remote feed parked the live loop, so the 5-minute event log read never ran."""
    from custom_components.paxton10 import hub as hub_mod
    from custom_components.paxton10 import source as source_mod

    relay = FakeRelay()
    clock = {"now": 0.0}
    monkeypatch.setattr(source_mod, "_monotonic", lambda: clock["now"])
    monkeypatch.setattr(hub_mod, "DEFAULT_POLL_TIMEOUT", 0.01)

    async def no_direct(self: PaxtonConnection) -> None:
        return None

    async def remote_hub(self: PaxtonConnection) -> Any:
        await self.get("/api/v1/System/Software/Version")
        return relay, lambda: "tok"

    with patch.object(PaxtonConnection, "hub_target", no_direct), patch.object(PaxtonConnection, "remote_hub", remote_hub):
        entry = await setup(hass)
        src = source(entry)
        await until(lambda: src.mode == MODE_LIVE)
        fired = capture(hass)
        # No pushes at all, but an event the feed never delivered is in the log.
        server.events.append(event(101))
        clock["now"] = source_mod.RECONCILE_INTERVAL + 1
        for _ in range(200):
            if fired:
                break
            await asyncio.sleep(0.01)
            await hass.async_block_till_done()
        assert [e.data["event_id"] for e in fired] == [eid(101)]
        await hass.config_entries.async_unload(entry.entry_id)


async def test_remote_poll_returns_empty_when_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    from custom_components.paxton10 import hub as hub_mod

    monkeypatch.setattr(hub_mod, "DEFAULT_POLL_TIMEOUT", 0.01)
    feed = RemoteFeed(FakeRelay(), lambda: "tok")  # type: ignore[arg-type]
    await feed.connect()
    assert await feed.poll() == []
    assert feed.connected
