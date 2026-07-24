"""Config flow for the i3X integration (server and client entries)."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    AUTH_BASIC,
    AUTH_BEARER,
    AUTH_HEADER,
    AUTH_NONE,
    CONF_AUTH_TYPE,
    CONF_BASE_URL,
    CONF_EXCLUDE_ENTITY_GLOBS,
    CONF_HEADER_NAME,
    CONF_HEADER_VALUE,
    CONF_IMPORT_COUNTER_ELEMENT_IDS,
    CONF_IMPORT_ELEMENT_IDS,
    CONF_INCLUDE_DOMAINS,
    CONF_INCLUDE_ENTITY_GLOBS,
    CONF_LIVE_ELEMENT_IDS,
    CONF_LOCAL_ONLY,
    CONF_MODE,
    CONF_PASSWORD,
    CONF_SERVER_NAME,
    CONF_SUBSCRIPTION_TTL,
    CONF_TOKEN,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
    CONF_WRITE_ENABLED,
    CONF_WRITE_ENTITY_GLOBS,
    DEFAULT_LOCAL_ONLY,
    DEFAULT_SERVER_NAME,
    DEFAULT_SUBSCRIPTION_TTL,
    DOMAIN,
    MODE_CLIENT,
    MODE_SERVER,
)

_LOGGER = logging.getLogger(__name__)


def _server_options_schema(options: dict) -> vol.Schema:
    multi_text = TextSelector(TextSelectorConfig(multiple=True))
    return vol.Schema(
        {
            vol.Required(
                CONF_SERVER_NAME,
                default=options.get(CONF_SERVER_NAME, DEFAULT_SERVER_NAME),
            ): str,
            vol.Required(
                CONF_LOCAL_ONLY,
                default=options.get(CONF_LOCAL_ONLY, DEFAULT_LOCAL_ONLY),
            ): BooleanSelector(),
            vol.Optional(
                CONF_INCLUDE_DOMAINS,
                default=options.get(CONF_INCLUDE_DOMAINS, []),
            ): multi_text,
            vol.Optional(
                CONF_INCLUDE_ENTITY_GLOBS,
                default=options.get(CONF_INCLUDE_ENTITY_GLOBS, []),
            ): multi_text,
            vol.Optional(
                CONF_EXCLUDE_ENTITY_GLOBS,
                default=options.get(CONF_EXCLUDE_ENTITY_GLOBS, []),
            ): multi_text,
            vol.Required(
                CONF_SUBSCRIPTION_TTL,
                default=options.get(CONF_SUBSCRIPTION_TTL, DEFAULT_SUBSCRIPTION_TTL),
            ): vol.All(int, vol.Range(min=60, max=86400)),
            vol.Required(
                CONF_WRITE_ENABLED,
                default=options.get(CONF_WRITE_ENABLED, False),
            ): BooleanSelector(),
            vol.Optional(
                CONF_WRITE_ENTITY_GLOBS,
                default=options.get(CONF_WRITE_ENTITY_GLOBS, []),
            ): multi_text,
        }
    )


def _client_options_schema(options: dict) -> vol.Schema:
    multi_text = TextSelector(TextSelectorConfig(multiple=True))
    return vol.Schema(
        {
            vol.Optional(
                CONF_LIVE_ELEMENT_IDS,
                default=options.get(CONF_LIVE_ELEMENT_IDS, []),
            ): multi_text,
            vol.Optional(
                CONF_IMPORT_ELEMENT_IDS,
                default=options.get(CONF_IMPORT_ELEMENT_IDS, []),
            ): multi_text,
            vol.Optional(
                CONF_IMPORT_COUNTER_ELEMENT_IDS,
                default=options.get(CONF_IMPORT_COUNTER_ELEMENT_IDS, []),
            ): multi_text,
        }
    )


STEP_CLIENT_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_BASE_URL): TextSelector(
            TextSelectorConfig(type=TextSelectorType.URL)
        ),
        vol.Required(CONF_AUTH_TYPE, default=AUTH_BEARER): SelectSelector(
            SelectSelectorConfig(
                options=[AUTH_NONE, AUTH_BEARER, AUTH_BASIC, AUTH_HEADER],
                mode=SelectSelectorMode.DROPDOWN,
                translation_key="auth_type",
            )
        ),
        vol.Optional(CONF_TOKEN): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
        vol.Optional(CONF_USERNAME): str,
        vol.Optional(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
        vol.Optional(CONF_HEADER_NAME): str,
        vol.Optional(CONF_HEADER_VALUE): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
        vol.Required(CONF_VERIFY_SSL, default=True): BooleanSelector(),
    }
)


def _auth_incomplete(user_input: dict) -> bool:
    auth_type = user_input.get(CONF_AUTH_TYPE)
    if auth_type == AUTH_BEARER:
        return not user_input.get(CONF_TOKEN)
    if auth_type == AUTH_BASIC:
        return not user_input.get(CONF_USERNAME)
    if auth_type == AUTH_HEADER:
        return not user_input.get(CONF_HEADER_NAME)
    return False


class I3xConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle setup of i3X server and client entries."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="user", menu_options=["server", "client"]
        )

    async def async_step_server(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        await self.async_set_unique_id("server")
        self._abort_if_unique_id_configured()
        if user_input is not None:
            return self.async_create_entry(
                title="i3X Server",
                data={CONF_MODE: MODE_SERVER},
                options=user_input,
            )
        return self.async_show_form(
            step_id="server", data_schema=_server_options_schema({})
        )

    async def async_step_client(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            base_url = user_input[CONF_BASE_URL].rstrip("/")
            user_input[CONF_BASE_URL] = base_url
            await self.async_set_unique_id(f"client:{base_url}")
            self._abort_if_unique_id_configured()
            if _auth_incomplete(user_input):
                errors["base"] = "auth_incomplete"
            else:
                from . import build_api_client
                from .api import I3xAuthError, I3xError

                try:
                    api = build_api_client(self.hass, user_input)
                    await api.async_get_info()
                except I3xAuthError:
                    errors["base"] = "invalid_auth"
                except I3xError:
                    errors["base"] = "cannot_connect"
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("Unexpected error validating i3X server")
                    errors["base"] = "unknown"
                else:
                    host = urlparse(base_url).hostname or base_url
                    return self.async_create_entry(
                        title=f"i3X Client ({host})",
                        data={CONF_MODE: MODE_CLIENT, **user_input},
                    )
        return self.async_show_form(
            step_id="client", data_schema=STEP_CLIENT_SCHEMA, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return I3xOptionsFlow()


class I3xOptionsFlow(OptionsFlow):
    """Options: server exposure/write settings or client monitoring lists."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        options = dict(self.config_entry.options)
        if self.config_entry.data.get(CONF_MODE) == MODE_CLIENT:
            schema = _client_options_schema(options)
        else:
            schema = _server_options_schema(options)
        return self.async_show_form(step_id="init", data_schema=schema)
