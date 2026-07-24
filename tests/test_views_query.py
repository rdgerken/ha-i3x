"""HTTP tests for /objects/value and /objects/history."""

from __future__ import annotations

import re
from datetime import timedelta

from homeassistant.util import dt as dt_util

BOGUS = "i3x-test-nonexistent-7f3a9c"
UTC_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")


def _iso(dt) -> str:
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


async def test_objects_value(world, hass, hass_client) -> None:
    client = await hass_client()
    ids = [world["sensor"], world["light"], "home", BOGUS]
    resp = await client.post("/api/i3x/v1/objects/value", json={"elementIds": ids})
    body = await resp.json()
    assert [r["elementId"] for r in body["results"]] == ids

    sensor = body["results"][0]["result"]
    assert sensor["isComposition"] is False
    assert sensor["value"] == 21.5
    assert sensor["quality"] == "Good"
    assert UTC_TS_RE.match(sensor["timestamp"])

    light = body["results"][1]["result"]
    assert light["value"]["state"] is True
    assert light["value"]["brightness"] == 128.0

    root = body["results"][2]["result"]
    assert root["value"] is None
    assert root["quality"] == "GoodNoData"

    assert body["results"][3]["success"] is False
    assert body["results"][3]["responseDetail"]["status"] == 404
    assert body["success"] is False


async def test_composition_value_maxdepth(world, hass, hass_client) -> None:
    client = await hass_client()
    device_element = f"device:{world['device_id']}"

    # Default maxDepth=1: no components key.
    resp = await client.post(
        "/api/i3x/v1/objects/value", json={"elementIds": [device_element]}
    )
    result = (await resp.json())["results"][0]["result"]
    assert result["isComposition"] is True
    assert "components" not in result
    assert result["value"] is None
    assert result["quality"] == "GoodNoData"

    # maxDepth=0 (infinite): component entity values fold in.
    resp = await client.post(
        "/api/i3x/v1/objects/value",
        json={"elementIds": [device_element], "maxDepth": 0},
    )
    result = (await resp.json())["results"][0]["result"]
    components = result["components"]
    assert world["sensor"] in components
    assert components[world["sensor"]]["value"] == 21.5
    assert components[world["sensor"]]["quality"] == "Good"


async def test_composition_history_maxdepth(world, hass, hass_client) -> None:
    from pytest_homeassistant_custom_component.components.recorder.common import (
        async_wait_recording_done,
    )

    hass.states.async_set(
        world["sensor"],
        "22.5",
        {"unit_of_measurement": "°C", "device_class": "temperature"},
    )
    await async_wait_recording_done(hass)

    device_element = f"device:{world['device_id']}"
    start = _iso(dt_util.utcnow() - timedelta(hours=1))
    end = _iso(dt_util.utcnow() + timedelta(minutes=5))
    client = await hass_client()
    resp = await client.post(
        "/api/i3x/v1/objects/history",
        json={
            "elementIds": [device_element],
            "startTime": start,
            "endTime": end,
            "maxDepth": 2,
        },
    )
    result = (await resp.json())["results"][0]["result"]
    assert result["isComposition"] is True
    child = result["components"][world["sensor"]]
    assert any(v["value"] == 22.5 for v in child["values"])


async def test_history_lts_backfill(world, hass, hass_client, monkeypatch) -> None:
    """Spans older than recorder data are served from long-term statistics."""
    from pytest_homeassistant_custom_component.components.recorder.common import (
        async_wait_recording_done,
    )

    import custom_components.i3x.server.history as history_mod

    await async_wait_recording_done(hass)

    old = dt_util.utcnow() - timedelta(days=20)
    old_hours = [
        old.replace(minute=0, second=0, microsecond=0) + timedelta(hours=i)
        for i in range(3)
    ]
    monkeypatch.setattr(
        history_mod,
        "statistics_during_period",
        lambda _hass, _s, _e, ids, _p, _u, _t: {
            world["sensor"]: [
                {"start": h.timestamp(), "mean": 15.0 + i} for i, h in enumerate(old_hours)
            ]
        },
    )

    client = await hass_client()
    start = _iso(dt_util.utcnow() - timedelta(days=30))
    end = _iso(dt_util.utcnow() + timedelta(minutes=5))
    resp = await client.post(
        "/api/i3x/v1/objects/history",
        json={"elementIds": [world["sensor"]], "startTime": start, "endTime": end},
    )
    values = (await resp.json())["results"][0]["result"]["values"]
    assert [v["value"] for v in values[:3]] == [15.0, 16.0, 17.0]
    stamps = [v["timestamp"] for v in values]
    assert stamps == sorted(stamps)
    # Recorder-era points (the world fixture's 21.5) follow the LTS ones.
    assert any(v["value"] == 21.5 for v in values[3:])


