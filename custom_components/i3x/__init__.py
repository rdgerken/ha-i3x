"""i3X integration for Home Assistant.

Exposes Home Assistant as a CESMII i3X 1.0 server: areas, devices, and
entities become a browsable contextualized address space (objects, JSON-Schema
object types, values, recorder-backed history, and polled subscriptions)
served under /api/i3x/v1 on Home Assistant's own HTTP server.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_MODE, DOMAIN, MODE_SERVER
from .server import I3xServer
from .server.views import register_views

_LOGGER = logging.getLogger(__name__)


@dataclass
class I3xData:
    """Runtime data stored on the config entry."""

    server: I3xServer | None = None


I3xConfigEntry = ConfigEntry[I3xData]


async def async_setup_entry(hass: HomeAssistant, entry: I3xConfigEntry) -> bool:
    """Set up an i3X config entry."""
    mode = entry.data.get(CONF_MODE, MODE_SERVER)
    entry.runtime_data = I3xData()

    if mode == MODE_SERVER:
        register_views(hass)
        server = I3xServer(hass, entry)
        await server.async_start()
        entry.runtime_data.server = server
        hass.data.setdefault(DOMAIN, {})["server"] = server
        _LOGGER.info("i3X server started at /api/i3x/v1")
    else:
        _LOGGER.error("Unsupported i3X mode: %s", mode)
        return False

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: I3xConfigEntry) -> bool:
    """Unload an i3X config entry."""
    server = entry.runtime_data.server if entry.runtime_data else None
    if server is not None:
        await server.async_stop()
        if hass.data.get(DOMAIN, {}).get("server") is server:
            hass.data[DOMAIN]["server"] = None
    return True


async def _async_update_listener(hass: HomeAssistant, entry: I3xConfigEntry) -> None:
    """Reload the entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)
