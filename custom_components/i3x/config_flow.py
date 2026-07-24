"""Config flow for the i3X integration."""

from __future__ import annotations

from typing import Any

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
    TextSelector,
    TextSelectorConfig,
)

from .const import (
    CONF_EXCLUDE_ENTITY_GLOBS,
    CONF_INCLUDE_DOMAINS,
    CONF_INCLUDE_ENTITY_GLOBS,
    CONF_LOCAL_ONLY,
    CONF_MODE,
    CONF_SERVER_NAME,
    CONF_SUBSCRIPTION_TTL,
    CONF_WRITE_ENABLED,
    CONF_WRITE_ENTITY_GLOBS,
    DEFAULT_LOCAL_ONLY,
    DEFAULT_SERVER_NAME,
    DEFAULT_SUBSCRIPTION_TTL,
    DOMAIN,
    MODE_SERVER,
)


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


class I3xConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle setup of the i3X server."""

    VERSION = 1

    async def async_step_user(
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
            step_id="user", data_schema=_server_options_schema({})
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return I3xOptionsFlow()


class I3xOptionsFlow(OptionsFlow):
    """Options: exposure filtering, local-only guard, subscription TTL."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=_server_options_schema(dict(self.config_entry.options)),
        )
