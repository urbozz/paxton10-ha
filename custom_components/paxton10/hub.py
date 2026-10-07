"""Live event hub: ASP.NET SignalR 2.x over long polling, as the Paxton10 web UI uses it.

The local web UI starts the jQuery SignalR client with transport "longPolling" only, on
/signalr, with the bearer token on the query string and one hub, "System". The protocol:

1. GET /signalr/negotiate returns the connection token and the timeouts.
2. GET /signalr/connect returns at once with the first message id ("S": 1).
3. GET /signalr/start confirms the transport.
4. POST /signalr/send invokes a hub method, for example SubscribeToLiveEvents(filter).
5. GET /signalr/poll is held open by the server until there are messages or it times out.
   Each reply carries the next message id ("C"). Repeat.

No Home Assistant imports, so tools can use this module directly.

Safety: invoke() only sends the hub methods on HUB_METHOD_ALLOWLIST, all of which subscribe
to or unsubscribe from notifications. Nothing here can change the site.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import aiohttp

from .api import PaxtonAuthError, PaxtonBlockedRequest, PaxtonError

_LOGGER = logging.getLogger(__name__)

HUB_NAME = "System"
CLIENT_PROTOCOL = "2.1"
CONNECTION_DATA = json.dumps([{"name": HUB_NAME.lower()}], separators=(",", ":"))
DEFAULT_POLL_TIMEOUT = 110  # seconds; negotiate's ConnectionTimeout replaces it
REQUEST_TIMEOUT = 20

METHOD_SUBSCRIBE_EVENTS = "SubscribeToLiveEvents"
METHOD_UNSUBSCRIBE_EVENTS = "UnsubscribeFromLiveEvents"
NOTIFY_EVENTS = "newLiveEventNotification"
HUB_METHOD_ALLOWLIST = frozenset({METHOD_SUBSCRIBE_EVENTS, METHOD_UNSUBSCRIBE_EVENTS})


class HubDisconnected(PaxtonError):
    """The server ended the connection, or asked the client to reconnect."""


@dataclass(frozen=True)
class HubMessage:
    """One server push: a hub method name and its arguments."""

    hub: str
    method: str
    args: list[Any]


class LongPollHub:
    """One SignalR 2.x connection to the local Paxton10 server."""

    def __init__(
        self, session: aiohttp.ClientSession, base_url: str, token: Callable[[], str | None]
    ) -> None:
        self._session = session
        self._base = base_url.rstrip("/")
        self._token = token  # read on every request, so a renewed token is used at once
        self._connection_token: str | None = None
        self._message_id: str | None = None
        self._groups_token: str | None = None
        self._poll_timeout: float = DEFAULT_POLL_TIMEOUT
        self._poll_delay: float = 0
        self._invocation = 0

    @property
    def connected(self) -> bool:
        return self._connection_token is not None

    def _query(self, **extra: str) -> dict[str, str]:
        query = {"clientProtocol": CLIENT_PROTOCOL, "connectionData": CONNECTION_DATA}
        if self._connection_token is not None:
            query["transport"] = "longPolling"
            query["connectionToken"] = self._connection_token
        token = self._token()
        if token:
            query["bearer_token"] = token
        query.update(extra)
        return query

    async def _request(
        self, method: str, path: str, query: dict[str, str], timeout: float = REQUEST_TIMEOUT, body: str | None = None
    ) -> Any:
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        try:
            async with self._session.request(
                method,
                f"{self._base}/signalr/{path}",
                params=query,
                data=body,
                headers=headers,
                ssl=False,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                text = await resp.text()
                status = resp.status
        except asyncio.TimeoutError as err:
            raise PaxtonError(f"hub {path}: no reply from {self._base} within {timeout:.0f} s") from err
        except aiohttp.ClientError as err:
            raise PaxtonError(f"hub {path}: {type(err).__name__}: {err}".rstrip(": ")) from err
        if status == 401:
            raise PaxtonAuthError(f"hub {path}: token rejected")
        if status >= 400:
            raise PaxtonError(f"hub {path}: HTTP {status}")
        try:
            return json.loads(text) if text else {}
        except ValueError as err:
            raise PaxtonError(f"hub {path}: reply isn't JSON") from err

    async def connect(self) -> None:
        """Negotiate, connect, and start. Raises PaxtonError, or PaxtonAuthError on a rejected token."""
        self._reset()
        info = await self._request("GET", "negotiate", self._query())
        token = info.get("ConnectionToken") if isinstance(info, dict) else None
        if not isinstance(token, str) or not token:
            raise PaxtonError("hub negotiate: no connection token")
        timeout = info.get("ConnectionTimeout")
        if isinstance(timeout, (int, float)) and timeout > 0:
            self._poll_timeout = float(timeout)
        delay = info.get("LongPollDelay")
        self._poll_delay = float(delay) if isinstance(delay, (int, float)) and delay > 0 else 0
        self._connection_token = token
        first = await self._request("GET", "connect", self._query(), timeout=self._poll_timeout + REQUEST_TIMEOUT)
        self._absorb(first)
        started = await self._request("GET", "start", self._query())
        if not isinstance(started, dict) or started.get("Response") != "started":
            raise PaxtonError(f"hub start: unexpected reply {str(started)[:80]}")

    async def invoke(self, method: str, *args: Any) -> Any:
        """Call a hub method and return its result. Only methods on HUB_METHOD_ALLOWLIST are sent."""
        if method not in HUB_METHOD_ALLOWLIST:
            raise PaxtonBlockedRequest(f"hub method {method} is not on the allowlist")
        if not self.connected:
            raise HubDisconnected("hub is not connected")
        invocation = str(self._invocation)
        self._invocation += 1
        data = json.dumps({"H": HUB_NAME.lower(), "M": method, "A": list(args), "I": invocation})
        reply = await self._request("POST", "send", self._query(), body=urlencode({"data": data}))
        if isinstance(reply, dict) and reply.get("E"):
            raise PaxtonError(f"hub {method}: {str(reply['E'])[:200]}")
        return reply.get("R") if isinstance(reply, dict) else None

    async def poll(self) -> list[HubMessage]:
        """Wait for the next batch of server pushes. An empty list means the poll timed out normally."""
        if not self.connected:
            raise HubDisconnected("hub is not connected")
        if self._poll_delay:
            await asyncio.sleep(self._poll_delay / 1000)
        extra = {"messageId": self._message_id or ""}
        if self._groups_token:
            extra["groupsToken"] = self._groups_token
        reply = await self._request(
            "GET", "poll", self._query(**extra), timeout=self._poll_timeout + REQUEST_TIMEOUT
        )
        return self._absorb(reply)

    def _absorb(self, reply: Any) -> list[HubMessage]:
        """Take the cursor and groups token from a connect or poll reply, and return its messages."""
        if not isinstance(reply, dict):
            return []
        if isinstance(reply.get("C"), str):
            self._message_id = reply["C"]
        if isinstance(reply.get("G"), str):
            self._groups_token = reply["G"]
        if isinstance(reply.get("L"), (int, float)):
            self._poll_delay = float(reply["L"])
        if reply.get("D") == 1:
            self._reset()
            raise HubDisconnected("hub: the server ended the connection")
        if reply.get("T") == 1:
            self._reset()
            raise HubDisconnected("hub: the server asked the client to reconnect")
        messages: list[HubMessage] = []
        for raw in reply.get("M") or []:
            if isinstance(raw, dict) and isinstance(raw.get("M"), str):
                args = raw.get("A")
                messages.append(HubMessage(str(raw.get("H") or ""), raw["M"], args if isinstance(args, list) else []))
        return messages

    async def close(self) -> None:
        """Tell the server the connection is gone. Best effort: the server times it out anyway."""
        if self.connected:
            try:
                await self._request("POST", "abort", self._query(), timeout=5)
            except PaxtonError as err:
                _LOGGER.debug("Hub abort failed: %s", err)
        self._reset()

    def _reset(self) -> None:
        self._connection_token = None
        self._message_id = None
        self._groups_token = None


def event_rows(message: HubMessage) -> list[dict[str, Any]]:
    """The event rows in a newLiveEventNotification push, newest first like the event log page."""
    if message.method.lower() != NOTIFY_EVENTS.lower() or not message.args:
        return []
    first = message.args[0]
    rows = first if isinstance(first, list) else message.args
    return [r for r in rows if isinstance(r, dict)]
