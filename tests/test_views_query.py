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


async def test_put_value_not_implemented(world, hass, hass_client) -> None:
    client = await hass_client()
    resp = await client.put(
        "/api/i3x/v1/objects/value",
        json={"updates": [{"elementId": world["switch"], "value": {"value": 0}}]},
    )
    assert resp.status == 501
    body = await resp.json()
    assert body["success"] is False
    assert body["responseDetail"]["status"] == 501


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


async def test_put_history_not_implemented(world, hass, hass_client) -> None:
    client = await hass_client()
    resp = await client.put(
        "/api/i3x/v1/objects/history",
        json={"updates": []},
    )
    assert resp.status == 501
