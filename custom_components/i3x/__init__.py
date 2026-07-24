"""i3X integration for Home Assistant.

Both halves of CESMII i3X 1.0:

- **Server**: exposes Home Assistant's areas, devices, and entities as a
  browsable contextualized address space (objects, JSON-Schema object types,
  values, recorder-backed history, subscriptions incl. SSE) under /api/i3x/v1.
- **Client**: connects to external i3X servers, surfacing remote objects as
  sensor entities (live via SSE or sync polling) and importing their history
  into long-term statistics; the i3x.write service writes values back.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .api import I3xApiClient, I3xAuthError, I3xError
from .const import (
    ATTR_CONFIG_ENTRY_ID,
    ATTR_ELEMENT_ID,
    ATTR_QUALITY,
    ATTR_TIMESTAMP,
    ATTR_VALUE,
    CONF_AUTH_TYPE,
    CONF_BASE_URL,
    CONF_HEADER_NAME,
    CONF_HEADER_VALUE,
    CONF_IMPORT_COUNTER_ELEMENT_IDS,
    CONF_IMPORT_ELEMENT_IDS,
    CONF_MODE,
    CONF_PASSWORD,
    CONF_TOKEN,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
    DOMAIN,
    MODE_CLIENT,
    MODE_SERVER,
    QUALITY_BAD,
    QUALITY_GOOD,
    QUALITY_GOOD_NO_DATA,
    QUALITY_UNCERTAIN,
    SERVICE_WRITE,
)
from .server import I3xServer
from .server.views import register_views

_LOGGER = logging.getLogger(__name__)

CLIENT_PLATFORMS = [Platform.SENSOR]


@dataclass
class I3xData:
    """Runtime data stored on the config entry."""

    server: I3xServer | None = None
    api: I3xApiClient | None = None
    info: dict[str, Any] | None = None
    capabilities: dict[str, Any] = field(default_factory=dict)
    coordinator: Any = None
    push: Any = None
    importer: Any = None


I3xConfigEntry = ConfigEntry[I3xData]

SERVICE_WRITE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ELEMENT_ID): cv.string,
        vol.Required(ATTR_VALUE): vol.Any(dict, list, bool, int, float, cv.string),
        vol.Optional(ATTR_QUALITY): vol.In(
            [QUALITY_GOOD, QUALITY_GOOD_NO_DATA, QUALITY_BAD, QUALITY_UNCERTAIN]
        ),
        vol.Optional(ATTR_TIMESTAMP): cv.datetime,
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
    }
)


def build_api_client(hass: HomeAssistant, data: dict) -> I3xApiClient:
    """Construct an API client from config-entry data."""
    return I3xApiClient(
        async_get_clientsession(hass),
        data[CONF_BASE_URL],
        auth_type=data.get(CONF_AUTH_TYPE, "none"),
        token=data.get(CONF_TOKEN),
        username=data.get(CONF_USERNAME),
        password=data.get(CONF_PASSWORD),
        header_name=data.get(CONF_HEADER_NAME),
        header_value=data.get(CONF_HEADER_VALUE),
        verify_ssl=data.get(CONF_VERIFY_SSL, True),
    )


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
    elif mode == MODE_CLIENT:
        api = build_api_client(hass, entry.data)
        try:
            info = await api.async_get_info()
        except I3xAuthError as err:
            raise ConfigEntryNotReady(f"i3X authentication failed: {err}") from err
        except I3xError as err:
            raise ConfigEntryNotReady(f"Cannot reach i3X server: {err}") from err
        entry.runtime_data.api = api
        entry.runtime_data.info = info
        entry.runtime_data.capabilities = info.get("capabilities") or {}

        await hass.config_entries.async_forward_entry_setups(entry, CLIENT_PLATFORMS)

        measurement_ids = entry.options.get(CONF_IMPORT_ELEMENT_IDS, [])
        counter_ids = entry.options.get(CONF_IMPORT_COUNTER_ELEMENT_IDS, [])
        if measurement_ids or counter_ids:
            from .statistics import I3xStatisticsImporter

            importer = I3xStatisticsImporter(
                hass, api, measurement_ids, counter_ids
            )
            entry.runtime_data.importer = importer
            importer.async_start()
        _LOGGER.info("i3X client connected to %s", api.base_url)
    else:
        _LOGGER.error("Unsupported i3X mode: %s", mode)
        return False

    _async_register_services(hass)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: I3xConfigEntry) -> bool:
    """Unload an i3X config entry."""
    runtime = entry.runtime_data
    mode = entry.data.get(CONF_MODE, MODE_SERVER)
    if mode == MODE_SERVER:
        if runtime and runtime.server is not None:
            await runtime.server.async_stop()
            if hass.data.get(DOMAIN, {}).get("server") is runtime.server:
                hass.data[DOMAIN]["server"] = None
        return True

    ok = await hass.config_entries.async_unload_platforms(entry, CLIENT_PLATFORMS)
    if runtime:
        if runtime.push is not None:
            await runtime.push.async_stop()
        if runtime.importer is not None:
            runtime.importer.async_stop()
    return ok


async def _async_update_listener(hass: HomeAssistant, entry: I3xConfigEntry) -> None:
    """Reload the entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


def _async_register_services(hass: HomeAssistant) -> None:
    """Register domain services (idempotent)."""
    if hass.services.has_service(DOMAIN, SERVICE_WRITE):
        return

    def _client_entries() -> list[I3xConfigEntry]:
        return [
            e
            for e in hass.config_entries.async_entries(DOMAIN)
            if e.data.get(CONF_MODE) == MODE_CLIENT
            and e.state is ConfigEntryState.LOADED
        ]

    async def _handle_write(call: ServiceCall) -> None:
        entries = _client_entries()
        if entry_id := call.data.get(ATTR_CONFIG_ENTRY_ID):
            entries = [e for e in entries if e.entry_id == entry_id]
        if not entries:
            raise HomeAssistantError("No loaded i3X client entry to write through")
        entry = entries[0]
        capabilities = entry.runtime_data.capabilities or {}
        if not (capabilities.get("update") or {}).get("current"):
            raise HomeAssistantError(
                "The remote i3X server does not declare update.current"
            )
        vqt: dict[str, Any] = {"value": call.data[ATTR_VALUE]}
        if ATTR_QUALITY in call.data:
            vqt["quality"] = call.data[ATTR_QUALITY]
        if ATTR_TIMESTAMP in call.data:
            vqt["timestamp"] = (
                dt_util.as_utc(call.data[ATTR_TIMESTAMP])
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z")
            )
        try:
            results = await entry.runtime_data.api.async_put_values(
                [{"elementId": call.data[ATTR_ELEMENT_ID], "value": vqt}]
            )
        except I3xError as err:
            raise HomeAssistantError(f"i3X write failed: {err}") from err
        item = results[0] if results else {}
        if not item.get("success"):
            detail = (item.get("responseDetail") or {}).get("detail", "unknown error")
            raise HomeAssistantError(f"i3X write rejected: {detail}")

    hass.services.async_register(
        DOMAIN,
        SERVICE_WRITE,
        _handle_write,
        schema=SERVICE_WRITE_SCHEMA,
        supports_response=SupportsResponse.NONE,
    )
