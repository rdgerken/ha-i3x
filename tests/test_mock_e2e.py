"""Interop test against the official conformance suite's mock server.

Run the spec repo's reference mock (`node conformance-tests/mock/server.js`,
port 8331) and set I3X_MOCK_URL=http://127.0.0.1:8331/v1 to enable these.
They prove the client half against a known-good third-party implementation.
"""

from __future__ import annotations

import os

import aiohttp
import pytest

from custom_components.i3x.api import I3xApiClient

MOCK_URL = os.environ.get("I3X_MOCK_URL")

pytestmark = pytest.mark.skipif(
    not MOCK_URL, reason="I3X_MOCK_URL not set (mock server not running)"
)


@pytest.fixture
async def mock_client(socket_enabled):
    session = aiohttp.ClientSession()
    yield I3xApiClient(session, MOCK_URL)
    await session.close()


async def test_browse_and_read(mock_client) -> None:
    info = await mock_client.async_get_info()
    assert info["specVersion"].startswith("1")

    roots = await mock_client.async_get_objects(root=True)
    assert roots and roots[0]["parentId"] is None

    all_objects = await mock_client.async_get_objects()
    ids = [o["elementId"] for o in all_objects][:5]
    results = await mock_client.async_get_values(ids)
    assert all(item["success"] for item in results)
    for item in results:
        vqt = item["result"]
        assert vqt["quality"] in ("Good", "GoodNoData", "Bad", "Uncertain")


async def test_subscription_roundtrip(mock_client) -> None:
    roots = await mock_client.async_get_objects(root=True)
    target = roots[0]["elementId"]

    sub_id = await mock_client.async_create_subscription("ha-i3x-e2e", "e2e")
    reg = await mock_client.async_register("ha-i3x-e2e", sub_id, [target])
    assert reg[0]["success"] is True

    batches = await mock_client.async_sync("ha-i3x-e2e", sub_id)
    assert isinstance(batches, list)
    if batches:
        assert batches[0]["updates"][0]["elementId"]

    await mock_client.async_delete_subscription("ha-i3x-e2e", sub_id)


async def test_stream_one_frame(mock_client) -> None:
    roots = await mock_client.async_get_objects(root=True)
    sub_id = await mock_client.async_create_subscription("ha-i3x-e2e-s", "e2e")
    await mock_client.async_register(
        "ha-i3x-e2e-s", sub_id, [roots[0]["elementId"]]
    )
    async for updates in mock_client.stream("ha-i3x-e2e-s", sub_id):
        assert isinstance(updates, list)
        break
    await mock_client.async_delete_subscription("ha-i3x-e2e-s", sub_id)
