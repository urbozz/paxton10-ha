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
OPT_INCLUDE_CREDENTIAL_DESCRIPTIONS = "include_credential_descriptions"

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

# EventTypeId values from the Paxton10 web app's DMSEventType enum. Not the CategoryId values.
# The intercom types (14x) aren't in the enum: they were read from a live 4.11 event log.
EVENT_TYPES: dict[int, str] = {
    1: "unknown_credential",  # credNotFound: "Access denied - Unknown credential"
    2: "lost_credential",  # credLost
    3: "access_not_made",  # accessNotMade
    4: "no_permission",  # noPermission
    5: "access_permitted",
    6: "exit_request",  # "Valid exit request at Door": the exit button
    7: "opened_by_software",  # "Door unlocked from software" or "Gate unlocked from software"
    8: "unlocked",  # timedUnlock
    9: "relocked",  # timedRelock
    10: "left_open",
    11: "closed",
    16: "forced",
    17: "toggled_open",  # toggleOpen
    18: "toggled_closed",  # toggleClose
    140: "intercom_unlocked",  # "Door unlocked by <user>"
    141: "intercom_not_unlocked",  # "Door not unlocked by <user>": the called user declined (live 4.11 export)
    142: "call_not_answered",  # "Call not answered by <user>"
    145: "call_made",  # "Call made from <panel> to <user>"
}
# TranslatableFields parameter values on access events: "[Entry reader]" and "[Exit reader]".
READERS: dict[int, str] = {541134: "entry", 541135: "exit"}
# Intercom events name the called user in a TranslatableFields parameter, not in UserData. The panel's
# parameter looks the same (Value 0), so the text template's key says which position is the user:
# 530058 "Door unlocked by <user>" (door, user), 530060 "Call not answered by <user>" (user, panel),
# and 530083 "Call made from <panel> to <user>" (panel, user).
INTERCOM_USER_PARAM: dict[int, int] = {530058: 1, 530060: 0, 530083: 1}
EVENT_TYPE_OTHER = "other"


# Paxton's credential type names (the web app's CredentialTypes list), keyed by letters only.
# CredentialData.Credential on an event is the credential's free-text description: often just the
# type name, sometimes with a number, but also an email address, a date, or a note. Only a plain
# type name is ever turned into a credential type; any other description gives none.
CREDENTIAL_TYPES: dict[str, str] = {
    "proximitycard": "proximity_card",
    "proximityisocard": "proximity_iso_card",
    "proximityisocardmagstrip": "proximity_iso_card_magstrip",
    "encryptedproximitycard": "encrypted_proximity_card",
    "keyfob": "keyfob",
    "encryptedkeyfob": "encrypted_keyfob",
    "minihandsfreekeyfob": "mini_hands_free_keyfob",
    "handsfreecredential": "hands_free_credential",
    "handsfreekeycard": "hands_free_key_card",
    "smartcredential": "smart_credential",
    "securetoken": "secure_token",
    "watchprox": "watchprox",
    "vehiclenumberplate": "vehicle_number_plate",
    "mobilephonenumber": "mobile_phone_number",
    "pin": "pin",
    "password": "password",
}
