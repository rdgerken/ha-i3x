"""Object Type generation for the i3X server.

A single per-domain descriptor table drives BOTH the JSON Schema published in
/objecttypes AND the value payloads built in values.py, so the two can never
drift apart (the conformance suite validates every returned value against its
declared type schema).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from homeassistant.util import slugify

from ..const import (
    NAMESPACE_URI,
    REL_COMPONENT_OF,
    REL_HAS_CHILDREN,
    REL_HAS_COMPONENT,
    REL_HAS_PARENT,
    TYPE_PREFIX,
)

# Value kinds for leaf (scalar) entities.
KIND_BOOLEAN = "boolean"
KIND_NUMERIC = "numeric"
KIND_STRING = "string"
KIND_STRUCTURED = "structured"

# Domains whose primary content lives behind a response-returning service
# call rather than in the state machine; the value object carries it under
# this key, populated from the server's ServiceDataCache.
SERVICE_KEYS = {"todo": "items", "calendar": "events", "weather": "forecast"}


@dataclass(frozen=True)
class AttrSpec:
    """One declared attribute of a structured domain type."""

    name: str
    json_type: str  # "number" | "string" | "boolean"


@dataclass(frozen=True)
class StructuredDescriptor:
    """Shape of a structured domain's value object."""

    state_type: str  # "boolean" | "string" | "number"
    attributes: tuple[AttrSpec, ...] = field(default_factory=tuple)


def _n(name: str) -> AttrSpec:
    return AttrSpec(name, "number")


def _s(name: str) -> AttrSpec:
    return AttrSpec(name, "string")


def _b(name: str) -> AttrSpec:
    return AttrSpec(name, "boolean")


# Domains that expose a structured value object (state + curated attributes).
STRUCTURED_DOMAINS: dict[str, StructuredDescriptor] = {
    "light": StructuredDescriptor(
        "boolean",
        (_n("brightness"), _n("color_temp_kelvin"), _s("color_mode"), _s("effect")),
    ),
    "climate": StructuredDescriptor(
        "string",
        (
            _n("current_temperature"),
            _n("temperature"),
            _n("target_temp_high"),
            _n("target_temp_low"),
            _n("current_humidity"),
            _s("hvac_action"),
            _s("preset_mode"),
            _s("fan_mode"),
        ),
    ),
    "cover": StructuredDescriptor(
        "string", (_n("current_position"), _n("current_tilt_position"))
    ),
    "fan": StructuredDescriptor(
        "boolean",
        (_n("percentage"), _s("preset_mode"), _b("oscillating"), _s("direction")),
    ),
    "media_player": StructuredDescriptor(
        "string",
        (
            _n("volume_level"),
            _b("is_volume_muted"),
            _s("source"),
            _s("media_title"),
            _s("media_artist"),
            _s("app_name"),
        ),
    ),
    "weather": StructuredDescriptor(
        "string",
        (
            _n("temperature"),
            _n("humidity"),
            _n("pressure"),
            _n("wind_speed"),
            _n("wind_bearing"),
            _n("cloud_coverage"),
            _n("uv_index"),
        ),
    ),
    "vacuum": StructuredDescriptor("string", (_n("battery_level"), _s("fan_speed"))),
    "humidifier": StructuredDescriptor(
        "boolean", (_n("humidity"), _n("current_humidity"), _s("mode"))
    ),
    "water_heater": StructuredDescriptor(
        "string", (_n("current_temperature"), _n("temperature"), _s("operation_mode"))
    ),
    "alarm_control_panel": StructuredDescriptor("string", (_s("changed_by"),)),
    # Service-payload domains (see SERVICE_KEYS): the state plus curated
    # attributes here, with items/events/forecast grafted on by the server.
    "todo": StructuredDescriptor("number"),
    "calendar": StructuredDescriptor(
        "boolean",
        (_s("message"), _s("start_time"), _s("end_time"), _b("all_day")),
    ),
}

# Domains whose state is a plain on/off style boolean.
BOOLEAN_DOMAINS = {
    "binary_sensor",
    "switch",
    "input_boolean",
    "siren",
    "remote",
    "automation",
    "script",
}

# Domains whose state is always numeric.
NUMERIC_DOMAINS = {"number", "input_number", "counter"}


@dataclass(frozen=True)
class EntityTyping:
    """Resolved i3X typing for one entity."""

    type_id: str
    kind: str  # KIND_*
    structured: StructuredDescriptor | None = None
    service_key: str | None = None  # value key filled from ServiceDataCache


