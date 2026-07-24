"""Sensor entities for remote i3X objects (client entries)."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_LIVE_ELEMENT_IDS, DOMAIN
from .coordinator import I3xLiveCoordinator, I3xPushManager

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry,  # I3xConfigEntry
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create one sensor per monitored remote elementId."""
    element_ids: list[str] = entry.options.get(CONF_LIVE_ELEMENT_IDS, [])
    if not element_ids:
        return
    runtime = entry.runtime_data
    coordinator = I3xLiveCoordinator(hass, entry, runtime.api, element_ids)
    runtime.coordinator = coordinator
    await coordinator.async_config_entry_first_refresh()

    capabilities = runtime.capabilities or {}
    stream_capable = bool(
        (capabilities.get("subscribe") or {}).get("stream", False)
    )
    push = I3xPushManager(
        hass, entry, runtime.api, coordinator, element_ids, stream_capable
    )
    runtime.push = push
    push.async_start()

    async_add_entities(
        I3xObjectSensor(coordinator, entry, element_id)
        for element_id in element_ids
    )


class I3xObjectSensor(CoordinatorEntity[I3xLiveCoordinator], SensorEntity):
    """Latest VQT of a single remote i3X object."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: I3xLiveCoordinator,
        entry,
        element_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._element_id = element_id
        record = coordinator.meta.get(element_id) or {}
        self._attr_name = record.get("displayName") or element_id
        self._attr_unique_id = f"{entry.entry_id}_{element_id}"
        if coordinator.schema_types.get(element_id) in ("number", "integer"):
            self._attr_state_class = SensorStateClass.MEASUREMENT
        server_name = None
        if entry.runtime_data and entry.runtime_data.info:
            server_name = entry.runtime_data.info.get("serverName")
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="CESMII i3X",
            model=server_name or "i3X server",
            entry_type=DeviceEntryType.SERVICE,
        )

    def _vqt(self) -> dict[str, Any]:
        return (self.coordinator.data or {}).get(self._element_id) or {}

    @property
    def available(self) -> bool:
        vqt = self._vqt()
        return (
            super().available
            and bool(vqt)
            and vqt.get("quality") != "Bad"
        )

    @property
    def native_value(self) -> Any:
        value = self._vqt().get("value")
        if isinstance(value, bool):
            return "on" if value else "off"
        if isinstance(value, dict):
            # Structured object value: expose its state field; the full
            # object rides along in the attributes.
            state = value.get("state")
            if isinstance(state, bool):
                return "on" if state else "off"
            return state
        if isinstance(value, list):
            return None
        return value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        vqt = self._vqt()
        attrs: dict[str, Any] = {
            "element_id": self._element_id,
            "quality": vqt.get("quality"),
            "source_timestamp": vqt.get("timestamp"),
            "type_element_id": (
                self.coordinator.meta.get(self._element_id) or {}
            ).get("typeElementId"),
        }
        if isinstance(vqt.get("value"), dict):
            attrs["object_value"] = vqt["value"]
        return attrs
