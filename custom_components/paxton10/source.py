"""Update sources: PollingSource polls everything; LiveSource takes events from the live hub.

Entities never talk to a source. The coordinator receives SourceUpdate objects and
entities read the coordinator's data, so swapping the source changes no entity.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .api import PaxtonAuthError, PaxtonError
from .connection import PaxtonConnection, PaxtonForbidden
from .const import EVENT_PAGE_SIZE
from .discovery import read_devices, read_summary
from .hub import METHOD_SUBSCRIBE_EVENTS, HubUnauthorized, LongPollHub, event_rows
from .models import (
    Device,
    DoorEvent,
    Site,
    event_filter,
    live_event_filter,
    name_hardware,
    parse_event,
)

_LOGGER = logging.getLogger(__name__)

MAX_BACKOFF = 300
SEEN_EVENT_IDS = 1000  # well over one page, so an event never comes back as new
EVENTS_PATH = f"/api/v2/Events/?page=0&pageSize={EVENT_PAGE_SIZE}"
RECONCILE_INTERVAL = 300  # while live, also read the event log this often, in case a push was lost
NOT_DIRECT_RECHECK = 300  # while not on Direct, poll for this long before checking the route again
_sleep = asyncio.sleep  # tests replace this, not asyncio.sleep itself
_monotonic = time.monotonic  # and this

MODE_LIVE = "live"
MODE_POLLING = "polling"
MODE_STARTING = "starting"
NO_TIME = datetime(2000, 1, 1, tzinfo=timezone.utc)  # sorts events without a time as oldest

KIND_DEVICES = "devices"
KIND_EVENTS = "events"


@dataclass
class SourceUpdate:
    """What a source delivers. None means 'no change to this part'.

    kind names the read that produced it, so one read's success never clears another's failure.
    """

    kind: str
    devices: dict[int, Device] | None = None
    summary: dict[str, int] | None = None
    events: list[DoorEvent] = field(default_factory=list)
    error: PaxtonError | None = None


UpdateCallback = Callable[[SourceUpdate], Awaitable[None]]


class UpdateSource(ABC):
    """Delivers device state and new events to a callback."""

    @abstractmethod
    async def async_start(self, callback: UpdateCallback) -> None:
        """Start delivering updates."""

    @abstractmethod
    async def async_stop(self) -> None:
        """Stop and release everything."""


class PollingSource(UpdateSource):
    """Polls devices and the event log on two timers."""

    def __init__(
        self,
        conn: PaxtonConnection,
        site: Site,
        device_interval: float,
        event_interval: float,
        include_user_names: bool,
    ) -> None:
        self._conn = conn
        self._site = site
        self._device_interval = device_interval
        self._event_interval = event_interval
        self._include_user_names = include_user_names
        self._callback: UpdateCallback | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self.last_event_id: str | None = None  # newest event seen, for diagnostics
        self._seen: deque[str] = deque(maxlen=SEEN_EVENT_IDS)
        self._seen_set: set[str] = set()
        self._baselined = False
        self.auth_failed = False
        self.mode = MODE_POLLING  # how events arrive, for diagnostics

    def set_site(self, site: Site) -> None:
        """Use a rediscovered layout from the next poll on."""
        self._site = site

    async def async_start(self, callback: UpdateCallback) -> None:
        self._callback = callback
        # Baseline the event log first, so events from before startup are never replayed.
        try:
            await self.poll_events()
        except PaxtonAuthError:
            raise
        except PaxtonError as err:
            _LOGGER.debug("First event poll failed, the event loop will retry: %s", err)
        loop = asyncio.get_running_loop()
        if self._site.can_read_devices or self._site.can_read_summary:
            self._tasks.append(
                loop.create_task(
                    self._run(self.poll_devices, self._device_interval, KIND_DEVICES), name="paxton10 devices"
                )
            )
        self._tasks.append(loop.create_task(self._events_loop(), name="paxton10 events"))

    async def _events_loop(self) -> None:
        await self._run(self.poll_events, self._event_interval, KIND_EVENTS)

    async def async_stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()
        self._callback = None

    async def _run(self, poll: Callable[[], Awaitable[None]], interval: float, kind: str) -> None:
        failures = 0
        while True:
            await _sleep(min(interval * (2**failures), MAX_BACKOFF) if failures else interval)
            if self.auth_failed:
                return
            try:
                await poll()
                failures = 0
            except PaxtonAuthError as err:
                # Stop both loops. Retrying a rejected password can lock the Paxton account;
                # the reauth flow reloads the entry with new credentials.
                self.auth_failed = True
                if self._callback:
                    await self._callback(SourceUpdate(kind, error=err))
                return
            except PaxtonError as err:
                failures = min(failures + 1, 6)
                if self._callback:
                    await self._callback(SourceUpdate(kind, error=err))
            except Exception as err:
                _LOGGER.exception("Unexpected error in Paxton10 poll")
                failures = min(failures + 1, 6)
                if self._callback:
                    await self._callback(SourceUpdate(kind, error=PaxtonError(str(err))))

    async def poll_devices(self) -> None:
        update = SourceUpdate(KIND_DEVICES)
        if self._site.can_read_devices:
            try:
                update.devices = await read_devices(self._conn)
                name_hardware(update.devices, self._site.doors)
            except PaxtonForbidden:
                self._site.can_read_devices = False
        if self._site.can_read_summary:
            try:
                update.summary = await read_summary(self._conn)
            except PaxtonForbidden:
                self._site.can_read_summary = False
        if self._callback:
            await self._callback(update)

    async def poll_events(self) -> None:
        body = await self._conn.post(EVENTS_PATH, event_filter(self._site.server.utc_offset_minutes))
        raw = body.get("Result") if isinstance(body, dict) else None
        if not isinstance(raw, list):
            raise PaxtonError("event poll returned no Result list")
        # The page is newest first. Event ids don't sort, so new means not seen before.
        events = [e for e in (parse_event(r, self._include_user_names) for r in raw if isinstance(r, dict)) if e]
        if self._baselined and events and all(e.event_id not in self._seen_set for e in events):
            _LOGGER.debug("Every event on the page is new, so some may have been missed")
        # A successful poll always reports, even with nothing new: that clears an earlier failure.
        await self._deliver(events, replay=self._baselined, report_empty=True)
        self._baselined = True

    async def _deliver(self, events: list[DoorEvent], replay: bool = True, report_empty: bool = False) -> None:
        """Hand on the events not seen before, oldest first. events is newest first.

        With replay False (the startup baseline), events are only marked seen.
        """
        new: list[DoorEvent] = []
        if replay:
            for e in reversed(events):
                if e.event_id not in self._seen_set and e.event_id not in {n.event_id for n in new}:
                    new.append(e)
        if (new or report_empty) and self._callback:
            await self._callback(SourceUpdate(KIND_EVENTS, events=new))
        # Mark events seen only once they are delivered. If the callback raised,
        # the next poll offers the same events again.
        for e in reversed(events):
            self._remember(e.event_id)
        if events:
            self.last_event_id = events[0].event_id

    def _remember(self, event_id: str) -> None:
        if event_id in self._seen_set:
            return
        if len(self._seen) == self._seen.maxlen:
            self._seen_set.discard(self._seen[0])
        self._seen.append(event_id)
        self._seen_set.add(event_id)


def _unnamed_user(row: dict[str, Any]) -> bool:
    """A row about a user (UserData has a UserId) that doesn't say who."""
    user = row.get("UserData")
    if not isinstance(user, dict) or not isinstance(user.get("UserId"), int):
        return False
    parsed = parse_event(row, include_user=True)
    return parsed is not None and parsed.user_name is None


