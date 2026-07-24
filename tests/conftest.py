"""Shared fixtures for the i3X test suite.

Fixture-ordering convention (from pytest-homeassistant-custom-component):
`recorder_mock` must be instantiated BEFORE `hass`, so tests and fixtures
request them explicitly in that order.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

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


# --------------------------------------------------------------- fake server


def _iso(dt) -> str:
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


class FakeI3xServer:
    """A tiny canned i3X 1.0 server for exercising the client half."""

    def __init__(self, *, stream: bool = False, require_bearer: str | None = None):
        self.stream_capable = stream
        self.require_bearer = require_bearer
        self.value = 21.5
        self.put_updates: list[dict] = []
        self.sync_calls = 0
        self.seen_auth: list[str | None] = []
        app = web.Application()
        v1 = "/v1"
        app.router.add_get(f"{v1}/info", self._info)
        app.router.add_post(f"{v1}/objects/list", self._list)
        app.router.add_post(f"{v1}/objecttypes/query", self._types)
        app.router.add_post(f"{v1}/objects/value", self._values)
        app.router.add_put(f"{v1}/objects/value", self._put_values)
        app.router.add_post(f"{v1}/objects/history", self._history)
        app.router.add_post(f"{v1}/subscriptions", self._sub_create)
        app.router.add_post(f"{v1}/subscriptions/register", self._sub_register)
        app.router.add_post(f"{v1}/subscriptions/sync", self._sub_sync)
        app.router.add_post(f"{v1}/subscriptions/delete", self._sub_delete)
        app.router.add_post(f"{v1}/subscriptions/stream", self._sub_stream)
        self.server = TestServer(app)

    async def start(self) -> str:
        await self.server.start_server()
        return str(self.server.make_url("/v1"))

    async def stop(self) -> None:
        await self.server.close()

    def _check_auth(self, request):
        self.seen_auth.append(request.headers.get("Authorization"))
        if self.require_bearer is not None:
            if request.headers.get("Authorization") != f"Bearer {self.require_bearer}":
                return web.json_response(
                    {
                        "success": False,
                        "responseDetail": {
                            "title": "Unauthorized",
                            "status": 401,
                            "detail": "bad token",
                        },
                    },
                    status=401,
                )
        return None

    async def _info(self, request):
        if resp := self._check_auth(request):
            return resp
        return web.json_response(
            {
                "success": True,
                "result": {
                    "specVersion": "1.0",
                    "serverName": "Fake Plant",
                    "capabilities": {
                        "query": {"history": True},
                        "update": {"current": True, "history": False},
                        "subscribe": {"stream": self.stream_capable},
                    },
                },
            }
        )

    def _object(self, element_id):
        known = {
            "plant.temp": ("Plant Temperature", "type-number"),
            "plant.energy": ("Plant Energy", "type-number"),
        }
        if element_id not in known:
            return None
        name, type_id = known[element_id]
        return {
            "elementId": element_id,
            "displayName": name,
            "typeElementId": type_id,
            "parentId": None,
            "isComposition": False,
            "isExtended": False,
        }

    async def _list(self, request):
        if resp := self._check_auth(request):
            return resp
        body = await request.json()
        results = []
        for eid in body["elementIds"]:
            obj = self._object(eid)
            if obj:
                results.append({"success": True, "elementId": eid, "result": obj})
            else:
                results.append(
                    {
                        "success": False,
                        "elementId": eid,
                        "responseDetail": {
                            "title": "Not Found",
                            "status": 404,
                            "detail": f"nope: {eid}",
                        },
                    }
                )
        return web.json_response(
            {"success": all(r["success"] for r in results), "results": results}
        )

    async def _types(self, request):
        if resp := self._check_auth(request):
            return resp
        body = await request.json()
        results = [
            {
                "success": True,
                "elementId": tid,
                "result": {
                    "elementId": tid,
                    "displayName": "Number",
                    "namespaceUri": "urn:fake",
                    "sourceTypeId": "Number",
                    "schema": {"type": "number"},
                },
            }
            for tid in body["elementIds"]
        ]
        return web.json_response({"success": True, "results": results})

    async def _values(self, request):
        if resp := self._check_auth(request):
            return resp
        body = await request.json()
        results = []
        for eid in body["elementIds"]:
            if self._object(eid):
                results.append(
                    {
                        "success": True,
                        "elementId": eid,
                        "result": {
                            "isComposition": False,
                            "value": self.value,
                            "quality": "Good",
                            "timestamp": _iso(dt_util.utcnow()),
                        },
                    }
                )
            else:
                results.append(
                    {
                        "success": False,
                        "elementId": eid,
                        "responseDetail": {
                            "title": "Not Found",
                            "status": 404,
                            "detail": eid,
                        },
                    }
                )
        return web.json_response(
            {"success": all(r["success"] for r in results), "results": results}
        )

    async def _put_values(self, request):
        if resp := self._check_auth(request):
            return resp
        body = await request.json()
        self.put_updates.extend(body["updates"])
        results = [
            {"success": True, "elementId": u["elementId"], "result": None}
            for u in body["updates"]
        ]
        return web.json_response({"success": True, "results": results})

    async def _history(self, request):
        if resp := self._check_auth(request):
            return resp
        body = await request.json()
        # Three hourly counter readings ending last full hour.
        now = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
        values = [
            {
                "value": 100.0 + 10 * i,
                "quality": "Good",
                "timestamp": _iso(now - timedelta(hours=3 - i, minutes=-10)),
            }
            for i in range(3)
        ]
        results = [
            {
                "success": True,
                "elementId": eid,
                "result": {"isComposition": False, "values": values},
            }
            for eid in body["elementIds"]
        ]
        return web.json_response({"success": True, "results": results})

    async def _sub_create(self, request):
        if resp := self._check_auth(request):
            return resp
        body = await request.json()
        return web.json_response(
            {
                "success": True,
                "result": {
                    "clientId": body.get("clientId"),
                    "subscriptionId": "fake-sub-1",
                    "displayName": body.get("displayName"),
                },
            }
        )

    async def _sub_register(self, request):
        if resp := self._check_auth(request):
            return resp
        body = await request.json()
        return web.json_response(
            {
                "success": True,
                "results": [
                    {"success": True, "elementId": eid, "result": None}
                    for eid in body["elementIds"]
                ],
            }
        )

    async def _sub_sync(self, request):
        if resp := self._check_auth(request):
            return resp
        self.sync_calls += 1
        batches = [
            {
                "sequenceNumber": self.sync_calls,
                "updates": [
                    {
                        "elementId": "plant.temp",
                        "value": 30.0 + self.sync_calls,
                        "quality": "Good",
                        "timestamp": _iso(dt_util.utcnow()),
                    }
                ],
            }
        ]
        return web.json_response({"success": True, "result": batches})

    async def _sub_delete(self, request):
        if resp := self._check_auth(request):
            return resp
        body = await request.json()
        return web.json_response(
            {
                "success": True,
                "results": [
                    {"success": True, "subscriptionId": sid, "result": None}
                    for sid in body.get("subscriptionIds", [])
                ],
            }
        )

    async def _sub_stream(self, request):
        if resp := self._check_auth(request):
            return resp
        response = web.StreamResponse()
        response.headers["Content-Type"] = "text/event-stream"
        await response.prepare(request)
        frames = [
            [
                {
                    "elementId": "plant.temp",
                    "value": 41.0 + i,
                    "quality": "Good",
                    "timestamp": _iso(dt_util.utcnow()),
                }
            ]
            for i in range(2)
        ]
        await response.write(b": keep-alive\n\n")
        for frame in frames:
            await response.write(f"data: {json.dumps(frame)}\n\n".encode())
        await response.write_eof()
        return response


@pytest.fixture
async def fake_i3x(socket_enabled):
    """A running FakeI3xServer; yields (server, base_url)."""
    server = FakeI3xServer()
    base_url = await server.start()
    yield server, base_url
    await server.stop()
