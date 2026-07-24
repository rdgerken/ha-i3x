"""Live-value coordinator and push loop for i3X client entries."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .api import I3xApiClient, I3xApiError, I3xConnectionError, I3xError
from .const import (
    DEFAULT_LIVE_SCAN_INTERVAL_SECONDS,
    DOMAIN,
    SYNC_POLL_INTERVAL_SECONDS,
)

_LOGGER = logging.getLogger(__name__)

PUSH_ERROR_BACKOFF_SECONDS = 30


class I3xLiveCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Batched current-value reads for the monitored remote objects."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        api: I3xApiClient,
        element_ids: list[str],
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_client",
            config_entry=entry,
            update_interval=timedelta(seconds=DEFAULT_LIVE_SCAN_INTERVAL_SECONDS),
        )
        self.api = api
        self.element_ids = element_ids
        self.meta: dict[str, dict[str, Any]] = {}  # elementId -> object record
        self.schema_types: dict[str, Any] = {}  # elementId -> JSON-Schema "type"

    async def _async_setup(self) -> None:
        """Fetch object records and their type schemas once."""
        try:
            objects = await self.api.async_list_objects(self.element_ids)
        except I3xError as err:
            raise UpdateFailed(f"Cannot list remote objects: {err}") from err
        type_ids: list[str] = []
        for item in objects:
            if item.get("success") and item.get("result"):
                record = item["result"]
                self.meta[record["elementId"]] = record
                if record.get("typeElementId"):
                    type_ids.append(record["typeElementId"])
            else:
                _LOGGER.warning(
                    "Remote object not found: %s", item.get("elementId")
                )
        if type_ids:
            try:
                types = await self.api.async_query_object_types(
                    list(dict.fromkeys(type_ids))
                )
            except I3xError:
                types = []
            schema_by_type = {
                t["result"]["elementId"]: (t["result"].get("schema") or {})
                for t in types
                if t.get("success") and t.get("result")
            }
            for element_id, record in self.meta.items():
                schema = schema_by_type.get(record.get("typeElementId"), {})
                self.schema_types[element_id] = schema.get("type")

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        try:
            results = await self.api.async_get_values(self.element_ids)
        except I3xError as err:
            raise UpdateFailed(str(err)) from err
        return {
            item["elementId"]: item["result"]
            for item in results
            if item.get("success") and item.get("result")
        }

    def apply_push(self, updates: list[dict]) -> None:
        """Merge pushed VQT updates into the coordinator data."""
        data = dict(self.data or {})
        changed = False
        for update in updates:
            element_id = update.get("elementId")
            if element_id in self.meta or element_id in (self.data or {}):
                data[element_id] = {
                    "isComposition": False,
                    "value": update.get("value"),
                    "quality": update.get("quality"),
                    "timestamp": update.get("timestamp"),
                }
                changed = True
        if changed:
            self.async_set_updated_data(data)


class I3xPushManager:
    """Keeps a server-side subscription alive and feeds the coordinator.

    Prefers SSE when the server declares subscribe.stream; falls back to
    /sync polling. Recreates the subscription on 404 (TTL expiry or server
    restart) and backs off on connection errors.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        api: I3xApiClient,
        coordinator: I3xLiveCoordinator,
        element_ids: list[str],
        stream_capable: bool,
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._api = api
        self._coordinator = coordinator
        self._element_ids = element_ids
        self._stream_capable = stream_capable
        self._client_id = f"ha-i3x-{entry.entry_id}"
        self._subscription_id: str | None = None
        self._task: asyncio.Task | None = None
        # Diagnostics
        self.mode = "stream" if stream_capable else "sync"
        self.updates_received = 0
        self.gaps_seen = 0
        self.last_error: str | None = None

    def async_start(self) -> None:
        self._task = self._entry.async_create_background_task(
            self._hass, self._run(), name=f"{DOMAIN}_push_{self._entry.entry_id}"
        )

    async def async_stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        if self._subscription_id is not None:
            try:
                await self._api.async_delete_subscription(
                    self._client_id, self._subscription_id
                )
            except I3xError:
                pass
            self._subscription_id = None

    async def _run(self) -> None:
        while True:
            try:
                self._subscription_id = await self._api.async_create_subscription(
                    self._client_id, "Home Assistant i3X client"
                )
                await self._api.async_register(
                    self._client_id, self._subscription_id, self._element_ids
                )
                if self._stream_capable:
                    await self._run_stream()
                else:
                    await self._run_sync()
            except asyncio.CancelledError:
                raise
            except I3xApiError as err:
                self.last_error = str(err)
                if err.status == 404:
                    # TTL expiry or server restart: recreate immediately.
                    self._subscription_id = None
                    continue
                _LOGGER.warning("i3X push error: %s", err)
                await asyncio.sleep(PUSH_ERROR_BACKOFF_SECONDS)
            except I3xConnectionError as err:
                self.last_error = str(err)
                _LOGGER.debug("i3X push connection lost: %s", err)
                await asyncio.sleep(PUSH_ERROR_BACKOFF_SECONDS)

    async def _run_stream(self) -> None:
        assert self._subscription_id is not None
        async for updates in self._api.stream(
            self._client_id, self._subscription_id
        ):
            self.updates_received += len(updates)
            self._coordinator.apply_push(updates)

    async def _run_sync(self) -> None:
        assert self._subscription_id is not None
        last_seq: int | None = None
        while True:
            batches = await self._api.async_sync(
                self._client_id, self._subscription_id, last_seq
            )
            if last_seq is not None and batches:
                first = batches[0]["sequenceNumber"]
                if first > last_seq + 1:
                    # Queue overflowed server-side; live state self-heals on
                    # the next update, but record the gap for diagnostics.
                    self.gaps_seen += 1
            for batch in batches:
                self.updates_received += len(batch["updates"])
                self._coordinator.apply_push(batch["updates"])
            if batches:
                last_seq = batches[-1]["sequenceNumber"]
            await asyncio.sleep(SYNC_POLL_INTERVAL_SECONDS)

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "subscription_id": self._subscription_id,
            "updates_received": self.updates_received,
            "gaps_seen": self.gaps_seen,
            "last_error": self.last_error,
        }
