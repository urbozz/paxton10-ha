"""Paxton10 API client with two transports.

DirectTransport: HTTPS to the Paxton10 server on the site network (self-signed certificate).
RemoteTransport: Paxton's remote access service (p10remote.com). The web UI tunnels every API
call over an Azure SignalR hub as a "UiApiRequest" message, and replies arrive as
"CloudApiResponse" messages matched by messageId.

No Home Assistant imports, so tools can use this module directly.

Safety: the client refuses DELETE outright (DELETE /api/v2/Events/All wipes the event log),
and only sends POST or PUT to paths on WRITE_ALLOWLIST or READ_POSTS.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import aiohttp

_LOGGER = logging.getLogger(__name__)

RECORD_SEPARATOR = "\x1e"
NEGOTIATE_URL = "https://negotiate.p10remote.com/api/negotiateclient?remoteId={remote_id}"

# POSTs that only read (query endpoints).
READ_POSTS = (
    re.compile(r"^/token$"),
    re.compile(r"^/api/v2/Events/\?page=\d+(&pageSize=\d+)?$"),
    re.compile(r"^/api/v1/Events/Type/CountByDay$"),
)
# POSTs that change something. Each one is deliberate.
WRITE_ALLOWLIST = (
    re.compile(r"^/api/v2/System/ActivateAppliances$"),  # open door / outputs
)


class PaxtonError(Exception):
    """Any API failure."""


class PaxtonAuthError(PaxtonError):
    """Sign-in rejected or token expired."""


class PaxtonBlockedRequest(PaxtonError):
    """The client refused to send a request that isn't on an allowlist."""


@dataclass
class Response:
    status: int
    body: Any  # parsed JSON when possible, else text


def password_hash(password: str) -> str:
    """The web app sends SHA-1 of the UTF-8 password as lowercase hex, not the password itself."""
    return hashlib.sha1(password.encode("utf-8")).hexdigest()


def check_allowed(method: str, path: str, allow_writes: bool) -> None:
    method = method.upper()
    if method == "GET":
        return
    if method == "DELETE":
        raise PaxtonBlockedRequest(f"DELETE is never sent ({path})")
    if any(p.match(path) for p in READ_POSTS):
        return
    if allow_writes and any(p.match(path) for p in WRITE_ALLOWLIST):
        return
    raise PaxtonBlockedRequest(f"{method} {path} is not on the allowlist (writes {'on' if allow_writes else 'off'})")


def _parse(text: str) -> Any:
    try:
        return json.loads(text) if text else None
    except ValueError:
        return text


