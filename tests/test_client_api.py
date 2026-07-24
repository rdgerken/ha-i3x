"""Tests for the async i3X API client against the fake server."""

from __future__ import annotations

import aiohttp
import pytest

from custom_components.i3x.api import I3xApiClient, I3xAuthError
from tests.conftest import FakeI3xServer


@pytest.fixture
async def http_session(socket_enabled):
    session = aiohttp.ClientSession()
    yield session
    await session.close()


async def test_info_and_values(http_session, fake_i3x) -> None:
    server, base_url = fake_i3x
    client = I3xApiClient(http_session, base_url)
    info = await client.async_get_info()
    assert info["specVersion"] == "1.0"
    assert info["capabilities"]["query"]["history"] is True

    results = await client.async_get_values(["plant.temp", "bogus"])
    assert results[0]["success"] is True
    assert results[0]["result"]["value"] == 21.5
    assert results[1]["success"] is False


async def test_bearer_auth(http_session, socket_enabled) -> None:
    server = FakeI3xServer(require_bearer="sekrit")
    base_url = await server.start()
    try:
        good = I3xApiClient(
            http_session, base_url, auth_type="bearer", token="sekrit"
        )
        assert (await good.async_get_info())["serverName"] == "Fake Plant"

        bad = I3xApiClient(http_session, base_url, auth_type="bearer", token="wrong")
        with pytest.raises(I3xAuthError):
            await bad.async_get_info()
    finally:
        await server.stop()


async def test_subscription_lifecycle(http_session, fake_i3x) -> None:
    server, base_url = fake_i3x
    client = I3xApiClient(http_session, base_url)
    sub_id = await client.async_create_subscription("test-client", "Test")
    assert sub_id == "fake-sub-1"
    results = await client.async_register("test-client", sub_id, ["plant.temp"])
    assert results[0]["success"] is True
    batches = await client.async_sync("test-client", sub_id)
    assert batches[0]["updates"][0]["elementId"] == "plant.temp"
    await client.async_delete_subscription("test-client", sub_id)


async def test_stream_frames(http_session, fake_i3x) -> None:
    server, base_url = fake_i3x
    client = I3xApiClient(http_session, base_url)
    frames = []
    async for updates in client.stream("test-client", "fake-sub-1"):
        frames.append(updates)
    assert len(frames) == 2
    assert frames[0][0]["value"] == 41.0
    assert frames[1][0]["value"] == 42.0


async def test_put_values(http_session, fake_i3x) -> None:
    server, base_url = fake_i3x
    client = I3xApiClient(http_session, base_url)
    results = await client.async_put_values(
        [{"elementId": "plant.temp", "value": {"value": 25.0}}]
    )
    assert results[0]["success"] is True
    assert server.put_updates[0]["value"]["value"] == 25.0
