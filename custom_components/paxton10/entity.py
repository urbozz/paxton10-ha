"""Base entities and device info."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import Paxton10ConfigEntry, Paxton10Coordinator
from .models import Device, Door, door_model, hardware_model


def server_device_info(coordinator: Paxton10Coordinator) -> DeviceInfo:
    server = coordinator.data.server
    return DeviceInfo(
        identifiers={(DOMAIN, server.site_id)},
        manufacturer=MANUFACTURER,
        model="Paxton10 server",
        name=server.system_name,
        sw_version=server.version,
    )


def _via(coordinator: Paxton10Coordinator, info: DeviceInfo) -> DeviceInfo:
    if coordinator.server_device_id:
        info["via_device_id"] = coordinator.server_device_id
    return info


def door_device_info(coordinator: Paxton10Coordinator, door: Door) -> DeviceInfo:
    sid = coordinator.site_id
    info = DeviceInfo(
        identifiers={(DOMAIN, f"{sid}_{door.entity_id}")},
        manufacturer=MANUFACTURER,
        model=door_model(door.appliance_type),
        name=door.name,
    )
    # Doors hang off the controller that drives them, so the device page shows the wiring.
    if parent := coordinator.door_parent_device_id(door.entity_id):
        info["via_device_id"] = parent
    return info


def hardware_device_info(coordinator: Paxton10Coordinator, device: Device) -> DeviceInfo:
    sid = coordinator.site_id
    return _via(
        coordinator,
        DeviceInfo(
            identifiers={(DOMAIN, f"{sid}_{device.entity_id}")},
            manufacturer=MANUFACTURER,
            model=hardware_model(device),
            name=device.name,
            serial_number=device.serial,
            sw_version=device.firmware,
        ),
    )


class Paxton10Entity(CoordinatorEntity[Paxton10Coordinator]):
    """Base for every Paxton10 entity."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: Paxton10Coordinator, object_id: str, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.site_id}_{object_id}_{key}"
        self._attr_translation_key = key


class DoorEntity(Paxton10Entity):
    def __init__(self, coordinator: Paxton10Coordinator, door: Door, key: str) -> None:
        super().__init__(coordinator, str(door.entity_id), key)
        self.door_id = door.entity_id
        self._attr_device_info = door_device_info(coordinator, door)

    @property
    def door(self) -> Door | None:
        return self.coordinator.data.doors.get(self.door_id)

    @property
    def available(self) -> bool:
        return super().available and self.door is not None


class HardwareEntity(Paxton10Entity):
    def __init__(self, coordinator: Paxton10Coordinator, device: Device, key: str) -> None:
        super().__init__(coordinator, str(device.entity_id), key)
        self.device_id = device.entity_id
        self._attr_device_info = hardware_device_info(coordinator, device)

    @property
    def device(self) -> Device | None:
        return self.coordinator.data.devices.get(self.device_id)

    @property
    def available(self) -> bool:
        return super().available and self.device is not None


@callback
def add_entities_dynamically(
    entry: Paxton10ConfigEntry,
    keys: Callable[[], Iterable[int]],
    build: Callable[[int], list[Entity]],
    add: Callable[[list[Entity]], None],
) -> None:
    """Add entities now, then for any door or device that appears later."""
    coordinator = entry.runtime_data
    known: set[int] = set()

    @callback
    def check() -> None:
        new = [k for k in keys() if k not in known]
        if not new:
            return
        known.update(new)
        add([e for k in new for e in build(k)])

    check()
    entry.async_on_unload(coordinator.async_add_listener(check))
    # When a device is removed, its entities go with it. Forget the id so they're added again
    # if the device comes back.
    entry.async_on_unload(coordinator.async_add_forget_listener(known.discard))