def classify_entity(
    domain: str,
    device_class: str | None,
    unit: str | None,
    state_str: str | None,
) -> EntityTyping:
    """Map an HA entity to its i3X Object Type id and value kind."""
    if domain in STRUCTURED_DOMAINS:
        return EntityTyping(
            f"{TYPE_PREFIX}{domain}",
            KIND_STRUCTURED,
            STRUCTURED_DOMAINS[domain],
            service_key=SERVICE_KEYS.get(domain),
        )
    if domain in BOOLEAN_DOMAINS:
        suffix = f".{device_class}" if device_class else ""
        return EntityTyping(f"{TYPE_PREFIX}{domain}{suffix}", KIND_BOOLEAN)
    if domain in NUMERIC_DOMAINS:
        return EntityTyping(f"{TYPE_PREFIX}{domain}.numeric", KIND_NUMERIC)
    if domain == "sensor":
        if device_class == "enum":
            return EntityTyping(f"{TYPE_PREFIX}sensor.text", KIND_STRING)
        numeric = unit is not None
        if not numeric and state_str is not None:
            try:
                float(state_str)
                numeric = True
            except (TypeError, ValueError):
                numeric = False
        if numeric:
            parts = ["sensor", device_class or "numeric"]
            if unit:
                parts.append(slugify(unit) or "unitless")
            return EntityTyping(TYPE_PREFIX + ".".join(parts), KIND_NUMERIC)
        return EntityTyping(f"{TYPE_PREFIX}sensor.text", KIND_STRING)
    # Fallback heuristic for every other domain: boolean-looking states map to
    # booleans, otherwise the state is exposed as a string (always safe).
    from ..const import BINARY_STATE_MAP  # local import to avoid cycle at module load

    if state_str is not None and state_str in BINARY_STATE_MAP:
        return EntityTyping(f"{TYPE_PREFIX}{domain}", KIND_BOOLEAN)
    return EntityTyping(f"{TYPE_PREFIX}{domain}.text", KIND_STRING)


def _nullable(json_type: str) -> dict:
    return {"type": [json_type, "null"]}


def schema_for(typing: EntityTyping) -> dict:
    """JSON Schema for an entity typing — the single source shared with values.py."""
    if typing.kind == KIND_BOOLEAN:
        return {"type": "boolean"}
    if typing.kind == KIND_NUMERIC:
        return {"type": "number"}
    if typing.kind == KIND_STRING:
        return {"type": "string"}
    desc = typing.structured
    assert desc is not None
    props: dict[str, dict] = {"state": _nullable(desc.state_type)}
    for attr in desc.attributes:
        props[attr.name] = _nullable(attr.json_type)
    if typing.service_key:
        props[typing.service_key] = _nullable("array")
    return {"type": "object", "properties": props, "required": ["state"]}


def display_name_for_type(type_id: str) -> str:
    """Human-readable name for a generated type id."""
    body = type_id.removeprefix(TYPE_PREFIX)
    return body.replace(".", " ").replace("_", " ").title()


def source_type_id_for(type_id: str) -> str:
    """Provenance id: the HA-side concept this type was generated from."""
    return f"homeassistant:{type_id.removeprefix(TYPE_PREFIX)}"


# --- Structural types (root/area/device) and the UnknownType placeholder ---

STRUCTURAL_TYPES: dict[str, dict] = {
    f"{TYPE_PREFIX}home": {
        "displayName": "Home",
        "sourceTypeId": "homeassistant:home",
        "schema": {"type": "object"},
    },
    f"{TYPE_PREFIX}area": {
        "displayName": "Area",
        "sourceTypeId": "homeassistant:area",
        "schema": {"type": "object"},
    },
    f"{TYPE_PREFIX}device": {
        "displayName": "Device",
        "sourceTypeId": "homeassistant:device",
        "schema": {"type": "object"},
    },
    f"{TYPE_PREFIX}unknown": {
        "displayName": "Unknown Type",
        "sourceTypeId": "UnknownType",
        "schema": {"type": "object"},
    },
}


def object_type_response(type_id: str, schema: dict, display_name: str, source_type_id: str) -> dict:
    """Build one ObjectTypeResponse record."""
    return {
        "elementId": type_id,
        "displayName": display_name,
        "namespaceUri": NAMESPACE_URI,
        "sourceTypeId": source_type_id,
        "version": "1.0.0",
        "schema": schema,
    }


def relationship_types() -> list[dict]:
    """The registered relationship types (each with its registered reverse)."""

    def rel(element_id: str, reverse_of: str) -> dict:
        return {
            "elementId": element_id,
            "displayName": element_id,
            "namespaceUri": NAMESPACE_URI,
            "relationshipId": element_id,
            "reverseOf": reverse_of,
        }

    return [
        rel(REL_HAS_PARENT, REL_HAS_CHILDREN),
        rel(REL_HAS_CHILDREN, REL_HAS_PARENT),
        rel(REL_HAS_COMPONENT, REL_COMPONENT_OF),
        rel(REL_COMPONENT_OF, REL_HAS_COMPONENT),
    ]
