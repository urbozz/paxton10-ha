"""One event entity per door, fed by the coordinator's event stream."""

from __future__ import annotations

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import EVENT_TYPE_OTHER, EVENT_TYPES
from .coordinator import Paxton10ConfigEntry, Paxton10Coordinator
from .entity import DoorEntity, add_entities_dynamically
from .models import Door, DoorEvent

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: Paxton10ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    add_entities_dynamically(
        entry,
        lambda: coordinator.data.doors,
        lambda d: [DoorEventEntity(coordinator, coordinator.data.doors[d])],
        async_add_entities,
    )


class DoorEventEntity(DoorEntity, EventEntity):
    _attr_event_types = [*EVENT_TYPES.values(), EVENT_TYPE_OTHER]  # noqa: RUF012
    _attr_name = None  # the door's main entity: event.<door>, not event.<door>_door_event

    def __init__(self, coordinator: Paxton10Coordinator, door: Door) -> None:
        super().__init__(coordinator, door, "door_event")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.async_add_event_listener(self.door_id, self._handle_event))

    @callback
    def _handle_event(self, event: DoorEvent) -> None:
        attributes: dict[str, object] = {"event_id": event.event_id, "event_type_id": event.event_type_id}
        if event.time:
            attributes["time"] = event.time.isoformat()
        if event.reader:
            attributes["reader"] = event.reader
        if self.coordinator.options.include_user_names and event.user_name:
            attributes["user_name"] = event.user_name
        if self.coordinator.options.include_user_names and event.credential:
            attributes["credential"] = event.credential
        self._trigger_event(event.event_type, attributes)
        self.async_write_ha_state()
