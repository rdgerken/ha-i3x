"""Shared fixtures for the i3X test suite.

Fixture-ordering convention (from pytest-homeassistant-custom-component):
`recorder_mock` must be instantiated BEFORE `hass`, so tests and fixtures
request them explicitly in that order.
"""

from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.setup import async_setup_component

from custom_components.i3x.const import CONF_MODE, DOMAIN, MODE_SERVER


@pytest.fixture
def server_options() -> dict:
    """Default server options; override in tests via parametrize/fixture."""
    return {}


@pytest.fixture
async def server_entry(
    recorder_mock, enable_custom_integrations, hass, server_options
) -> MockConfigEntry:
    """A fully set up i3X server config entry on a test hass."""
    assert await async_setup_component(hass, "http", {})
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="i3X Server",
        data={CONF_MODE: MODE_SERVER},
        options=server_options,
        unique_id="server",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


@pytest.fixture
async def world(server_entry, hass) -> dict:
    """Seed an area → device → entity hierarchy plus loose entities."""
    from homeassistant.helpers import (
        area_registry as ar,
        device_registry as dr,
        entity_registry as er,
    )

    area_reg = ar.async_get(hass)
    kitchen = area_reg.async_create("Kitchen")

    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=server_entry.entry_id,
        identifiers={("test", "thermo-1")},
        manufacturer="Acme",
        model="Thermo 9000",
        name="Kitchen Thermostat",
    )
    dev_reg.async_update_device(device.id, area_id=kitchen.id)

    ent_reg = er.async_get(hass)
    ent_reg.async_get_or_create(
        "sensor",
        "test",
        "thermo-1-temp",
        suggested_object_id="kitchen_temp",
        device_id=device.id,
    )
    hass.states.async_set(
        "sensor.kitchen_temp",
        "21.5",
        {"unit_of_measurement": "°C", "device_class": "temperature"},
    )
    # Entities with no registry entry hang off the root.
    hass.states.async_set("switch.fountain", "on")
    hass.states.async_set(
        "light.porch", "on", {"brightness": 128, "color_mode": "brightness"}
    )
    await hass.async_block_till_done()
    return {
        "area_id": kitchen.id,
        "device_id": device.id,
        "sensor": "sensor.kitchen_temp",
        "switch": "switch.fountain",
        "light": "light.porch",
    }
