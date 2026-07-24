"""Recorder-backed history for POST /objects/history.

All recorder access runs in the recorder executor. Rows are capped at
MAX_HISTORY_ROWS per request; a truncated response is returned as HTTP 206
with a responseDetail (spec: server-imposed limits must never be silent).
"""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.recorder import get_instance, history
from homeassistant.core import HomeAssistant

from ..const import MAX_HISTORY_ROWS
from .http_util import item_not_found, item_ok
from .model import AddressSpace
from .values import state_to_vqt


async def fetch_history(
    hass: HomeAssistant,
    snapshot: AddressSpace,
    element_ids: list[str],
    start: datetime,
    end: datetime,
) -> tuple[list[dict], bool]:
    """Return (bulk results aligned with element_ids, truncated flag)."""
    entity_ids = [
        eid
        for eid in element_ids
        if (obj := snapshot.objects.get(eid)) is not None and obj.entity_id is not None
    ]

    states_map: dict[str, list] = {}
    if entity_ids:
        states_map = await get_instance(hass).async_add_executor_job(
            _blocking_history, hass, start, end, entity_ids
        )

    results: list[dict] = []
    total_rows = 0
    truncated = False
    for element_id in element_ids:
        obj = snapshot.objects.get(element_id)
        if obj is None:
            results.append(item_not_found("elementId", element_id))
            continue
        values: list[dict] = []
        if obj.entity_id is not None and obj.typing is not None:
            for state in states_map.get(obj.entity_id, ()):
                if state.last_updated < start or state.last_updated > end:
                    continue
                if total_rows >= MAX_HISTORY_ROWS:
                    truncated = True
                    break
                values.append(state_to_vqt(state, obj.typing))
                total_rows += 1
        results.append(
            item_ok("elementId", element_id, {"isComposition": False, "values": values})
        )
    return results, truncated


def _blocking_history(
    hass: HomeAssistant, start: datetime, end: datetime, entity_ids: list[str]
) -> dict[str, list]:
    """Run inside the recorder executor."""
    return history.get_significant_states(
        hass,
        start,
        end,
        entity_ids,
        include_start_time_state=False,
        significant_changes_only=True,
        minimal_response=False,
        no_attributes=False,
    )
