"""Fixtures. A fake transport sits under the real PaxtonClient, so the allowlist runs in every test.

All data here is synthetic. Never put probe dumps or live data in tests.
"""

from __future__ import annotations

import json
from collections.abc import Generator
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.paxton10.api import (
    PaxtonClient,
    PaxtonError,
    Response,
    password_hash,
)
from custom_components.paxton10.const import (
    CONF_PASSWORD_HASH,
    CONF_ROUTE,
    CONF_TARGET,
    CONF_USERNAME,
    DOMAIN,
    ROUTE_DIRECT,
)

SITE_ID = "00000000-0000-4000-8000-000000000001"
USERNAME = "ha@example.com"
PASSWORD = "test-password"
GROUP_SUFFIX = "/Children?includeEmptyGroups=true&page=0&pageSize=100&sortBy=null&sortDirection=null"


def tree() -> dict[int, dict[str, Any]]:
    def group(gid: int, name: str, children: bool = True) -> dict[str, Any]:
        return {"GroupEntityId": gid, "GroupName": name, "HasChildren": children, "ParentId": 4}

    door = {"EntityId": 2001, "Name": "Main Entrance Door", "ApplianceType": 1}
    return {
        4: {
            "Groups": [
                group(3001, "Ground Floor"),
                group(3002, "Car Park"),
                group(3003, "Building A"),
                group(1300, "Empty", False),
            ],
            "Appliances": [],
        },
        3001: {"Groups": [], "Appliances": [door]},
        3002: {"Groups": [], "Appliances": [{"EntityId": 2002, "Name": "Vehicle Gate", "ApplianceType": 2}]},
        # The same door again, plus a contact input that isn't a door.
        3003: {"Groups": [], "Appliances": [door, {"EntityId": 1500, "Name": "Contact", "ApplianceType": 6}]},
    }


def controller(entity_id: int, door_id: int, status: int = 1) -> dict[str, Any]:
    return {
        "Description": "Paxton10 Door Controller GEN1",
        "ModelName": "GEN1",
        "DeviceType": 9,
        "EntityId": entity_id,
        "Status": status,
        "UniqueId": f"C{entity_id}",
        "FirmwareVersion": "3.00.17013.672",
        "IPv4Address": "192.0.2.10",
        "LastContact": "2026-01-02T03:04:05.7697139+00:00",
        "BatteryStatus": {"Charge": 3, "State": 2},
        "PSUPowerStatus": {"PowerState": 2},
        "Connectors": [{"Peripherals": [{"MappedAppliance": {"ApplianceId": door_id}}, {"MappedAppliance": None}]}],
    }


def panel(entity_id: int) -> dict[str, Any]:
    return {
        "Description": "Paxton10 Entry Panel",
        "Name": "Building A",
        "DeviceType": 15,
        "EntityId": entity_id,
        "Status": 1,
        "UniqueId": f"P{entity_id}",
        "FirmwareVersion": "4.02.16916.801",
        "IPv4Address": "192.0.2.20",
        "LastContact": None,
        "Connectors": [],
    }


def event(event_id: int, type_id: int = 7, door: int = 2001, user: Any = None) -> dict[str, Any]:
    return {
        "EventId": event_id,
        "EventTime": "2026-10-07T14:00:00.123+01:00",
        "EventTypeId": type_id,
        "CategoryId": 7,
        "ApplianceIds": [door],
        "UserData": user,
        "Information": "",
    }