class DirectTransport:
    """HTTPS straight to the server, for example https://192.0.2.10."""

    name = "direct"

    def __init__(self, session: aiohttp.ClientSession, host: str) -> None:
        self._session = session
        self._base = f"https://{host}"

    @property
    def base_url(self) -> str:
        """For the live hub, which runs on the same server."""
        return self._base

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def send(self, method: str, path: str, token: str | None, body: str | None, content_type: str) -> Response:
        headers = {"Accept": "application/json", "DmsLanguage": "en-GB"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if body is not None:
            headers["Content-Type"] = content_type
        try:
            async with self._session.request(
                method,
                self._base + path,
                data=body,
                headers=headers,
                ssl=False,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                return Response(resp.status, _parse(await resp.text()))
        except asyncio.TimeoutError as err:
            raise PaxtonError(f"{method} {path}: no reply from {self._base} within 20 s") from err
        except aiohttp.ClientError as err:
            # Some aiohttp errors have an empty str(), so always name the type.
            raise PaxtonError(f"{method} {path}: {type(err).__name__}: {err}".rstrip(": ")) from err


class RemoteTransport:
    """Paxton remote access: SignalR hub at hub.p10remote.com, routed to the site by remote ID."""

    name = "remote"

    def __init__(self, session: aiohttp.ClientSession, remote_id: str) -> None:
        self._session = session
        self._remote_id = remote_id
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._reader: asyncio.Task[None] | None = None
        self._pending: dict[str, asyncio.Future[Response]] = {}
        self._invocation = 0
        self._open = False

    async def start(self) -> None:
        timeout = aiohttp.ClientTimeout(total=20)
        async with self._session.get(NEGOTIATE_URL.format(remote_id=self._remote_id), timeout=timeout) as resp:
            if resp.status != 200:
                raise PaxtonError(f"remote negotiate failed: HTTP {resp.status}")
            info = await resp.json(content_type=None)
        hub_url, access_token = info["url"], info["accessToken"]
        auth = {"Authorization": f"Bearer {access_token}"}

        base, _, query = hub_url.partition("?")
        negotiate = f"{base.rstrip('/')}/negotiate?{query}&negotiateVersion=1"
        async with self._session.post(negotiate, headers=auth, timeout=timeout) as resp:
            if resp.status != 200:
                raise PaxtonError(f"hub negotiate failed: HTTP {resp.status}")
            neg = await resp.json(content_type=None)
        connection = neg.get("connectionToken") or neg.get("connectionId")

        ws_url = hub_url.replace("https://", "wss://", 1) + "&" + urlencode({"id": connection})
        self._ws = await self._session.ws_connect(
            ws_url, headers=auth, heartbeat=15, timeout=aiohttp.ClientWSTimeout(ws_receive=60)
        )
        await self._ws.send_str(json.dumps({"protocol": "json", "version": 1}) + RECORD_SEPARATOR)
        handshake = await self._ws.receive(timeout=10)
        if handshake.type != aiohttp.WSMsgType.TEXT or handshake.data.strip(RECORD_SEPARATOR) not in ("{}",):
            raise PaxtonError(f"hub handshake failed: {handshake.data!r}"[:200])
        self._open = True
        self._reader = asyncio.create_task(self._read())

    async def close(self) -> None:
        if self._reader:
            self._reader.cancel()
            # A reader that already died still gets the cleanup below.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader
            self._reader = None
        self._fail_pending()
        if self._ws and not self._ws.closed:
            await self._ws.close()

    def _fail_pending(self) -> None:
        self._open = False  # send() refuses until start() runs again
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(PaxtonError("remote connection closed"))
        self._pending.clear()

    async def _read(self) -> None:
        assert self._ws
        try:
            await self._read_frames()
        except Exception as err:  # noqa: BLE001  # receive timeout, dropped connection, or a malformed frame
            _LOGGER.debug("Remote relay connection lost: %s", err)
        finally:
            # Fail waiting calls first, so they don't wait for the websocket close handshake.
            self._fail_pending()
        if not self._ws.closed:
            await self._ws.close()

    async def _read_frames(self) -> None:
        assert self._ws
        async for msg in self._ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    return
                continue
            for frame in filter(None, msg.data.split(RECORD_SEPARATOR)):
                try:
                    data = json.loads(frame)
                except ValueError:
                    continue
                if data.get("type") == 6:  # ping
                    await self._ws.send_str(json.dumps({"type": 6}) + RECORD_SEPARATOR)
                elif data.get("type") == 1 and data.get("target") == "CloudApiResponse":
                    try:
                        reply = json.loads(data["arguments"][0])
                        message_id = reply.get("messageId")
                    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
                        _LOGGER.debug("Ignoring a malformed CloudApiResponse")
                        continue
                    fut = self._pending.pop(message_id, None)
                    if fut and not fut.done():
                        fut.set_result(Response(int(reply.get("statusCode") or 0), _parse(reply.get("payload"))))
                elif data.get("type") == 7:  # server closing
                    return

    async def send(self, method: str, path: str, token: str | None, body: str | None, content_type: str) -> Response:
        if not self._ws or self._ws.closed or not self._open:
            raise PaxtonError("remote connection is not open")
        message_id = f"{path}-{uuid.uuid4()}"
        inner: dict[str, Any] = {
            "url": path,
            "verb": method.upper(),
            "contentType": content_type,
            "bearerToken": f"Bearer {token}" if token else "",
            "remoteId": self._remote_id,
            "messageId": message_id,
        }
        if body is not None:
            inner["payload"] = body
        self._invocation += 1
        fut = asyncio.get_running_loop().create_future()
        self._pending[message_id] = fut
        envelope = {
            "arguments": [json.dumps(inner)],
            "invocationId": str(self._invocation),
            "target": "UiApiRequest",
            "type": 1,
        }
        await self._ws.send_str(json.dumps(envelope) + RECORD_SEPARATOR)
        try:
            return await asyncio.wait_for(fut, timeout=20)
        except asyncio.TimeoutError as err:
            self._pending.pop(message_id, None)
            raise PaxtonError(f"{method} {path}: no reply from the site within 20 s") from err


class PaxtonClient:
    """Signs in and makes API calls over either transport."""

    def __init__(self, transport: DirectTransport | RemoteTransport, allow_writes: bool = False) -> None:
        self.transport = transport
        self.allow_writes = allow_writes
        self._token: str | None = None
        self.token_expires_in: int | None = None

    @property
    def token(self) -> str | None:
        """The bearer token from the last sign-in. The live hub passes it on its query string."""
        return self._token

    async def sign_in(self, username: str, password: str) -> None:
        await self.sign_in_with_hash(username, password_hash(password))

    async def sign_in_with_hash(self, username: str, pw_hash: str) -> None:
        """Sign in with a precomputed password_hash(), so callers never have to keep the password."""
        await self.transport.close()
        await self.transport.start()
        form = urlencode({"grant_type": "password", "username": username, "password": pw_hash})
        resp = await self._send("POST", "/token", form, "application/x-www-form-urlencoded", authed=False)
        body = resp.body if isinstance(resp.body, dict) else {}
        if resp.status != 200 or "access_token" not in body:
            raise PaxtonAuthError(body.get("error_description") or body.get("error") or f"HTTP {resp.status}")
        self._token = body["access_token"]
        self.token_expires_in = body.get("expires_in")

    async def close(self) -> None:
        await self.transport.close()

    async def get(self, path: str) -> Response:
        return await self._send("GET", path, None, "application/json; charset=utf-8")

    async def post(self, path: str, data: Any) -> Response:
        return await self._send("POST", path, json.dumps(data), "application/json; charset=utf-8")

    async def _send(self, method: str, path: str, body: str | None, content_type: str, authed: bool = True) -> Response:
        check_allowed(method, path, self.allow_writes)
        resp = await self.transport.send(method, path, self._token if authed else None, body, content_type)
        if authed and resp.status == 401:
            raise PaxtonAuthError(f"{method} {path}: token rejected")
        return resp
