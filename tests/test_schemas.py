"""Tests for entity classification and type-schema generation."""

from __future__ import annotations

from custom_components.i3x.server.schemas import (
    KIND_BOOLEAN,
    KIND_NUMERIC,
    KIND_STRING,
    KIND_STRUCTURED,
    STRUCTURED_DOMAINS,
    classify_entity,
    display_name_for_type,
    relationship_types,
    schema_for,
    source_type_id_for,
)


def test_structured_domain_classification() -> None:
    typing = classify_entity("light", None, None, "on")
    assert typing.kind == KIND_STRUCTURED
    assert typing.type_id == "type:light"
    assert typing.structured is STRUCTURED_DOMAINS["light"]


def test_boolean_domains() -> None:
    assert classify_entity("switch", None, None, "off").kind == KIND_BOOLEAN
    typing = classify_entity("binary_sensor", "motion", None, "on")
    assert typing.type_id == "type:binary_sensor.motion"
    assert typing.kind == KIND_BOOLEAN


def test_sensor_numeric_with_unit() -> None:
    typing = classify_entity("sensor", "temperature", "°F", "72.5")
    assert typing.kind == KIND_NUMERIC
    assert typing.type_id == "type:sensor.temperature.degf"


def test_sensor_numeric_without_unit() -> None:
    typing = classify_entity("sensor", None, None, "42")
    assert typing.kind == KIND_NUMERIC


def test_sensor_enum_is_string() -> None:
    typing = classify_entity("sensor", "enum", None, "idle")
    assert typing.kind == KIND_STRING
    assert typing.type_id == "type:sensor.text"


def test_fallback_domain_boolean_state() -> None:
    typing = classify_entity("valve", None, None, "open")
    assert typing.kind == KIND_BOOLEAN


def test_fallback_domain_text_state() -> None:
    typing = classify_entity("sun", None, None, "above_horizon")
    assert typing.kind == KIND_STRING
    assert typing.type_id == "type:sun.text"


def test_leaf_schemas() -> None:
    assert schema_for(classify_entity("switch", None, None, "on")) == {
        "type": "boolean"
    }
    assert schema_for(classify_entity("sensor", None, "W", "5")) == {"type": "number"}
    assert schema_for(classify_entity("sensor", "enum", None, "x")) == {
        "type": "string"
    }


def test_structured_schema_covers_descriptor() -> None:
    for domain, desc in STRUCTURED_DOMAINS.items():
        schema = schema_for(classify_entity(domain, None, None, None))
        assert schema["type"] == "object"
        assert schema["required"] == ["state"]
        props = schema["properties"]
        assert set(props) == {"state"} | {a.name for a in desc.attributes}
        for attr in desc.attributes:
            assert props[attr.name] == {"type": [attr.json_type, "null"]}


def test_type_metadata_helpers() -> None:
    assert display_name_for_type("type:sensor.temperature.f") == "Sensor Temperature F"
    assert source_type_id_for("type:light") == "homeassistant:light"


def test_relationship_types_symmetric() -> None:
    rels = {r["elementId"]: r for r in relationship_types()}
    assert rels["HasParent"]["reverseOf"] == "HasChildren"
    assert rels["HasChildren"]["reverseOf"] == "HasParent"
    for rel in rels.values():
        assert rels[rel["reverseOf"]]["reverseOf"] == rel["elementId"]
