"""Diagnostics for the i3X integration."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from . import I3xConfigEntry


REDACT_KEYS = ("token", "password", "header_value")


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: I3xConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    entry_data = {
        key: ("**REDACTED**" if key in REDACT_KEYS and value else value)
        for key, value in entry.data.items()
    }
    data: dict[str, Any] = {
        "entry_data": entry_data,
        "options": dict(entry.options),
    }
    runtime = entry.runtime_data
    if runtime is None:
        return data
    if runtime.server is not None:
        snapshot = runtime.server.model.snapshot()
        data["server"] = {
            "objects": len(snapshot.objects),
            "types": len(snapshot.types),
            "subscriptions": runtime.server.subscriptions.count,
            "capabilities": runtime.server.capabilities(),
        }
    if runtime.api is not None:
        data["client"] = {
            "base_url": runtime.api.base_url,
            "capabilities": runtime.capabilities,
            "push": runtime.push.stats if runtime.push else None,
            "importer": runtime.importer.stats if runtime.importer else None,
        }
    return data
