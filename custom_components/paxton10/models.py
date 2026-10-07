"""Site model: what the integration knows about doors, devices, and the server.

Parsers take the raw JSON from the Paxton10 API. No Home Assistant imports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .const import EVENT_TYPE_OTHER, EVENT_TYPES, INTERCOM_USER_PARAM, READERS

KIND_CONTROLLER = "controller"
KIND_ENTRY_PANEL = "entry_panel"

# Controller battery and power codes, from the enums in the Paxton10 4.11 web app
# (BatteryCharge, BatteryState, PSUPowerStatus). Codes the web app calls Unknown map to None.
BATTERY_CHARGE: dict[int, str] = {0: "not_connected", 1: "critical", 2: "low", 3: "good"}
BATTERY_STATE: dict[int, str] = {1: "discharging", 2: "charging"}
POWER_SUPPLY: dict[int, str] = {1: "failure", 2: "external"}



def battery_level(charge: int | None, state: int | None) -> str | None:
    # The web app's icon rule, `Charge || State || NotConnected` read as a BatteryCharge. A fitted battery
    # can report Charge 0 with State 2 (charging): the web app shows Low, and State 1 (discharging) Critical.
    if charge == 0 and state:
        charge = state
    return BATTERY_CHARGE.get(charge) if charge is not None else None


# System/Summary descriptions mapped to sensor keys.
SUMMARY_KEYS: dict[str, str] = {
    "Active users": "active_users",
    "Total users": "total_users",
    "Total devices": "total_devices",
    "Unacknowledged alarms": "unacknowledged_alarms",
    "Offline devices": "offline_devices",
}


@dataclass(frozen=True)
class Door:
    """A door, gate, or barrier."""

    entity_id: int
    name: str
    appliance_type: int
    entity_type_id: int
    parent_id: int
    group_name: str | None


@dataclass
class Device:
    """A door controller or entry panel."""

    entity_id: int
    kind: str
    name: str
    model: str
    serial: str | None
    firmware: str | None
    ip: str | None
    status: int | None
    last_contact: datetime | None
    battery_charge: int | None = None
    battery_state: int | None = None
    psu_state: int | None = None
    door_ids: tuple[int, ...] = ()

    @property
    def online(self) -> bool | None:
        # Status 1 for every device while the server's summary showed 0 offline devices.
        # Other values are assumed offline until checked against the web UI.
        if self.status is None:
            return None
        return self.status == 1


@dataclass
class Server:
    """The Paxton10 server itself."""

    site_id: str
    system_name: str
    server_name: str | None
    version: str | None
    utc_offset_minutes: int


@dataclass
class Site:
    """Everything discovered at setup and refreshed by the update source."""

    server: Server
    doors: dict[int, Door] = field(default_factory=dict)
    devices: dict[int, Device] = field(default_factory=dict)
    summary: dict[str, int] = field(default_factory=dict)
    # Which optional reads the account is allowed to make.
    can_read_devices: bool = True
    can_read_summary: bool = True


@dataclass(frozen=True)
class DoorEvent:
    """One event from the Paxton10 event log, reduced to what Home Assistant needs."""

    event_id: str
    event_type_id: int | None
    event_type: str
    time: datetime | None
    door_ids: tuple[int, ...]
    user_name: str | None
    reader: str | None = None  # entry or exit, on access events


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    # .NET writes seven fractional digits; Python accepts six.
    head, dot, rest = text.partition(".")
    if dot:
        n = 0
        while n < len(rest) and rest[n].isdigit():
            n += 1
        digits, tz = rest[:n], rest[n:]
        text = f"{head}.{digits[:6]}{tz}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def offset_suffix(minutes: int) -> str:
    sign = "+" if minutes >= 0 else "-"
    hours, mins = divmod(abs(minutes), 60)
    return f"{sign}{hours:02d}:{mins:02d}"


def parse_server(version: Any, server_name: Any, parameters: dict[str, Any]) -> Server:
    regional = parameters.get("RegionalSettings") or {}
    name = server_name.get("ServerName") if isinstance(server_name, dict) else None
    return Server(
        site_id=str(parameters["SiteId"]),
        system_name=parameters.get("SystemName") or "Paxton10",
        server_name=name,
        version=version if isinstance(version, str) else None,
        utc_offset_minutes=int(regional.get("MinutesUtcOffset") or 0),
    )


def parse_summary(body: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for stat in body if isinstance(body, list) else []:
        for item in stat.get("Data") or []:
            key = SUMMARY_KEYS.get(item.get("Description"))
            if key is not None and isinstance(item.get("Value"), int):
                out[key] = item["Value"]
    return out


def _mapped_doors(device: dict[str, Any]) -> tuple[int, ...]:
    ids: list[int] = []
    for connector in device.get("Connectors") or []:
        for peripheral in connector.get("Peripherals") or []:
            mapped = peripheral.get("MappedAppliance") or {}
            appliance_id = mapped.get("ApplianceId")
            if isinstance(appliance_id, int) and appliance_id not in ids:
                ids.append(appliance_id)
    return tuple(ids)


def parse_devices(body: Any, kind: str) -> dict[int, Device]:
    out: dict[int, Device] = {}
    for raw in body if isinstance(body, list) else []:
        entity_id = raw.get("EntityId")
        if not isinstance(entity_id, int):
            continue
        battery = raw.get("BatteryStatus") or {}
        psu = raw.get("PSUPowerStatus") or {}
        name = raw.get("Name") if kind == KIND_ENTRY_PANEL else None
        out[entity_id] = Device(
            entity_id=entity_id,
            kind=kind,
            name=name or raw.get("Description") or f"Device {entity_id}",
            model=raw.get("ModelName") or raw.get("Description") or kind,
            serial=raw.get("UniqueId"),
            firmware=raw.get("FirmwareVersion"),
            ip=raw.get("IPv4Address"),
            status=raw.get("Status"),
            last_contact=parse_time(raw.get("LastContact")),
            battery_charge=battery.get("Charge") if kind == KIND_CONTROLLER else None,
            battery_state=battery.get("State") if kind == KIND_CONTROLLER else None,
            psu_state=psu.get("PowerState") if kind == KIND_CONTROLLER else None,
            door_ids=_mapped_doors(raw),
        )
    return out


def name_hardware(devices: dict[int, Device], doors: dict[int, Door]) -> None:
    """Name controllers and entry panels after the door they serve.

    Controllers all share one description, and an entry panel nobody named reports its serial
    as its name. Never build a name from the serial: diagnostics redact serials, and names are logged.
    """
    for device in devices.values():
        names = [doors[d].name for d in device.door_ids if d in doors]
        if device.kind == KIND_CONTROLLER:
            device.name = f"{names[0]} controller" if names else f"Controller {device.entity_id}"
        elif names:
            device.name = f"{names[0]} entry panel"
        elif device.name == device.serial or device.name.isdigit():
            device.name = f"Entry panel {device.entity_id}"


def door_model(appliance_type: int) -> str:
    return {1: "Door", 2: "Gate", 3: "Barrier"}.get(appliance_type, "Door")


def hardware_model(device: Device) -> str:
    return device.model if device.kind == KIND_CONTROLLER else "Paxton10 Entry Panel"


def _event_door_ids(raw: dict[str, Any]) -> tuple[int, ...]:
    # Live door events leave ApplianceIds empty and name the door in ApplianceData.
    ids = [i for i in raw.get("ApplianceIds") or [] if isinstance(i, int)]
    appliance = raw.get("ApplianceData")
    if isinstance(appliance, dict) and isinstance(appliance.get("ApplianceId"), int):
        ids.append(appliance["ApplianceId"])
    return tuple(dict.fromkeys(ids))


def parse_event(raw: dict[str, Any], include_user: bool) -> DoorEvent | None:
    # 4.11 sends a 24-character string id. It doesn't sort by time, so the source tracks ids it has seen.
    event_id = raw.get("EventId")
    if isinstance(event_id, bool) or not isinstance(event_id, (str, int)) or event_id == "":
        return None
    type_id = raw.get("EventTypeId")
    return DoorEvent(
        event_id=str(event_id),
        event_type_id=type_id if isinstance(type_id, int) else None,
        event_type=EVENT_TYPES.get(type_id, EVENT_TYPE_OTHER) if isinstance(type_id, int) else EVENT_TYPE_OTHER,
        time=parse_time(raw.get("EventTime")),
        door_ids=_event_door_ids(raw),
        user_name=(_user_name(raw.get("UserData")) or _intercom_user(raw)) if include_user else None,
        reader=_reader(raw),
    )


def _intercom_user(raw: dict[str, Any]) -> str | None:
    fields = raw.get("TranslatableFields")
    if not isinstance(fields, dict) or (index := INTERCOM_USER_PARAM.get(fields.get("InformationTranslationKey"))) is None:  # type: ignore[arg-type]
        return None
    params = fields.get("Parameters")
    if not isinstance(params, list) or len(params) <= index or not isinstance(params[index], dict):
        return None
    name = params[index].get("Description")
    return name.strip() or None if isinstance(name, str) else None


def _reader(raw: dict[str, Any]) -> str | None:
    fields = raw.get("TranslatableFields")
    params = fields.get("Parameters") if isinstance(fields, dict) else None
    for param in params if isinstance(params, list) else []:
        if isinstance(param, dict) and (reader := READERS.get(param.get("Value"))):  # type: ignore[arg-type]
            return reader
    return None


def _user_name(user_data: Any) -> str | None:
    # Live 4.11 sends a dict with UserName. Accept the other likely forms too.
    if isinstance(user_data, list):
        user_data = user_data[0] if user_data else None
    if isinstance(user_data, str):
        return user_data or None
    if not isinstance(user_data, dict):
        return None
    for key in ("Name", "UserName", "FullName", "DisplayName"):
        value = user_data.get(key)
        if isinstance(value, str) and value:
            return value
    parts = [user_data.get(k) for k in ("FirstName", "Surname", "LastName")]
    joined = " ".join(p for p in parts if isinstance(p, str) and p)
    return joined or None


def event_filter(utc_offset_minutes: int) -> dict[str, Any]:
    """The body the Paxton10 web UI posts to /api/v2/Events/."""
    suffix = offset_suffix(utc_offset_minutes)
    return {
        "OrderBy": "EventTime",
        "SortDirection": "Desc",
        "ApplianceIds": None,
        "UserIds": None,
        "EventCategoryIds": None,
        "EventTypeIds": None,
        "CustomDataIds": [],
        "StandardEventColumns": ["time", "userName", "where", "SiteName", "info", "entityIconId"],
        "AlarmEventsFirst": False,
        "StartTimeWithOffset": f"2000-01-01T00:00:00.000{suffix}",
        "EndTimeWithOffset": f"3000-01-01T00:00:00.000{suffix}",
        "IsTimeRangeSelected": False,
        "Video": False,
        "ClusterIds": None,
    }


# Event grid column ids in the 4.11 web app (gridHelpers): userName 0, where 2, info 4, time 6, entityIconId 11.
LIVE_EVENT_COLUMNS = [6, 0, 2, 4, 11]
# DMSEventCategory: every category the event log has (valid access through acknowledged alarms).
ALL_EVENT_CATEGORIES = list(range(1, 10))


def live_event_filter(utc_offset_minutes: int, all_categories: bool = False) -> dict[str, Any]:
    """The filter the web UI passes to the hub's SubscribeToLiveEvents.

    Unlike the REST event query, the hub only accepts column ids as numbers.
    EventCategoryIds None means no restriction, as in the web app's LiveEventsHub defaults.
    """
    suffix = offset_suffix(utc_offset_minutes)
    return {
        "SortDirection": "Desc",
        "OrderBy": "EventTime",
        "OrderByCustomField": None,
        "StandardEventColumns": list(LIVE_EVENT_COLUMNS),
        "EventTypeIds": None,
        "CustomDataIds": [],
        "UserIds": None,
        "AlarmEventsFirst": False,
        "ApplianceIds": None,
        "StartTimeWithOffset": f"2000-01-01T00:00:00.000{suffix}",
        "EndTimeWithOffset": f"3000-01-01T00:00:00.000{suffix}",
        "IsTimeRangeSelected": False,
        "EventCategoryIds": list(ALL_EVENT_CATEGORIES) if all_categories else None,
        "Video": False,
    }
