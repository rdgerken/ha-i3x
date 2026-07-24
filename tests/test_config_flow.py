"""Tests for the i3X config and options flows."""

from __future__ import annotations

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType

from custom_components.i3x.const import (
    CONF_LOCAL_ONLY,
    CONF_MODE,
    CONF_SERVER_NAME,
    CONF_SUBSCRIPTION_TTL,
    DOMAIN,
    MODE_SERVER,
)

USER_INPUT = {
    CONF_SERVER_NAME: "Test i3X",
    CONF_LOCAL_ONLY: True,
    "include_domains": [],
    "include_entity_globs": [],
    "exclude_entity_globs": [],
    CONF_SUBSCRIPTION_TTL: 600,
}


async def test_user_flow_creates_server_entry(
    recorder_mock, enable_custom_integrations, hass
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    assert entry.data == {CONF_MODE: MODE_SERVER}
    assert entry.options[CONF_SERVER_NAME] == "Test i3X"
    assert entry.unique_id == "server"
    await hass.async_block_till_done()


async def test_second_server_aborts(
    recorder_mock, enable_custom_integrations, hass
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    await hass.async_block_till_done()

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_options_flow(server_entry, hass) -> None:
    result = await hass.config_entries.options.async_init(server_entry.entry_id)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {**USER_INPUT, CONF_SERVER_NAME: "Renamed", CONF_SUBSCRIPTION_TTL: 900},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert server_entry.options[CONF_SERVER_NAME] == "Renamed"
    assert server_entry.options[CONF_SUBSCRIPTION_TTL] == 900