async def test_todo_value_includes_items(world, hass, hass_client) -> None:
    """Todo entities expose their actual list items, not just the count."""
    from homeassistant.core import SupportsResponse

    from custom_components.i3x.const import DOMAIN

    items = [
        {"uid": "1", "summary": "Milk", "status": "needs_action"},
        {"uid": "2", "summary": "Eggs", "status": "completed"},
    ]

    async def handle_get_items(call):
        return {"todo.groceries": {"items": items}}

    hass.services.async_register(
        "todo", "get_items", handle_get_items,
        supports_response=SupportsResponse.ONLY,
    )
    hass.states.async_set("todo.groceries", "1")
    await hass.async_block_till_done()
    hass.data[DOMAIN]["server"].model.invalidate()

    client = await hass_client()
    resp = await client.post(
        "/api/i3x/v1/objects/value", json={"elementIds": ["todo.groceries"]}
    )
    result = (await resp.json())["results"][0]["result"]
    assert result["quality"] == "Good"
    assert result["value"]["state"] == 1.0
    assert result["value"]["items"] == items

    # The declared type is a structured object schema.
    resp = await client.post(
        "/api/i3x/v1/objecttypes/query", json={"elementIds": ["type:todo"]}
    )
    schema = (await resp.json())["results"][0]["result"]["schema"]
    assert schema["type"] == "object"
    assert set(schema["properties"]) == {"state", "items"}

    # An echo write of the exact read value is a no-op success.
    resp = await client.put(
        "/api/i3x/v1/objects/value",
        json={
            "updates": [
                {"elementId": "todo.groceries", "value": {"value": result["value"]}}
            ]
        },
    )
    assert (await resp.json())["results"][0]["success"] is True


async def test_calendar_value_includes_events(world, hass, hass_client) -> None:
    from homeassistant.core import SupportsResponse

    from custom_components.i3x.const import DOMAIN

    events = [
        {
            "start": "2026-07-25T18:00:00-04:00",
            "end": "2026-07-25T21:00:00-04:00",
            "summary": "Birthday party",
        }
    ]

    async def handle_get_events(call):
        assert "start_date_time" in call.data and "end_date_time" in call.data
        return {"calendar.family": {"events": events}}

    hass.services.async_register(
        "calendar", "get_events", handle_get_events,
        supports_response=SupportsResponse.ONLY,
    )
    hass.states.async_set("calendar.family", "off", {"message": ""})
    await hass.async_block_till_done()
    hass.data[DOMAIN]["server"].model.invalidate()

    client = await hass_client()
    resp = await client.post(
        "/api/i3x/v1/objects/value", json={"elementIds": ["calendar.family"]}
    )
    result = (await resp.json())["results"][0]["result"]
    assert result["value"]["state"] is False
    assert result["value"]["events"] == events


async def test_weather_value_includes_forecast(world, hass, hass_client) -> None:
    from homeassistant.core import SupportsResponse

    from custom_components.i3x.const import DOMAIN

    forecast = [
        {"datetime": "2026-07-25T00:00:00Z", "condition": "sunny", "temperature": 88},
        {"datetime": "2026-07-26T00:00:00Z", "condition": "rainy", "temperature": 79},
    ]
    seen_types = []

    async def handle_get_forecasts(call):
        seen_types.append(call.data.get("type"))
        return {"weather.home": {"forecast": forecast}}

    hass.services.async_register(
        "weather", "get_forecasts", handle_get_forecasts,
        supports_response=SupportsResponse.ONLY,
    )
    hass.states.async_set(
        "weather.home",
        "sunny",
        {"temperature": 85.0, "humidity": 40, "supported_features": 3},
    )
    await hass.async_block_till_done()
    hass.data[DOMAIN]["server"].model.invalidate()

    client = await hass_client()
    resp = await client.post(
        "/api/i3x/v1/objects/value", json={"elementIds": ["weather.home"]}
    )
    result = (await resp.json())["results"][0]["result"]
    assert result["value"]["state"] == "sunny"
    assert result["value"]["temperature"] == 85.0
    assert result["value"]["forecast"] == forecast
    assert seen_types == ["daily"]  # prefers daily when supported


async def test_objects_value_unavailable(world, hass, hass_client) -> None:
    hass.states.async_set(
        world["sensor"],
        "unavailable",
        {"unit_of_measurement": "°C", "device_class": "temperature"},
    )
    client = await hass_client()
    resp = await client.post(
        "/api/i3x/v1/objects/value", json={"elementIds": [world["sensor"]]}
    )
    result = (await resp.json())["results"][0]["result"]
    assert result["value"] is None
    assert result["quality"] == "Bad"


