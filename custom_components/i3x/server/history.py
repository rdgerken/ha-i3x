"""Recorder- and statistics-backed history for POST /objects/history.

Two data sources, merged per entity:

- The recorder supplies full-fidelity states inside its purge window.
- For spans older than the recorder's earliest point, numeric entities are
  backfilled from long-term statistics (hourly rows, kept forever), so
  history requests reach years back instead of the ~10-day purge window.

Compositions (devices) honor maxDepth: depth > 1 (or 0 = infinite) adds a
``components`` map with each component entity's history.

All recorder access runs in the recorder executor. Rows are capped at
MAX_HISTORY_ROWS per request; a truncated response is returned as HTTP 206
with a responseDetail (spec: server-imposed limits must never be silent).
"""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.recorder import get_instance, history
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from ..const import MAX_HISTORY_ROWS, QUALITY_GOOD
from .http_util import item_not_found, item_ok
from .model import AddressSpace, I3xObject
from .schemas import KIND_NUMERIC, KIND_STRUCTURED
from .values import iso_z, state_to_vqt


async def fetch_history(
    hass: HomeAssistant,
    snapshot: AddressSpace,
    element_ids: list[str],
    start: datetime,
    end: datetime,
    max_depth: int = 1,
) -> tuple[list[dict], bool]:
    """Return (bulk results aligned with element_ids, truncated flag)."""
    # Which compositions expand, and every entity we need data for.
    components: dict[str, list[I3xObject]] = {}
    involved: dict[str, I3xObject] = {}
    for eid in element_ids:
        obj = snapshot.objects.get(eid)
        if obj is None:
            continue
        if obj.entity_id is not None:
            involved[obj.entity_id] = obj
        if obj.is_composition and max_depth != 1:
            children = snapshot.component_children(obj)
            components[eid] = children
            for child in children:
                if child.entity_id is not None:
                    involved[child.entity_id] = child

    # Entities whose value includes attributes need attribute-only changes:
    # HA's significant-changes filter drops them for every domain outside
    # recorder SIGNIFICANT_DOMAINS, which would silently lose (for example)
    # every brightness change on a light that stayed on. For scalar entities
    # the state IS the value, so attribute-only rows are pure duplicates and
    # the filter stays on.
    entity_ids = list(involved)
    structured_ids = [
        eid
        for eid, obj in involved.items()
        if obj.typing is not None and obj.typing.kind == KIND_STRUCTURED
    ]
    structured_set = set(structured_ids)
    scalar_ids = [eid for eid in entity_ids if eid not in structured_set]

    states_map: dict[str, list] = {}
    recorder = get_instance(hass)
    for ids, significant_only in ((scalar_ids, True), (structured_ids, False)):
        if not ids:
            continue
        states_map.update(
            await recorder.async_add_executor_job(
                _blocking_history, hass, start, end, ids, significant_only
            )
        )

    # Long-term statistics backfill for numeric entities whose recorder data
    # starts after the requested window does.
    earliest: dict[str, datetime | None] = {}
    for entity_id in entity_ids:
        states = [
            s
            for s in states_map.get(entity_id, ())
            if start <= s.last_updated <= end
        ]
        states_map[entity_id] = states
        earliest[entity_id] = states[0].last_updated if states else None

    lts_candidates = [
        entity_id
        for entity_id, obj in involved.items()
        if obj.typing is not None
        and obj.typing.kind == KIND_NUMERIC
        and (earliest[entity_id] is None or start < earliest[entity_id])
    ]
    stats_map: dict[str, list[dict]] = {}
    if lts_candidates:
        stats_map = await get_instance(hass).async_add_executor_job(
            _blocking_statistics, hass, start, end, lts_candidates
        )

    counter = _RowCounter()

    def entity_values(obj: I3xObject) -> list[dict]:
        values: list[dict] = []
        cutoff = earliest[obj.entity_id]
        for row in stats_map.get(obj.entity_id, ()):
            value = row.get("mean")
            if value is None:
                value = row.get("state")
            if value is None:
                continue
            when = dt_util.utc_from_timestamp(row["start"])
            if when < start or when > end:
                continue
            if cutoff is not None and when >= cutoff:
                continue  # the recorder covers this span at full fidelity
            if not counter.take():
                return values
            values.append(
                {
                    "value": float(value),
                    "quality": QUALITY_GOOD,
                    "timestamp": iso_z(when),
                }
            )
        for state in states_map.get(obj.entity_id, ()):
            if not counter.take():
                return values
            values.append(state_to_vqt(state, obj.typing))
        return values

    results: list[dict] = []
    for element_id in element_ids:
        obj = snapshot.objects.get(element_id)
        if obj is None:
            results.append(item_not_found("elementId", element_id))
            continue
        values: list[dict] = []
        if obj.entity_id is not None and obj.typing is not None:
            values = entity_values(obj)
        result: dict = {"isComposition": obj.is_composition, "values": values}
        if element_id in components:
            result["components"] = {
                child.element_id: {
                    "values": (
                        entity_values(child)
                        if child.entity_id is not None and child.typing is not None
                        else []
                    )
                }
                for child in components[element_id]
            }
        results.append(item_ok("elementId", element_id, result))
    return results, counter.exhausted


class _RowCounter:
    """Shared row budget across every entity in one request."""

    def __init__(self) -> None:
        self.remaining = MAX_HISTORY_ROWS
        self.exhausted = False

    def take(self) -> bool:
        if self.remaining <= 0:
            self.exhausted = True
            return False
        self.remaining -= 1
        return True


def _blocking_history(
    hass: HomeAssistant,
    start: datetime,
    end: datetime,
    entity_ids: list[str],
    significant_changes_only: bool = True,
) -> dict[str, list]:
    """Run inside the recorder executor."""
    return history.get_significant_states(
        hass,
        start,
        end,
        entity_ids,
        include_start_time_state=False,
        significant_changes_only=significant_changes_only,
        minimal_response=False,
        no_attributes=False,
    )


def _blocking_statistics(
    hass: HomeAssistant, start: datetime, end: datetime, statistic_ids: list[str]
) -> dict[str, list[dict]]:
    """Run inside the recorder executor."""
    return statistics_during_period(
        hass,
        start,
        end,
        set(statistic_ids),
        "hour",
        None,
        {"mean", "state"},
    )