class LiveSource(PollingSource):
    """Takes events from the server's live hub on Direct, and polls everything else.

    Devices and the summary are polled as in PollingSource. Events come from the hub's
    newLiveEventNotification pushes. While the hub is down, or the active route isn't Direct
    (the hub is only on the site network), events are polled at the event interval and the hub
    is retried with backoff. A hub failure on its own never makes entities unavailable.
    """

    async def async_start(self, callback: UpdateCallback) -> None:
        self.mode = MODE_STARTING
        await super().async_start(callback)

    async def _events_loop(self) -> None:
        failures = 0
        while not self.auth_failed:
            try:
                failures = await self._hub_cycle(failures)
                await self._poll_for(min(self._event_interval * (2 ** min(failures, 6)), MAX_BACKOFF))
            except PaxtonAuthError as err:
                # A rejected password on sign-in or a poll. Stop, as the polling loops do, and let reauth take over.
                self.auth_failed = True
                if self._callback:
                    await self._callback(SourceUpdate(KIND_EVENTS, error=err))
                return

    async def _hub_cycle(self, failures: int) -> int:
        """Connect, subscribe, and listen until the hub fails. Returns the new failure count.

        Off Direct there's no hub: poll until it's time to check the route again.
        Raises PaxtonAuthError for a rejected password.
        """
        hub: LongPollHub | None = None
        try:
            target = await self._conn.hub_target()
            if target is None:
                self._set_mode(MODE_POLLING, "the active route isn't Direct")
                await self._poll_for(NOT_DIRECT_RECHECK)
                return 0
            hub = LongPollHub(self._conn.session, *target)
            await hub.connect()
            await hub.invoke(METHOD_SUBSCRIBE_EVENTS, live_event_filter(self._site.server.utc_offset_minutes))
            # Catch up on anything logged before the subscription took effect.
            await self._poll_once()
            self._set_mode(MODE_LIVE)
            failures = 0
            await self._listen(hub)
        except PaxtonAuthError:
            raise
        except HubUnauthorized as err:
            # The token expired. Sign in again on the next attempt.
            _LOGGER.debug("Paxton10 live hub rejected the token, signing in again: %s", err)
            await self._conn.renew()
        except PaxtonError as err:
            self._set_mode(MODE_POLLING, str(err))
        except Exception as err:
            _LOGGER.exception("Unexpected error in the Paxton10 live hub")
            self._set_mode(MODE_POLLING, str(err))
        finally:
            if hub:
                await hub.close()
        self._set_mode(MODE_POLLING, "reconnecting")
        return failures + 1

    async def _listen(self, hub: LongPollHub) -> None:
        """Hold the long poll and deliver pushes until the hub fails."""
        last_reconcile = _monotonic()
        while True:
            messages = await hub.poll()
            rows = [row for message in messages for row in event_rows(message)]
            if rows:
                events = self._parse_newest_first(rows)
                if self._include_user_names and any(_unnamed_user(row) for row in rows):
                    # Live rows carry the user's id but not their name. The event log row has the
                    # name, so fire from there. Anything not on the page yet still fires below.
                    await self._poll_once()
                await self._deliver(events)
            if _monotonic() - last_reconcile >= RECONCILE_INTERVAL:
                await self._poll_once()
                last_reconcile = _monotonic()

    def _parse_newest_first(self, rows: list[dict[str, Any]]) -> list[DoorEvent]:
        events = [e for e in (parse_event(r, self._include_user_names) for r in rows) if e]
        # A push can hold several rows. Order them like a log page, newest first, by event time.
        return sorted(events, key=lambda e: e.time or NO_TIME, reverse=True)

    async def _poll_once(self) -> None:
        """One event poll. Reports a read failure like the polling loop. Raises PaxtonAuthError."""
        try:
            await self.poll_events()
        except PaxtonAuthError:
            raise
        except PaxtonError as err:
            if self._callback:
                await self._callback(SourceUpdate(KIND_EVENTS, error=err))

    async def _poll_for(self, seconds: float) -> None:
        """Poll events at the event interval for about this long, starting now."""
        polls = max(1, math.ceil(seconds / self._event_interval))
        for n in range(polls):
            if self.auth_failed:  # the device loop hit a rejected password
                return
            await self._poll_once()
            if n < polls - 1:
                await _sleep(self._event_interval)

    def _set_mode(self, mode: str, reason: str = "") -> None:
        if mode == self.mode:
            return
        self.mode = mode
        if mode == MODE_LIVE:
            _LOGGER.info("Paxton10 live events connected")
        else:
            _LOGGER.info("Paxton10 live events unavailable, polling the event log instead: %s", reason)
