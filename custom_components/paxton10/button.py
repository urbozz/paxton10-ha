"""Open buttons for doors, gates, and barriers. Created only when door control is on."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import PaxtonBlockedRequest, PaxtonError
from .connection import PaxtonForbidden
from .const import DOMAIN, LOCK_ACTOR
from .coordinator import Paxton10ConfigEntry, Paxton10Coordinator
from .discovery import read_door_states
from .entity import DoorEntity, add_entities_dynamically
from .models import DOOR_FORCED_OR_LEFT_OPEN, DOOR_UNLOCKED, Door
from .source import MODE_LIVE

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1
RELEASE_PATH = "/api/v2/System/ActivateAppliances"
# Paxton accepts a release it then doesn't carry out, for example while an intruder alarm is armed or
# the door is in lockdown. The web app shows a release as failed if the door's state doesn't change
# within 5 s, so the button does the same.
CONFIRM_TIMEOUT = 5.0
CONFIRM_POLL = 1.0  # how often to read the door's state while the live feed is down
OPEN_STATES = frozenset({DOOR_UNLOCKED, DOOR_FORCED_OR_LEFT_OPEN})


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
    """Releases the door for its configured door open time, as Paxton's control device does. There is no lock command."""

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
        if await self._confirm_unlocked(door.entity_id) is False:
            _LOGGER.warning("Paxton10 accepted the release of %s, but the door didn't unlock", door.name)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="door_not_opened",
                translation_placeholders={"door": door.name},
            )

    async def _confirm_unlocked(self, door_id: int) -> bool | None:
        """Whether the door unlocked within CONFIRM_TIMEOUT. None if that can't be told."""
        coordinator = self.coordinator
        if not coordinator.data.can_read_door_states:
            return None
        if coordinator.data.door_states.get(door_id) in OPEN_STATES:
            return None  # already unlocked, for example by a time profile or toggle
        live = coordinator.source is not None and getattr(coordinator.source, "mode", None) == MODE_LIVE
        unlocked = asyncio.Event()

        def check() -> None:
            if coordinator.data.door_states.get(door_id) in OPEN_STATES:
                unlocked.set()

        remove = coordinator.async_add_listener(check)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + CONFIRM_TIMEOUT
        try:
            while True:
                # A live push confirms it. Without the live feed, read the door's state instead,
                # and read it once more at the end in case a push was missed.
                left = deadline - loop.time()
                if not live or left <= 0:
                    try:
                        states = await read_door_states(coordinator.conn, [door_id])
                    except PaxtonError as err:
                        _LOGGER.debug("Couldn't read the state of door %s after opening it: %s", door_id, err)
                        return None
                    if states.get(door_id) in OPEN_STATES:
                        return True
                if left <= 0:
                    return False
                try:
                    async with asyncio.timeout(min(left, CONFIRM_POLL)):
                        await unlocked.wait()
                    return True
                except TimeoutError:
                    pass
        finally:
            remove()
