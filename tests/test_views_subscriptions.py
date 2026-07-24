"""HTTP tests for the subscription lifecycle, sync semantics, and TTL."""

from __future__ import annotations

import time

from custom_components.i3x.const import DOMAIN

BOGUS = "i3x-test-nonexistent-7f3a9c"
CLIENT = "pytest-client-1"


async def _create(client, client_id=CLIENT):
    resp = await client.post(
        "/api/i3x/v1/subscriptions",
        json={"clientId": client_id, "displayName": "pytest"},
    )
    assert resp.status == 200
    return (await resp.json())["result"]["subscriptionId"]


async def test_create_requires_client_id(world, hass, hass_client) -> None:
    client = await hass_client()
    resp = await client.post("/api/i3x/v1/subscriptions", json={"displayName": "x"})
    assert resp.status == 400
    body = await resp.json()
    assert body["responseDetail"]["status"] == 400


async def test_lifecycle_and_sync(world, hass, hass_client) -> None:
    client = await hass_client()
    sub_id = await _create(client)

    # Register: mixed valid + bogus is a per-item partial failure.
    resp = await client.post(
        "/api/i3x/v1/subscriptions/register",
        json={
            "clientId": CLIENT,
            "subscriptionId": sub_id,
            "elementIds": [world["sensor"], BOGUS],
        },
    )
    body = await resp.json()
    assert body["results"][0]["success"] is True
    assert body["results"][1]["success"] is False
    assert body["results"][1]["responseDetail"]["status"] == 404

    # Duplicate registration is idempotent.
    resp = await client.post(
        "/api/i3x/v1/subscriptions/register",
        json={
            "clientId": CLIENT,
            "subscriptionId": sub_id,
            "elementIds": [world["sensor"]],
        },
    )
    assert (await resp.json())["results"][0]["success"] is True

    # List shows the monitored object.
    resp = await client.post(
        "/api/i3x/v1/subscriptions/list",
        json={"clientId": CLIENT, "subscriptionIds": [sub_id]},
    )
    detail = (await resp.json())["results"][0]["result"]
    assert detail["subscriptionId"] == sub_id
    assert [m["elementId"] for m in detail["monitoredObjects"]] == [world["sensor"]]

    # First sync: empty queue stages a snapshot batch of current values.
    resp = await client.post(
        "/api/i3x/v1/subscriptions/sync",
        json={"clientId": CLIENT, "subscriptionId": sub_id},
    )
    batches = (await resp.json())["result"]
    assert batches
    seqs = [b["sequenceNumber"] for b in batches]
    assert seqs == sorted(seqs)
    update = batches[0]["updates"][0]
    assert update["elementId"] == world["sensor"]
    assert update["quality"] == "Good"

    # A state change queues an event batch.
    hass.states.async_set(
        world["sensor"],
        "23.0",
        {"unit_of_measurement": "°C", "device_class": "temperature"},
    )
    await hass.async_block_till_done()

    resp = await client.post(
        "/api/i3x/v1/subscriptions/sync",
        json={"clientId": CLIENT, "subscriptionId": sub_id},
    )
    batches = (await resp.json())["result"]
    assert any(
        u["value"] == 23.0 for b in batches for u in b["updates"]
    ), batches

    # Ack removes everything up to lastSequenceNumber.
    last = batches[-1]["sequenceNumber"]
    resp = await client.post(
        "/api/i3x/v1/subscriptions/sync",
        json={"clientId": CLIENT, "subscriptionId": sub_id, "lastSequenceNumber": last},
    )
    batches = (await resp.json())["result"]
    assert all(b["sequenceNumber"] > last for b in batches)

    # -1 clears the whole queue in one round trip.
    max_seen = max(b["sequenceNumber"] for b in batches) if batches else last
    resp = await client.post(
        "/api/i3x/v1/subscriptions/sync",
        json={"clientId": CLIENT, "subscriptionId": sub_id, "lastSequenceNumber": -1},
    )
    batches = (await resp.json())["result"]
    assert all(b["sequenceNumber"] > max_seen for b in batches)

    # Unregister: valid + not-registered are per-item results.
    resp = await client.post(
        "/api/i3x/v1/subscriptions/unregister",
        json={
            "clientId": CLIENT,
            "subscriptionId": sub_id,
            "elementIds": [world["sensor"], BOGUS],
        },
    )
    body = await resp.json()
    assert body["results"][0]["success"] is True
    assert body["results"][1]["success"] is False

    # Delete, then sync → 404 with a proper error envelope.
    resp = await client.post(
        "/api/i3x/v1/subscriptions/delete",
        json={"clientId": CLIENT, "subscriptionIds": [sub_id]},
    )
    assert (await resp.json())["results"][0]["success"] is True

    resp = await client.post(
        "/api/i3x/v1/subscriptions/sync",
        json={"clientId": CLIENT, "subscriptionId": sub_id},
    )
    assert resp.status == 404
    body = await resp.json()
    assert body["success"] is False
    assert body["responseDetail"]["status"] == 404


