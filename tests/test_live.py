"""Live events: the SignalR long-poll hub client and LiveSource, against a fake hub server."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Generator
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs

import aiohttp
import pytest
from homeassistant.core import HomeAssistant

from custom_components.paxton10 import source as source_mod
from custom_components.paxton10.api import (
    DirectTransport,
    PaxtonAuthError,
    PaxtonBlockedRequest,
    PaxtonClient,
    PaxtonError,
    password_hash,
)
from custom_components.paxton10.connection import PaxtonConnection
from custom_components.paxton10.const import ROUTE_DIRECT, ROUTE_REMOTE
from custom_components.paxton10.diagnostics import async_get_config_entry_diagnostics
from custom_components.paxton10.hub import (
    METHOD_SUBSCRIBE_EVENTS,
    METHOD_UNSUBSCRIBE_EVENTS,
    HubDisconnected,
    HubMessage,
    HubUnauthorized,
    LongPollHub,
    event_rows,
)
from custom_components.paxton10.models import live_event_filter
from custom_components.paxton10.source import MODE_LIVE, MODE_POLLING, LiveSource

from .conftest import PASSWORD, USERNAME, FakeServer, eid, event
from .test_init import EVENTS, capture, setup, source

BASE = "https://192.0.2.1"


class FakeResp:
    def __init__(self, status: int, text: str) -> None:
        self.status, self._text = status, text

    async def text(self) -> str:
        return self._text


class FakeHub:
    """A SignalR 2.x long-poll server: holds each poll open until a push is queued."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.invoked: list[tuple[str, list[Any]]] = []
        self.pushes: asyncio.Queue[Any] = asyncio.Queue()
        self.once: dict[str, Any] = {}  # path -> reply, status, or exception for the next request only
        self.send_error: str | None = None
        self.refuse: set[str] = set()  # hub methods that answer with an error
        self.message = 0

    def request(self, method: str, url: str, *, params: dict[str, str], data: str | None, **_: Any) -> Any:
        hub = self

        class Ctx:
            async def __aenter__(self) -> FakeResp:
                return await hub.handle(url.rsplit("/", 1)[-1], params, data)

            async def __aexit__(self, *args: object) -> None:
                return None

        return Ctx()

    def paths(self) -> list[str]:
        return [p for p, _ in self.calls]

    async def handle(self, path: str, params: dict[str, str], data: str | None) -> FakeResp:
        self.calls.append((path, dict(params)))
        if path in self.once:
            forced = self.once.pop(path)
            if isinstance(forced, BaseException):
                raise forced
            if isinstance(forced, int):
                return FakeResp(forced, "")
            return FakeResp(200, forced if isinstance(forced, str) else json.dumps(forced))
        reply: Any
        if path == "negotiate":
            reply = {"ConnectionToken": "ct", "ConnectionTimeout": 110.0, "LongPollDelay": 0.0}
        elif path == "connect":
            reply = {"C": "c0", "S": 1, "M": []}
        elif path == "start":
            reply = {"Response": "started"}
        elif path == "send":
            body = json.loads(parse_qs(data or "")["data"][0])
            self.invoked.append((body["M"], body["A"]))
            error = self.send_error or ("refused" if body["M"] in self.refuse else None)
            reply = {"I": body["I"], "E": error} if error else {"I": body["I"]}
        elif path == "poll":
            item = await self.pushes.get()
            if isinstance(item, BaseException):
                raise item
            if isinstance(item, dict):
                reply = item
            else:
                self.message += 1
                reply = {
                    "C": f"c{self.message}",
                    "M": [{"H": "System", "M": "newLiveEventNotification", "A": [item]}],
                }
        else:  # abort
            reply = {}
        return FakeResp(200, json.dumps(reply))

    def subscriptions(self) -> int:
        return sum(1 for m, _ in self.invoked if m == METHOD_SUBSCRIBE_EVENTS)


@pytest.fixture
def hub(server: FakeServer) -> Generator[FakeHub]:
    """Point LiveSource at the fake hub, as if the active route were Direct."""
    fake = FakeHub()
    direct = {"on": True}

    async def hub_target(self: PaxtonConnection) -> tuple[str, Callable[[], str | None]] | None:
        await self.get("/api/v1/System/Software/Version")  # signs in, as the real one does
        return (BASE, lambda: "tok") if direct["on"] else None

    fake.direct = direct  # type: ignore[attr-defined]
    with (
        patch.object(PaxtonConnection, "hub_target", hub_target),
        patch.object(source_mod, "LongPollHub", lambda session, base, token: LongPollHub(fake, base, token)),  # type: ignore[arg-type]
    ):
        yield fake


@pytest.fixture
def fast_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    sleeps: list[float] = []

    async def fake(delay: float) -> None:
        sleeps.append(delay)
        await asyncio.sleep(0)

    monkeypatch.setattr(source_mod, "_sleep", fake)
    return sleeps


