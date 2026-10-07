"""Constants for the Paxton10 integration."""

from __future__ import annotations

DOMAIN = "paxton10"
MANUFACTURER = "Paxton"

# Config entry data
CONF_ROUTE = "route"
CONF_TARGET = "target"  # server address (direct) or remote ID (remote)
CONF_USERNAME = "username"
CONF_PASSWORD_HASH = "password_hash"  # SHA-1 hex, as the API expects. Never the plain password.

ROUTE_DIRECT = "direct"
ROUTE_REMOTE = "remote"

# Options
OPT_ALLOW_DOOR_CONTROL = "allow_door_control"
OPT_DEVICE_INTERVAL = "device_interval"
OPT_EVENT_INTERVAL = "event_interval"
OPT_FALLBACK = "fallback"
OPT_FALLBACK_TARGET = "fallback_target"
OPT_INCLUDE_USER_NAMES = "include_user_names"

DEFAULT_DEVICE_INTERVAL = 30
DEFAULT_EVENT_INTERVAL = 10
MIN_EVENT_INTERVAL = 5
MIN_DEVICE_INTERVAL = 10

EVENT_PAXTON10 = "paxton10_event"
EVENT_PAGE_SIZE = 50

ROOT_GROUP = 4
LOCK_ACTOR = 1002  # actorIds.lockOut in the web app
# Appliance types that are doors in this sense: door, gate, barrier.
DOOR_APPLIANCE_TYPES = (1, 2, 3)

# EventTypeId values from the Paxton10 web app. Not the CategoryId values.
EVENT_TYPES: dict[int, str] = {
    5: "access_permitted",
    7: "opened_by_software",
    8: "unlocked",
    9: "relocked",
    10: "left_open",
    11: "closed",
    16: "forced",
}
EVENT_TYPE_OTHER = "other"
