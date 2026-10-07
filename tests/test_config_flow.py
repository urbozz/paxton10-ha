"""Config flow, reauth, reconfigure, and options."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.paxton10.api import password_hash
from custom_components.paxton10.config_flow import door_sample, normalize_target
from custom_components.paxton10.const import (
    CONF_PASSWORD_HASH,
    CONF_ROUTE,
    CONF_TARGET,
    CONF_USERNAME,
    DOMAIN,
    OPT_ALLOW_DOOR_CONTROL,
    OPT_DEVICE_INTERVAL,
    OPT_EVENT_INTERVAL,
    OPT_FALLBACK,
    OPT_FALLBACK_TARGET,
    OPT_INCLUDE_USER_NAMES,
    ROUTE_DIRECT,
    ROUTE_REMOTE,
)
from custom_components.paxton10.models import Door, Server, Site

from .conftest import PASSWORD, SITE_ID, USERNAME, FakeServer, entity_id, make_entry


async def _to_server(hass: HomeAssistant, route: str) -> str:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_ROUTE: route})
    # One step id per route, so the field label matches the route.
    assert result["step_id"] == f"server_{route}"
    return result["flow_id"]


def _server(target: str, password: str = PASSWORD) -> dict[str, str]:
    return {CONF_TARGET: target, CONF_USERNAME: f" {USERNAME} ", CONF_PASSWORD: password}


@pytest.mark.parametrize(
    ("route", "typed", "target"),
    [
        (ROUTE_DIRECT, " 192.0.2.1 ", "192.0.2.1"),
        (ROUTE_DIRECT, "https://192.0.2.1/login", "192.0.2.1"),
        (ROUTE_REMOTE, "abc123", "abc123"),
        (ROUTE_REMOTE, "https://ABC123.paxton10remote.com/#/login", "abc123"),
    ],
)
async def test_user_flow(hass: HomeAssistant, server: FakeServer, route: str, typed: str, target: str) -> None:
    flow_id = await _to_server(hass, route)
    result = await hass.config_entries.flow.async_configure(flow_id, _server(typed))
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"
    assert result["description_placeholders"] == {
        "name": "Test Site",
        "server": "PAXTON10-TEST",
        "version": "4.11.9753.20528",
        "doors": "2",
        "door_names": "Main Entrance Door, Vehicle Gate",
        "devices": "2",
    }
    with patch("custom_components.paxton10.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            flow_id, {OPT_ALLOW_DOOR_CONTROL: True, OPT_INCLUDE_USER_NAMES: False}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == SITE_ID
    assert result["data"] == {
        CONF_ROUTE: route,
        CONF_TARGET: target,
        CONF_USERNAME: USERNAME,
        CONF_PASSWORD_HASH: password_hash(PASSWORD),
    }
    # The two switches on the confirm step are options, so Configure can change them later.
    assert result["result"].options == {OPT_ALLOW_DOOR_CONTROL: True, OPT_INCLUDE_USER_NAMES: False}
    # The plain password is never stored.
    assert PASSWORD not in str(result["data"])
    assert server.calls[0][0] == target


@pytest.mark.parametrize(
    ("break_it", "error", "placeholders"),
    [
        ("auth", {"base": "invalid_auth"}, {}),
        ("down", {"base": "cannot_connect"}, {"error": "192.0.2.1 unreachable"}),
        ("crash", {"base": "unknown"}, {}),
        ("empty", {CONF_TARGET: "invalid_direct_target"}, {}),
    ],
)
async def test_user_flow_errors(
    hass: HomeAssistant, server: FakeServer, break_it: str, error: dict[str, str], placeholders: dict[str, str]
) -> None:
    flow_id = await _to_server(hass, ROUTE_DIRECT)
    data = _server("192.0.2.1")
    if break_it == "auth":
        data = _server("192.0.2.1", "wrong")
    elif break_it == "down":
        server.down.add("192.0.2.1")
    elif break_it == "empty":
        data = _server("https://")
    if break_it == "crash":
        with patch("custom_components.paxton10.config_flow.discover_site", side_effect=RuntimeError):
            result = await hass.config_entries.flow.async_configure(flow_id, data)
    else:
        result = await hass.config_entries.flow.async_configure(flow_id, data)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "server_direct"
    assert result["errors"] == error
    # The error says why, on the screen where the address can be fixed.
    assert result["description_placeholders"] == placeholders

    # The same step recovers once the problem is fixed, with the address and username kept.
    server.down.clear()
    result = await hass.config_entries.flow.async_configure(flow_id, _server("192.0.2.1"))
    assert result["step_id"] == "confirm"


async def test_user_flow_already_configured(hass: HomeAssistant, server: FakeServer) -> None:
    make_entry().add_to_hass(hass)
    flow_id = await _to_server(hass, ROUTE_DIRECT)
    result = await hass.config_entries.flow.async_configure(flow_id, _server("192.0.2.1"))
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


@pytest.mark.parametrize(
    ("route", "raw", "expected"),
    [
        (ROUTE_DIRECT, "192.0.2.50", "192.0.2.50"),
        (ROUTE_DIRECT, "https://192.0.2.50:8443/", "192.0.2.50:8443"),
        (ROUTE_DIRECT, "paxton.example.lan/app", "paxton.example.lan"),
        (ROUTE_DIRECT, "  ", ""),
        (ROUTE_REMOTE, "abc123", "abc123"),
        (ROUTE_REMOTE, "ABC123.paxton10remote.com", "abc123"),
        (ROUTE_REMOTE, "https://abc123.p10remote.com/", "abc123"),
        (ROUTE_REMOTE, "https://example.com/x", ""),
        (ROUTE_REMOTE, "", ""),
    ],
)
def test_normalize_target(route: str, raw: str, expected: str) -> None:
    assert normalize_target(route, raw) == expected


def test_door_sample() -> None:
    site = Site(server=Server("s", "S", None, None, 0))
    assert door_sample(site) == "-"
    site.doors = {i: Door(i, f"Door {i:02d}", 1, 1, 1, None) for i in range(7)}
    assert door_sample(site) == "Door 00, Door 01, Door 02, Door 03, Door 04, and 2 more"


async def test_reauth(hass: HomeAssistant, server: FakeServer) -> None:
    entry = make_entry(**{CONF_PASSWORD_HASH: password_hash("old")})
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: "wrong"}
    )
    assert result["errors"] == {"base": "invalid_auth"}

    server.down.add("192.0.2.1")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}
    )
    assert result["errors"] == {"base": "cannot_connect"}
    assert result["description_placeholders"]["error"] == "192.0.2.1 unreachable"
    server.down.clear()

    with patch("custom_components.paxton10.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD_HASH] == password_hash(PASSWORD)


async def test_reauth_wrong_site(hass: HomeAssistant, server: FakeServer) -> None:
    entry = make_entry()
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, unique_id="another-site")
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_site"


async def test_reconfigure(hass: HomeAssistant, server: FakeServer) -> None:
    entry = make_entry()
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_ROUTE: ROUTE_REMOTE})
    assert result["step_id"] == "reconfigure_server_remote"

    server.down.add("abc123")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TARGET: "abc123"})
    assert result["errors"] == {"base": "cannot_connect"}
    assert result["description_placeholders"] == {"error": "abc123 unreachable"}

    server.down.clear()
    with patch("custom_components.paxton10.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_TARGET: "https://abc123.paxton10remote.com/"}
        )
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_ROUTE] == ROUTE_REMOTE
    assert entry.data[CONF_TARGET] == "abc123"


async def test_reconfigure_wrong_site(hass: HomeAssistant, server: FakeServer) -> None:
    entry = make_entry()
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, unique_id="another-site")
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_ROUTE: ROUTE_DIRECT})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TARGET: "192.0.2.9"})
    assert result["reason"] == "wrong_site"


async def test_options(hass: HomeAssistant, server: FakeServer) -> None:
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    assert result["description_placeholders"] == {"other_route": ROUTE_REMOTE}

    options = {
        OPT_ALLOW_DOOR_CONTROL: True,
        OPT_DEVICE_INTERVAL: 60,
        OPT_EVENT_INTERVAL: 5,
        OPT_FALLBACK: True,
        OPT_INCLUDE_USER_NAMES: False,
    }
    result = await hass.config_entries.options.async_configure(result["flow_id"], options)
    assert result["errors"] == {OPT_FALLBACK_TARGET: "fallback_target_required"}

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**options, OPT_FALLBACK_TARGET: "https://abc123.paxton10remote.com"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options[OPT_ALLOW_DOOR_CONTROL] is True
    assert entry.options[OPT_FALLBACK_TARGET] == "abc123"
    # The entry reloaded with door control on, so the buttons exist now.
    assert entity_id(hass, "button", 2001, "open") is not None
