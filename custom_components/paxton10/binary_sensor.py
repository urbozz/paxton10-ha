"""Connectivity for controllers and entry panels, and the lock state of each door."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.binary_sensor.const import BinarySensorDeviceClass
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import Paxton10ConfigEntry, Paxton10Coordinator
from .entity import DoorEntity, HardwareEntity, add_entities_dynamically
from .models import (
    DOOR_FORCED_OR_LEFT_OPEN,
    DOOR_LOCKED,
    DOOR_OFFLINE,
    DOOR_ONLINE,
    DOOR_STATES,
    DOOR_UNLOCKED,
    Device,
    Door,
)

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
    if coordinator.data.can_read_door_states:
        add_entities_dynamically(
            entry,
            lambda: coordinator.data.doors,
            lambda d: [
                DoorLockSensor(coordinator, coordinator.data.doors[d]),
                DoorAlarmSensor(coordinator, coordinator.data.doors[d]),
            ],
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


class DoorStateEntity(DoorEntity, BinarySensorEntity):
    """A door sensor fed by Paxton's door state. Unavailable while Paxton reports the door offline."""

    @property
    def _state(self) -> int | None:
        return self.coordinator.data.door_states.get(self.door_id)

    @property
    def available(self) -> bool:
        return super().available and self._state != DOOR_OFFLINE


class DoorLockSensor(DoorStateEntity):
    """On while the door is unlocked: for its open time after a release, or held open.

    Paxton reports the lock state, not a door contact. Forced or left open (which needs a
    door contact) counts as unlocked, and shows in the door_state attribute.
    """

    _attr_device_class = BinarySensorDeviceClass.LOCK

    def __init__(self, coordinator: Paxton10Coordinator, door: Door) -> None:
        super().__init__(coordinator, door, "lock")

    @property
    def is_on(self) -> bool | None:
        state = self._state
        if state in (DOOR_UNLOCKED, DOOR_FORCED_OR_LEFT_OPEN):
            return True
        if state == DOOR_LOCKED:
            return False
        return None  # "online" or a value Paxton hasn't documented: the lock state isn't known

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        state = self._state
        return {"door_state": DOOR_STATES.get(state, "unknown") if state is not None else None}


class DoorAlarmSensor(DoorStateEntity):
    """On while Paxton reports the door forced or left open.

    UNTESTED: built from the Paxton10 4.11 web app's ApplianceState enum (forcedLeftOpenAlarm = 3).
    No live site has reported state 3 yet: it needs a door contact, and the test site has none
    wired. Disabled by default until confirmed. How the state clears (relock, close, or alarm
    acknowledgement) is also unconfirmed.
    """

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: Paxton10Coordinator, door: Door) -> None:
        super().__init__(coordinator, door, "forced_or_left_open")

    @property
    def is_on(self) -> bool | None:
        state = self._state
        if state == DOOR_FORCED_OR_LEFT_OPEN:
            return True
        if state in (DOOR_UNLOCKED, DOOR_LOCKED, DOOR_ONLINE):
            return False
        return None
