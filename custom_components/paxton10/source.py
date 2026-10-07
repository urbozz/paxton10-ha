"""Update sources. Phase 1 polls; the live SignalR hub can replace PollingSource later.

Entities never talk to a source. The coordinator receives SourceUpdate objects and
entities read the coordinator's data, so swapping the source changes no entity.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from .api import PaxtonAuthError, PaxtonError
from .connection import PaxtonConnection, PaxtonForbidden
from .const import EVENT_PAGE_SIZE
from .discovery import read_devices, read_summary
from .models import Device, DoorEvent, Site, event_filter, name_controllers, parse_event

_LOGGER = logging.getLogger(__name__)

MAX_BACKOFF = 300
EVENTS_PATH = f"/api/v2/Events/?page=0&pageSize={EVENT_PAGE_SIZE}"
_sleep = asyncio.sleep  # tests replace this, not asyncio.sleep itself

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
        self.last_event_id: int | None = None
        self.auth_failed = False

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
        self._tasks.append(
            loop.create_task(self._run(self.poll_events, self._event_interval, KIND_EVENTS), name="paxton10 events")
        )

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
                name_controllers(update.devices, self._site.doors)
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
        events = [e for e in (parse_event(r, self._include_user_names) for r in raw if isinstance(r, dict)) if e]
        newest = max((e.event_id for e in events), default=None)
        if self.last_event_id is None:
            self.last_event_id = newest if newest is not None else 0
            new: list[DoorEvent] = []
        else:
            new = sorted((e for e in events if e.event_id > self.last_event_id), key=lambda e: e.event_id)
        if self._callback:
            await self._callback(SourceUpdate(KIND_EVENTS, events=new))
        # Move the cursor only once the events are delivered. If the callback raised,
        # the next poll offers the same events again.
        if newest is not None and newest > self.last_event_id:
            self.last_event_id = newest
