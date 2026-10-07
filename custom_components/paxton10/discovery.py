"""Read the site layout: server, doors, controllers, and entry panels. Reads only."""

from __future__ import annotations

import logging
from typing import Any

from .api import PaxtonError
from .connection import PaxtonConnection, PaxtonForbidden, PaxtonNotFound
from .const import DOOR_APPLIANCE_TYPES, ROOT_GROUP
from .models import (
    KIND_CONTROLLER,
    KIND_ENTRY_PANEL,
    Device,
    Door,
    Server,
    Site,
    name_hardware,
    parse_devices,
    parse_server,
    parse_summary,
)

_LOGGER = logging.getLogger(__name__)

PATH_VERSION = "/api/v1/System/Software/Version"
PATH_SERVER_NAME = "/api/v1/System/ServerName"
PATH_PARAMETERS = "/api/v2/System/Parameters/All"
PATH_SUMMARY = "/api/v1/System/Summary"
PATH_CONTROLLERS = "/api/v1/Devices/1/false?page=0&pageSize=100"
PATH_ENTRY_PANELS = "/api/v1/Devices/3/false?page=0&pageSize=100"
MAX_GROUPS = 200  # stop a malformed tree from looping


def group_path(group_id: int) -> str:
    return (
        f"/api/v3/groups/{group_id}/Children?includeEmptyGroups=true&page=0&pageSize=100&sortBy=null&sortDirection=null"
    )


async def read_server(conn: PaxtonConnection) -> Server:
    parameters = await conn.get(PATH_PARAMETERS)
    if not isinstance(parameters, dict) or not parameters.get("SiteId"):
        raise PaxtonError("System/Parameters has no SiteId")
    version = await _optional(conn, PATH_VERSION)
    server_name = await _optional(conn, PATH_SERVER_NAME)
    return parse_server(version, server_name, parameters)


async def _optional(conn: PaxtonConnection, path: str) -> Any:
    try:
        return await conn.get(path)
    except PaxtonForbidden:
        return None


async def read_doors(conn: PaxtonConnection) -> dict[int, Door]:
    """Walk the device tree from the root group. Doors appear in several groups; keep one of each."""
    group_names: dict[int, str] = {}
    found: dict[int, dict[str, Any]] = {}
    queue: list[int] = [ROOT_GROUP]
    seen: set[int] = set()
    while queue and len(seen) < MAX_GROUPS:
        group_id = queue.pop(0)
        if group_id in seen:
            continue
        seen.add(group_id)
        body = await conn.get(group_path(group_id))
        if not isinstance(body, dict):
            continue
        for group in body.get("Groups") or []:
            gid = group.get("GroupEntityId")
            if isinstance(gid, int):
                group_names[gid] = group.get("GroupName") or ""
                if group.get("HasChildren") is not False:
                    queue.append(gid)
        for appliance in body.get("Appliances") or []:
            eid = appliance.get("EntityId")
            if isinstance(eid, int) and appliance.get("ApplianceType") in DOOR_APPLIANCE_TYPES:
                found.setdefault(eid, appliance)

    doors: dict[int, Door] = {}
    for eid, appliance in found.items():
        # The release call needs the door's own parent group, which only the entity record gives.
        try:
            entity = await conn.get(f"/api/v1/Entity/{eid}")
        except (PaxtonForbidden, PaxtonNotFound):
            entity = None
        if not isinstance(entity, dict) or not isinstance(entity.get("ParentId"), int):
            _LOGGER.warning("Skipping door entity %s: no parent group in its entity record", eid)
            continue
        doors[eid] = Door(
            entity_id=eid,
            name=appliance.get("Name") or entity.get("Description") or f"Door {eid}",
            appliance_type=int(appliance["ApplianceType"]),
            entity_type_id=int(entity.get("EntityTypeId") or 26),
            parent_id=entity["ParentId"],
            group_name=group_names.get(entity["ParentId"]) or None,
        )
    return doors


async def read_devices(conn: PaxtonConnection) -> dict[int, Device]:
    devices = parse_devices(await conn.get(PATH_CONTROLLERS), KIND_CONTROLLER)
    devices.update(parse_devices(await conn.get(PATH_ENTRY_PANELS), KIND_ENTRY_PANEL))
    return devices


async def read_summary(conn: PaxtonConnection) -> dict[str, int]:
    return parse_summary(await conn.get(PATH_SUMMARY))


async def discover_site(conn: PaxtonConnection) -> Site:
    """Full read of the site. Optional reads the account can't make are skipped, not fatal."""
    site = Site(server=await read_server(conn), doors=await read_doors(conn))
    try:
        site.devices = await read_devices(conn)
        name_hardware(site.devices, site.doors)
    except PaxtonForbidden:
        site.can_read_devices = False
        _LOGGER.info("This Paxton account can't read devices; no controller or panel entities")
    try:
        site.summary = await read_summary(conn)
    except PaxtonForbidden:
        site.can_read_summary = False
        _LOGGER.info("This Paxton account can't read the system summary")
    return site
