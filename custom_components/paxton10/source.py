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
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

from .api import PaxtonAuthError, PaxtonError
from .connection import PaxtonConnection, PaxtonForbidden, PaxtonNotFound
from .const import DEFAULT_DEVICE_FULL_INTERVAL, EVENT_PAGE_SIZE, HARDWARE_EVENT_TYPES
from .discovery import read_devices, read_door_states, read_summary
from .hub import (
    METHOD_SUBSCRIBE_DOOR_STATE,
    METHOD_SUBSCRIBE_EVENTS,
    HubDisconnected,
    HubUnauthorized,
    LiveFeed,
    LongPollHub,
    RemoteFeed,
    door_state_rows,
    event_rows,
)
from .models import (
    Device,
    DoorEvent,
    Site,
    event_filter,
    live_event_filter,
    name_hardware,
    parse_door_states,
    parse_event,
)

_LOGGER = logging.getLogger(__name__)

MAX_BACKOFF = 300
SEEN_EVENT_IDS = 1000  # well over one page, so an event never comes back as new
EVENTS_PATH = f"/api/v2/Events/?page=0&pageSize={EVENT_PAGE_SIZE}"
RECONCILE_INTERVAL = 300  # while live, also read the event log this often, in case a push was lost
NOT_DIRECT_RECHECK = 300  # with no live feed on the active route, poll this long before checking again
_sleep = asyncio.sleep  # tests replace this, not asyncio.sleep itself
_monotonic = time.monotonic  # and this

MODE_LIVE = "live"
MODE_POLLING = "polling"
MODE_STARTING = "starting"
NO_TIME = datetime(2000, 1, 1, tzinfo=timezone.utc)  # sorts events without a time as oldest

KIND_DEVICES = "devices"
KIND_EVENTS = "events"
KIND_DOOR_STATES = "door_states"  # live door state pushes; polled door state comes with KIND_DEVICES
KIND_STATUS = "status"  # the cheap part of a device poll, delivered on its own when the list read fails


@dataclass
class SourceUpdate:
    """What a source delivers. None means 'no change to this part'.

    kind names the read that produced it, so one read's success never clears another's failure.
    """

    kind: str
    devices: dict[int, Device] | None = None
    summary: dict[str, int] | None = None
    door_states: dict[int, int] | None = None  # changed doors only; merged into the site's door states
    events: list[DoorEvent] = field(default_factory=list)
    error: PaxtonError | None = None


UpdateCallback = Callable[[SourceUpdate], Awaitable[None]]


def _watch(summary: dict[str, int]) -> tuple[int | None, int | None]:
    """The summary counts that change when a device does."""
    return summary.get("offline_devices"), summary.get("unacknowledged_alarms")


