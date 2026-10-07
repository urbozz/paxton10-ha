"""Logbook lines for Paxton10 events, so the door's history shows who opened it."""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.components.logbook.const import (
    LOGBOOK_ENTRY_ENTITY_ID,
    LOGBOOK_ENTRY_MESSAGE,
    LOGBOOK_ENTRY_NAME,
)
from homeassistant.core import Event, HomeAssistant, callback

from .const import DOMAIN, EVENT_PAXTON10, EVENT_TYPE_OTHER


@callback
def async_describe_events(
    hass: HomeAssistant,
    async_describe_event: Callable[[str, str, Callable[[Event], dict[str, str | None]]], None],
) -> None:
    @callback
    def describe(event: Event) -> dict[str, str | None]:
        data = event.data
        event_type = data.get("event_type") or EVENT_TYPE_OTHER
        if event_type == EVENT_TYPE_OTHER:
            message = f"logged Paxton event type {data.get('event_type_id')}"
        else:
            message = f"logged {event_type.replace('_', ' ')}"
        if user := data.get("user_name"):
            # The user a call went to, or the user who opened or released the door.
            message += f" {'to' if event_type == 'call_made' else 'by'} {user}"
        return {
            LOGBOOK_ENTRY_NAME: data.get("door_name") or "Paxton10",
            LOGBOOK_ENTRY_MESSAGE: message,
            LOGBOOK_ENTRY_ENTITY_ID: data.get("entity_id"),
        }

    async_describe_event(DOMAIN, EVENT_PAXTON10, describe)
