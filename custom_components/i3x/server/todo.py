"""Todo-list item fetching for the i3X server.

A todo entity's state is only the count of open items; the items themselves
are exposed exclusively through the todo.get_items service. This cache fetches
them on demand and keys freshness on the entity's last_updated timestamp, so
items refresh exactly when the list changes and repeated browsing stays cheap.
"""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant, State

from .values import iso_z

_LOGGER = logging.getLogger(__name__)

ITEM_FIELDS = ("uid", "summary", "status", "due", "description")


class TodoItemsCache:
    """Per-entity cache of todo items, invalidated by state timestamp."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._items: dict[str, tuple[str, list | None]] = {}

    def peek(self, entity_id: str) -> list | None:
        """Best-available items without awaiting (may be stale or None)."""
        record = self._items.get(entity_id)
        return record[1] if record else None

    async def async_get(self, entity_id: str, state: State) -> list | None:
        """Items current as of the given state, fetching if stale."""
        key = iso_z(state.last_updated)
        record = self._items.get(entity_id)
        if record and record[0] == key:
            return record[1]
        items = await self._async_fetch(entity_id)
        self._items[entity_id] = (key, items)
        return items

    async def _async_fetch(self, entity_id: str) -> list | None:
        try:
            response = await self._hass.services.async_call(
                "todo",
                "get_items",
                {"entity_id": entity_id},
                blocking=True,
                return_response=True,
            )
        except Exception as err:  # noqa: BLE001 - never fail a read over items
            _LOGGER.debug("todo.get_items failed for %s: %s", entity_id, err)
            return None
        data = (response or {}).get(entity_id) or {}
        items = data.get("items")
        if not isinstance(items, list):
            return None
        return [
            {
                field: item[field]
                for field in ITEM_FIELDS
                if item.get(field) is not None
            }
            for item in items
            if isinstance(item, dict)
        ]
