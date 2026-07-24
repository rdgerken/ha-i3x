"""HTTP tests for /info and the exploratory endpoints."""

from __future__ import annotations

BOGUS = "i3x-test-nonexistent-7f3a9c"


async def test_info_unauthenticated(world, hass, hass_client_no_auth) -> None:
    client = await hass_client_no_auth()
    resp = await client.get("/api/i3x/v1/info")
    assert resp.status == 200
    body = await resp.json()
    assert body["success"] is True
    info = body["result"]
    assert info["specVersion"] == "1.0"
    assert info["serverName"] == "Home Assistant i3X"
    caps = info["capabilities"]
    assert caps["query"] == {"history": True}
    assert caps["update"] == {"current": False, "history": False}
    assert caps["subscribe"] == {"stream": False}


async def test_data_endpoints_require_auth(world, hass, hass_client_no_auth) -> None:
    client = await hass_client_no_auth()
    resp = await client.get("/api/i3x/v1/namespaces")
    assert resp.status == 401


async def test_gzip_negotiated(world, hass, hass_client) -> None:
    client = await hass_client()
    resp = await client.get(
        "/api/i3x/v1/namespaces", headers={"Accept-Encoding": "gzip"}
    )
    assert resp.status == 200
    assert resp.headers.get("Content-Encoding") == "gzip"
    body = await resp.json()
    assert body["success"] is True


async def test_namespaces(world, hass, hass_client) -> None:
    client = await hass_client()
    body = await (await client.get("/api/i3x/v1/namespaces")).json()
    assert len(body["result"]) == 1
    ns = body["result"][0]
    assert ns["uri"].startswith("https://")
    assert ns["displayName"]


async def test_objecttypes_and_query(world, hass, hass_client) -> None:
    client = await hass_client()
    body = await (await client.get("/api/i3x/v1/objecttypes")).json()
    types = body["result"]
    assert types, "expected generated object types"
    for rec in types:
        for key in ("elementId", "displayName", "namespaceUri", "sourceTypeId"):
            assert isinstance(rec[key], str) and rec[key]
        assert isinstance(rec["schema"], dict)

    ids = [t["elementId"] for t in types[:3]][::-1] + [BOGUS]
    resp = await client.post("/api/i3x/v1/objecttypes/query", json={"elementIds": ids})
    body = await resp.json()
    assert body["success"] is False  # one item failed
    assert [r.get("elementId") for r in body["results"]] == ids
    assert body["results"][-1]["success"] is False
    assert body["results"][-1]["responseDetail"]["status"] == 404
    for item in body["results"][:-1]:
        assert item["success"] is True


async def test_relationshiptypes(world, hass, hass_client) -> None:
    client = await hass_client()
    body = await (await client.get("/api/i3x/v1/relationshiptypes")).json()
    rels = {r["elementId"]: r for r in body["result"]}
    assert set(rels) == {"HasParent", "HasChildren"}
    for rel in rels.values():
        assert rels[rel["reverseOf"]]["reverseOf"] == rel["elementId"]
        assert rel["relationshipId"]

    resp = await client.post(
        "/api/i3x/v1/relationshiptypes/query",
        json={"elementIds": ["HasParent", BOGUS]},
    )
    body = await resp.json()
    assert body["results"][0]["success"] is True
    assert body["results"][1]["responseDetail"]["status"] == 404


async def test_objects_listing_and_filters(world, hass, hass_client) -> None:
    client = await hass_client()
    body = await (await client.get("/api/i3x/v1/objects")).json()
    objects = {o["elementId"]: o for o in body["result"]}
    assert "home" in objects
    assert world["sensor"] in objects
    for obj in objects.values():
        assert isinstance(obj["isComposition"], bool)
        assert obj["typeElementId"]

    roots = (await (await client.get("/api/i3x/v1/objects?root=true")).json())["result"]
    assert [o["elementId"] for o in roots] == ["home"]
    assert roots[0]["parentId"] is None

    type_id = objects[world["sensor"]]["typeElementId"]
    filtered = (
        await (
            await client.get(f"/api/i3x/v1/objects?typeElementId={type_id}")
        ).json()
    )["result"]
    assert filtered
    assert all(o["typeElementId"] == type_id for o in filtered)

    with_meta = (
        await (await client.get("/api/i3x/v1/objects?includeMetadata=true")).json()
    )["result"]
    for obj in with_meta:
        meta = obj["metadata"]
        assert isinstance(meta["typeNamespaceUri"], str)
        assert isinstance(meta["sourceTypeId"], str)


async def test_objects_list_bulk(world, hass, hass_client) -> None:
    client = await hass_client()
    ids = [world["sensor"], "home", BOGUS]
    resp = await client.post("/api/i3x/v1/objects/list", json={"elementIds": ids})
    body = await resp.json()
    assert [r["elementId"] for r in body["results"]] == ids
    assert body["results"][2]["success"] is False
    assert body["results"][2]["responseDetail"]["status"] == 404


async def test_related_bidirectional_and_filter(world, hass, hass_client) -> None:
    client = await hass_client()
    device_element = f"device:{world['device_id']}"

    resp = await client.post(
        "/api/i3x/v1/objects/related", json={"elementIds": [world["sensor"]]}
    )
    edges = (await resp.json())["results"][0]["result"]
    assert any(
        e["sourceRelationship"] == "HasParent"
        and e["object"]["elementId"] == device_element
        for e in edges
    )

    # Reverse direction from the parent.
    resp = await client.post(
        "/api/i3x/v1/objects/related", json={"elementIds": [device_element]}
    )
    edges = (await resp.json())["results"][0]["result"]
    assert any(
        e["sourceRelationship"] == "HasChildren"
        and e["object"]["elementId"] == world["sensor"]
        for e in edges
    )

    # relationshipType filter.
    resp = await client.post(
        "/api/i3x/v1/objects/related",
        json={"elementIds": [device_element], "relationshipType": "HasParent"},
    )
    edges = (await resp.json())["results"][0]["result"]
    assert edges
    assert all(e["sourceRelationship"] == "HasParent" for e in edges)

    # Unknown element → per-item 404.
    resp = await client.post(
        "/api/i3x/v1/objects/related", json={"elementIds": [BOGUS]}
    )
    body = await resp.json()
    assert body["results"][0]["success"] is False


async def test_unconfigured_server_returns_503(
    world, hass, hass_client, server_entry
) -> None:
    client = await hass_client()
    await hass.config_entries.async_unload(server_entry.entry_id)
    await hass.async_block_till_done()
    resp = await client.get("/api/i3x/v1/namespaces")
    assert resp.status == 503
    body = await resp.json()
    assert body["success"] is False
    assert body["responseDetail"]["status"] == 503
