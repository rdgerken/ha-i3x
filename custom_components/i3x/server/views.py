"""HomeAssistantView subclasses implementing the i3X 1.0 endpoints.

Views are registered once per HA runtime (registration is permanent) and
resolve the live server engine through hass.data on every request, so config
entry reloads need no re-registration. Every response — including every error
— goes through HomeAssistantView.json() so the i3X envelope and gzip
negotiation apply uniformly.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from ..const import API_BASE, DOMAIN, SSE_HEARTBEAT_SECONDS
from .http_util import (
    ProblemError,
    bulk_body,
    check_local_only,
    item_not_found,
    item_ok,
    parse_json_body,
    problem_body,
    require_id_list,
    require_updates_list,
)
from .model import namespaces
from .values import no_data_vqt, state_to_vqt

_LOGGER = logging.getLogger(__name__)


class _I3xBaseView(HomeAssistantView):
    """Shared plumbing: engine lookup, guards, problem handling."""

    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    def _engine(self):
        engine = self.hass.data.get(DOMAIN, {}).get("server")
        if engine is None:
            raise ProblemError(
                503, "Service Unavailable", "The i3X server is not configured"
            )
        return engine

    async def _run(self, request: web.Request, handler) -> web.Response:
        try:
            engine = self._engine()
            check_local_only(request, engine.local_only)
            return await handler(engine, request)
        except ProblemError as err:
            return self.json(
                problem_body(err.status, err.title, err.detail),
                status_code=err.status,
            )
        except Exception:  # noqa: BLE001 - never leak a raw 500 page
            _LOGGER.exception("Unhandled error in i3X endpoint %s", request.path)
            return self.json(
                problem_body(500, "Internal Server Error", "Unexpected server error"),
                status_code=500,
            )

    @staticmethod
    def _client_id(body: dict) -> str:
        client_id = body.get("clientId")
        if not isinstance(client_id, str) or not client_id:
            raise ProblemError(400, "Bad Request", "clientId is required")
        return client_id

    @staticmethod
    def _not_implemented(detail: str) -> ProblemError:
        return ProblemError(501, "Not Implemented", detail)


class I3xInfoView(_I3xBaseView):
    """GET /info — capabilities and health. MUST NOT require auth."""

    url = f"{API_BASE}/info"
    name = "api:i3x:info"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        async def handler(engine, request):
            engine.info_limiter.check(request.remote)
            return self.json({"success": True, "result": engine.info_payload()})

        return await self._run(request, handler)


class I3xNamespacesView(_I3xBaseView):
    url = f"{API_BASE}/namespaces"
    name = "api:i3x:namespaces"

    async def get(self, request: web.Request) -> web.Response:
        async def handler(engine, request):
            return self.json({"success": True, "result": namespaces()})

        return await self._run(request, handler)


class I3xObjectTypesView(_I3xBaseView):
    url = f"{API_BASE}/objecttypes"
    name = "api:i3x:objecttypes"

    async def get(self, request: web.Request) -> web.Response:
        async def handler(engine, request):
            ns_filter = request.query.get("namespaceUri")
            types = list(engine.model.snapshot().types.values())
            if ns_filter:
                types = [t for t in types if t["namespaceUri"] == ns_filter]
            return self.json({"success": True, "result": types})

        return await self._run(request, handler)


class I3xObjectTypesQueryView(_I3xBaseView):
    url = f"{API_BASE}/objecttypes/query"
    name = "api:i3x:objecttypes:query"

    async def post(self, request: web.Request) -> web.Response:
        async def handler(engine, request):
            body = await parse_json_body(request)
            ids = require_id_list(body)
            types = engine.model.snapshot().types
            results = [
                item_ok("elementId", tid, types[tid])
                if tid in types
                else item_not_found("elementId", tid, "Object type")
                for tid in ids
            ]
            return self.json(bulk_body(results))

        return await self._run(request, handler)


class I3xRelationshipTypesView(_I3xBaseView):
    url = f"{API_BASE}/relationshiptypes"
    name = "api:i3x:relationshiptypes"

    async def get(self, request: web.Request) -> web.Response:
        async def handler(engine, request):
            ns_filter = request.query.get("namespaceUri")
            rels = list(engine.model.snapshot().relationship_types.values())
            if ns_filter:
                rels = [r for r in rels if r["namespaceUri"] == ns_filter]
            return self.json({"success": True, "result": rels})

        return await self._run(request, handler)


class I3xRelationshipTypesQueryView(_I3xBaseView):
    url = f"{API_BASE}/relationshiptypes/query"
    name = "api:i3x:relationshiptypes:query"

    async def post(self, request: web.Request) -> web.Response:
        async def handler(engine, request):
            body = await parse_json_body(request)
            ids = require_id_list(body)
            rels = engine.model.snapshot().relationship_types
            results = [
                item_ok("elementId", rid, rels[rid])
                if rid in rels
                else item_not_found("elementId", rid, "Relationship type")
                for rid in ids
            ]
            return self.json(bulk_body(results))

        return await self._run(request, handler)


class I3xObjectsView(_I3xBaseView):
    url = f"{API_BASE}/objects"
    name = "api:i3x:objects"

    async def get(self, request: web.Request) -> web.Response:
        async def handler(engine, request):
            snapshot = engine.model.snapshot()
            include_metadata = request.query.get("includeMetadata") == "true"
            type_filter = request.query.get("typeElementId")
            root_only = request.query.get("root") == "true"
            objs = snapshot.objects.values()
            if type_filter:
                objs = [o for o in objs if o.type_id == type_filter]
            if root_only:
                objs = [o for o in objs if o.parent_id is None]
            return self.json(
                {
                    "success": True,
                    "result": [
                        snapshot.object_response(o, include_metadata) for o in objs
                    ],
                }
            )

        return await self._run(request, handler)


class I3xObjectsListView(_I3xBaseView):
    url = f"{API_BASE}/objects/list"
    name = "api:i3x:objects:list"

    async def post(self, request: web.Request) -> web.Response:
        async def handler(engine, request):
            body = await parse_json_body(request)
            ids = require_id_list(body)
            include_metadata = body.get("includeMetadata") is True
            snapshot = engine.model.snapshot()
            results = []
            for eid in ids:
                obj = snapshot.objects.get(eid)
                if obj is None:
                    results.append(item_not_found("elementId", eid))
                else:
                    results.append(
                        item_ok(
                            "elementId",
                            eid,
                            snapshot.object_response(obj, include_metadata),
                        )
                    )
            return self.json(bulk_body(results))

        return await self._run(request, handler)


class I3xObjectsRelatedView(_I3xBaseView):
    url = f"{API_BASE}/objects/related"
    name = "api:i3x:objects:related"

    async def post(self, request: web.Request) -> web.Response:
        async def handler(engine, request):
            body = await parse_json_body(request)
            ids = require_id_list(body)
            include_metadata = body.get("includeMetadata") is True
            relationship_type = body.get("relationshipType")
            snapshot = engine.model.snapshot()
            results = []
            for eid in ids:
                obj = snapshot.objects.get(eid)
                if obj is None:
                    results.append(item_not_found("elementId", eid))
                else:
                    results.append(
                        item_ok(
                            "elementId",
                            eid,
                            snapshot.related(obj, relationship_type, include_metadata),
                        )
                    )
            return self.json(bulk_body(results))

        return await self._run(request, handler)


class I3xObjectsValueView(_I3xBaseView):
    url = f"{API_BASE}/objects/value"
    name = "api:i3x:objects:value"

    async def post(self, request: web.Request) -> web.Response:
        async def handler(engine, request):
            body = await parse_json_body(request)
            ids = require_id_list(body)
            max_depth = _parse_max_depth(body)
            snapshot = engine.model.snapshot()
            results = []
            for eid in ids:
                obj = snapshot.objects.get(eid)
                if obj is None:
                    results.append(item_not_found("elementId", eid))
                    continue
                result = {
                    "isComposition": obj.is_composition,
                    **(await self._object_vqt_fresh(engine, obj)),
                }
                # maxDepth recurses through HasComponent only; 1 = no
                # recursion (default), 0 = infinite.
                if obj.is_composition and max_depth != 1:
                    result["components"] = {
                        child.element_id: self._object_vqt(engine, child)
                        for child in snapshot.component_children(obj)
                    }
                results.append(item_ok("elementId", eid, result))
            return self.json(bulk_body(results))

        return await self._run(request, handler)

    async def _object_vqt_fresh(self, engine, obj) -> dict:
        """VQT for a directly-requested object; fetches todo items live."""
        if obj.entity_id is not None and obj.typing is not None:
            state = self.hass.states.get(obj.entity_id)
            if state is not None:
                if obj.typing.service_key:
                    payload = await engine.service_cache.async_get(
                        obj.entity_id, state
                    )
                    return state_to_vqt(state, obj.typing, service_data=payload)
                return state_to_vqt(state, obj.typing)
        return no_data_vqt()

    def _object_vqt(self, engine, obj) -> dict:
        """Synchronous VQT (component children); todo items from cache."""
        if obj.entity_id is not None and obj.typing is not None:
            state = self.hass.states.get(obj.entity_id)
            if state is not None:
                if obj.typing.service_key:
                    return state_to_vqt(
                        state,
                        obj.typing,
                        service_data=engine.service_cache.peek(obj.entity_id),
                    )
                return state_to_vqt(state, obj.typing)
        return no_data_vqt()

    async def put(self, request: web.Request) -> web.Response:
        async def handler(engine, request):
            from .writes import write_values

            body = await parse_json_body(request)
            updates = require_updates_list(body)
            results = await write_values(
                self.hass,
                engine.model.snapshot(),
                updates,
                engine.write_enabled,
                engine.write_filter,
                engine.service_cache,
            )
            return self.json(bulk_body(results))

        return await self._run(request, handler)


class I3xObjectsHistoryView(_I3xBaseView):
    url = f"{API_BASE}/objects/history"
    name = "api:i3x:objects:history"

    async def post(self, request: web.Request) -> web.Response:
        async def handler(engine, request):
            from .history import fetch_history

            body = await parse_json_body(request)
            ids = require_id_list(body)
            start = _parse_time(body.get("startTime"), "startTime")
            end = _parse_time(body.get("endTime"), "endTime")
            if start > end:
                start, end = end, start
            snapshot = engine.model.snapshot()
            results, truncated = await fetch_history(
                self.hass, snapshot, ids, start, end, _parse_max_depth(body)
            )
            payload: dict[str, Any] = bulk_body(results)
            if truncated:
                payload["responseDetail"] = {
                    "title": "Partial Content",
                    "status": 206,
                    "detail": (
                        "History was truncated at the server's row limit; "
                        "narrow the time range or request fewer elements"
                    ),
                }
                return self.json(payload, status_code=206)
            return self.json(payload)

        return await self._run(request, handler)

    async def put(self, request: web.Request) -> web.Response:
        async def handler(engine, request):
            from .writes import write_history

            body = await parse_json_body(request)
            updates = require_updates_list(body)
            results = await write_history(
                self.hass, engine.model.snapshot(), updates
            )
            return self.json(bulk_body(results))

        return await self._run(request, handler)


def _parse_max_depth(body: dict) -> int:
    raw = body.get("maxDepth", 1)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        return 1
    return raw


def _parse_time(raw: Any, field: str):
    if not isinstance(raw, str) or not raw:
        raise ProblemError(400, "Bad Request", f"{field} is required")
    parsed = dt_util.parse_datetime(raw)
    if parsed is None:
        raise ProblemError(
            400, "Bad Request", f"{field} must be a valid RFC 3339 timestamp"
        )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_util.UTC)
    return parsed


class I3xSubscriptionsView(_I3xBaseView):
    url = f"{API_BASE}/subscriptions"
    name = "api:i3x:subscriptions"

    async def post(self, request: web.Request) -> web.Response:
        async def handler(engine, request):
            body = await parse_json_body(request)
            client_id = self._client_id(body)
            display_name = body.get("displayName")
            result = engine.subscriptions.create(client_id, display_name)
            return self.json({"success": True, "result": result})

        return await self._run(request, handler)


class I3xSubscriptionsListView(_I3xBaseView):
    url = f"{API_BASE}/subscriptions/list"
    name = "api:i3x:subscriptions:list"

    async def post(self, request: web.Request) -> web.Response:
        async def handler(engine, request):
            body = await parse_json_body(request)
            client_id = self._client_id(body)
            sub_ids = require_id_list(body, "subscriptionIds")
            return self.json(
                bulk_body(engine.subscriptions.list_items(client_id, sub_ids))
            )

        return await self._run(request, handler)


class I3xSubscriptionsDeleteView(_I3xBaseView):
    url = f"{API_BASE}/subscriptions/delete"
    name = "api:i3x:subscriptions:delete"

    async def post(self, request: web.Request) -> web.Response:
        async def handler(engine, request):
            body = await parse_json_body(request)
            client_id = self._client_id(body)
            sub_ids = require_id_list(body, "subscriptionIds")
            return self.json(
                bulk_body(engine.subscriptions.delete_items(client_id, sub_ids))
            )

        return await self._run(request, handler)


class I3xSubscriptionsRegisterView(_I3xBaseView):
    url = f"{API_BASE}/subscriptions/register"
    name = "api:i3x:subscriptions:register"

    async def post(self, request: web.Request) -> web.Response:
        async def handler(engine, request):
            body = await parse_json_body(request)
            client_id = self._client_id(body)
            sub_id = body.get("subscriptionId")
            if not isinstance(sub_id, str) or not sub_id:
                raise ProblemError(400, "Bad Request", "subscriptionId is required")
            ids = require_id_list(body)
            max_depth = body.get("maxDepth", 1)
            if not isinstance(max_depth, int) or isinstance(max_depth, bool):
                max_depth = 1
            return self.json(
                bulk_body(
                    engine.subscriptions.register(client_id, sub_id, ids, max_depth)
                )
            )

        return await self._run(request, handler)


class I3xSubscriptionsUnregisterView(_I3xBaseView):
    url = f"{API_BASE}/subscriptions/unregister"
    name = "api:i3x:subscriptions:unregister"

    async def post(self, request: web.Request) -> web.Response:
        async def handler(engine, request):
            body = await parse_json_body(request)
            client_id = self._client_id(body)
            sub_id = body.get("subscriptionId")
            if not isinstance(sub_id, str) or not sub_id:
                raise ProblemError(400, "Bad Request", "subscriptionId is required")
            ids = require_id_list(body)
            return self.json(
                bulk_body(engine.subscriptions.unregister(client_id, sub_id, ids))
            )

        return await self._run(request, handler)


class I3xSubscriptionsSyncView(_I3xBaseView):
    url = f"{API_BASE}/subscriptions/sync"
    name = "api:i3x:subscriptions:sync"

    async def post(self, request: web.Request) -> web.Response:
        async def handler(engine, request):
            body = await parse_json_body(request)
            client_id = self._client_id(body)
            sub_id = body.get("subscriptionId")
            if not isinstance(sub_id, str) or not sub_id:
                raise ProblemError(400, "Bad Request", "subscriptionId is required")
            batches, overflowed = engine.subscriptions.sync(
                client_id, sub_id, body.get("lastSequenceNumber")
            )
            payload: dict[str, Any] = {"success": True, "result": batches}
            if overflowed:
                payload["responseDetail"] = {
                    "title": "Partial Content",
                    "status": 206,
                    "detail": (
                        "The subscription queue overflowed and the oldest "
                        "updates were dropped; backfill via POST /objects/history"
                    ),
                }
                return self.json(payload, status_code=206)
            return self.json(payload)

        return await self._run(request, handler)


class I3xSubscriptionsStreamView(_I3xBaseView):
    url = f"{API_BASE}/subscriptions/stream"
    name = "api:i3x:subscriptions:stream"

    async def post(self, request: web.Request) -> web.StreamResponse:
        # Validation errors go through the normal problem path; once the
        # stream opens, the response is raw SSE (deliberately uncompressed).
        try:
            engine = self._engine()
            check_local_only(request, engine.local_only)
            body = await parse_json_body(request)
            client_id = self._client_id(body)
            sub_id = body.get("subscriptionId")
            if not isinstance(sub_id, str) or not sub_id:
                raise ProblemError(400, "Bad Request", "subscriptionId is required")
            sub, handle = engine.subscriptions.stream_open(client_id, sub_id)
        except ProblemError as err:
            return self.json(
                problem_body(err.status, err.title, err.detail),
                status_code=err.status,
            )

        response = web.StreamResponse()
        response.headers["Content-Type"] = "text/event-stream"
        response.headers["Cache-Control"] = "no-cache"
        response.headers["X-Accel-Buffering"] = "no"
        await response.prepare(request)
        try:
            while not handle.close_event.is_set():
                if request.transport is None or request.transport.is_closing():
                    break
                # At-most-once: drain and send everything queued right now.
                for batch in engine.subscriptions.drain(sub):
                    payload = json.dumps(batch["updates"])
                    await response.write(f"data: {payload}\n\n".encode())
                sub.touch()
                waiters = [
                    asyncio.ensure_future(handle.close_event.wait()),
                    asyncio.ensure_future(sub.data_event.wait()),
                ]
                done, pending = await asyncio.wait(
                    waiters,
                    timeout=SSE_HEARTBEAT_SECONDS,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                if not done:
                    await response.write(b": keep-alive\n\n")
            # Clean end-of-stream (takeover, deletion, or shutdown).
            await response.write_eof()
        except (ConnectionResetError, ConnectionError, OSError):
            pass  # client went away
        finally:
            engine.subscriptions.stream_close(sub_id, handle)
        return response


ALL_VIEWS = (
    I3xInfoView,
    I3xNamespacesView,
    I3xObjectTypesView,
    I3xObjectTypesQueryView,
    I3xRelationshipTypesView,
    I3xRelationshipTypesQueryView,
    I3xObjectsView,
    I3xObjectsListView,
    I3xObjectsRelatedView,
    I3xObjectsValueView,
    I3xObjectsHistoryView,
    I3xSubscriptionsView,
    I3xSubscriptionsListView,
    I3xSubscriptionsDeleteView,
    I3xSubscriptionsRegisterView,
    I3xSubscriptionsUnregisterView,
    I3xSubscriptionsSyncView,
    I3xSubscriptionsStreamView,
)


def register_views(hass: HomeAssistant) -> None:
    """Register every i3X view exactly once per HA runtime."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get("views_registered"):
        return
    for view_cls in ALL_VIEWS:
        hass.http.register_view(view_cls(hass))
    domain_data["views_registered"] = True