async def until(check: Callable[[], bool], hass: HomeAssistant | None = None) -> None:
    for _ in range(500):
        if check():
            return
        await asyncio.sleep(0)
        if hass:
            await hass.async_block_till_done()
    raise AssertionError("condition never became true")


def live(entry: Any) -> LiveSource:
    src = source(entry)
    assert isinstance(src, LiveSource)
    return src


async def test_live_events(hass: HomeAssistant, server: FakeServer, hub: FakeHub, fast_sleep: list[float]) -> None:
    entry = await setup(hass)
    src = live(entry)
    await until(lambda: src.mode == MODE_LIVE)
    # The hub gets the web UI's live filter, and the token on the query string.
    assert hub.invoked == [
        (METHOD_SUBSCRIBE_EVENTS, [live_event_filter(src._site.server.utc_offset_minutes)]),
        ("SubscribeToApplianceStateNotifications", [[2001, 2002]]),
    ]
    negotiate = hub.calls[0][1]
    assert negotiate["bearer_token"] == "tok" and negotiate["connectionData"] == '[{"name":"system"}]'
    rest_polls = len(server.sent("POST", EVENTS))

    fired = capture(hass)
    hub.pushes.put_nowait([event(101, 16)])
    await until(lambda: len(fired) == 1, hass)
    assert (fired[0].data["event_id"], fired[0].data["event_type"], fired[0].data["door_entity_id"]) == (
        eid(101),
        "forced",
        2001,
    )
    # No polling while live.
    assert len(server.sent("POST", EVENTS)) == rest_polls

    # Several rows in one push arrive oldest first, and a repeat of a seen event is dropped.
    older = {**event(103), "EventTime": "2026-10-07T13:59:00+01:00"}
    hub.pushes.put_nowait([event(102), older, event(101, 16)])
    await until(lambda: len(fired) == 3, hass)
    assert [e.data["event_id"] for e in fired[1:]] == [eid(103), eid(102)]
    # Each poll sends the cursor from the previous reply.
    await until(lambda: hub.paths().count("poll") >= 3)
    assert hub.calls[-1][1]["messageId"] == "c2"

    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["event_source"] == MODE_LIVE

    await hass.config_entries.async_unload(entry.entry_id)
    assert hub.paths()[-1] == "abort"


async def test_live_reconnects_and_polls_meanwhile(
    hass: HomeAssistant, server: FakeServer, hub: FakeHub, fast_sleep: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="custom_components.paxton10.source")
    entry = await setup(hass)
    src = live(entry)
    await until(lambda: src.mode == MODE_LIVE)
    fired = capture(hass)

    # The server ends the connection. An event logged meanwhile comes from the fallback poll.
    server.events.append(event(101))
    hub.pushes.put_nowait({"T": 1})
    await until(lambda: hub.subscriptions() == 2, hass)
    await until(lambda: src.mode == MODE_LIVE)
    assert [e.data["event_id"] for e in fired] == [eid(101)]
    assert "live events unavailable" in caplog.text and "live events connected" in caplog.text
    # The fallback polls at the event interval (the device loop's 30 s sleeps share this list).
    assert 10 in fast_sleep

    # A dropped poll is a failure too, and nothing is lost or repeated.
    hub.pushes.put_nowait(aiohttp.ClientError("dropped"))
    await until(lambda: hub.subscriptions() == 3, hass)
    hub.pushes.put_nowait([event(101), event(102)])
    await until(lambda: len(fired) == 2, hass)
    assert [e.data["event_id"] for e in fired] == [eid(101), eid(102)]
    await hass.config_entries.async_unload(entry.entry_id)


async def test_live_expired_token_signs_in_again(
    hass: HomeAssistant, server: FakeServer, hub: FakeHub, fast_sleep: list[float]
) -> None:
    entry = await setup(hass)
    src = live(entry)
    await until(lambda: src.mode == MODE_LIVE)
    tokens = server.tokens
    hub.once["negotiate"] = 401
    hub.pushes.put_nowait({"T": 1})
    await until(lambda: hub.subscriptions() == 2, hass)
    assert server.tokens > tokens
    assert not src.auth_failed
    await hass.config_entries.async_unload(entry.entry_id)


async def test_live_off_direct_polls(
    hass: HomeAssistant, server: FakeServer, hub: FakeHub, fast_sleep: list[float]
) -> None:
    hub.direct["on"] = False  # type: ignore[attr-defined]
    entry = await setup(hass)
    src = live(entry)
    fired = capture(hass)
    server.events.append(event(101))
    await until(lambda: len(fired) == 1, hass)
    assert src.mode == MODE_POLLING
    assert hub.calls == []
    await hass.config_entries.async_unload(entry.entry_id)


