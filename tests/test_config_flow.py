"""Config flow, reauth, reconfigure, and options."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.paxton10.api import password_hash
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

from .conftest import PASSWORD, SITE_ID, USERNAME, FakeServer, entity_id, make_entry


async def _to_account(hass: HomeAssistant, route: str, target: str) -> str:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_ROUTE: route})
    assert result["step_id"] == "connection"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TARGET: f" {target} "})
    assert result["step_id"] == "account"
    return result["flow_id"]


@pytest.mark.parametrize(("route", "target"), [(ROUTE_DIRECT, "192.0.2.1"), (ROUTE_REMOTE, "abc123")])
async def test_user_flow(hass: HomeAssistant, server: FakeServer, route: str, target: str) -> None:
    flow_id = await _to_account(hass, route, target)
    result = await hass.config_entries.flow.async_configure(flow_id, {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"
    assert result["description_placeholders"] == {
        "name": "Test Site",
        "server": "PAXTON10-TEST",
        "version": "4.11.9753.20528",
        "doors": "2",
        "devices": "2",
    }
    with patch("custom_components.paxton10.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(flow_id, {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == SITE_ID
    assert result["data"] == {
        CONF_ROUTE: route,
        CONF_TARGET: target,
        CONF_USERNAME: USERNAME,
        CONF_PASSWORD_HASH: password_hash(PASSWORD),
    }
    # The plain password is never stored.
    assert PASSWORD not in str(result["data"])
    assert server.calls[0][0] == target


@pytest.mark.parametrize(
    ("break_it", "error"),
    [
        ("auth", "invalid_auth"),
        ("down", "cannot_connect"),
        ("crash", "unknown"),
    ],
)
async def test_user_flow_errors(hass: HomeAssistant, server: FakeServer, break_it: str, error: str) -> None:
    flow_id = await _to_account(hass, ROUTE_DIRECT, "192.0.2.1")
    password = PASSWORD
    if break_it == "auth":
        password = "wrong"
    elif break_it == "down":
        server.down.add("192.0.2.1")
    if break_it == "crash":
        with patch("custom_components.paxton10.config_flow.discover_site", side_effect=RuntimeError):
            result = await hass.config_entries.flow.async_configure(
                flow_id, {CONF_USERNAME: USERNAME, CONF_PASSWORD: password}
            )
    else:
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_USERNAME: USERNAME, CONF_PASSWORD: password}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "account"
    assert result["errors"] == {"base": error}

    # The same step recovers once the problem is fixed.
    server.down.clear()
    result = await hass.config_entries.flow.async_configure(flow_id, {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD})
    assert result["step_id"] == "confirm"


async def test_user_flow_already_configured(hass: HomeAssistant, server: FakeServer) -> None:
    make_entry().add_to_hass(hass)
    flow_id = await _to_account(hass, ROUTE_DIRECT, "192.0.2.1")
    result = await hass.config_entries.flow.async_configure(flow_id, {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth(hass: HomeAssistant, server: FakeServer) -> None:
    entry = make_entry(**{CONF_PASSWORD_HASH: password_hash("old")})
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: "wrong"}
    )
    assert result["errors"] == {"base": "invalid_auth"}

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
    assert result["step_id"] == "reconfigure_connection"

    server.down.add("abc123")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TARGET: "abc123"})
    assert result["errors"] == {"base": "cannot_connect"}

    server.down.clear()
    with patch("custom_components.paxton10.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TARGET: "abc123"})
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
        result["flow_id"], {**options, OPT_FALLBACK_TARGET: "abc123"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options[OPT_ALLOW_DOOR_CONTROL] is True
    # The entry reloaded with door control on, so the buttons exist now.
    assert entity_id(hass, "button", 2001, "open") is not None
