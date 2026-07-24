"""Import remote i3X object history into HA long-term statistics.

Periodically reads VQT history for configured elementIds, aggregates completed
hours, and inserts them via async_add_external_statistics with `i3x:<slug>`
statistic IDs.

Two kinds:
- Measurement elementIds -> hourly mean/min/max.
- Counter elementIds     -> hourly state + cumulative sum, meter-reset aware,
  resuming from the last stored row so re-imports never double-count.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .api import I3xApiClient, I3xError
from .const import (
    DEFAULT_IMPORT_INTERVAL_MINUTES,
    DOMAIN,
    IMPORT_MAX_LOOKBACK_HOURS,
    QUALITY_GOOD,
)

_LOGGER = logging.getLogger(__name__)

# StatisticMeanType landed in HA 2025.4 (has_mean deprecated after).
try:  # pragma: no cover - version shim
    from homeassistant.components.recorder.models import StatisticMeanType

    _HAS_MEAN_TYPE = True
except ImportError:  # pragma: no cover
    StatisticMeanType = None  # type: ignore[assignment]
    _HAS_MEAN_TYPE = False


def element_to_statistic_id(element_id: str) -> str:
    """Convert an i3X elementId to a valid external statistic ID."""
    slug = re.sub(r"[^a-z0-9_]", "_", element_id.lower())
    return f"{DOMAIN}:{slug}"


def accumulate_counter(
    readings: dict[datetime, float],
    prev_state: float | None,
    prev_sum: float,
) -> list[dict[str, Any]]:
    """Turn hourly meter readings into state+sum statistics rows.

    Meter-reset aware: a reading lower than the previous one counts its full
    value as the delta (consumption since the reset). Pure function so the
    math is unit-testable.
    """
    stats: list[dict[str, Any]] = []
    for bucket_start in sorted(readings):
        state = readings[bucket_start]
        if prev_state is None:
            delta = 0.0  # first ever import: establish the baseline
        elif state < prev_state:
            delta = state  # meter reset: count from zero
        else:
            delta = state - prev_state
        prev_sum += delta
        prev_state = state
        stats.append({"start": bucket_start, "state": state, "sum": prev_sum})
    return stats


def _floor_hour(when: datetime) -> datetime:
    return when.replace(minute=0, second=0, microsecond=0)


class I3xStatisticsImporter:
    """Pulls hourly aggregates from a remote i3X server into HA statistics."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: I3xApiClient,
        measurement_ids: list[str],
        counter_ids: list[str],
    ) -> None:
        self._hass = hass
        self._api = api
        # An elementId listed in both is treated as a counter.
        self._counter_ids = list(dict.fromkeys(counter_ids))
        self._measurement_ids = [
            e for e in dict.fromkeys(measurement_ids) if e not in self._counter_ids
        ]
        self._unsub = None

        # Diagnostics
        self.last_run: datetime | None = None
        self.last_error: str | None = None
        self.hours_imported = 0

    def async_start(self) -> None:
        self._unsub = async_track_time_interval(
            self._hass,
            self._async_import_tick,
            timedelta(minutes=DEFAULT_IMPORT_INTERVAL_MINUTES),
        )
        # Kick off an initial import shortly after startup.
        self._hass.async_create_task(self.async_import())

    def async_stop(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None

    async def _async_import_tick(self, _now: Any = None) -> None:
        await self.async_import()

    async def async_import(self) -> None:
        """Import completed hours for every configured elementId."""
        if not self._measurement_ids and not self._counter_ids:
            return
        self.last_run = dt_util.utcnow()
        try:
            for element_id in self._measurement_ids:
                await self._async_import_one(element_id, is_counter=False)
            for element_id in self._counter_ids:
                await self._async_import_one(element_id, is_counter=True)
            self.last_error = None
        except I3xError as err:
            self.last_error = str(err)
            _LOGGER.warning("i3X statistics import failed: %s", err)

    # --- Shared helpers -----------------------------------------------------

    async def _async_read_points(
        self, element_id: str, start: datetime, end: datetime
    ) -> list[tuple[datetime, float]]:
        """Read Good-quality numeric history as time-sorted pairs."""

        def _iso(dt: datetime) -> str:
            return (
                dt_util.as_utc(dt).isoformat(timespec="seconds").replace("+00:00", "Z")
            )

        results = await self._api.async_get_history(
            [element_id], _iso(start), _iso(end)
        )
        points: list[tuple[datetime, float]] = []
        for item in results:
            if not item.get("success") or not item.get("result"):
                continue
            for vqt in item["result"].get("values", []):
                if vqt.get("quality") != QUALITY_GOOD:
                    continue
                raw = vqt.get("value")
                if isinstance(raw, bool):
                    raw = 1.0 if raw else 0.0
                try:
                    value = float(raw)
                except (TypeError, ValueError):
                    continue
                when = dt_util.parse_datetime(str(vqt.get("timestamp")))
                if when is None:
                    continue
                when = dt_util.as_utc(when)
                if start <= when < end:
                    points.append((when, value))
        points.sort(key=lambda p: p[0])
        return points

    async def _async_get_last_stat(
        self, statistic_id: str, types: set[str]
    ) -> dict[str, Any] | None:
        last = await get_instance(self._hass).async_add_executor_job(
            get_last_statistics, self._hass, 1, statistic_id, False, types
        )
        rows = last.get(statistic_id) or []
        return rows[0] if rows else None

    @staticmethod
    def _row_start(row: dict[str, Any]) -> datetime | None:
        raw = row.get("start")
        if raw is None:
            return None
        if isinstance(raw, datetime):
            return dt_util.as_utc(raw)
        return dt_util.utc_from_timestamp(float(raw))

    def _metadata(self, element_id: str, *, is_counter: bool) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "source": DOMAIN,
            "statistic_id": element_to_statistic_id(element_id),
            "name": f"i3X {element_id}",
            "unit_of_measurement": None,
            "unit_class": None,
            "has_sum": is_counter,
        }
        if _HAS_MEAN_TYPE:
            metadata["mean_type"] = (
                StatisticMeanType.NONE if is_counter else StatisticMeanType.ARITHMETIC
            )
        else:
            metadata["has_mean"] = not is_counter
        return metadata

    def _resume_window(
        self, last_row: dict[str, Any] | None
    ) -> tuple[datetime, datetime]:
        window_end = _floor_hour(dt_util.utcnow())
        if last_row is not None and (last_start := self._row_start(last_row)):
            start = last_start + timedelta(hours=1)
        else:
            start = window_end - timedelta(hours=IMPORT_MAX_LOOKBACK_HOURS)
        return start, window_end

    async def _async_import_one(self, element_id: str, *, is_counter: bool) -> None:
        statistic_id = element_to_statistic_id(element_id)
        types = {"start", "state", "sum"} if is_counter else {"start"}
        last_row = await self._async_get_last_stat(statistic_id, types)
        start, window_end = self._resume_window(last_row)
        if start >= window_end:
            return
        points = await self._async_read_points(element_id, start, window_end)
        if not points:
            return

        if is_counter:
            readings: dict[datetime, float] = {}
            for when, value in points:
                readings[_floor_hour(when)] = value
            prev_state: float | None = None
            prev_sum = 0.0
            if last_row is not None:
                if last_row.get("state") is not None:
                    prev_state = float(last_row["state"])
                if last_row.get("sum") is not None:
                    prev_sum = float(last_row["sum"])
            stats = accumulate_counter(readings, prev_state, prev_sum)
        else:
            buckets: dict[datetime, list[float]] = defaultdict(list)
            for when, value in points:
                buckets[_floor_hour(when)].append(value)
            stats = [
                {
                    "start": bucket_start,
                    "mean": sum(vals) / len(vals),
                    "min": min(vals),
                    "max": max(vals),
                }
                for bucket_start, vals in sorted(buckets.items())
            ]

        async_add_external_statistics(
            self._hass, self._metadata(element_id, is_counter=is_counter), stats
        )
        self.hours_imported += len(stats)
        _LOGGER.debug("Imported %d buckets for %s", len(stats), statistic_id)

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "measurement_ids": self._measurement_ids,
            "counter_ids": self._counter_ids,
            "last_run": self.last_run.isoformat() if self.last_run else None,
            "hours_imported": self.hours_imported,
            "last_error": self.last_error,
        }
