"""End-to-end tests for a client entry against the fake i3X server."""

from __future__ import annotations

import asyncio

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.exceptions import HomeAssistantError

from custom_components.i3x.const import CONF_MODE, DOMAIN, MODE_CLIENT


def _client_entry(base_url: str, options: dict | None = None) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="i3X Client (fake)",
        data={
            CONF_MODE: MODE_CLIENT,
            "base_url": base_url,
            "auth_type": "none",
            "verify_ssl": True,
        },
        options=options or {},
        unique_id=f"client:{base_url}",
    )


async def _setup(hass, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_client_sensors_and_push(
    recorder_mock, enable_custom_integrations, hass, fake_i3x
) -> None:
    server, base_url = fake_i3x
    entry = _client_entry(
        base_url, {"live_element_ids": ["plant.temp", "plant.energy"]}
    )
    await _setup(hass, entry)

    coordinator = entry.runtime_data.coordinator
    assert coordinator is not None
    assert coordinator.data["plant.temp"]["value"] == 21.5

    # Sensor entities exist with the polled value.
    states = [
        s for s in hass.states.async_all("sensor") if s.state not in ("unknown",)
    ]
    values = {s.state for s in states}
    assert "21.5" in values

    # The sync push loop delivers server-side updates without polling.
    for _ in range(50):
        await asyncio.sleep(0.05)
        if coordinator.data["plant.temp"]["value"] == 31.0:
            break
    assert coordinator.data["plant.temp"]["value"] == 31.0
    assert server.sync_calls >= 1

    # Unload stops the push loop and deletes the subscription cleanly.
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_write_service(
    recorder_mock, enable_custom_integrations, hass, fake_i3x
) -> None:
    server, base_url = fake_i3x
    entry = _client_entry(base_url)
    await _setup(hass, entry)

    await hass.services.async_call(
        DOMAIN,
        "write",
        {"element_id": "plant.temp", "value": 33.3, "quality": "Good"},
        blocking=True,
    )
    assert server.put_updates[-1]["elementId"] == "plant.temp"
    assert server.put_updates[-1]["value"]["value"] == 33.3

    # Unknown target entry id → clean error.
    try:
        await hass.services.async_call(
            DOMAIN,
            "write",
            {
                "element_id": "plant.temp",
                "value": 1,
                "config_entry_id": "does-not-exist",
            },
            blocking=True,
        )
        raise AssertionError("expected HomeAssistantError")
    except HomeAssistantError:
        pass


async def test_client_statistics_import(
    recorder_mock, enable_custom_integrations, hass, fake_i3x, monkeypatch
) -> None:
    import custom_components.i3x.statistics as stats_mod

    captured: list[tuple[dict, list]] = []
    monkeypatch.setattr(
        stats_mod,
        "async_add_external_statistics",
        lambda _hass, metadata, stats: captured.append((metadata, stats)),
    )

    server, base_url = fake_i3x
    entry = _client_entry(
        base_url,
        {
            "import_element_ids": ["plant.temp"],
            "import_counter_element_ids": ["plant.energy"],
        },
    )
    await _setup(hass, entry)
    importer = entry.runtime_data.importer
    assert importer is not None
    await importer.async_import()

    by_id = {meta["statistic_id"]: (meta, rows) for meta, rows in captured}
    meas_meta, meas_rows = by_id["i3x:plant_temp"]
    assert meas_meta["has_sum"] is False
    assert meas_rows[0]["mean"] == 100.0

    counter_meta, counter_rows = by_id["i3x:plant_energy"]
    assert counter_meta["has_sum"] is True
    # Baseline hour contributes 0; subsequent deltas accumulate.
    assert [row["sum"] for row in counter_rows] == [0.0, 10.0, 20.0]
    assert [row["state"] for row in counter_rows] == [100.0, 110.0, 120.0]