@dataclass
class FakeServer:
    """Answers API calls like a small Paxton10 site."""

    password_hash: str = password_hash(PASSWORD)
    groups: dict[int, dict[str, Any]] = field(default_factory=tree)
    controllers: list[dict[str, Any]] = field(default_factory=lambda: [controller(4001, 2001)])
    panels: list[dict[str, Any]] = field(default_factory=lambda: [panel(4002)])
    events: list[dict[str, Any]] = field(default_factory=lambda: [event(100), event(99)])
    status: dict[str, int] = field(default_factory=dict)  # path -> forced HTTP status
    down: set[str] = field(default_factory=set)  # targets that can't be reached
    expire_tokens: int = 0  # reject this many authed calls with 401
    calls: list[tuple[str, str, str, Any]] = field(default_factory=list)  # (target, method, path, body)
    tokens: int = 0

    def entities(self) -> dict[int, dict[str, Any]]:
        return {
            2001: {"Id": 2001, "EntityTypeId": 26, "ParentId": 3001, "Description": "Main Entrance Door"},
            2002: {"Id": 2002, "EntityTypeId": 26, "ParentId": 3002, "Description": "Vehicle Gate"},
            1200: {"Id": 1200, "EntityTypeId": 26, "ParentId": 3001, "Description": "New Door"},
        }

    def handle(self, target: str, method: str, path: str, token: str | None, body: str | None) -> Response:
        parsed = json.loads(body) if body and path != "/token" else None
        self.calls.append((target, method, path, parsed))
        if target in self.down:
            raise PaxtonError(f"{target} unreachable")
        if path == "/token":
            fields = dict(p.split("=", 1) for p in (body or "").split("&"))
            if fields.get("password") != self.password_hash:
                return Response(400, {"error": "AuthenticationFailedInvalidCredentials"})
            self.tokens += 1
            return Response(200, {"access_token": f"tok{self.tokens}", "expires_in": 43199})
        if self.expire_tokens:
            self.expire_tokens -= 1
            return Response(401, None)
        if path in self.status:
            return Response(self.status[path], None)
        if path == "/api/v2/System/Parameters/All":
            return Response(
                200, {"SiteId": SITE_ID, "SystemName": "Test Site", "RegionalSettings": {"MinutesUtcOffset": 60}}
            )
        if path == "/api/v1/System/Software/Version":
            return Response(200, "4.11.9753.20528")
        if path == "/api/v1/System/ServerName":
            return Response(200, {"ServerName": "PAXTON10-TEST"})
        if path == "/api/v1/System/Summary":
            return Response(
                200,
                [
                    {
                        "Data": [
                            {"Value": 28, "Description": "Active users"},
                            {"Value": 83, "Description": "Total users"},
                            {"Value": 2, "Description": "Total devices"},
                        ]
                    },
                    {
                        "Data": [
                            {"Value": 0, "Description": "Unacknowledged alarms"},
                            {"Value": 0, "Description": "Offline devices"},
                            {"Value": 1, "Description": "Logged in users"},
                        ]
                    },
                ],
            )
        if path.startswith("/api/v3/groups/") and path.endswith(GROUP_SUFFIX):
            gid = int(path.removeprefix("/api/v3/groups/").removesuffix(GROUP_SUFFIX))
            return Response(200, {"GroupEntityId": gid, **self.groups.get(gid, {"Groups": [], "Appliances": []})})
        if path.startswith("/api/v1/Entity/"):
            ent = self.entities().get(int(path.rsplit("/", 1)[1]))
            return Response(200 if ent else 404, ent)
        if path == "/api/v1/Devices/1/false?page=0&pageSize=100":
            return Response(200, self.controllers)
        if path == "/api/v1/Devices/3/false?page=0&pageSize=100":
            return Response(200, self.panels)
        if path.startswith("/api/v2/Events/?page=0"):
            return Response(200, {"Result": sorted(self.events, key=lambda e: -e["EventId"]), "TotalPages": 1})
        if path == "/api/v2/System/ActivateAppliances":
            return Response(200, None)
        return Response(404, None)

    def sent(self, method: str, path: str) -> list[Any]:
        return [c[3] for c in self.calls if c[1] == method and c[2] == path]


class FakeTransport:
    def __init__(self, server: FakeServer, name: str, target: str) -> None:
        self.server, self.name, self.target = server, name, target
        self.started = 0

    async def start(self) -> None:
        self.started += 1

    async def close(self) -> None:
        return None

    async def send(self, method: str, path: str, token: str | None, body: str | None, content_type: str) -> Response:
        return self.server.handle(self.target, method, path, token, body)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    return None


@pytest.fixture
def server() -> Generator[FakeServer]:
    fake = FakeServer()

    def create(session: Any, route: str, target: str, allow_writes: bool) -> PaxtonClient:
        return PaxtonClient(FakeTransport(fake, route, target), allow_writes=allow_writes)  # type: ignore[arg-type]

    with patch("custom_components.paxton10.connection.create_client", side_effect=create):
        yield fake


def make_entry(options: dict[str, Any] | None = None, **data: Any) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Test Site",
        unique_id=SITE_ID,
        data={
            CONF_ROUTE: ROUTE_DIRECT,
            CONF_TARGET: "192.0.2.1",
            CONF_USERNAME: USERNAME,
            CONF_PASSWORD_HASH: password_hash(PASSWORD),
            **data,
        },
        options=options or {},
    )


def entity_id(hass: Any, platform: str, object_id: str | int, key: str) -> str | None:
    """Look an entity up by unique ID. HA 2026 puts the area in entity IDs, so don't guess them."""
    from homeassistant.helpers import entity_registry as er

    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{SITE_ID}_{object_id}_{key}")
