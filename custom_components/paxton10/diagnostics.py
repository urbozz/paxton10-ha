"""Diagnostics. Credentials, addresses, serials, and personal data are redacted."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_PASSWORD_HASH, CONF_TARGET, CONF_USERNAME, OPT_FALLBACK_TARGET
from .coordinator import Paxton10ConfigEntry

TO_REDACT = {
    CONF_PASSWORD_HASH,
    CONF_USERNAME,
    CONF_TARGET,
    OPT_FALLBACK_TARGET,
    "ip",
    "serial",
    "site_id",
    "user_name",
}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: Paxton10ConfigEntry) -> dict[str, Any]:
    coordinator = entry.runtime_data
    site = coordinator.data
    source = coordinator.source
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        "active_route": coordinator.conn.active_route,
        "last_update_success": coordinator.last_update_success,
        "last_event_id": getattr(source, "last_event_id", None),
        "event_source": getattr(source, "mode", None),
        "server": async_redact_data(asdict(site.server), TO_REDACT),
        "can_read_devices": site.can_read_devices,
        "can_read_summary": site.can_read_summary,
        "summary": site.summary,
        "doors": [asdict(d) for d in site.doors.values()],
        "devices": [async_redact_data(asdict(d), TO_REDACT) for d in site.devices.values()],
    }
