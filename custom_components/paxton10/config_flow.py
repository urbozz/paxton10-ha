"""Config flow: route, then server and account, then confirm. Plus reauth, reconfigure, and options."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import probatio
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
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
    OPT_INCLUDE_CREDENTIAL_NAMES,
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
REMOTE_DOMAINS = ("paxton10remote.com", "p10remote.com")
DOOR_SAMPLE = 5


def normalize_target(route: str, raw: str) -> str:
    """Accept what an installer is likely to paste: a bare value, a URL, or a host with a path.

    Direct keeps host[:port]. Remote takes the first label of a paxton10remote.com host.
    Returns "" when nothing usable is left.
    """
    text = raw.strip()
    host = urlsplit(text if "://" in text else f"//{text}").netloc
    host = host.rsplit("@", 1)[-1].strip()
    if route == ROUTE_REMOTE:
        name = host.split(":", 1)[0].lower()
        for domain in REMOTE_DOMAINS:
            if name.endswith(f".{domain}"):
                return name.removesuffix(f".{domain}").split(".")[-1]
        return name if name.isalnum() else ""
    return host


def server_schema(route: str, defaults: Mapping[str, Any], with_account: bool = True) -> probatio.Schema:
    fields: dict[Any, Any] = {
        # No default address: every site's server is different.
        probatio.Required(CONF_TARGET, default=defaults.get(CONF_TARGET) or probatio.UNDEFINED): str,
    }
    if with_account:
        fields[probatio.Required(CONF_USERNAME, default=defaults.get(CONF_USERNAME) or probatio.UNDEFINED)] = (
            EMAIL_SELECTOR
        )
        fields[probatio.Required(CONF_PASSWORD)] = PASSWORD_SELECTOR
    return probatio.Schema(fields)


def door_sample(site: Site) -> str:
    names = sorted(d.name for d in site.doors.values())
    shown = ", ".join(names[:DOOR_SAMPLE])
    more = len(names) - DOOR_SAMPLE
    return f"{shown}, and {more} more" if more > 0 else shown or "-"


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
) -> tuple[Site | None, dict[str, str], dict[str, str]]:
    """Returns the site, or form errors plus placeholders that say what went wrong."""
    if not target:
        return None, {CONF_TARGET: f"invalid_{route}_target"}, {}
    try:
        return await validate(hass, route, target, username, pw_hash), {}, {}
    except PaxtonAuthError:
        return None, {"base": "invalid_auth"}, {}
    except PaxtonError as err:
        _LOGGER.debug("Paxton10 connection test failed: %s", err)
        return None, {"base": "cannot_connect"}, {"error": str(err)}
    except Exception:
        _LOGGER.exception("Unexpected error testing the Paxton10 connection")
        return None, {"base": "unknown"}, {}


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
            return await self.async_step_server()
        return self.async_show_form(step_id="user", data_schema=ROUTE_SCHEMA)

    async def async_step_server(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Address and account on one screen, so a connection error can be fixed where it shows."""
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        if user_input is not None:
            target = normalize_target(self._route, user_input[CONF_TARGET])
            username = user_input[CONF_USERNAME].strip()
            pw_hash = password_hash(user_input[CONF_PASSWORD])
            site, errors, placeholders = await _try(self.hass, self._route, target, username, pw_hash)
            if site:
                await self.async_set_unique_id(site.server.site_id)
                self._abort_if_unique_id_configured()
                self._site = site
                self._data = {
                    CONF_ROUTE: self._route,
                    CONF_TARGET: target,
                    CONF_USERNAME: username,
                    CONF_PASSWORD_HASH: pw_hash,
                }
                return await self.async_step_confirm()
        # One step id per route, so the field is labelled "Server address" or "Remote ID".
        return self.async_show_form(
            step_id=f"server_{self._route}",
            data_schema=server_schema(self._route, user_input or {}),
            errors=errors,
            description_placeholders=placeholders,
            last_step=False,
        )

    async_step_server_direct = async_step_server
    async_step_server_remote = async_step_server

    async def async_step_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        assert self._site
        if user_input is not None:
            return self.async_create_entry(
                title=self._site.server.system_name,
                data=self._data,
                options={
                    OPT_ALLOW_DOOR_CONTROL: user_input[OPT_ALLOW_DOOR_CONTROL],
                    OPT_INCLUDE_USER_NAMES: user_input[OPT_INCLUDE_USER_NAMES],
                },
            )
        server = self._site.server
        return self.async_show_form(
            step_id="confirm",
            data_schema=probatio.Schema(
                {
                    probatio.Required(OPT_ALLOW_DOOR_CONTROL, default=False): BooleanSelector(),
                    probatio.Required(OPT_INCLUDE_USER_NAMES, default=False): BooleanSelector(),
                }
            ),
            description_placeholders={
                "name": server.system_name,
                "server": server.server_name or "-",
                "version": server.version or "-",
                "doors": str(len(self._site.doors)),
                "door_names": door_sample(self._site),
                "devices": str(len(self._site.devices)),
            },
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        if user_input is not None:
            username = user_input[CONF_USERNAME].strip()
            pw_hash = password_hash(user_input[CONF_PASSWORD])
            site, errors, placeholders = await _try(
                self.hass, entry.data[CONF_ROUTE], entry.data[CONF_TARGET], username, pw_hash
            )
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
            description_placeholders=placeholders,
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            self._route = user_input[CONF_ROUTE]
            return await self.async_step_reconfigure_server()
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(ROUTE_SCHEMA, {CONF_ROUTE: entry.data[CONF_ROUTE]}),
        )

    async def async_step_reconfigure_server(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        if user_input is not None:
            target = normalize_target(self._route, user_input[CONF_TARGET])
            site, errors, placeholders = await _try(
                self.hass, self._route, target, entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD_HASH]
            )
            if site:
                await self.async_set_unique_id(site.server.site_id)
                self._abort_if_unique_id_mismatch(reason="wrong_site")
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_ROUTE: self._route, CONF_TARGET: target}
                )
        same_route = entry.data[CONF_ROUTE] == self._route
        defaults = user_input or ({CONF_TARGET: entry.data[CONF_TARGET]} if same_route else {})
        return self.async_show_form(
            step_id=f"reconfigure_server_{self._route}",
            data_schema=server_schema(self._route, defaults, with_account=False),
            errors=errors,
            description_placeholders=placeholders,
        )

    async_step_reconfigure_server_direct = async_step_reconfigure_server
    async_step_reconfigure_server_remote = async_step_reconfigure_server


class Paxton10OptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        other = ROUTE_REMOTE if self.config_entry.data[CONF_ROUTE] == ROUTE_DIRECT else ROUTE_DIRECT
        if user_input is not None:
            user_input = dict(user_input)
            target = normalize_target(other, user_input.pop(OPT_FALLBACK_TARGET, None) or "")
            if user_input.get(OPT_FALLBACK) and not target:
                errors[OPT_FALLBACK_TARGET] = "fallback_target_required"
            else:
                # Store the target normalized, and only while the fallback is on.
                if user_input.get(OPT_FALLBACK):
                    user_input[OPT_FALLBACK_TARGET] = target
                return self.async_create_entry(data=user_input)
        options = self.config_entry.options
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
                probatio.Required(OPT_INCLUDE_CREDENTIAL_NAMES, default=False): BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(schema, user_input or options),
            description_placeholders={"other_route": other},
            errors=errors,
        )
