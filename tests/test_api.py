"""The vendored client: allowlist, password hash, and both transports, with no network."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Self
from unittest.mock import MagicMock

import aiohttp
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.paxton10.api import (
    RECORD_SEPARATOR,
    DirectTransport,
    PaxtonAuthError,
    PaxtonBlockedRequest,
    PaxtonClient,
    PaxtonError,
    RemoteTransport,
    check_allowed,
    password_hash,
)
from custom_components.paxton10.connection import create_client

RELEASE = "/api/v2/System/ActivateAppliances"
PASSWORD_SHA1 = "5baa61e4c9b93f3f0682250b6cf8331b7ee68fd8"  # gitleaks:allow (SHA-1 of "password")


def test_password_hash() -> None:
    # SHA-1 of the UTF-8 bytes, lowercase hex.
    assert password_hash("password") == PASSWORD_SHA1
    assert len(password_hash("pässwörd")) == 40


@pytest.mark.parametrize("allow_writes", [False, True])
@pytest.mark.parametrize("path", ["/api/v2/Events/All", RELEASE, "/api/v1/User/1"])
def test_delete_is_never_sent(path: str, allow_writes: bool) -> None:
    with pytest.raises(PaxtonBlockedRequest, match="DELETE is never sent"):
        check_allowed("DELETE", path, allow_writes)


def test_allowlist() -> None:
    check_allowed("GET", "/anything", False)
    check_allowed("POST", "/token", False)
    check_allowed("POST", "/api/v2/Events/?page=0&pageSize=50", False)
    with pytest.raises(PaxtonBlockedRequest):
        check_allowed("POST", RELEASE, False)
    check_allowed("POST", RELEASE, True)
    # Writes that look like reads stay blocked even with door control on.
    for method, path in (
        ("POST", "/api/v2/device/states"),
        ("PUT", "/api/v1/User"),
        ("POST", "/api/v2/Events/?page=0;x"),
    ):
        with pytest.raises(PaxtonBlockedRequest):
            check_allowed(method, path, True)


async def test_direct_transport(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.post("https://192.0.2.1/token", json={"access_token": "abc", "expires_in": 43199})
    aioclient_mock.get("https://192.0.2.1/api/v1/System/Software/Version", text='"4.11"')
    aioclient_mock.get("https://192.0.2.1/plain", text="not json")
    aioclient_mock.get("https://192.0.2.1/expired", status=401)
    aioclient_mock.get("https://192.0.2.1/broken", exc=aiohttp.ClientError("boom"))
    aioclient_mock.get("https://192.0.2.1/silent", exc=aiohttp.ClientError())
    aioclient_mock.get("https://192.0.2.1/slow", exc=TimeoutError())
    client = create_client(async_get_clientsession(hass), "direct", "192.0.2.1", False)
    await client.sign_in("user@example.com", "password")
    assert client.token_expires_in == 43199
    form = aioclient_mock.mock_calls[0][2]
    assert f"password={PASSWORD_SHA1}" in form and "password=password" not in form

    resp = await client.get("/api/v1/System/Software/Version")
    assert (resp.status, resp.body) == (200, "4.11")
    assert aioclient_mock.mock_calls[1][3]["Authorization"] == "Bearer abc"
    assert (await client.get("/plain")).body == "not json"
    with pytest.raises(PaxtonAuthError):
        await client.get("/expired")
    with pytest.raises(PaxtonError, match="ClientError: boom"):
        await client.get("/broken")
    # An exception with an empty str() still says what failed.
    with pytest.raises(PaxtonError, match=r"GET /silent: ClientError$"):
        await client.get("/silent")
    with pytest.raises(PaxtonError, match=r"no reply from https://192\.0\.2\.1 within 20 s"):
        await client.get("/slow")
    await client.close()


async def test_direct_sign_in_rejected(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.post("https://192.0.2.1/token", status=400, json={"error": "AuthenticationFailedInvalidCredentials"})
    client = PaxtonClient(DirectTransport(async_get_clientsession(hass), "192.0.2.1"))
    with pytest.raises(PaxtonAuthError, match="AuthenticationFailedInvalidCredentials"):
        await client.sign_in("u", "p")


def test_create_client_routes() -> None:
    session = MagicMock()
    assert create_client(session, "remote", "abc123", True).transport.name == "remote"
    assert create_client(session, "remote", "abc123", True).allow_writes
    with pytest.raises(ValueError):
        create_client(session, "carrier-pigeon", "x", False)


class FakeWS:
    """A SignalR JSON-protocol websocket that answers UiApiRequest like the Paxton relay."""

    def __init__(self, handshake: str = "{}" + RECORD_SEPARATOR, reply: bool = True) -> None:
        self.sent: list[dict[str, Any]] = []
        self.closed = False
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._handshake = handshake
        self._reply = reply
        self.hub_error: str | None = None  # the relay's answer to a UiSignalrMessage call

    def _msg(self, data: str, kind: aiohttp.WSMsgType = aiohttp.WSMsgType.TEXT) -> Any:
        return MagicMock(type=kind, data=data)

    async def receive(self, timeout: float | None = None) -> Any:
        return self._msg(self._handshake)

    async def send_str(self, data: str) -> None:
        frame = json.loads(data.rstrip(RECORD_SEPARATOR))
        self.sent.append(frame)
        if frame.get("target") == "UiSignalrMessage" and self._reply:
            done = {"type": 3, "invocationId": frame["invocationId"]}
            done.update({"error": self.hub_error} if self.hub_error else {"result": None})
            await self._queue.put(self._msg(json.dumps(done) + RECORD_SEPARATOR))
            return
        if frame.get("target") != "UiApiRequest" or not self._reply:
            return
        inner = json.loads(frame["arguments"][0])
        status, payload = (200, json.dumps({"access_token": "tok"})) if inner["url"] == "/token" else (200, '"4.11"')
        reply = json.dumps({"payload": payload, "statusCode": status, "messageId": inner["messageId"]})
        # A ping, junk, an unrelated message, then the reply, in one websocket message.
        batch = [
            json.dumps({"type": 6}),
            "not json",
            json.dumps({"type": 1, "target": "Other", "arguments": []}),
            json.dumps({"type": 1, "target": "CloudApiResponse", "arguments": [reply]}),
        ]
        await self._queue.put(self._msg(RECORD_SEPARATOR.join(batch) + RECORD_SEPARATOR))

    def push(self, kind: aiohttp.WSMsgType, data: str = "") -> None:
        self._queue.put_nowait(self._msg(data, kind))

    def __aiter__(self) -> FakeWS:
        return self

    async def __anext__(self) -> Any:
        msg = await self._queue.get()
        if msg is None:
            raise StopAsyncIteration
        return msg

    async def close(self) -> None:
        self.closed = True
        self._queue.put_nowait(None)


class FakeResp:
    def __init__(self, status: int, body: dict[str, Any]) -> None:
        self.status, self._body = status, body

    async def json(self, content_type: Any = None) -> dict[str, Any]:
        return self._body

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


def fake_session(ws: FakeWS, negotiate: int = 200, hub: int = 200) -> MagicMock:
    session = MagicMock()
    session.get = MagicMock(
        return_value=FakeResp(negotiate, {"url": "https://hub.example/client/?hub=x", "accessToken": "at"})
    )
    session.post = MagicMock(return_value=FakeResp(hub, {"connectionToken": "ct"}))

    async def ws_connect(url: str, **kwargs: Any) -> FakeWS:
        assert url == "wss://hub.example/client/?hub=x&id=ct"
        return ws

    session.ws_connect = ws_connect
    return session


async def test_remote_transport() -> None:
    ws = FakeWS()
    client = PaxtonClient(RemoteTransport(fake_session(ws), "abc123"))
    await client.sign_in("u", "p")
    assert (await client.get("/api/v1/System/Software/Version")).body == "4.11"
    assert ws.sent[0] == {"protocol": "json", "version": 1}
    request = json.loads([f for f in ws.sent if f.get("target") == "UiApiRequest"][-1]["arguments"][0])
    assert request["bearerToken"] == "Bearer tok" and request["remoteId"] == "abc123" and "payload" not in request
    assert {"type": 6} in ws.sent  # answered the ping
    await client.close()
    assert ws.closed
    with pytest.raises(PaxtonError, match="not open"):
        await client.get("/x")


@pytest.mark.parametrize(
    ("negotiate", "hub", "handshake", "match"),
    [
        (500, 200, "{}", "remote negotiate failed"),
        (200, 500, "{}", "hub negotiate failed"),
        (200, 200, '{"error":"nope"}', "hub handshake failed"),
    ],
)
async def test_remote_start_failures(negotiate: int, hub: int, handshake: str, match: str) -> None:
    transport = RemoteTransport(fake_session(FakeWS(handshake=handshake), negotiate, hub), "abc123")
    with pytest.raises(PaxtonError, match=match):
        await transport.start()


@pytest.mark.parametrize("kind", [aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.TEXT])
async def test_remote_connection_lost(kind: aiohttp.WSMsgType) -> None:
    ws = FakeWS(reply=False)
    transport = RemoteTransport(fake_session(ws), "abc123")
    await transport.start()
    pending = asyncio.ensure_future(transport.send("GET", "/x", "tok", None, "application/json"))
    await asyncio.sleep(0)
    ws.push(aiohttp.WSMsgType.BINARY)  # ignored
    ws.push(kind, json.dumps({"type": 7}) + RECORD_SEPARATOR)  # closed, or the server's close message
    with pytest.raises(PaxtonError, match="closed"):
        await pending
    await transport.close()


async def test_remote_close_fails_pending() -> None:
    ws = FakeWS(reply=False)
    transport = RemoteTransport(fake_session(ws), "abc123")
    await transport.start()
    pending = asyncio.ensure_future(transport.send("POST", "/x", "tok", "{}", "application/json"))
    await asyncio.sleep(0)
    assert json.loads(ws.sent[-1]["arguments"][0])["payload"] == "{}"
    await transport.close()
    with pytest.raises(PaxtonError, match="closed"):
        await pending


async def test_remote_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    ws = FakeWS(reply=False)
    transport = RemoteTransport(fake_session(ws), "abc123")
    await transport.start()

    async def no_reply(fut: Any, timeout: float) -> Any:
        raise TimeoutError

    monkeypatch.setattr("custom_components.paxton10.api.asyncio.wait_for", no_reply)
    with pytest.raises(PaxtonError, match="no reply"):
        await transport.send("GET", "/x", None, None, "application/json")
    assert transport._pending == {}
    await transport.close()


@pytest.mark.parametrize(
    ("break_it", "match"),
    [
        ("client_error", "remote connect: ClientConnectionError: refused"),
        ("timeout", "remote connect: no reply within 20 s"),
        ("no_url", "remote connect: KeyError: 'url'"),
    ],
)
async def test_remote_start_wraps_network_errors(break_it: str, match: str) -> None:
    """Regression: a timeout or client error on Remote escaped as a non-PaxtonError and could end the events task."""
    session = fake_session(FakeWS())
    if break_it == "client_error":
        session.get = MagicMock(side_effect=aiohttp.ClientConnectionError("refused"))
    elif break_it == "timeout":
        session.get = MagicMock(side_effect=TimeoutError())
    else:
        session.get = MagicMock(return_value=FakeResp(200, {"accessToken": "at"}))
    with pytest.raises(PaxtonError, match=match):
        await RemoteTransport(session, "abc123").start()