async def _nap(delay: float, wake: asyncio.Event | None) -> None:
    """Sleep for delay, or until wake is set."""
    if wake is None:
        await _sleep(delay)
        return
    sleeper = asyncio.ensure_future(_sleep(delay))
    waker = asyncio.ensure_future(wake.wait())
    try:
        await asyncio.wait({sleeper, waker}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (sleeper, waker):
            task.cancel()
        await asyncio.gather(sleeper, waker, return_exceptions=True)
        wake.clear()


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
        include_credential_names: bool = False,
        device_full_interval: float = DEFAULT_DEVICE_FULL_INTERVAL,
    ) -> None:
        self._conn = conn
        self._site = site
        self._device_interval = device_interval
        self._event_interval = event_interval
        self._include_user_names = include_user_names
        self._include_credential_names = include_credential_names
        self._device_full_interval = device_full_interval
        self._devices_read_at = _monotonic()  # discovery has just read the device list
        self._devices_requested = False
        self._device_wake = asyncio.Event()
        self._summary_watch = _watch(site.summary)
        self._callback: UpdateCallback | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self.last_event_id: str | None = None  # newest event seen, for diagnostics
        self._seen: deque[str] = deque(maxlen=SEEN_EVENT_IDS)
        self._seen_set: set[str] = set()
        self._baselined = False
        self.auth_failed = False
        self.mode = MODE_POLLING  # how events arrive, for diagnostics
        self._door_pushed_at: dict[int, float] = {}  # door id -> when its last live state arrived

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
        if self._site.can_read_devices or self._site.can_read_summary or self._site.can_read_door_states:
            self._tasks.append(
                loop.create_task(
                    self._run(self.poll_devices, self._device_interval, KIND_DEVICES, wake=self._device_wake),
                    name="paxton10 devices",
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
            except Exception:
                # A task that already ended with an error mustn't stop the rest of the shutdown.
                _LOGGER.exception("Paxton10 update task %s had failed", task.get_name())
        self._tasks.clear()
        self._callback = None

    async def _run(
        self, poll: Callable[[], Awaitable[None]], interval: float, kind: str, wake: asyncio.Event | None = None
    ) -> None:
        failures = 0
        while True:
            await _nap(min(interval * (2**failures), MAX_BACKOFF) if failures else interval, wake)
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

    async def poll_devices(self, full: bool = False) -> None:
        """Cheap reads every time; the controller list only when it's needed.

        The summary and door states are a few hundred bytes. The controller list is about 57 KB
        per controller, so it's read when asked (full), when a hardware event asked for it, when
        the summary's offline or alarm counts change, when the account can't read the summary
        (nothing else would show a change), or once the full refresh interval has passed.
        """
        update = SourceUpdate(KIND_DEVICES)
        if self._site.can_read_summary:
            try:
                update.summary = await read_summary(self._conn)
            except PaxtonForbidden:
                self._site.can_read_summary = False
        if update.summary is not None and (watch := _watch(update.summary)) != self._summary_watch:
            self._summary_watch = watch
            full = True
        if self._site.can_read_door_states:
            started = _monotonic()
            try:
                states = await read_door_states(self._conn, sorted(self._site.doors))
            except (PaxtonForbidden, PaxtonNotFound):
                self._site.can_read_door_states = False
            else:
                # A push that arrived while this read was in flight is newer: keep it.
                update.door_states = {
                    door: state for door, state in states.items() if self._door_pushed_at.get(door, -1.0) < started
                }
        due = _monotonic() - self._devices_read_at >= self._device_full_interval
        if self._site.can_read_devices and (full or due or self._devices_requested or not self._site.can_read_summary):
            try:
                update.devices = await read_devices(self._conn)
                name_hardware(update.devices, self._site.doors)
            except PaxtonForbidden:
                self._site.can_read_devices = False
            except PaxtonError:
                # Keep the reason for this read (a count change or a hardware event), so the next
                # poll tries again rather than waiting for the full refresh. Hand on the summary and
                # door states already read. As KIND_STATUS they don't clear the device failure that
                # the run loop reports next, so entities don't flicker back between failed retries.
                self._devices_requested = True
                if self._callback and (update.summary is not None or update.door_states is not None):
                    await self._callback(replace(update, kind=KIND_STATUS))
                raise
            else:
                self._devices_read_at = _monotonic()
            self._devices_requested = False
        if self._callback:
            await self._callback(update)

    def request_device_refresh(self) -> None:
        """Read the device list on the next device poll, and wake the device loop for it now."""
        self._devices_requested = True
        self._device_wake.set()

    async def poll_events(self) -> None:
        body = await self._conn.post(EVENTS_PATH, event_filter(self._site.server.utc_offset_minutes))
        raw = body.get("Result") if isinstance(body, dict) else None
        if not isinstance(raw, list):
            raise PaxtonError("event poll returned no Result list")
        # The page is newest first. Event ids don't sort, so new means not seen before.
        events = [e for e in (parse_event(r, self._include_user_names, self._include_credential_names) for r in raw if isinstance(r, dict)) if e]
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
        if any(e.event_type_id in HARDWARE_EVENT_TYPES for e in new):
            # A controller went offline, lost power, and so on: show it now, not at the next full read.
            self.request_device_refresh()
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
            except Exception:
                # Never let the events task end on a bug: device polling would carry on and hide it.
                _LOGGER.exception("Unexpected error in the Paxton10 event loop")
                failures = min(failures + 1, 6)
                await _sleep(self._event_interval)

    async def _hub_cycle(self, failures: int) -> int:
        """Connect, subscribe, and listen until the hub fails. Returns the new failure count.

        Direct uses the server's long poll, Remote the relay socket. With neither (a transport
        without a hub), poll until it's time to check the route again.
        Raises PaxtonAuthError for a rejected password.
        """
        hub: LiveFeed | None = None
        try:
            hub = await self._open_feed()
            if hub is None:
                self._set_mode(MODE_POLLING, "the active route has no live feed")
                await self._poll_for(NOT_DIRECT_RECHECK)
                return 0
            await hub.connect()
            await hub.invoke(METHOD_SUBSCRIBE_EVENTS, live_event_filter(self._site.server.utc_offset_minutes))
            await self._subscribe_door_states(hub)
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

    async def _open_feed(self) -> LiveFeed | None:
        if (target := await self._conn.hub_target()) is not None:
            return LongPollHub(self._conn.session, *target)
        if (remote := await self._conn.remote_hub()) is not None:
            return RemoteFeed(*remote)
        return None

    async def _subscribe_door_states(self, hub: LiveFeed) -> None:
        """Door state pushes are a bonus: if the server refuses them, the device poll still reads door state."""
        if not self._site.can_read_door_states or not self._site.doors:
            return
        try:
            await hub.invoke(METHOD_SUBSCRIBE_DOOR_STATE, sorted(self._site.doors))
        except HubDisconnected:
            raise
        except PaxtonError as err:
            _LOGGER.debug("Paxton10 live door state unavailable, door state is polled instead: %s", err)

    async def _listen(self, hub: LiveFeed) -> None:
        """Hold the long poll and deliver pushes until the hub fails."""
        last_reconcile = _monotonic()
        while True:
            messages = await hub.poll()
            states = [row for message in messages for row in door_state_rows(message)]
            if states and (door_states := parse_door_states(states)):
                now = _monotonic()
                self._door_pushed_at.update(dict.fromkeys(door_states, now))
                if self._callback:
                    await self._callback(SourceUpdate(KIND_DOOR_STATES, door_states=door_states))
            rows = [row for message in messages for row in event_rows(message)]
            if rows:
                events = self._parse_newest_first(rows)
                if self._include_user_names and any(_unnamed_user(row) for row in rows):
                    # Live rows carry the user's id but not their name. The event log row has the
                    # name, so fire from there. Anything not on the page yet still fires below.
                    # A failed lookup only costs the name, so it doesn't make entities unavailable.
                    await self._poll_once(report=False)
                await self._deliver(events)
            if _monotonic() - last_reconcile >= RECONCILE_INTERVAL:
                await self._poll_once()
                last_reconcile = _monotonic()

    def _parse_newest_first(self, rows: list[dict[str, Any]]) -> list[DoorEvent]:
        events = [e for e in (parse_event(r, self._include_user_names, self._include_credential_names) for r in rows) if e]
        # A push can hold several rows. Order them like a log page, newest first, by event time.
        return sorted(events, key=lambda e: e.time or NO_TIME, reverse=True)

    async def _poll_once(self, report: bool = True) -> None:
        """One event poll. Raises PaxtonAuthError; other failures never escape.

        With report, a failed read is reported like the polling loop's, which marks entities
        unavailable until a read succeeds. Without it, the failure is only logged.
        """
        try:
            await self.poll_events()
        except PaxtonAuthError:
            raise
        except Exception as err:
            if not isinstance(err, PaxtonError):
                _LOGGER.exception("Unexpected error reading the Paxton10 event log")
                err = PaxtonError(str(err))
            if not report:
                _LOGGER.debug("Paxton10 event log read failed: %s", err)
            elif self._callback:
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
