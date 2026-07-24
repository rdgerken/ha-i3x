"""Tests for the security guards: local_only, /info rate limit, request caps."""

from __future__ import annotations

import custom_components.i3x.server.http_util as http_util
from custom_components.i3x.server.http_util import (
    InfoRateLimiter,
    ProblemError,
    is_local_address,
)

CLIENT = "pytest-client-sec"


def test_is_local_address() -> None:
    assert is_local_address("127.0.0.1")
    assert is_local_address("::1")
    assert is_local_address("192.168.1.10")
    assert is_local_address("172.24.6.55")
    assert is_local_address("10.0.0.1")
    assert not is_local_address("8.8.8.8")
    assert not is_local_address("2606:4700::1111")
    assert not is_local_address(None)
    assert not is_local_address("not-an-ip")


def test_info_rate_limiter() -> None:
    limiter = InfoRateLimiter(rate_per_minute=60, burst=3, max_ips=2)
    now = 1000.0
    # Local addresses are always exempt.
    for _ in range(10):
        limiter.check("192.168.1.5", now)
    # Non-local: burst of 3, then 429.
    for _ in range(3):
        limiter.check("8.8.8.8", now)
    try:
        limiter.check("8.8.8.8", now)
        raise AssertionError("expected ProblemError")
    except ProblemError as err:
        assert err.status == 429
    # Tokens refill over time.
    limiter.check("8.8.8.8", now + 2.0)
    # LRU bound: a third IP evicts the oldest without error.
    limiter.check("1.1.1.1", now)
    limiter.check("9.9.9.9", now)


async def test_local_only_blocks_external(
    world, hass, hass_client, hass_client_no_auth, monkeypatch
) -> None:
    """With local_only on (default), non-local sources get 403 everywhere."""
    monkeypatch.setattr(http_util, "is_local_address", lambda remote: False)

    client = await hass_client()
    resp = await client.get("/api/i3x/v1/namespaces")
    assert resp.status == 403
    body = await resp.json()
    assert body["success"] is False
    assert body["responseDetail"]["status"] == 403

    anon = await hass_client_no_auth()
    resp = await anon.get("/api/i3x/v1/info")
    assert resp.status == 403


async def test_bulk_cap(world, hass, hass_client) -> None:
    client = await hass_client()
    ids = [f"sensor.fake_{i}" for i in range(501)]
    resp = await client.post("/api/i3x/v1/objects/value", json={"elementIds": ids})
    assert resp.status == 400
    body = await resp.json()
    assert "limit" in body["responseDetail"]["detail"]


async def test_malformed_body(world, hass, hass_client) -> None:
    client = await hass_client()
    resp = await client.post(
        "/api/i3x/v1/objects/value",
        data=b"{not json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status == 400

    resp = await client.post("/api/i3x/v1/objects/value", json={"elementIds": "nope"})
    assert resp.status == 400


async def test_subscription_caps(world, hass, hass_client, monkeypatch) -> None:
    import custom_components.i3x.server.subscriptions as subs_mod

    monkeypatch.setattr(subs_mod, "MAX_SUBSCRIPTIONS_PER_CLIENT", 2)
    client = await hass_client()
    for _ in range(2):
        resp = await client.post(
            "/api/i3x/v1/subscriptions", json={"clientId": CLIENT}
        )
        assert resp.status == 200
    resp = await client.post("/api/i3x/v1/subscriptions", json={"clientId": CLIENT})
    assert resp.status == 400
