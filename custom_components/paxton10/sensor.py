"""Server summary sensors and per-device diagnostics."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorEntityDescription, SensorStateClass
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import Paxton10ConfigEntry, Paxton10Coordinator
from .entity import HardwareEntity, Paxton10Entity, add_entities_dynamically, server_device_info
from .models import KIND_CONTROLLER, Device

PARALLEL_UPDATES = 0

SERVER = "server"


# Keys match models.SUMMARY_KEYS values.
SUMMARY_SENSORS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(key="active_users", state_class=SensorStateClass.MEASUREMENT),
    SensorEntityDescription(key="total_users", state_class=SensorStateClass.MEASUREMENT),
    SensorEntityDescription(
        key="total_devices", state_class=SensorStateClass.MEASUREMENT, entity_category=EntityCategory.DIAGNOSTIC
    ),
    SensorEntityDescription(key="unacknowledged_alarms", state_class=SensorStateClass.MEASUREMENT),
    SensorEntityDescription(key="offline_devices", state_class=SensorStateClass.MEASUREMENT),
)


@dataclass(frozen=True, kw_only=True)
class DeviceDescription(SensorEntityDescription):
    value: Callable[[Device], str | int | datetime | None]
    controllers_only: bool = False


# Battery and PSU values are raw numbers: their meaning isn't confirmed against the web UI yet.
DEVICE_SENSORS: tuple[DeviceDescription, ...] = (
    DeviceDescription(
        key="last_contact",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value=lambda d: d.last_contact,
    ),
    DeviceDescription(key="firmware", entity_category=EntityCategory.DIAGNOSTIC, value=lambda d: d.firmware),
    DeviceDescription(
        key="ip_address",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value=lambda d: d.ip,
    ),
    DeviceDescription(
        key="battery_charge",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        controllers_only=True,
        value=lambda d: d.battery_charge,
    ),
    DeviceDescription(
        key="battery_state",
        entity_category=EntityCategory.DIAGNOSTIC,
        controllers_only=True,
        value=lambda d: d.battery_state,
    ),
    DeviceDescription(
        key="psu_state",
        entity_category=EntityCategory.DIAGNOSTIC,
        controllers_only=True,
        value=lambda d: d.psu_state,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: Paxton10ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    server: list[Entity] = [VersionSensor(coordinator)]
    if coordinator.data.can_read_summary:
        server.extend(SummarySensor(coordinator, d) for d in SUMMARY_SENSORS)
    async_add_entities(server)

    def build(device_id: int) -> list[Entity]:
        device = coordinator.data.devices[device_id]
        return [
            DeviceSensor(coordinator, device, d)
            for d in DEVICE_SENSORS
            if not d.controllers_only or device.kind == KIND_CONTROLLER
        ]

    add_entities_dynamically(entry, lambda: coordinator.data.devices, build, async_add_entities)


class ServerEntity(Paxton10Entity):
    def __init__(self, coordinator: Paxton10Coordinator, key: str) -> None:
        super().__init__(coordinator, SERVER, key)
        self._attr_device_info = server_device_info(coordinator)


class VersionSensor(ServerEntity, SensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: Paxton10Coordinator) -> None:
        super().__init__(coordinator, "software_version")

    @property
    def native_value(self) -> str | None:
        return self.coordinator.data.server.version


class SummarySensor(ServerEntity, SensorEntity):
    entity_description: SensorEntityDescription

    def __init__(self, coordinator: Paxton10Coordinator, description: SensorEntityDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> int | None:
        return self.coordinator.data.summary.get(self.entity_description.key)


class DeviceSensor(HardwareEntity, SensorEntity):
    entity_description: DeviceDescription

    def __init__(self, coordinator: Paxton10Coordinator, device: Device, description: DeviceDescription) -> None:
        super().__init__(coordinator, device, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> str | int | datetime | None:
        device = self.device
        return self.entity_description.value(device) if device else None
