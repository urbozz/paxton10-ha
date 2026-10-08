"""Signed-in connection to one Paxton10 site, with automatic re-sign-in and an optional fallback route.

No Home Assistant imports, so it can be tested and reused on its own.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import Any

import aiohttp

from .api import (
    DirectTransport,
    PaxtonAuthError,
    PaxtonBlockedRequest,
    PaxtonClient,
    PaxtonError,
    RemoteTransport,
    Response,
)
from .const import ROUTE_DIRECT, ROUTE_REMOTE

_LOGGER = logging.getLogger(__name__)


class PaxtonForbidden(PaxtonError):
    """The account isn't allowed to make this call (HTTP 403 or 405)."""


class PaxtonNotFound(PaxtonError):
    """HTTP 404."""


def create_client(session: aiohttp.ClientSession, route: str, target: str, allow_writes: bool) -> PaxtonClient:
    """Build a client for one route. Tests patch this."""
    if route == ROUTE_DIRECT:
        transport: DirectTransport | RemoteTransport = DirectTransport(session, target)
    elif route == ROUTE_REMOTE:
        transport = RemoteTransport(session, target)
    else:
        raise ValueError(f"unknown route {route!r}")
    return PaxtonClient(transport, allow_writes=allow_writes)


def other_route(route: str) -> str:
    return ROUTE_REMOTE if route == ROUTE_DIRECT else ROUTE_DIRECT


class PaxtonConnection:
    """Keeps one signed-in client. Signs in again on a 401 or a dropped connection."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        route: str,
        target: str,
        username: str,
        pw_hash: str,
        allow_writes: bool = False,
        fallback_target: str | None = None,
    ) -> None:
        self._session = session
        self._routes: list[tuple[str, str]] = [(route, target)]
        if fallback_target:
            self._routes.append((other_route(route), fallback_target))
        self._username = username
        self._pw_hash = pw_hash
        self.allow_writes = allow_writes
        self._client: PaxtonClient | None = None
        self.active_route: str | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        """Sign in on the first route that works. Auth errors are final: the password is wrong on every route."""
        async with self._lock:
            await self._connect_locked()

    async def _connect_locked(self) -> None:
        await self._drop()
        last: PaxtonError | None = None
        for route, target in self._routes:
            client = create_client(self._session, route, target, self.allow_writes)
            try:
                await client.sign_in_with_hash(self._username, self._pw_hash)
            except PaxtonAuthError:
                await client.close()
                raise
            except PaxtonError as err:
                await client.close()
                _LOGGER.warning("Paxton10 sign-in over %s failed: %s", route, err)
                last = err
                continue
            if route != self._routes[0][0]:
                _LOGGER.warning("Paxton10 is using the fallback route (%s)", route)
            self._client, self.active_route = client, route
            return
        raise last or PaxtonError("no route configured")

    async def try_primary(self) -> bool:
        """While on the fallback route, try the primary route on a separate client.

        The working fallback client stays in place unless the primary signs in. Returns True on a switch.
        """
        route, target = self._routes[0]
        if self.active_route in (None, route):
            return False
        probe = create_client(self._session, route, target, self.allow_writes)
        try:
            await probe.sign_in_with_hash(self._username, self._pw_hash)
        except PaxtonError as err:
            await probe.close()
            _LOGGER.debug("Paxton10 primary route (%s) still unavailable: %s", route, err)
            return False
        async with self._lock:
            old, self._client, self.active_route = self._client, probe, route
        if old:
            with contextlib.suppress(Exception):
                await old.close()
        _LOGGER.info("Paxton10 is back on the %s route", route)
        return True

    async def _drop(self) -> None:
        if self._client:
            with contextlib.suppress(Exception):
                await self._client.close()
        self._client, self.active_route = None, None

    async def close(self) -> None:
        async with self._lock:
            await self._drop()

    @property
    def session(self) -> aiohttp.ClientSession:
        return self._session

    async def hub_target(self) -> tuple[str, Callable[[], str | None]] | None:
        """The live hub's base URL and a token getter, or None when the active route isn't Direct.

        Signs in first if needed. The getter reads the current token on every call, so the hub
        picks up a new token after a re-sign-in without reconnecting.
        """
        async with self._lock:
            if self._client is None:
                await self._connect_locked()
            client = self._client
        assert client
        if self.active_route != ROUTE_DIRECT or not isinstance(client.transport, DirectTransport):
            return None

        def token() -> str | None:
            return self._client.token if self._client else None

        return client.transport.base_url, token

    async def remote_hub(self) -> tuple[RemoteTransport, Callable[[], str | None]] | None:
        """The relay transport and a token getter for live pushes on Remote, or None off Remote.

        Signs in first if needed. Pushes ride the REST socket, so a reconnect of the client means
        a new transport: the old one closes, and the live feed subscribes again on the new one.
        """
        async with self._lock:
            if self._client is None:
                await self._connect_locked()
            client = self._client
        assert client
        if self.active_route != ROUTE_REMOTE or not isinstance(client.transport, RemoteTransport):
            return None

        def token() -> str | None:
            return self._client.token if self._client else None

        return client.transport, token

    async def renew(self) -> None:
        """Drop the client, so the next call signs in again. For a token the hub rejected."""
        async with self._lock:
            await self._drop()

    async def get(self, path: str) -> Any:
        return await self._request("GET", path, None)

    async def post(self, path: str, data: Any) -> Any:
        return await self._request("POST", path, data)

    async def _request(self, method: str, path: str, data: Any) -> Any:
        """Send one call and return the parsed body. Retries once after signing in again."""
        for attempt in (1, 2):
            async with self._lock:
                if self._client is None:
                    await self._connect_locked()
                client = self._client
            assert client
            try:
                resp: Response = await (client.get(path) if method == "GET" else client.post(path, data))
            except PaxtonBlockedRequest:
                raise
            except PaxtonAuthError:
                # Token expired or rejected. Sign in again once; a second 401 is real.
                if attempt == 2:
                    raise
                async with self._lock:
                    if self._client is client:
                        await self._drop()
                continue
            except PaxtonError:
                # Dropped relay or network error. Reconnect on the next call.
                async with self._lock:
                    if self._client is client:
                        await self._drop()
                raise
            if resp.status in (403, 405):
                raise PaxtonForbidden(f"{method} {path}: HTTP {resp.status}")
            if resp.status == 404:
                raise PaxtonNotFound(f"{method} {path}: HTTP 404")
            if resp.status >= 400:
                raise PaxtonError(f"{method} {path}: HTTP {resp.status}")
            return resp.body
        raise PaxtonError("unreachable")  # pragma: no cover
