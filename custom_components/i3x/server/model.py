"""Address-space model: Home Assistant registries → i3X objects and types.

Builds a cached snapshot of the exposed address space:

    home (root) → areas → devices → entities

Every object is reachable from the single root, parentId always resolves, and
/objects/related synthesizes HasParent/HasChildren edges in both directions
from the snapshot's indexes (HA registries only store the "up" pointer).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.helpers.entityfilter import generate_filter

from ..const import (
    AREA_PREFIX,
    CONF_EXCLUDE_ENTITY_GLOBS,
    CONF_INCLUDE_DOMAINS,
    CONF_INCLUDE_ENTITY_GLOBS,
    DEVICE_PREFIX,
    NAMESPACE_NAME,
    NAMESPACE_URI,
    REL_HAS_CHILDREN,
    REL_HAS_PARENT,
    ROOT_ELEMENT_ID,
    TYPE_PREFIX,
)
from .schemas import (
    STRUCTURAL_TYPES,
    EntityTyping,
    classify_entity,
    display_name_for_type,
    object_type_response,
    relationship_types,
    schema_for,
    source_type_id_for,
)

SNAPSHOT_MAX_AGE = 30.0  # seconds; also invalidated by registry events


@dataclass
class I3xObject:
    """One object instance in the exposed address space."""

    element_id: str
    display_name: str
    type_id: str
    parent_id: str | None
    entity_id: str | None = None  # set only for entity-backed objects
    typing: EntityTyping | None = None  # set only for entity-backed objects
    description: str | None = None


class AddressSpace:
    """Immutable snapshot of the exposed address space."""

    def __init__(
        self,
        objects: dict[str, I3xObject],
        types: dict[str, dict],
        children: dict[str, list[str]],
    ) -> None:
        self.objects = objects
        self.types = types
        self.children = children
        self.relationship_types = {r["elementId"]: r for r in relationship_types()}

    # ------------------------------------------------------------- responses
    def object_response(self, obj: I3xObject, include_metadata: bool) -> dict:
        resp: dict = {
            "elementId": obj.element_id,
            "displayName": obj.display_name,
            "typeElementId": obj.type_id,
            "parentId": obj.parent_id,
            "isComposition": False,
            "isExtended": False,
        }
        if include_metadata:
            type_rec = self.types.get(obj.type_id)
            relationships: dict[str, list[str]] = {}
            if obj.parent_id is not None:
                relationships[REL_HAS_PARENT] = [obj.parent_id]
            child_ids = self.children.get(obj.element_id)
            if child_ids:
                relationships[REL_HAS_CHILDREN] = list(child_ids)
            resp["metadata"] = {
                "description": obj.description,
                "typeNamespaceUri": NAMESPACE_URI,
                "sourceTypeId": type_rec["sourceTypeId"] if type_rec else "UnknownType",
                "relationships": relationships,
            }
        return resp

    def related(
        self, obj: I3xObject, relationship_type: str | None, include_metadata: bool
    ) -> list[dict]:
        """All relationship edges of an object, both directions."""
        edges: list[dict] = []
        if relationship_type in (None, REL_HAS_PARENT) and obj.parent_id is not None:
            parent = self.objects.get(obj.parent_id)
            if parent:
                edges.append(
                    {
                        "sourceRelationship": REL_HAS_PARENT,
                        "object": self.object_response(parent, include_metadata),
                    }
                )
        if relationship_type in (None, REL_HAS_CHILDREN):
            for child_id in self.children.get(obj.element_id, ()):
                child = self.objects.get(child_id)
                if child:
                    edges.append(
                        {
                            "sourceRelationship": REL_HAS_CHILDREN,
                            "object": self.object_response(child, include_metadata),
                        }
                    )
        return edges


class I3xModel:
    """Owns the cached address-space snapshot and its invalidation."""

    def __init__(self, hass: HomeAssistant, options: dict) -> None:
        self._hass = hass
        self._snapshot: AddressSpace | None = None
        self._built_at = 0.0
        self._unsubs: list = []
        self.apply_options(options)

    def apply_options(self, options: dict) -> None:
        self._filter = generate_filter(
            include_domains=options.get(CONF_INCLUDE_DOMAINS, []),
            include_entities=[],
            exclude_domains=[],
            exclude_entities=[],
            include_entity_globs=options.get(CONF_INCLUDE_ENTITY_GLOBS, []),
            exclude_entity_globs=options.get(CONF_EXCLUDE_ENTITY_GLOBS, []),
        )
        self.invalidate()

    def entity_exposed(self, entity_id: str) -> bool:
        return self._filter(entity_id)

    @callback
    def async_start(self) -> None:
        for event_type in (
            er.EVENT_ENTITY_REGISTRY_UPDATED,
            dr.EVENT_DEVICE_REGISTRY_UPDATED,
            ar.EVENT_AREA_REGISTRY_UPDATED,
        ):
            self._unsubs.append(
                self._hass.bus.async_listen(event_type, self._async_registry_updated)
            )

    @callback
    def async_stop(self) -> None:
        while self._unsubs:
            self._unsubs.pop()()

    @callback
    def _async_registry_updated(self, _event: Event) -> None:
        self.invalidate()

    def invalidate(self) -> None:
        self._snapshot = None

    def snapshot(self) -> AddressSpace:
        now = time.monotonic()
        if self._snapshot is None or now - self._built_at > SNAPSHOT_MAX_AGE:
            self._snapshot = self._build()
            self._built_at = now
        return self._snapshot

    # ----------------------------------------------------------------- build
    def _build(self) -> AddressSpace:
        hass = self._hass
        ent_reg = er.async_get(hass)
        dev_reg = dr.async_get(hass)
        area_reg = ar.async_get(hass)

        objects: dict[str, I3xObject] = {}
        children: dict[str, list[str]] = {}
        types: dict[str, dict] = {}

        for type_id, spec in STRUCTURAL_TYPES.items():
            types[type_id] = object_type_response(
                type_id, spec["schema"], spec["displayName"], spec["sourceTypeId"]
            )

        def add(obj: I3xObject) -> None:
            objects[obj.element_id] = obj
            if obj.parent_id is not None:
                children.setdefault(obj.parent_id, []).append(obj.element_id)

        add(
            I3xObject(
                element_id=ROOT_ELEMENT_ID,
                display_name=hass.config.location_name or "Home",
                type_id=f"{TYPE_PREFIX}home",
                parent_id=None,
                description="Home Assistant instance root",
            )
        )

        area_ids: set[str] = set()
        for area in area_reg.async_list_areas():
            area_ids.add(area.id)
            add(
                I3xObject(
                    element_id=f"{AREA_PREFIX}{area.id}",
                    display_name=area.name,
                    type_id=f"{TYPE_PREFIX}area",
                    parent_id=ROOT_ELEMENT_ID,
                    description="Home Assistant area",
                )
            )

        # Entities first pass: which devices are actually referenced.
        states = [s for s in hass.states.async_all() if self._filter(s.entity_id)]
        used_device_ids: set[str] = set()
        entity_entries: dict[str, er.RegistryEntry] = {}
        for state in states:
            entry = ent_reg.async_get(state.entity_id)
            if entry is not None:
                entity_entries[state.entity_id] = entry
                if entry.device_id:
                    used_device_ids.add(entry.device_id)

        for device_id in used_device_ids:
            device = dev_reg.async_get(device_id)
            if device is None:
                continue
            parent = (
                f"{AREA_PREFIX}{device.area_id}"
                if device.area_id in area_ids
                else ROOT_ELEMENT_ID
            )
            label = device.name_by_user or device.name or device_id
            desc_bits = [b for b in (device.manufacturer, device.model) if b]
            add(
                I3xObject(
                    element_id=f"{DEVICE_PREFIX}{device_id}",
                    display_name=label,
                    type_id=f"{TYPE_PREFIX}device",
                    parent_id=parent,
                    description=" ".join(desc_bits) or "Home Assistant device",
                )
            )

        for state in states:
            entry = entity_entries.get(state.entity_id)
            parent = ROOT_ELEMENT_ID
            if entry is not None and entry.device_id in used_device_ids:
                parent = f"{DEVICE_PREFIX}{entry.device_id}"
            else:
                area_id = entry.area_id if entry is not None else None
                if area_id in area_ids:
                    parent = f"{AREA_PREFIX}{area_id}"
            domain = state.entity_id.split(".", 1)[0]
            typing = classify_entity(
                domain,
                state.attributes.get("device_class"),
                state.attributes.get("unit_of_measurement"),
                state.state,
            )
            if typing.type_id not in types:
                types[typing.type_id] = object_type_response(
                    typing.type_id,
                    schema_for(typing),
                    display_name_for_type(typing.type_id),
                    source_type_id_for(typing.type_id),
                )
            add(
                I3xObject(
                    element_id=state.entity_id,
                    display_name=state.attributes.get("friendly_name")
                    or state.entity_id,
                    type_id=typing.type_id,
                    parent_id=parent,
                    entity_id=state.entity_id,
                    typing=typing,
                    description=f"Home Assistant entity {state.entity_id}",
                )
            )

        return AddressSpace(objects, types, children)


def namespaces() -> list[dict]:
    """The single namespace all generated types live in."""
    return [{"uri": NAMESPACE_URI, "displayName": NAMESPACE_NAME}]
