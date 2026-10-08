"""Coordinator: holds the site model and turns source updates into entity state and bus events."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import PaxtonAuthError, PaxtonError
from .connection import PaxtonConnection
from .const import (
    CONF_PASSWORD_HASH,
    CONF_ROUTE,
    CONF_TARGET,
    CONF_USERNAME,
    DEFAULT_DEVICE_INTERVAL,
    DEFAULT_EVENT_INTERVAL,
    DOMAIN,
    EVENT_PAXTON10,
    MANUFACTURER,
    OPT_ALLOW_DOOR_CONTROL,
    OPT_DEVICE_INTERVAL,
    OPT_EVENT_INTERVAL,
    OPT_FALLBACK,
    OPT_FALLBACK_TARGET,
    OPT_INCLUDE_USER_NAMES,
)
from .discovery import discover_site
from .models import KIND_CONTROLLER, DoorEvent, Site, door_model, hardware_model
from .source import LiveSource, PollingSource, SourceUpdate, UpdateSource

_LOGGER = logging.getLogger(__name__)

REDISCOVER_INTERVAL = timedelta(hours=1)
ISSUE_FALLBACK = "using_fallback_route"

type Paxton10ConfigEntry = ConfigEntry[Paxton10Coordinator]

EventListener = Callable[[DoorEvent], None]
ForgetListener = Callable[[int], None]


@dataclass(frozen=True)
class EntryOptions:
    allow_door_control: bool
    device_interval: int
    event_interval: int
    fallback_target: str | None
    include_user_names: bool

    @classmethod
    def from_entry(cls, entry: ConfigEntry) -> EntryOptions:
        o = entry.options
        return cls(
            allow_door_control=bool(o.get(OPT_ALLOW_DOOR_CONTROL, False)),
            device_interval=int(o.get(OPT_DEVICE_INTERVAL, DEFAULT_DEVICE_INTERVAL)),
            event_interval=int(o.get(OPT_EVENT_INTERVAL, DEFAULT_EVENT_INTERVAL)),
            fallback_target=(o.get(OPT_FALLBACK_TARGET) or None) if o.get(OPT_FALLBACK) else None,
            include_user_names=bool(o.get(OPT_INCLUDE_USER_NAMES, False)),
        )


def build_connection(hass: HomeAssistant, entry: ConfigEntry, options: EntryOptions) -> PaxtonConnection:
    return PaxtonConnection(
        async_get_clientsession(hass),
        entry.data[CONF_ROUTE],
        entry.data[CONF_TARGET],
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD_HASH],
        # The client blocks every write unless door control is on.
        allow_writes=options.allow_door_control,
        fallback_target=options.fallback_target,
    )


class Paxton10Coordinator(DataUpdateCoordinator[Site]):
    """Owns the connection and the update source.

    The source pushes updates to the coordinator, so the coordinator itself never polls
    (update_interval is None). Behind it, device status and the summary are polled, door events
    are live on Direct (polled on Remote or while the live feed is down), and the layout is
    rediscovered hourly.
    """

    config_entry: Paxton10ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: Paxton10ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=DOMAIN, update_interval=None)
        self.options = EntryOptions.from_entry(entry)
        self.conn = build_connection(hass, entry, self.options)
        self.source: UpdateSource | None = None
        self._event_listeners: dict[int | None, list[EventListener]] = {}
        self._unsub_rediscover: CALLBACK_TYPE | None = None
        self.server_device_id: str | None = None
        # Reads whose last poll failed. Entities stay unavailable until each one succeeds again.
        self._failing: set[str] = set()
        self._forget_listeners: list[ForgetListener] = []

    @property
    def site_id(self) -> str:
        return self.data.server.site_id

    async def _async_update_data(self) -> Site:
        """Full read of the site. Used at setup and by the hourly rediscovery."""
        try:
            site = await discover_site(self.conn)
        except PaxtonAuthError as err:
            raise ConfigEntryAuthFailed(translation_domain=DOMAIN, translation_key="invalid_auth") from err
        except PaxtonError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
                translation_placeholders={"error": str(err)},
            ) from err
        self._update_fallback_issue()
        return site

    @callback
    def register_server_device(self) -> None:
        """Register the server, then the controllers, so doors can point at their controller with via_device_id."""
        server = self.data.server
        registry = dr.async_get(self.hass)
        device = registry.async_get_or_create(
            config_entry_id=self.config_entry.entry_id,
            identifiers={(DOMAIN, server.site_id)},
            manufacturer=MANUFACTURER,
            model="Paxton10 server",
            name=server.system_name,
            sw_version=server.version,
        )
        self.server_device_id = device.id
        for hw in self.data.devices.values():
            if hw.kind == KIND_CONTROLLER:
                registry.async_get_or_create(
                    config_entry_id=self.config_entry.entry_id,
                    identifiers={(DOMAIN, f"{server.site_id}_{hw.entity_id}")},
                    manufacturer=MANUFACTURER,
                    model=hardware_model(hw),
                    name=hw.name,
                    serial_number=hw.serial,
                    sw_version=hw.firmware,
                    via_device_id=device.id,
                )

    @callback
    def door_parent_device_id(self, door_id: int) -> str | None:
        """The registry id of the controller that drives the door, else the server's."""
        registry = dr.async_get(self.hass)
        for hw in self.data.devices.values():
            if hw.kind == KIND_CONTROLLER and door_id in hw.door_ids:
                parent = registry.async_get_device_by_identifier(
                    (DOMAIN, f"{self.site_id}_{hw.entity_id}"), self.config_entry.entry_id
                )
                if parent:
                    return parent.id
        return self.server_device_id

    async def async_start(self) -> None:
        """Start the update source after the first refresh. Events come live from the hub on Direct."""
        self.source = LiveSource(
            self.conn,
            self.data,
            self.options.device_interval,
            self.options.event_interval,
            self.options.include_user_names,
        )
        try:
            await self.source.async_start(self._async_handle_update)
        except PaxtonAuthError as err:
            raise ConfigEntryAuthFailed(translation_domain=DOMAIN, translation_key="invalid_auth") from err
        self._unsub_rediscover = async_track_time_interval(
            self.hass, self._async_rediscover, REDISCOVER_INTERVAL, name="paxton10 rediscover"
        )

    async def async_stop_updates(self) -> None:
        """Stop the update tasks and close the connection, as Home Assistant shuts down.

        Home Assistant doesn't unload entries on shutdown. Without this, the live hub's long poll
        is still waiting when Home Assistant closes its HTTP sessions, and fails with an error.
        """
        if self.source:
            await self.source.async_stop()
            self.source = None
        await self.conn.close()

    async def async_shutdown(self) -> None:
        if self._unsub_rediscover:
            self._unsub_rediscover()
            self._unsub_rediscover = None
        try:
            if self.source:
                await self.source.async_stop()
                self.source = None
        finally:
            # Close the connection even if stopping the source failed.
            await self.conn.close()
            # Don't leave a repair behind for an integration that's unloaded or removed.
            ir.async_delete_issue(self.hass, DOMAIN, self._fallback_issue_id)
            await super().async_shutdown()

    async def _async_handle_update(self, update: SourceUpdate) -> None:
        if update.error is not None:
            if isinstance(update.error, PaxtonAuthError):
                _LOGGER.warning("Paxton10 rejected the stored credentials; starting reauthentication")
                self.config_entry.async_start_reauth(self.hass)
            self._failing.add(update.kind)
            # Marks every entity unavailable; the coordinator logs the change once.
            self.async_set_update_error(update.error)
            return
        site = self.data
        if update.devices is not None:
            site = replace(site, devices=update.devices)
        if update.summary is not None:
            site = replace(site, summary=update.summary)
        if update.door_states is not None:
            site = replace(site, door_states={**site.door_states, **update.door_states})
        for event in update.events:
            self._fire(event)
        self._update_fallback_issue()
        recovered = update.kind in self._failing
        self._failing.discard(update.kind)
        if recovered or update.devices is not None or update.summary is not None or update.door_states is not None:
            self._publish(site)

    @callback
    def _publish(self, site: Site) -> None:
        """Hand new data to entities, unless another read is still failing."""
        if self._failing:
            # Keep the newest data, but leave entities unavailable until every read works again.
            self.data = site
            return
        self.async_set_updated_data(site)
        self._sync_device_registry(site)

    async def _async_rediscover(self, _now: Any = None) -> None:
        """Pick up added and removed doors and devices, and move back off the fallback route."""
        try:
            await self.conn.try_primary()
            site = await discover_site(self.conn)
        except PaxtonError as err:
            _LOGGER.debug("Paxton10 rediscovery failed, keeping the current layout: %s", err)
            return
        if isinstance(self.source, PollingSource):
            self.source.set_site(site)
        self._update_fallback_issue()
        self._publish(site)
        self.remove_stale_devices(site)

    @callback
    def remove_stale_devices(self, site: Site) -> None:
        registry = dr.async_get(self.hass)
        current = self.current_identifiers(site)
        prefix = f"{site.server.site_id}_"
        for device in dr.async_entries_for_config_entry(registry, self.config_entry.entry_id):
            ours = [i[1] for i in device.identifiers if i[0] == DOMAIN]
            if not ours or any((DOMAIN, i) in current for i in ours):
                continue
            # Log the Paxton entity id, not the name: a generated name can contain a serial.
            _LOGGER.info("Removing Paxton10 device that's no longer on the server: %s", ", ".join(ours))
            registry.async_update_device(device.id, remove_config_entry_id=self.config_entry.entry_id)
            # Platforms forget the id, so the entities come back if the device reappears.
            for ident in ours:
                if ident.startswith(prefix) and ident[len(prefix) :].isdigit():
                    for forget in list(self._forget_listeners):
                        forget(int(ident[len(prefix) :]))

    @callback
    def async_add_forget_listener(self, listener: ForgetListener) -> CALLBACK_TYPE:
        self._forget_listeners.append(listener)

        @callback
        def remove() -> None:
            self._forget_listeners.remove(listener)

        return remove

    @callback
    def _sync_device_registry(self, site: Site) -> None:
        """Entities register device info only when they're added, so push later changes here."""
        registry = dr.async_get(self.hass)
        entry_id = self.config_entry.entry_id
        sid = site.server.site_id
        wanted: dict[str, dict[str, Any]] = {
            sid: {"name": site.server.system_name, "sw_version": site.server.version},
        }
        for door in site.doors.values():
            wanted[f"{sid}_{door.entity_id}"] = {
                "name": door.name,
                "model": door_model(door.appliance_type),
                "via_device_id": self.door_parent_device_id(door.entity_id),
            }
        for hw in site.devices.values():
            wanted[f"{sid}_{hw.entity_id}"] = {
                "name": hw.name,
                "model": hardware_model(hw),
                "sw_version": hw.firmware,
                "serial_number": hw.serial,
            }
        for ident, fields in wanted.items():
            device = registry.async_get_device_by_identifier((DOMAIN, ident), entry_id)
            if device is None:
                continue
            changes = {k: v for k, v in fields.items() if getattr(device, k) != v}
            if changes:
                registry.async_update_device(device.id, **changes)

    @staticmethod
    def current_identifiers(site: Site) -> set[tuple[str, str]]:
        sid = site.server.site_id
        ids = {(DOMAIN, sid)}
        ids.update((DOMAIN, f"{sid}_{i}") for i in site.doors)
        ids.update((DOMAIN, f"{sid}_{i}") for i in site.devices)
        return ids

    @property
    def _fallback_issue_id(self) -> str:
        return f"{ISSUE_FALLBACK}_{self.config_entry.entry_id}"

    @callback
    def _update_fallback_issue(self) -> None:
        primary = self.config_entry.data[CONF_ROUTE]
        if self.conn.active_route and self.conn.active_route != primary:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                self._fallback_issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_FALLBACK,
                translation_placeholders={"primary": primary, "active": self.conn.active_route},
            )
        elif self.conn.active_route == primary:
            ir.async_delete_issue(self.hass, DOMAIN, self._fallback_issue_id)

    # Events

    @callback
    def async_add_event_listener(self, door_id: int | None, listener: EventListener) -> CALLBACK_TYPE:
        self._event_listeners.setdefault(door_id, []).append(listener)

        @callback
        def remove() -> None:
            self._event_listeners.get(door_id, []).remove(listener)

        return remove

    @callback
    def _fire(self, event: DoorEvent) -> None:
        doors = self.data.doors
        ent_reg = er.async_get(self.hass)
        targets: list[int | None] = [d for d in event.door_ids if d in doors] or [None]
        for door_id in targets:
            # Home Assistant IDs for the door, so automations and the logbook can link to it.
            # door_entity_id stays the Paxton ID for existing automations.
            ha_entity_id = ha_device_id = None
            if door_id is not None and (
                entry := ent_reg.async_get_entity_id("event", DOMAIN, f"{self.site_id}_{door_id}_door_event")
            ):
                ha_entity_id = entry
                if reg_entry := ent_reg.async_get(entry):
                    ha_device_id = reg_entry.device_id
            data: dict[str, Any] = {
                "config_entry_id": self.config_entry.entry_id,
                "event_id": event.event_id,
                "event_type": event.event_type,
                "event_type_id": event.event_type_id,
                "door_entity_id": door_id,
                "door_name": doors[door_id].name if door_id is not None else None,
                "entity_id": ha_entity_id,
                "device_id": ha_device_id,
                "time": event.time.isoformat() if event.time else None,
                "reader": event.reader,
            }
            if self.options.include_user_names:
                data["user_name"] = event.user_name
                data["credential"] = event.credential
            self.hass.bus.async_fire(EVENT_PAXTON10, data)
            for listener in list(self._event_listeners.get(door_id, [])):
                listener(event)
