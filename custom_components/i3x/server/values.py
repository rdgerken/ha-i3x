"""State → VQT conversion for the i3X server.

Uses the same descriptors as schemas.py so every produced value conforms to
the JSON Schema its Object Type declares. Null values only ever pair with
quality Bad or GoodNoData (spec rule enforced by the conformance suite).
"""

from __future__ import annotations

from datetime import datetime

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import State
from homeassistant.util import dt as dt_util

from ..const import (
    BINARY_STATE_MAP,
    QUALITY_BAD,
    QUALITY_GOOD,
    QUALITY_GOOD_NO_DATA,
)
from .schemas import (
    KIND_BOOLEAN,
    KIND_NUMERIC,
    KIND_STRING,
    KIND_STRUCTURED,
    EntityTyping,
)


def iso_z(dt: datetime) -> str:
    """RFC 3339 UTC timestamp with a Z suffix (no timezone offset)."""
    return dt_util.as_utc(dt).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _coerce_attr(value, json_type: str):
    """Coerce an HA attribute to the declared JSON type, or None."""
    if value is None:
        return None
    if json_type == "boolean":
        return value if isinstance(value, bool) else None
    if json_type == "number":
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    if json_type == "string":
        if isinstance(value, str):
            return value
        if isinstance(value, (int, float, bool)):
            return str(value)
        return None
    return None


def state_to_value(
    state: State, typing: EntityTyping, service_data: list | None = None
):
    """Convert an HA state to (value, quality) per the entity's typing.

    The timestamp is handled separately (event time for live values,
    state.last_updated for reads). ``service_data`` carries the payload for
    service-backed domains (todo items, calendar events, weather forecasts)
    when the caller has it; None is honest for paths without service access,
    such as history.
    """
    raw = state.state
    if raw == STATE_UNAVAILABLE:
        return None, QUALITY_BAD
    if raw == STATE_UNKNOWN:
        return None, QUALITY_GOOD_NO_DATA

    if typing.kind == KIND_BOOLEAN:
        mapped = BINARY_STATE_MAP.get(raw)
        if mapped is None:
            return None, QUALITY_GOOD_NO_DATA
        return mapped, QUALITY_GOOD

    if typing.kind == KIND_NUMERIC:
        try:
            return float(raw), QUALITY_GOOD
        except (TypeError, ValueError):
            return None, QUALITY_GOOD_NO_DATA

    if typing.kind == KIND_STRING:
        return str(raw), QUALITY_GOOD

    if typing.kind == KIND_STRUCTURED:
        desc = typing.structured
        assert desc is not None
        if desc.state_type == "boolean":
            state_field = BINARY_STATE_MAP.get(raw)
        elif desc.state_type == "number":
            try:
                state_field = float(raw)
            except (TypeError, ValueError):
                state_field = None
        else:
            state_field = str(raw)
        value = {"state": state_field}
        for attr in desc.attributes:
            value[attr.name] = _coerce_attr(
                state.attributes.get(attr.name), attr.json_type
            )
        if typing.service_key:
            value[typing.service_key] = service_data
        return value, QUALITY_GOOD

    return None, QUALITY_GOOD_NO_DATA


def state_to_vqt(
    state: State, typing: EntityTyping, service_data: list | None = None
) -> dict:
    """Full VQT record for a current-value read."""
    value, quality = state_to_value(state, typing, service_data)
    return {
        "value": value,
        "quality": quality,
        "timestamp": iso_z(state.last_updated),
    }


def no_data_vqt(timestamp: datetime | None = None) -> dict:
    """VQT for objects without a live value (root, areas, devices)."""
    return {
        "value": None,
        "quality": QUALITY_GOOD_NO_DATA,
        "timestamp": iso_z(timestamp or dt_util.utcnow()),
    }
