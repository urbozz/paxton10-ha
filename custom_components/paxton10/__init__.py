"""The Paxton10 integration."""

from __future__ import annotations

from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN
from .coordinator import Paxton10ConfigEntry, Paxton10Coordinator

PLATFORMS = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.EVENT, Platform.SENSOR]


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
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    async def _async_stop(_event: Event) -> None:
        await coordinator.async_stop_updates()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_stop))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: Paxton10ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_shutdown()
    return unloaded


async def _async_options_updated(hass: HomeAssistant, entry: Paxton10ConfigEntry) -> None:
    # Door control and intervals change the client and the source, so start again.
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: Paxton10ConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Allow deleting a device only once the server no longer has it."""
    current = Paxton10Coordinator.current_identifiers(entry.runtime_data.data)
    return not any(i[0] == DOMAIN and i in current for i in device.identifiers)
