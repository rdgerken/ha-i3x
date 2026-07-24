"""Tests for the i3X config and options flows (server and client)."""

from __future__ import annotations

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType

from custom_components.i3x.const import (
    CONF_LOCAL_ONLY,
    CONF_MODE,
    CONF_SERVER_NAME,
    CONF_SUBSCRIPTION_TTL,
    DOMAIN,
    MODE_CLIENT,
    MODE_SERVER,
)

SERVER_INPUT = {
    CONF_SERVER_NAME: "Test i3X",
    CONF_LOCAL_ONLY: True,
    "include_domains": [],
    "include_entity_globs": [],
    "exclude_entity_globs": [],
    CONF_SUBSCRIPTION_TTL: 600,
    "write_enabled": False,
    "write_entity_globs": [],
}


async def _to_step(hass, step: str):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.MENU
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": step}
    )


async def test_server_flow_creates_entry(
    recorder_mock, enable_custom_integrations, hass
) -> None:
    result = await _to_step(hass, "server")
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "server"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], SERVER_INPUT
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
    result = await _to_step(hass, "server")
    await hass.config_entries.flow.async_configure(result["flow_id"], SERVER_INPUT)
    await hass.async_block_till_done()

    result = await _to_step(hass, "server")
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_client_flow_validates_and_creates(
    recorder_mock, enable_custom_integrations, hass, fake_i3x
) -> None:
    server, base_url = fake_i3x
    result = await _to_step(hass, "client")
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "client"

    # Incomplete auth combo → inline error.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"base_url": base_url, "auth_type": "bearer", "verify_ssl": True},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "auth_incomplete"}

    # Unreachable server → cannot_connect.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "base_url": "http://127.0.0.1:1/v1",
            "auth_type": "none",
            "verify_ssl": True,
        },
    )
    assert result["errors"] == {"base": "cannot_connect"}

    # Valid → entry created with mode=client.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"base_url": base_url, "auth_type": "none", "verify_ssl": True},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    assert entry.data[CONF_MODE] == MODE_CLIENT
    assert entry.data["base_url"] == base_url.rstrip("/")
    await hass.async_block_till_done()
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_server_options_flow(server_entry, hass) -> None:
    result = await hass.config_entries.options.async_init(server_entry.entry_id)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {**SERVER_INPUT, CONF_SERVER_NAME: "Renamed", CONF_SUBSCRIPTION_TTL: 900},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert server_entry.options[CONF_SERVER_NAME] == "Renamed"
    assert server_entry.options[CONF_SUBSCRIPTION_TTL] == 900
