"""Connectivity for controllers and entry panels."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.binary_sensor.const import BinarySensorDeviceClass
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import Paxton10ConfigEntry, Paxton10Coordinator
from .entity import HardwareEntity, add_entities_dynamically
from .models import Device

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: Paxton10ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    add_entities_dynamically(
        entry,
        lambda: coordinator.data.devices,
        lambda d: [ConnectivitySensor(coordinator, coordinator.data.devices[d])],
        async_add_entities,
    )


class ConnectivitySensor(HardwareEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: Paxton10Coordinator, device: Device) -> None:
        super().__init__(coordinator, device, "connectivity")

    @property
    def is_on(self) -> bool | None:
        device = self.device
        return device.online if device else None
