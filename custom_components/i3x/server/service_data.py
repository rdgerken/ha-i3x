"""Service-response entity data for the i3X server.

Three entity domains keep their primary content behind a response-returning
service call instead of in the state machine: todo items (todo.get_items),
calendar events (calendar.get_events), and weather forecasts
(weather.get_forecasts). This cache fetches that payload per entity and keys
freshness on the entity's last_updated timestamp plus a short TTL, because
list edits and forecast refreshes do not always change the entity state.

Sync code paths (subscription callbacks, component reads, write comparisons)
use peek() for the best cached payload; the async value view fetches fresh.
"""

from __future__ import annotations

import logging
import time
from datetime import timedelta

from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util

from ..const import SERVICE_DATA_TTL_SECONDS
from .values import iso_z

_LOGGER = logging.getLogger(__name__)

TODO_ITEM_FIELDS = ("uid", "summary", "status", "due", "description")
CALENDAR_EVENT_FIELDS = ("start", "end", "summary", "description", "location")
CALENDAR_WINDOW_DAYS = 7

# WeatherEntityFeature bit -> forecast type, in preference order.
FORECAST_TYPES = ((1, "daily"), (2, "hourly"), (4, "twice_daily"))


class ServiceDataCache:
    """Per-entity cache of service-response payloads."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        # entity_id -> (state_key, fetched_monotonic, payload)
        self._data: dict[str, tuple[str, float, list | None]] = {}

    def peek(self, entity_id: str) -> list | None:
        """Best-available payload without awaiting (may be stale or None)."""
        record = self._data.get(entity_id)
        return record[2] if record else None

    async def async_get(self, entity_id: str, state: State) -> list | None:
        """Payload current as of the given state, fetching when stale."""
        key = iso_z(state.last_updated)
        record = self._data.get(entity_id)
        if (
            record
            and record[0] == key
            and time.monotonic() - record[1] < SERVICE_DATA_TTL_SECONDS
        ):
            return record[2]
        payload = await self._async_fetch(entity_id, state)
        self._data[entity_id] = (key, time.monotonic(), payload)
        return payload

    async def _async_fetch(self, entity_id: str, state: State) -> list | None:
        domain = entity_id.split(".", 1)[0]
        try:
            if domain == "todo":
                return await self._fetch_todo(entity_id)
            if domain == "calendar":
                return await self._fetch_calendar(entity_id)
            if domain == "weather":
                return await self._fetch_weather(entity_id, state)
        except Exception as err:  # noqa: BLE001 - never fail a read over extras
            _LOGGER.debug("Service data fetch failed for %s: %s", entity_id, err)
        return None

    async def _call(self, domain: str, service: str, data: dict) -> dict:
        response = await self._hass.services.async_call(
            domain, service, data, blocking=True, return_response=True
        )
        return (response or {}).get(data["entity_id"]) or {}

    async def _fetch_todo(self, entity_id: str) -> list | None:
        items = (await self._call("todo", "get_items", {"entity_id": entity_id})).get(
            "items"
        )
        if not isinstance(items, list):
            return None
        return [
            {f: item[f] for f in TODO_ITEM_FIELDS if item.get(f) is not None}
            for item in items
            if isinstance(item, dict)
        ]

    async def _fetch_calendar(self, entity_id: str) -> list | None:
        now = dt_util.now()
        events = (
            await self._call(
                "calendar",
                "get_events",
                {
                    "entity_id": entity_id,
                    "start_date_time": now.isoformat(),
                    "end_date_time": (
                        now + timedelta(days=CALENDAR_WINDOW_DAYS)
                    ).isoformat(),
                },
            )
        ).get("events")
        if not isinstance(events, list):
            return None
        return [
            {f: event[f] for f in CALENDAR_EVENT_FIELDS if event.get(f) is not None}
            for event in events
            if isinstance(event, dict)
        ]

    async def _fetch_weather(self, entity_id: str, state: State) -> list | None:
        features = state.attributes.get("supported_features") or 0
        forecast_type = next(
            (name for bit, name in FORECAST_TYPES if features & bit), None
        )
        if forecast_type is None:
            return None
        forecast = (
            await self._call(
                "weather",
                "get_forecasts",
                {"entity_id": entity_id, "type": forecast_type},
            )
        ).get("forecast")
        return forecast if isinstance(forecast, list) else None
