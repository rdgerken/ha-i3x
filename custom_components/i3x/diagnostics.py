"""Diagnostics for the i3X integration."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from . import I3xConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: I3xConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    data: dict[str, Any] = {
        "entry_data": dict(entry.data),
        "options": dict(entry.options),
    }
    server = entry.runtime_data.server if entry.runtime_data else None
    if server is not None:
        snapshot = server.model.snapshot()
        data["server"] = {
            "objects": len(snapshot.objects),
            "types": len(snapshot.types),
            "subscriptions": server.subscriptions.count,
            "capabilities": server.capabilities(),
        }
    return data
