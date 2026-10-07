"""Open buttons for doors and the gate. Created only when door control is on."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import PaxtonBlockedRequest, PaxtonError
from .connection import PaxtonForbidden
from .const import DOMAIN, LOCK_ACTOR
from .coordinator import Paxton10ConfigEntry, Paxton10Coordinator
from .entity import DoorEntity, add_entities_dynamically
from .models import Door

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1
RELEASE_PATH = "/api/v2/System/ActivateAppliances"


def release_payload(door: Door) -> list[dict[str, object]]:
    """The body the web UI sends for Control device > Open door."""
    return [
        {
            "State": None,
            "Id": door.entity_id,
            "ApplianceTypeId": door.appliance_type,
            "EntityTypeId": door.entity_type_id,
            "IsGroup": False,
            "ParentId": door.parent_id,
            "ActorId": LOCK_ACTOR,
        }
    ]


async def async_setup_entry(
    hass: HomeAssistant, entry: Paxton10ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    if not coordinator.options.allow_door_control:
        return
    add_entities_dynamically(
        entry,
        lambda: coordinator.data.doors,
        lambda d: [DoorOpenButton(coordinator, coordinator.data.doors[d])],
        async_add_entities,
    )


class DoorOpenButton(DoorEntity, ButtonEntity):
    """Releases the door for its configured open time. There is no lock command."""

    def __init__(self, coordinator: Paxton10Coordinator, door: Door) -> None:
        super().__init__(coordinator, door, "open")

    async def async_press(self) -> None:
        door = self.door
        if door is None:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="door_gone")
        _LOGGER.info("Opening %s (entity %s) from Home Assistant", door.name, door.entity_id)
        try:
            await self.coordinator.conn.post(RELEASE_PATH, release_payload(door))
        except PaxtonBlockedRequest as err:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="door_control_off") from err
        except PaxtonForbidden as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="door_forbidden",
                translation_placeholders={"door": door.name},
            ) from err
        except PaxtonError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="door_failed",
                translation_placeholders={"door": door.name, "error": str(err)},
            ) from err