async def test_cross_client_scoping(world, hass, hass_client) -> None:
    client = await hass_client()
    sub_id = await _create(client)

    resp = await client.post(
        "/api/i3x/v1/subscriptions/sync",
        json={"clientId": "someone-else", "subscriptionId": sub_id},
    )
    assert resp.status == 404

    resp = await client.post(
        "/api/i3x/v1/subscriptions/list",
        json={"clientId": "someone-else", "subscriptionIds": [sub_id]},
    )
    assert (await resp.json())["results"][0]["success"] is False

    # Sync without a clientId at all → 400.
    resp = await client.post(
        "/api/i3x/v1/subscriptions/sync", json={"subscriptionId": sub_id}
    )
    assert resp.status == 400


async def _read_data_frame(resp, timeout=5.0):
    """Read SSE lines until a data: frame arrives; return its parsed JSON."""
    import asyncio
    import json

    while True:
        line = await asyncio.wait_for(resp.content.readline(), timeout)
        if line.startswith(b"data: "):
            return json.loads(line[6:].decode())


async def test_stream_delivers_updates(world, hass, hass_client) -> None:
    import asyncio

    client = await hass_client()
    sub_id = await _create(client)
    await client.post(
        "/api/i3x/v1/subscriptions/register",
        json={
            "clientId": CLIENT,
            "subscriptionId": sub_id,
            "elementIds": [world["sensor"]],
        },
    )

    resp = await client.post(
        "/api/i3x/v1/subscriptions/stream",
        json={"clientId": CLIENT, "subscriptionId": sub_id},
    )
    assert resp.status == 200
    assert resp.headers["Content-Type"].startswith("text/event-stream")

    # Sync while the stream is open must be refused.
    sync = await client.post(
        "/api/i3x/v1/subscriptions/sync",
        json={"clientId": CLIENT, "subscriptionId": sub_id},
    )
    assert sync.status == 400

    hass.states.async_set(
        world["sensor"],
        "25.5",
        {"unit_of_measurement": "°C", "device_class": "temperature"},
    )
    await hass.async_block_till_done()

    updates = await _read_data_frame(resp)
    assert isinstance(updates, list)
    assert updates[0]["elementId"] == world["sensor"]
    assert updates[0]["value"] == 25.5
    assert updates[0]["quality"] == "Good"
    resp.close()
    await asyncio.sleep(0)


async def test_stream_takeover_closes_previous(world, hass, hass_client) -> None:
    import asyncio

    client = await hass_client()
    sub_id = await _create(client)

    resp_a = await client.post(
        "/api/i3x/v1/subscriptions/stream",
        json={"clientId": CLIENT, "subscriptionId": sub_id},
    )
    assert resp_a.status == 200

    resp_b = await client.post(
        "/api/i3x/v1/subscriptions/stream",
        json={"clientId": CLIENT, "subscriptionId": sub_id},
    )
    assert resp_b.status == 200

    # Stream A must end cleanly (EOF, not an error) once B opens.
    remainder = await asyncio.wait_for(resp_a.content.read(), 10)
    assert isinstance(remainder, bytes)
    resp_b.close()
    await asyncio.sleep(0)

    # Wrong client on stream → 404 problem, not a stream.
    resp = await client.post(
        "/api/i3x/v1/subscriptions/stream",
        json={"clientId": "someone-else", "subscriptionId": sub_id},
    )
    assert resp.status == 404


async def test_ttl_expiry(world, hass, hass_client) -> None:
    client = await hass_client()
    sub_id = await _create(client)

    manager = hass.data[DOMAIN]["server"].subscriptions
    sub = manager._subs[sub_id]
    sub.last_activity = time.monotonic() - 10_000
    manager._janitor(None)

    resp = await client.post(
        "/api/i3x/v1/subscriptions/sync",
        json={"clientId": CLIENT, "subscriptionId": sub_id},
    )
    assert resp.status == 404


async def test_queue_overflow_returns_206(
    world, hass, hass_client, monkeypatch
) -> None:
    import custom_components.i3x.server.subscriptions as subs_mod

    monkeypatch.setattr(subs_mod, "MAX_QUEUED_BATCHES", 3)

    client = await hass_client()
    sub_id = await _create(client)
    await client.post(
        "/api/i3x/v1/subscriptions/register",
        json={
            "clientId": CLIENT,
            "subscriptionId": sub_id,
            "elementIds": [world["switch"]],
        },
    )
    for i in range(6):
        hass.states.async_set(world["switch"], "on" if i % 2 else "off")
        await hass.async_block_till_done()

    resp = await client.post(
        "/api/i3x/v1/subscriptions/sync",
        json={"clientId": CLIENT, "subscriptionId": sub_id},
    )
    assert resp.status == 206
    body = await resp.json()
    assert body["success"] is True
    assert body["responseDetail"]["status"] == 206
    assert len(body["result"]) == 3
