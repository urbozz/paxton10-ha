"""The Paxton10 integration."""

from __future__ import annotations

from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN, REMOVED_OPTIONS
from .coordinator import Paxton10ConfigEntry, Paxton10Coordinator

PLATFORMS = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.EVENT, Platform.SENSOR]


async def async_migrate_entry(hass: HomeAssistant, entry: Paxton10ConfigEntry) -> bool:
    """1.1 to 1.2: drop the interval options. The intervals are fixed now.

    Home Assistant refuses an entry from a newer major version before calling this.
    """
    if entry.minor_version < 2:
        options = {k: v for k, v in entry.options.items() if k not in REMOVED_OPTIONS}
        hass.config_entries.async_update_entry(entry, options=options, minor_version=2)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: Paxton10ConfigEntry) -> bool:
    coordinator = Paxton10Coordinator(hass, entry)
    try:
        await coordinator.async_config_entry_first_refresh()
        await coordinator.async_start()
    except BaseException:
        await coordinator.async_shutdown()
        raise
    entry.runtime_data = coordinator
    coordinator.register_server_device()
    coordinator.remove_stale_devices(coordinator.data)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def _async_stop(_event: Event) -> None:
        await coordinator.async_stop_updates()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_stop))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: Paxton10ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_shutdown()
    return unloaded


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: Paxton10ConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Allow deleting a device only once the server no longer has it."""
    current = Paxton10Coordinator.current_identifiers(entry.runtime_data.data)
    return not any(i[0] == DOMAIN and i in current for i in device.identifiers)