async def test_put_value_echo_and_denial(world, hass, hass_client) -> None:
    """Echo writes are no-op successes; changing writes need the allowlist."""
    client = await hass_client()

    # Echo the switch's current value (on → true) back: success, no-op.
    resp = await client.put(
        "/api/i3x/v1/objects/value",
        json={
            "updates": [
                {"elementId": world["switch"], "value": {"value": True}},
                {"elementId": BOGUS, "value": {"value": 1}},
            ]
        },
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["results"][0]["success"] is True
    assert body["results"][0]["result"] is None
    assert body["results"][1]["success"] is False
    assert body["results"][1]["responseDetail"]["status"] == 404
    assert hass.states.get(world["switch"]).state == "on"

    # A value-changing write without write_enabled → per-item 403.
    resp = await client.put(
        "/api/i3x/v1/objects/value",
        json={"updates": [{"elementId": world["switch"], "value": {"value": False}}]},
    )
    body = await resp.json()
    assert body["results"][0]["success"] is False
    assert body["results"][0]["responseDetail"]["status"] == 403
    # Missing value.value → per-item 400.
    resp = await client.put(
        "/api/i3x/v1/objects/value",
        json={"updates": [{"elementId": world["switch"], "value": {}}]},
    )
    body = await resp.json()
    assert body["results"][0]["responseDetail"]["status"] == 400


async def test_history(world, hass, hass_client) -> None:
    from pytest_homeassistant_custom_component.components.recorder.common import (
        async_wait_recording_done,
    )

    hass.states.async_set(
        world["sensor"],
        "22.0",
        {"unit_of_measurement": "°C", "device_class": "temperature"},
    )
    hass.states.async_set(
        world["sensor"],
        "22.5",
        {"unit_of_measurement": "°C", "device_class": "temperature"},
    )
    await async_wait_recording_done(hass)

    start = _iso(dt_util.utcnow() - timedelta(hours=1))
    end = _iso(dt_util.utcnow() + timedelta(minutes=5))
    client = await hass_client()
    resp = await client.post(
        "/api/i3x/v1/objects/history",
        json={
            "elementIds": [world["sensor"], "home", BOGUS],
            "startTime": start,
            "endTime": end,
        },
    )
    assert resp.status == 200
    body = await resp.json()
    assert [r["elementId"] for r in body["results"]] == [
        world["sensor"],
        "home",
        BOGUS,
    ]

    sensor = body["results"][0]["result"]
    assert sensor["isComposition"] is False
    values = sensor["values"]
    assert values, "expected recorder-backed history points"
    stamps = [v["timestamp"] for v in values]
    assert stamps == sorted(stamps)
    for vqt in values:
        assert UTC_TS_RE.match(vqt["timestamp"])
        assert vqt["quality"] in ("Good", "GoodNoData", "Bad", "Uncertain")
    assert any(v["value"] == 22.5 for v in values)

    # Structural object: empty history, still a valid result shape.
    assert body["results"][1]["result"]["values"] == []
    # Unknown element: per-item 404.
    assert body["results"][2]["success"] is False


async def test_history_time_validation(world, hass, hass_client) -> None:
    client = await hass_client()
    valid = _iso(dt_util.utcnow())

    for payload in (
        {"elementIds": [world["sensor"]], "endTime": valid},
        {"elementIds": [world["sensor"]], "startTime": valid},
        {"elementIds": [world["sensor"]], "startTime": "not-a-date", "endTime": valid},
    ):
        resp = await client.post("/api/i3x/v1/objects/history", json=payload)
        assert resp.status == 400
        body = await resp.json()
        assert body["success"] is False
        assert body["responseDetail"]["status"] == 400


async def test_put_history_idempotent_only(world, hass, hass_client) -> None:
    """Re-writing an existing point succeeds; novel points are refused."""
    client = await hass_client()

    # Read the sensor's current VQT, then write it back at its own timestamp.
    resp = await client.post(
        "/api/i3x/v1/objects/value", json={"elementIds": [world["sensor"]]}
    )
    vqt = (await resp.json())["results"][0]["result"]
    resp = await client.put(
        "/api/i3x/v1/objects/history",
        json={
            "updates": [
                {
                    "elementId": world["sensor"],
                    "value": {
                        "value": vqt["value"],
                        "quality": "Good",
                        "timestamp": vqt["timestamp"],
                    },
                }
            ]
        },
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["results"][0]["success"] is True, body

    # A novel history point → per-item refusal (recorder is append-only).
    resp = await client.put(
        "/api/i3x/v1/objects/history",
        json={
            "updates": [
                {
                    "elementId": world["sensor"],
                    "value": {
                        "value": 99.9,
                        "quality": "Good",
                        "timestamp": "2020-01-01T00:00:00Z",
                    },
                }
            ]
        },
    )
    body = await resp.json()
    assert body["results"][0]["success"] is False
    assert body["results"][0]["responseDetail"]["status"] == 400
