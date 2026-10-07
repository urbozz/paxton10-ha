"""Config flow: connection, account, confirm. Plus reauth, reconfigure, and options."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import probatio
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import PaxtonAuthError, PaxtonError, password_hash
from .connection import PaxtonConnection
from .const import (
    CONF_PASSWORD_HASH,
    CONF_ROUTE,
    CONF_TARGET,
    CONF_USERNAME,
    DEFAULT_DEVICE_INTERVAL,
    DEFAULT_EVENT_INTERVAL,
    DOMAIN,
    MIN_DEVICE_INTERVAL,
    MIN_EVENT_INTERVAL,
    OPT_ALLOW_DOOR_CONTROL,
    OPT_DEVICE_INTERVAL,
    OPT_EVENT_INTERVAL,
    OPT_FALLBACK,
    OPT_FALLBACK_TARGET,
    OPT_INCLUDE_USER_NAMES,
    ROUTE_DIRECT,
    ROUTE_REMOTE,
)
from .discovery import discover_site
from .models import Site

_LOGGER = logging.getLogger(__name__)

ROUTE_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_ROUTE, default=ROUTE_DIRECT): SelectSelector(
            SelectSelectorConfig(
                options=[ROUTE_DIRECT, ROUTE_REMOTE], translation_key="route", mode=SelectSelectorMode.LIST
            )
        )
    }
)
PASSWORD_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
EMAIL_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username"))


def target_schema(default: str | None = None) -> probatio.Schema:
    # No default address: every site's server is different.
    return probatio.Schema({probatio.Required(CONF_TARGET, default=default or probatio.UNDEFINED): str})


async def validate(hass: HomeAssistant, route: str, target: str, username: str, pw_hash: str) -> Site:
    """Sign in and read the site. Raises PaxtonAuthError or PaxtonError."""
    conn = PaxtonConnection(async_get_clientsession(hass), route, target, username, pw_hash)
    try:
        await conn.connect()
        return await discover_site(conn)
    finally:
        await conn.close()


async def _try(
    hass: HomeAssistant, route: str, target: str, username: str, pw_hash: str
) -> tuple[Site | None, dict[str, str]]:
    try:
        return await validate(hass, route, target, username, pw_hash), {}
    except PaxtonAuthError:
        return None, {"base": "invalid_auth"}
    except PaxtonError as err:
        _LOGGER.debug("Paxton10 connection test failed: %s", err)
        return None, {"base": "cannot_connect"}
    except Exception:
        _LOGGER.exception("Unexpected error testing the Paxton10 connection")
        return None, {"base": "unknown"}


class Paxton10ConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._route = ROUTE_DIRECT
        self._target = ""
        self._data: dict[str, Any] = {}
        self._site: Site | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> Paxton10OptionsFlow:
        return Paxton10OptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._route = user_input[CONF_ROUTE]
            return await self.async_step_connection()
        return self.async_show_form(step_id="user", data_schema=ROUTE_SCHEMA)

    async def async_step_connection(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._target = user_input[CONF_TARGET].strip()
            return await self.async_step_account()
        return self.async_show_form(
            step_id="connection",
            data_schema=target_schema(),
            description_placeholders={"route": self._route},
        )

    async def async_step_account(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            username = user_input[CONF_USERNAME].strip()
            pw_hash = password_hash(user_input[CONF_PASSWORD])
            site, errors = await _try(self.hass, self._route, self._target, username, pw_hash)
            if site:
                await self.async_set_unique_id(site.server.site_id)
                self._abort_if_unique_id_configured()
                self._site = site
                self._data = {
                    CONF_ROUTE: self._route,
                    CONF_TARGET: self._target,
                    CONF_USERNAME: username,
                    CONF_PASSWORD_HASH: pw_hash,
                }
                return await self.async_step_confirm()
        return self.async_show_form(
            step_id="account",
            data_schema=self.add_suggested_values_to_schema(
                probatio.Schema(
                    {
                        probatio.Required(CONF_USERNAME): EMAIL_SELECTOR,
                        probatio.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
                    }
                ),
                {CONF_USERNAME: user_input[CONF_USERNAME]} if user_input else {},
            ),
            errors=errors,
        )

    async def async_step_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        assert self._site
        if user_input is not None:
            return self.async_create_entry(title=self._site.server.system_name, data=self._data)
        server = self._site.server
        return self.async_show_form(
            step_id="confirm",
            description_placeholders={
                "name": server.system_name,
                "server": server.server_name or "-",
                "version": server.version or "-",
                "doors": str(len(self._site.doors)),
                "devices": str(len(self._site.devices)),
            },
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            username = user_input[CONF_USERNAME].strip()
            pw_hash = password_hash(user_input[CONF_PASSWORD])
            site, errors = await _try(self.hass, entry.data[CONF_ROUTE], entry.data[CONF_TARGET], username, pw_hash)
            if site:
                await self.async_set_unique_id(site.server.site_id)
                self._abort_if_unique_id_mismatch(reason="wrong_site")
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_USERNAME: username, CONF_PASSWORD_HASH: pw_hash}
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self.add_suggested_values_to_schema(
                probatio.Schema(
                    {
                        probatio.Required(CONF_USERNAME): EMAIL_SELECTOR,
                        probatio.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
                    }
                ),
                {CONF_USERNAME: entry.data[CONF_USERNAME]},
            ),
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            self._route = user_input[CONF_ROUTE]
            return await self.async_step_reconfigure_connection()
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(ROUTE_SCHEMA, {CONF_ROUTE: entry.data[CONF_ROUTE]}),
        )

    async def async_step_reconfigure_connection(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            target = user_input[CONF_TARGET].strip()
            site, errors = await _try(
                self.hass, self._route, target, entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD_HASH]
            )
            if site:
                await self.async_set_unique_id(site.server.site_id)
                self._abort_if_unique_id_mismatch(reason="wrong_site")
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_ROUTE: self._route, CONF_TARGET: target}
                )
        default = entry.data[CONF_TARGET] if entry.data[CONF_ROUTE] == self._route else None
        return self.async_show_form(
            step_id="reconfigure_connection",
            data_schema=target_schema(default),
            description_placeholders={"route": self._route},
            errors=errors,
        )


class Paxton10OptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input = dict(user_input)
            target = (user_input.pop(OPT_FALLBACK_TARGET, None) or "").strip()
            if user_input.get(OPT_FALLBACK) and not target:
                errors[OPT_FALLBACK_TARGET] = "fallback_target_required"
            else:
                # Store the target trimmed, and only while the fallback is on.
                if user_input.get(OPT_FALLBACK):
                    user_input[OPT_FALLBACK_TARGET] = target
                return self.async_create_entry(data=user_input)
        options = self.config_entry.options
        other = ROUTE_REMOTE if self.config_entry.data[CONF_ROUTE] == ROUTE_DIRECT else ROUTE_DIRECT
        schema = probatio.Schema(
            {
                probatio.Required(OPT_ALLOW_DOOR_CONTROL, default=False): BooleanSelector(),
                probatio.Required(OPT_DEVICE_INTERVAL, default=DEFAULT_DEVICE_INTERVAL): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_DEVICE_INTERVAL, max=3600, step=1, unit_of_measurement="s", mode=NumberSelectorMode.BOX
                    )
                ),
                probatio.Required(OPT_EVENT_INTERVAL, default=DEFAULT_EVENT_INTERVAL): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_EVENT_INTERVAL, max=3600, step=1, unit_of_measurement="s", mode=NumberSelectorMode.BOX
                    )
                ),
                probatio.Required(OPT_FALLBACK, default=False): BooleanSelector(),
                probatio.Optional(OPT_FALLBACK_TARGET): str,
                probatio.Required(OPT_INCLUDE_USER_NAMES, default=False): BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(schema, user_input or options),
            description_placeholders={"other_route": other},
            errors=errors,
        )