async def test_live_reconciles_with_the_event_log(
    hass: HomeAssistant, server: FakeServer, hub: FakeHub, fast_sleep: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = {"now": 0.0}
    monkeypatch.setattr(source_mod, "_monotonic", lambda: clock["now"])
    entry = await setup(hass)
    src = live(entry)
    await until(lambda: src.mode == MODE_LIVE)
    fired = capture(hass)
    polls = len(server.sent("POST", EVENTS))
    # A push the hub never delivered turns up at the next reconcile.
    server.events.append(event(101))
    clock["now"] = source_mod.RECONCILE_INTERVAL + 1
    hub.pushes.put_nowait({"C": "c9", "M": []})
    await until(lambda: len(fired) == 1, hass)
    assert len(server.sent("POST", EVENTS)) == polls + 1
    await hass.config_entries.async_unload(entry.entry_id)


async def test_live_loop_failures(hass: HomeAssistant, server: FakeServer, hub: FakeHub, fast_sleep: list[float]) -> None:
    entry = await setup(hass)
    src = live(entry)
    await src.async_stop()
    received: list[Any] = []

    async def cb(update: Any) -> None:
        received.append(update)

    src._callback = cb
    calls = {"n": 0}

    async def target() -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("bug")  # logged, then retried
        if calls["n"] == 2:
            server.status[EVENTS] = 500  # the fallback poll fails and says so
            raise PaxtonError("hub down")
        server.status.clear()
        raise PaxtonAuthError("password changed")

    with patch.object(src._conn, "hub_target", target):
        await src._events_loop()  # returns on the auth failure
    assert src.auth_failed
    errors = [type(u.error).__name__ for u in received if u.error]
    # The fallback poll reports its failures; the bug isn't reported as a read failure.
    assert errors[-1] == "PaxtonAuthError" and set(errors[:-1]) == {"PaxtonError"}
    await hass.config_entries.async_unload(entry.entry_id)


async def test_live_catch_up_auth_failure(
    hass: HomeAssistant, server: FakeServer, hub: FakeHub, fast_sleep: list[float]
) -> None:
    entry = await setup(hass)
    src = live(entry)
    await src.async_stop()
    src._callback = None

    async def rejected() -> None:
        raise PaxtonAuthError("rejected")

    with patch.object(src, "poll_events", rejected):
        await src._events_loop()
    assert src.auth_failed
    await hass.config_entries.async_unload(entry.entry_id)


# The hub client on its own.


async def connected(fake: FakeHub, token: str | None = "tok") -> LongPollHub:
    client = LongPollHub(fake, BASE + "/", lambda: token)  # type: ignore[arg-type]
    await client.connect()
    return client


async def test_hub_protocol() -> None:
    fake = FakeHub()
    client = await connected(fake)
    assert fake.paths() == ["negotiate", "connect", "start"]
    assert "connectionToken" not in fake.calls[0][1]
    assert fake.calls[1][1]["connectionToken"] == "ct" and fake.calls[1][1]["transport"] == "longPolling"

    assert await client.invoke(METHOD_SUBSCRIBE_EVENTS, {"a": 1}) is None
    assert await client.invoke(METHOD_UNSUBSCRIBE_EVENTS) is None
    assert fake.invoked == [(METHOD_SUBSCRIBE_EVENTS, [{"a": 1}]), (METHOD_UNSUBSCRIBE_EVENTS, [])]
    with pytest.raises(PaxtonBlockedRequest):
        await client.invoke("ActivateAppliances", [1])
    fake.send_error = "There was an error invoking Hub method"
    with pytest.raises(PaxtonError, match="error invoking"):
        await client.invoke(METHOD_SUBSCRIBE_EVENTS, {})

    # The groups token and poll delay from one reply go with the next poll.
    fake.pushes.put_nowait({"C": "c5", "G": "groups", "L": 1, "M": [{"H": "System", "M": "other", "A": "x"}, "junk"]})
    messages = await client.poll()
    assert messages == [HubMessage("System", "other", [])]
    fake.pushes.put_nowait([])
    await client.poll()
    assert fake.calls[-1][1]["messageId"] == "c5" and fake.calls[-1][1]["groupsToken"] == "groups"

    fake.pushes.put_nowait({"D": 1})
    with pytest.raises(HubDisconnected, match="ended"):
        await client.poll()
    assert not client.connected
    with pytest.raises(HubDisconnected):
        await client.poll()
    with pytest.raises(HubDisconnected):
        await client.invoke(METHOD_SUBSCRIBE_EVENTS, {})
    await client.close()  # not connected: no abort
    assert fake.paths()[-1] == "poll"


@pytest.mark.parametrize(
    ("path", "forced", "error", "match"),
    [
        ("negotiate", 401, HubUnauthorized, "token rejected"),
        ("negotiate", 500, PaxtonError, "HTTP 500"),
        ("negotiate", "not json", PaxtonError, "isn't JSON"),
        ("negotiate", {"ConnectionToken": ""}, PaxtonError, "no connection token"),
        ("negotiate", aiohttp.ClientError(), PaxtonError, "ClientError$"),
        ("negotiate", TimeoutError(), PaxtonError, "within 20 s"),
        ("start", {"Response": "nope"}, PaxtonError, "unexpected reply"),
    ],
)
async def test_hub_connect_errors(path: str, forced: Any, error: type[Exception], match: str) -> None:
    fake = FakeHub()
    fake.once[path] = forced
    with pytest.raises(error, match=match):
        await connected(fake, token=None)
    assert "bearer_token" not in fake.calls[0][1]


async def test_hub_close_ignores_abort_failure() -> None:
    fake = FakeHub()
    client = await connected(fake)
    fake.once["abort"] = 500
    await client.close()
    assert not client.connected


async def test_hub_uses_negotiated_timeouts() -> None:
    fake = FakeHub()
    fake.once["negotiate"] = {"ConnectionToken": "ct", "ConnectionTimeout": "bad", "LongPollDelay": 250}
    client = await connected(fake)
    assert client._poll_timeout == 110 and client._poll_delay == 250
    fake.once["poll"] = "[]"  # not an object: no messages
    with patch("custom_components.paxton10.hub.asyncio.sleep") as sleep:
        assert await client.poll() == []
    sleep.assert_awaited_once_with(0.25)


def test_event_rows() -> None:
    rows = [{"EventId": "a"}, "junk"]
    assert event_rows(HubMessage("System", "newLiveEventNotification", [rows])) == [{"EventId": "a"}]
    assert event_rows(HubMessage("System", "NEWLIVEEVENTNOTIFICATION", [{"EventId": "b"}])) == [{"EventId": "b"}]
    assert event_rows(HubMessage("System", "batteryStatusNotification", [rows])) == []
    assert event_rows(HubMessage("System", "newLiveEventNotification", [])) == []


async def test_connection_hub_target(server: FakeServer) -> None:
    session = aiohttp.ClientSession()
    try:
        conn = PaxtonConnection(session, ROUTE_DIRECT, "192.0.2.1", USERNAME, password_hash(PASSWORD))
        assert conn.session is session
        # The fake transport isn't Direct, so there's no hub.
        assert await conn.hub_target() is None
        client = PaxtonClient(DirectTransport(session, "192.0.2.1"))
        client._token = "t1"
        conn._client, conn.active_route = client, ROUTE_DIRECT
        target = await conn.hub_target()
        assert target and target[0] == BASE and target[1]() == "t1"
        # The getter follows the connection's current client.
        client._token = "t2"
        assert target[1]() == "t2"
        conn.active_route = ROUTE_REMOTE
        assert await conn.hub_target() is None
        await conn.renew()
        assert conn._client is None and target[1]() is None
    finally:
        await session.close()


async def test_live_auth_failure_in_fallback_poll(
    hass: HomeAssistant, server: FakeServer, hub: FakeHub, fast_sleep: list[float]
) -> None:
    """Regression: the fallback poll after a hub failure used to let an auth failure kill the task."""
    entry = await setup(hass)
    src = live(entry)
    await src.async_stop()
    received: list[Any] = []

    async def cb(update: Any) -> None:
        received.append(update)

    async def hub_down() -> Any:
        raise PaxtonError("hub down")

    async def rejected() -> None:
        raise PaxtonAuthError("password changed")

    src._callback = cb
    with patch.object(src._conn, "hub_target", hub_down), patch.object(src, "poll_events", rejected):
        await src._events_loop()
    assert src.auth_failed
    assert [type(u.error).__name__ for u in received] == ["PaxtonAuthError"]
    # Once the password is rejected, the fallback stops polling.
    polls = len(server.sent("POST", EVENTS))
    await src._poll_for(30)
    assert len(server.sent("POST", EVENTS)) == polls
    await hass.config_entries.async_unload(entry.entry_id)


async def test_live_off_direct_rechecks_the_route(
    hass: HomeAssistant, server: FakeServer, hub: FakeHub, fast_sleep: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    hub.direct["on"] = False  # type: ignore[attr-defined]
    monkeypatch.setattr(source_mod, "NOT_DIRECT_RECHECK", 20)
    entry = await setup(hass)
    src = live(entry)
    await src.async_stop()
    polls = len(server.sent("POST", EVENTS))
    assert await src._hub_cycle(3) == 0  # no hub to fail, so no backoff
    assert len(server.sent("POST", EVENTS)) == polls + 2
    await hass.config_entries.async_unload(entry.entry_id)


async def test_polling_source_still_polls_events(
    hass: HomeAssistant, server: FakeServer, fast_sleep: list[float]
) -> None:
    from custom_components.paxton10.source import PollingSource

    entry = await setup(hass)
    src = live(entry)
    plain = PollingSource(src._conn, src._site, 30, 10, False)
    got: list[str] = []

    async def cb(update: Any) -> None:
        got.extend(e.event_id for e in update.events)

    await plain.async_start(cb)
    try:
        server.events.append(event(101))
        await until(lambda: got == [eid(101)], hass)
    finally:
        await plain.async_stop()
    await hass.config_entries.async_unload(entry.entry_id)


async def test_live_takes_user_names_from_the_event_log(
    hass: HomeAssistant, server: FakeServer, hub: FakeHub, fast_sleep: list[float]
) -> None:
    """Live rows carry the user's id but not their name. With names on, the event log supplies it."""
    from custom_components.paxton10.const import OPT_INCLUDE_USER_NAMES

    entry = await setup(hass, {OPT_INCLUDE_USER_NAMES: True})
    src = live(entry)
    await until(lambda: src.mode == MODE_LIVE)
    fired = capture(hass)
    polls = len(server.sent("POST", EVENTS))

    server.events.append(event(101, 5, user={"UserId": 7, "UserName": "Alex Smith"}))
    hub.pushes.put_nowait([event(101, 5, user={"UserId": 7, "UserName": None})])
    await until(lambda: len(fired) == 1, hass)
    assert fired[0].data["user_name"] == "Alex Smith"
    assert len(server.sent("POST", EVENTS)) == polls + 1

    # Not on the page yet: it still fires, without the name, and only once.
    hub.pushes.put_nowait([event(102, 5, user={"UserId": 7})])
    await until(lambda: len(fired) == 2, hass)
    assert fired[1].data.get("user_name") is None
    # A row with no user, or a name already, needs no extra read.
    hub.pushes.put_nowait([event(103, 7), event(104, 5, user={"UserId": 8, "UserName": "Sam Lee"})])
    await until(lambda: len(fired) == 4, hass)
    assert len(server.sent("POST", EVENTS)) == polls + 2
    await hass.config_entries.async_unload(entry.entry_id)


# Review fixes: a failed task, a failed lookup, or a bug must not stop events or the shutdown.


async def test_stop_survives_a_failed_task(hass: HomeAssistant, server: FakeServer, fast_sleep: list[float]) -> None:
    entry = await setup(hass)
    src = live(entry)

    async def boom() -> None:
        raise RuntimeError("task bug")

    failed = asyncio.get_running_loop().create_task(boom(), name="paxton10 test")
    await asyncio.sleep(0)
    src._tasks.append(failed)
    await src.async_stop()  # logs the failed task instead of raising
    assert src._tasks == []
    await hass.config_entries.async_unload(entry.entry_id)


async def test_shutdown_closes_the_connection_even_if_stop_fails(
    hass: HomeAssistant, server: FakeServer, fast_sleep: list[float]
) -> None:
    from custom_components.paxton10.coordinator import Paxton10Coordinator

    entry = await setup(hass)
    coordinator: Paxton10Coordinator = entry.runtime_data
    src = live(entry)
    await src.async_stop()
    closed: list[bool] = []
    real_close = coordinator.conn.close

    async def close() -> None:
        closed.append(True)
        await real_close()

    async def broken_stop() -> None:
        raise RuntimeError("stop failed")

    with (
        patch.object(coordinator.conn, "close", close),
        patch.object(src, "async_stop", broken_stop),
        pytest.raises(RuntimeError),
    ):
        await coordinator.async_shutdown()
    assert closed == [True]


async def test_events_loop_survives_unexpected_errors(
    hass: HomeAssistant, server: FakeServer, hub: FakeHub, fast_sleep: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    entry = await setup(hass)
    src = live(entry)
    await src.async_stop()
    received: list[Any] = []

    async def cb(update: Any) -> None:
        received.append(update)

    src._callback = cb
    calls = {"hub": 0, "poll": 0}

    async def hub_down() -> Any:
        calls["hub"] += 1
        if calls["hub"] == 3:
            raise PaxtonAuthError("stop the test")
        raise PaxtonError("hub down")

    async def poll_events() -> None:
        calls["poll"] += 1
        raise TypeError("malformed row")  # a parser bug in the fallback poll

    real_poll_for = src._poll_for
    poll_for_calls = {"n": 0}

    async def poll_for(seconds: float) -> None:
        poll_for_calls["n"] += 1
        if poll_for_calls["n"] == 1:
            raise RuntimeError("bug in the fallback")  # escapes _poll_for itself
        await real_poll_for(seconds)

    with (
        patch.object(src._conn, "hub_target", hub_down),
        patch.object(src, "poll_events", poll_events),
        patch.object(src, "_poll_for", poll_for),
    ):
        await src._events_loop()
    # The loop carried on past both bugs, and only stopped on the auth failure.
    assert calls["hub"] == 3 and calls["poll"] >= 1
    assert "Unexpected error in the Paxton10 event loop" in caplog.text
    assert "Unexpected error reading the Paxton10 event log" in caplog.text
    errors = [type(u.error).__name__ for u in received if u.error]
    assert errors[-1] == "PaxtonAuthError" and "PaxtonError" in errors
    await hass.config_entries.async_unload(entry.entry_id)


async def test_failed_name_lookup_keeps_entities_available(
    hass: HomeAssistant, server: FakeServer, hub: FakeHub, fast_sleep: list[float]
) -> None:
    from custom_components.paxton10.const import OPT_INCLUDE_USER_NAMES

    entry = await setup(hass, {OPT_INCLUDE_USER_NAMES: True})
    src = live(entry)
    await until(lambda: src.mode == MODE_LIVE)
    fired = capture(hass)
    server.status[EVENTS] = 500
    hub.pushes.put_nowait([event(101, 5, user={"UserId": 7})])
    await until(lambda: len(fired) == 1, hass)
    assert fired[0].data.get("user_name") is None
    assert entry.runtime_data.last_update_success
    server.status.clear()
    await hass.config_entries.async_unload(entry.entry_id)


async def test_hub_allows_status_subscriptions_only() -> None:
    from custom_components.paxton10 import hub as hub_mod

    fake = FakeHub()
    client = await connected(fake)
    for method in (
        hub_mod.METHOD_SUBSCRIBE_DOOR_STATE,
        hub_mod.METHOD_UNSUBSCRIBE_DOOR_STATE,
        hub_mod.METHOD_SUBSCRIBE_DEVICE_STATUS,
        hub_mod.METHOD_UNSUBSCRIBE_DEVICE_STATUS,
        hub_mod.METHOD_SUBSCRIBE_BATTERY,
        hub_mod.METHOD_UNSUBSCRIBE_BATTERY,
    ):
        await client.invoke(method, [2001])
    assert len(fake.invoked) == 6
    # Every allowed method only subscribes or unsubscribes.
    assert all(m.startswith(("Subscribe", "Unsubscribe")) for m in hub_mod.HUB_METHOD_ALLOWLIST)


def test_door_state_read_is_allowed() -> None:
    from custom_components.paxton10.api import check_allowed

    check_allowed("POST", "/api/v1/Appliance/Connector/Status", allow_writes=False)
    with pytest.raises(PaxtonBlockedRequest):
        check_allowed("POST", "/api/v1/Appliance/Connector/Status/Set", allow_writes=False)


async def test_credential_type_only_with_user_names(
    hass: HomeAssistant, server: FakeServer, hub: FakeHub, fast_sleep: list[float]
) -> None:
    """Live fob events carry CredentialData. Only a credential type is passed on, and only with names on."""
    from custom_components.paxton10.const import OPT_INCLUDE_USER_NAMES

    fob = {
        **event(101, 5, user={"UserId": 7, "UserName": "Alex Smith"}),
        "CredentialData": {"CredentialId": 175, "Credential": " Keyfob ", "CredentialValue": "12345678", "UserId": 0},
    }
    for names, expected in ((True, "keyfob"), (False, None)):
        entry = await setup(hass, {OPT_INCLUDE_USER_NAMES: names})
        src = live(entry)
        await until(lambda: src.mode == MODE_LIVE)  # noqa: B023
        fired = capture(hass)
        hub.pushes.put_nowait([{**fob, "EventId": eid(101 if names else 102)}])
        await until(lambda: len(fired) == 1, hass)  # noqa: B023
        assert fired[0].data.get("credential") == expected
        state = hass.states.get("event.main_entrance_door")
        assert state and state.attributes.get("credential") == expected
        # The credential's own id and value never reach Home Assistant.
        assert "12345678" not in str(fired[0].data) and "175" not in str(state.attributes)
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.config_entries.async_remove(entry.entry_id)


async def test_stop_event_stops_updates_cleanly(
    hass: HomeAssistant, server: FakeServer, hub: FakeHub, fast_sleep: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    """Regression: on shutdown the long poll outlived Home Assistant's HTTP session and logged an error."""
    from homeassistant.const import EVENT_HOMEASSISTANT_STOP

    entry = await setup(hass)
    src = live(entry)
    await until(lambda: src.mode == MODE_LIVE)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    assert entry.runtime_data.source is None
    assert src._tasks == []
    assert hub.paths()[-1] == "abort"
    assert "Unexpected error" not in caplog.text
    # A later unload still works.
    assert await hass.config_entries.async_unload(entry.entry_id)



# Door lock state: polled with the devices, live on the hub.


def door_push(door: int, value: str) -> dict[str, Any]:
    return {"C": "cx", "M": [{"H": "System", "M": "applianceStateNotification", "A": [[
        {"StateValue": value, "Metric": 0, "EntityId": door, "Subnet": None}
    ]]}]}


async def test_door_lock_live(hass: HomeAssistant, server: FakeServer, hub: FakeHub, fast_sleep: list[float]) -> None:
    from homeassistant.const import (
        STATE_OFF,
        STATE_ON,
        STATE_UNAVAILABLE,
        STATE_UNKNOWN,
    )

    entry = await setup(hass)
    src = live(entry)
    await until(lambda: src.mode == MODE_LIVE)
    lock = "binary_sensor.main_entrance_door_lock"
    st = hass.states.get(lock)
    assert st and st.state == STATE_OFF and st.attributes["door_state"] == "locked"
    assert st.attributes["device_class"] == "lock"
    st = hass.states.get("binary_sensor.vehicle_gate_lock")
    assert st and st.state == STATE_ON

    for value, state, door_state in (
        ("1", STATE_ON, "unlocked"),
        ("3", STATE_ON, "forced_or_left_open"),
        ("5", STATE_UNKNOWN, "online"),
        ("9", STATE_UNKNOWN, "unknown"),
        ("4", STATE_UNAVAILABLE, None),
        ("2", STATE_OFF, "locked"),
    ):
        server.door_states[2001] = value  # the server reports what it pushed
        hub.pushes.put_nowait(door_push(2001, value))
        await until(
            lambda: (s := hass.states.get(lock)) is not None
            and s.state == state  # noqa: B023
            and (door_state is None or s.attributes.get("door_state") == door_state),  # noqa: B023
            hass,
        )
    # A push for one door leaves the others alone.
    assert hass.states.get("binary_sensor.vehicle_gate_lock").state == STATE_ON  # type: ignore[union-attr]
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["door_states"] == {2001: 2, 2002: 1} and diag["can_read_door_states"]
    await hass.config_entries.async_unload(entry.entry_id)


async def test_door_lock_polled_and_refused_push(
    hass: HomeAssistant, server: FakeServer, hub: FakeHub, fast_sleep: list[float]
) -> None:
    from homeassistant.const import STATE_ON

    hub.refuse.add("SubscribeToApplianceStateNotifications")
    entry = await setup(hass)
    src = live(entry)
    # The server refused live door state, but events are still live.
    await until(lambda: src.mode == MODE_LIVE)
    server.door_states[2001] = "1"
    await src.poll_devices()
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.main_entrance_door_lock").state == STATE_ON  # type: ignore[union-attr]
    # A server without the read stops being asked.
    server.status["/api/v1/Appliance/Connector/Status"] = 404
    await src.poll_devices()
    assert not src._site.can_read_door_states
    await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("status", [403, 404])
async def test_no_door_lock_without_the_read(hass: HomeAssistant, server: FakeServer, status: int) -> None:
    server.status["/api/v1/Appliance/Connector/Status"] = status
    entry = await setup(hass)
    assert hass.states.get("binary_sensor.main_entrance_door_lock") is None
    assert not entry.runtime_data.data.can_read_door_states
    await hass.config_entries.async_unload(entry.entry_id)


def test_door_state_parsing() -> None:
    from custom_components.paxton10.hub import door_state_rows
    from custom_components.paxton10.models import parse_door_states

    rows = [{"EntityId": 1, "StateValue": "2"}, {"EntityId": 2, "StateValue": 1}, {"EntityId": 3, "StateValue": "x"},
            {"EntityId": "4", "StateValue": "1"}, {"EntityId": 5}, "junk"]
    assert parse_door_states(rows) == {1: 2, 2: 1}
    assert parse_door_states({"not": "a list"}) == {}
    assert door_state_rows(HubMessage("System", "applianceStateNotification", [rows])) == rows[:5]
    assert door_state_rows(HubMessage("System", "applianceStateNotification", [{"EntityId": 1}])) == [{"EntityId": 1}]
    assert door_state_rows(HubMessage("System", "newLiveEventNotification", [rows])) == []
    assert door_state_rows(HubMessage("System", "applianceStateNotification", [])) == []


async def test_polled_door_state_never_overrides_a_newer_push(
    hass: HomeAssistant, server: FakeServer, fast_sleep: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = {"now": 100.0}
    monkeypatch.setattr(source_mod, "_monotonic", lambda: clock["now"])
    entry = await setup(hass)
    src = live(entry)
    await src.async_stop()
    received: list[Any] = []

    async def cb(update: Any) -> None:
        received.append(update)

    src._callback = cb
    real_read = source_mod.read_door_states

    async def slow_read(conn: Any, door_ids: list[int]) -> dict[int, int]:
        # A push for door 2001 lands while this read is in flight.
        clock["now"] = 105.0
        src._door_pushed_at[2001] = 105.0
        return await real_read(conn, door_ids)

    monkeypatch.setattr(source_mod, "read_door_states", slow_read)
    await src.poll_devices()
    assert received[-1].door_states == {2002: 1}  # 2001's polled state is older than its push
    await hass.config_entries.async_unload(entry.entry_id)


async def test_door_state_edges(hass: HomeAssistant, server: FakeServer, fast_sleep: list[float]) -> None:
    from custom_components.paxton10.discovery import read_door_states

    entry = await setup(hass)
    src = live(entry)
    await src.async_stop()
    calls = len(server.calls)
    assert await read_door_states(src._conn, []) == {}
    assert len(server.calls) == calls  # no doors, no read

    class Hub:
        def __init__(self, error: Exception | None = None) -> None:
            self.calls: list[str] = []
            self.error = error

        async def invoke(self, method: str, *args: Any) -> None:
            self.calls.append(method)
            if self.error:
                raise self.error

    # Without the read there's nothing to subscribe to.
    src._site.can_read_door_states = False
    skipped = Hub()
    await src._subscribe_door_states(skipped)  # type: ignore[arg-type]
    assert skipped.calls == []
    # A disconnect isn't swallowed: the hub cycle has to reconnect.
    src._site.can_read_door_states = True
    with pytest.raises(HubDisconnected):
        await src._subscribe_door_states(Hub(HubDisconnected("gone")))  # type: ignore[arg-type]
    await hass.config_entries.async_unload(entry.entry_id)


async def test_forced_or_left_open_sensor(
    hass: HomeAssistant, server: FakeServer, hub: FakeHub, fast_sleep: list[float]
) -> None:
    """Untested on a live site: state 3 is from the web app's enum. The sensor starts disabled."""
    from homeassistant.const import (
        STATE_OFF,
        STATE_ON,
        STATE_UNAVAILABLE,
        STATE_UNKNOWN,
    )
    from homeassistant.helpers import entity_registry as er

    entry = await setup(hass)
    registry = er.async_get(hass)
    alarm = "binary_sensor.main_entrance_door_forced_or_left_open"
    reg = registry.async_get(alarm)
    assert reg and reg.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert hass.states.get(alarm) is None

    registry.async_update_entity(alarm, disabled_by=None)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    src = live(entry)
    await until(lambda: src.mode == MODE_LIVE)
    st = hass.states.get(alarm)
    assert st and st.state == STATE_OFF and st.attributes["device_class"] == "problem"
    for value, state in (("3", STATE_ON), ("1", STATE_OFF), ("5", STATE_OFF), ("9", STATE_UNKNOWN), ("4", STATE_UNAVAILABLE)):
        server.door_states[2001] = value
        hub.pushes.put_nowait(door_push(2001, value))
        await until(lambda: (s := hass.states.get(alarm)) is not None and s.state == state, hass)  # noqa: B023
    await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(("names", "descriptions"), [(True, True), (False, True), (True, False)])
async def test_credential_description_option(
    hass: HomeAssistant, server: FakeServer, hub: FakeHub, fast_sleep: list[float], names: bool, descriptions: bool
) -> None:
    """The raw description only appears with its own option on, independent of user names."""
    from custom_components.paxton10.const import (
        OPT_INCLUDE_CREDENTIAL_DESCRIPTIONS,
        OPT_INCLUDE_USER_NAMES,
    )

    entry = await setup(hass, {OPT_INCLUDE_USER_NAMES: names, OPT_INCLUDE_CREDENTIAL_DESCRIPTIONS: descriptions})
    src = live(entry)
    await until(lambda: src.mode == MODE_LIVE)
    fired = capture(hass)
    fob = {
        **event(101, 5, user={"UserId": 7, "UserName": "Alex Smith"}),
        "CredentialData": {"CredentialId": 22, "Credential": "alex.smith@example.com", "CredentialValue": "12345678"},
    }
    hub.pushes.put_nowait([fob])
    await until(lambda: len(fired) == 1, hass)
    attrs = hass.states.get("event.main_entrance_door").attributes  # type: ignore[union-attr]
    expected = "alex.smith@example.com" if descriptions else None
    assert fired[0].data.get("credential_description") == expected
    assert attrs.get("credential_description") == expected
    # The email is never mistaken for a credential type, and the number never appears.
    assert fired[0].data.get("credential") is None and "credential" not in attrs
    assert "12345678" not in str(fired[0].data) and "12345678" not in str(attrs)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert "alex.smith@example.com" not in str(diag)
    await hass.config_entries.async_unload(entry.entry_id)
