"""Tests for state → VQT conversion, including schema/value drift guards."""

from __future__ import annotations

import re
from datetime import UTC, datetime

from homeassistant.core import State

from custom_components.i3x.server.schemas import (
    STRUCTURED_DOMAINS,
    classify_entity,
    schema_for,
)
from custom_components.i3x.server.values import iso_z, no_data_vqt, state_to_vqt

# The conformance suite's UTC timestamp rule.
UTC_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")


def test_iso_z_format() -> None:
    stamp = iso_z(datetime(2026, 7, 24, 5, 30, 12, 345678, tzinfo=UTC))
    assert UTC_TS_RE.match(stamp)
    assert stamp.endswith("Z")
    assert "+" not in stamp


def _vqt(entity_id: str, state: str, attrs: dict | None = None) -> dict:
    ha_state = State(entity_id, state, attrs or {})
    domain = entity_id.split(".")[0]
    typing = classify_entity(
        domain,
        (attrs or {}).get("device_class"),
        (attrs or {}).get("unit_of_measurement"),
        state,
    )
    return state_to_vqt(ha_state, typing)


def test_boolean_states() -> None:
    assert _vqt("switch.x", "on")["value"] is True
    assert _vqt("switch.x", "off")["value"] is False
    assert _vqt("switch.x", "on")["quality"] == "Good"


def test_numeric_state() -> None:
    vqt = _vqt("sensor.t", "21.5", {"unit_of_measurement": "°C"})
    assert vqt["value"] == 21.5
    assert vqt["quality"] == "Good"


def test_unavailable_pairs_null_with_bad() -> None:
    vqt = _vqt("sensor.t", "unavailable", {"unit_of_measurement": "°C"})
    assert vqt["value"] is None
    assert vqt["quality"] == "Bad"


def test_unknown_pairs_null_with_goodnodata() -> None:
    vqt = _vqt("sensor.t", "unknown", {"unit_of_measurement": "°C"})
    assert vqt["value"] is None
    assert vqt["quality"] == "GoodNoData"


def test_numeric_typed_entity_with_text_state() -> None:
    # A numeric-typed entity that produces garbage must degrade to
    # GoodNoData/null rather than violate its declared schema.
    ha_state = State("sensor.t", "borked", {"unit_of_measurement": "°C"})
    typing = classify_entity("sensor", None, "°C", "21.5")
    vqt = state_to_vqt(ha_state, typing)
    assert vqt["value"] is None
    assert vqt["quality"] == "GoodNoData"


def test_structured_light_value() -> None:
    vqt = _vqt(
        "light.x",
        "on",
        {"brightness": 128, "color_temp_kelvin": 3000, "color_mode": "color_temp"},
    )
    value = vqt["value"]
    assert value["state"] is True
    assert value["brightness"] == 128.0
    assert value["color_temp_kelvin"] == 3000.0
    assert value["color_mode"] == "color_temp"
    assert value["effect"] is None


def test_no_data_vqt() -> None:
    vqt = no_data_vqt()
    assert vqt["value"] is None
    assert vqt["quality"] == "GoodNoData"
    assert UTC_TS_RE.match(vqt["timestamp"])


def _validates(schema: dict, value) -> bool:
    """Minimal JSON-Schema check mirroring the conformance validator subset."""
    if value is None:
        return True  # null handling is covered by quality pairing rules
    types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]

    def type_ok(t: str, v) -> bool:
        if t == "null":
            return v is None
        if t == "boolean":
            return isinstance(v, bool)
        if t == "number":
            return isinstance(v, (int, float)) and not isinstance(v, bool)
        if t == "string":
            return isinstance(v, str)
        if t == "object":
            return isinstance(v, dict)
        return False

    if not any(type_ok(t, value) for t in types):
        return False
    if isinstance(value, dict) and "properties" in schema:
        for key, sub in schema["properties"].items():
            if key in value and not _validates(sub, value[key]):
                return False
        for key in schema.get("required", []):
            if key not in value:
                return False
    return True


def test_structured_values_conform_to_their_schemas() -> None:
    """Drift guard: every structured domain's value validates against its schema."""
    sample_attrs = {
        "brightness": 200,
        "color_temp_kelvin": 2700,
        "color_mode": "color_temp",
        "effect": "rainbow",
        "current_temperature": 21.0,
        "temperature": 22.5,
        "target_temp_high": 24,
        "target_temp_low": 18,
        "current_humidity": 55,
        "hvac_action": "heating",
        "preset_mode": "eco",
        "fan_mode": "auto",
        "current_position": 70,
        "current_tilt_position": 10,
        "percentage": 66,
        "oscillating": True,
        "direction": "forward",
        "volume_level": 0.4,
        "is_volume_muted": False,
        "source": "Spotify",
        "media_title": "Song",
        "media_artist": "Artist",
        "app_name": "Music",
        "humidity": 45,
        "pressure": 1013.2,
        "wind_speed": 12.3,
        "wind_bearing": 200,
        "cloud_coverage": 40,
        "uv_index": 5,
        "battery_level": 88,
        "fan_speed": "high",
        "mode": "auto",
        "operation_mode": "heat_pump",
        "changed_by": "Ryan",
    }
    domain_states = {
        "light": "on",
        "climate": "heat",
        "cover": "open",
        "fan": "on",
        "media_player": "playing",
        "weather": "sunny",
        "vacuum": "cleaning",
        "humidifier": "on",
        "water_heater": "heat_pump",
        "alarm_control_panel": "armed_home",
    }
    for domain in STRUCTURED_DOMAINS:
        typing = classify_entity(domain, None, None, domain_states[domain])
        schema = schema_for(typing)
        state = State(f"{domain}.sample", domain_states[domain], sample_attrs)
        vqt = state_to_vqt(state, typing)
        assert vqt["quality"] == "Good"
        assert _validates(schema, vqt["value"]), (domain, vqt["value"], schema)
