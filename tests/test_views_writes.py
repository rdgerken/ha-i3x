"""Tests for value-changing writes with write_enabled + allowlist on."""

from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import async_mock_service


@pytest.fixture
def server_options() -> dict:
    return {
        "write_enabled": True,
        "write_entity_globs": ["switch.*", "input_number.*", "light.*"],
    }


async def test_boolean_write_dispatches_service(world, hass, hass_client) -> None:
    calls = async_mock_service(hass, "switch", "turn_off")
    client = await hass_client()
    resp = await client.put(
        "/api/i3x/v1/objects/value",
        json={"updates": [{"elementId": world["switch"], "value": {"value": False}}]},
    )
    body = await resp.json()
    assert body["results"][0]["success"] is True, body
    assert len(calls) == 1
    assert calls[0].data["entity_id"] == world["switch"]


async def test_structured_light_write(world, hass, hass_client) -> None:
    calls = async_mock_service(hass, "light", "turn_on")
    client = await hass_client()
    resp = await client.put(
        "/api/i3x/v1/objects/value",
        json={
            "updates": [
                {
                    "elementId": world["light"],
                    "value": {"value": {"state": True, "brightness": 200}},
                }
            ]
        },
    )
    body = await resp.json()
    assert body["results"][0]["success"] is True, body
    assert calls[0].data["brightness"] == 200


async def test_write_outside_allowlist_denied(world, hass, hass_client) -> None:
    # The sensor is exposed but not allowlisted — and sensors are read-only
    # anyway. A value-changing write must fail per-item.
    client = await hass_client()
    resp = await client.put(
        "/api/i3x/v1/objects/value",
        json={"updates": [{"elementId": world["sensor"], "value": {"value": 99.0}}]},
    )
    body = await resp.json()
    assert body["results"][0]["success"] is False
    assert body["results"][0]["responseDetail"]["status"] == 403


async def test_lock_never_writable(world, hass, hass_client, server_entry) -> None:
    hass.states.async_set("lock.front_door", "locked")
    hass.config_entries.async_update_entry(
        server_entry,
        options={**server_entry.options, "write_entity_globs": ["lock.*"]},
    )
    await hass.async_block_till_done()
    client = await hass_client()
    resp = await client.put(
        "/api/i3x/v1/objects/value",
        json={"updates": [{"elementId": "lock.front_door", "value": {"value": False}}]},
    )
    body = await resp.json()
    assert body["results"][0]["success"] is False
    assert "lock" in body["results"][0]["responseDetail"]["detail"]
