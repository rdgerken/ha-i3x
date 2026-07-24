"""Tests for the address-space model (hierarchy, indexes, related edges)."""

from __future__ import annotations

from custom_components.i3x.const import DOMAIN


def _engine(hass):
    return hass.data[DOMAIN]["server"]


async def test_hierarchy(world, hass) -> None:
    snapshot = _engine(hass).model.snapshot()
    objects = snapshot.objects

    root = objects["home"]
    assert root.parent_id is None
    assert root.type_id == "type:home"

    area = objects[f"area:{world['area_id']}"]
    assert area.parent_id == "home"
    assert area.display_name == "Kitchen"

    device = objects[f"device:{world['device_id']}"]
    assert device.parent_id == f"area:{world['area_id']}"

    sensor = objects[world["sensor"]]
    assert sensor.parent_id == f"device:{world['device_id']}"
    assert sensor.type_id == "type:sensor.temperature.degc"

    # Loose entities hang off the root.
    assert objects[world["switch"]].parent_id == "home"

    # Every parentId resolves and every non-root object is reachable from root.
    for obj in objects.values():
        if obj.parent_id is not None:
            assert obj.parent_id in objects
    reachable: set[str] = set()
    stack = ["home"]
    while stack:
        current = stack.pop()
        reachable.add(current)
        stack.extend(snapshot.children.get(current, ()))
    assert reachable == set(objects)


async def test_types_registered_for_all_objects(world, hass) -> None:
    snapshot = _engine(hass).model.snapshot()
    for obj in snapshot.objects.values():
        assert obj.type_id in snapshot.types, obj.element_id
    for type_rec in snapshot.types.values():
        assert type_rec["sourceTypeId"]
        assert isinstance(type_rec["schema"], dict)


async def test_related_edges_bidirectional(world, hass) -> None:
    snapshot = _engine(hass).model.snapshot()
    sensor = snapshot.objects[world["sensor"]]
    device_id = f"device:{world['device_id']}"

    up = snapshot.related(sensor, None, False)
    assert any(
        e["sourceRelationship"] == "HasParent"
        and e["object"]["elementId"] == device_id
        for e in up
    )
    device = snapshot.objects[device_id]
    down = snapshot.related(device, "HasChildren", False)
    assert any(e["object"]["elementId"] == world["sensor"] for e in down)
    assert all(e["sourceRelationship"] == "HasChildren" for e in down)


async def test_entity_filter_hides_everywhere(
    recorder_mock, enable_custom_integrations, hass
) -> None:
    """Filtered entities are invisible in objects, values, and children."""
    # Separate setup with an exclusion option.
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from homeassistant.setup import async_setup_component

    from custom_components.i3x.const import CONF_MODE, MODE_SERVER

    assert await async_setup_component(hass, "http", {})
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_MODE: MODE_SERVER},
        options={"exclude_entity_globs": ["sensor.secret_*"]},
        unique_id="server",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    hass.states.async_set("sensor.secret_token", "abc")
    hass.states.async_set("sensor.public_temp", "20")
    snapshot = _engine(hass).model.snapshot()
    assert "sensor.secret_token" not in snapshot.objects
    assert "sensor.public_temp" in snapshot.objects
    for children in snapshot.children.values():
        assert "sensor.secret_token" not in children
