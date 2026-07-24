"""Subscription manager for the i3X server.

Event-driven: one EVENT_STATE_CHANGED listener fans updates out to per-
subscription queues with monotonic sequence numbers. Sync applies the ack
(lastSequenceNumber; -1 clears all) and — when the queue is empty — stages a
snapshot batch of the monitored objects' current values ("poll-style capture",
matching the spec's reference implementation) so pollers always converge.

Scoping: every call requires a clientId; a subscription accessed with the
wrong clientId is indistinguishable from a nonexistent one (404).
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import timedelta

from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval

from ..const import (
    MAX_MONITORED_PER_SUBSCRIPTION,
    MAX_QUEUED_BATCHES,
    MAX_SSE_STREAMS,
    MAX_SUBSCRIPTIONS_PER_CLIENT,
    MAX_SUBSCRIPTIONS_TOTAL,
    SUBSCRIPTION_JANITOR_INTERVAL,
)
from .http_util import ProblemError, item_not_found, item_ok, item_problem
from .schemas import KIND_TODO
from .values import no_data_vqt, state_to_vqt


class StreamHandle:
    """Identity + close signal for one active SSE stream."""

    def __init__(self) -> None:
        self.close_event = asyncio.Event()


@dataclass
class Subscription:
    """One client-scoped subscription with its update queue."""

    subscription_id: str
    client_id: str
    display_name: str | None
    monitored: dict[str, int] = field(default_factory=dict)  # elementId -> maxDepth
    queue: deque = field(default_factory=deque)
    next_seq: int = 1
    overflow: bool = False
    last_activity: float = field(default_factory=time.monotonic)
    stream: StreamHandle | None = None
    data_event: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def stream_active(self) -> bool:
        return self.stream is not None

    def touch(self) -> None:
        self.last_activity = time.monotonic()


class SubscriptionManager:
    """Owns all subscriptions and the shared state-changed listener."""

    def __init__(self, hass: HomeAssistant, engine) -> None:
        self._hass = hass
        self._engine = engine
        self._subs: dict[str, Subscription] = {}  # subscriptionId -> Subscription
        self._watchers: dict[str, set[str]] = {}  # elementId -> subscriptionIds
        self._unsubs: list = []

    # ------------------------------------------------------------- lifecycle
    @callback
    def async_start(self) -> None:
        self._unsubs.append(
            self._hass.bus.async_listen(EVENT_STATE_CHANGED, self._handle_state_changed)
        )
        self._unsubs.append(
            async_track_time_interval(
                self._hass,
                self._janitor,
                timedelta(seconds=SUBSCRIPTION_JANITOR_INTERVAL),
            )
        )

    @callback
    def async_stop(self) -> None:
        while self._unsubs:
            self._unsubs.pop()()
        self._subs.clear()
        self._watchers.clear()

    @callback
    def _janitor(self, _now) -> None:
        ttl = self._engine.subscription_ttl
        deadline = time.monotonic() - ttl
        for sub_id in [
            s.subscription_id
            for s in self._subs.values()
            if s.last_activity < deadline and not s.stream_active
        ]:
            self._drop(sub_id)

    def _drop(self, sub_id: str) -> None:
        sub = self._subs.pop(sub_id, None)
        if sub is None:
            return
        if sub.stream is not None:
            sub.stream.close_event.set()
            sub.stream = None
        for element_id in sub.monitored:
            watchers = self._watchers.get(element_id)
            if watchers:
                watchers.discard(sub_id)
                if not watchers:
                    del self._watchers[element_id]

    # ------------------------------------------------------------- eventing
    @callback
    def _handle_state_changed(self, event: Event) -> None:
        entity_id = event.data.get("entity_id")
        watchers = self._watchers.get(entity_id)
        if not watchers:
            return
        new_state = event.data.get("new_state")
        if new_state is None:
            return
        snapshot = self._engine.model.snapshot()
        obj = snapshot.objects.get(entity_id)
        if obj is None or obj.typing is None:
            return
        if obj.typing.kind == KIND_TODO:
            # Items live behind a service call: push the count with the
            # cached items now, then follow up once fresh items arrive.
            cached = self._engine.todo_cache.peek(entity_id)
            vqt = state_to_vqt(new_state, obj.typing, todo_items=cached)
            self._fan_out(entity_id, vqt)
            self._hass.async_create_task(
                self._async_todo_follow_up(entity_id, new_state, obj.typing, cached)
            )
            return
        self._fan_out(entity_id, state_to_vqt(new_state, obj.typing))

    def _fan_out(self, entity_id: str, vqt: dict) -> None:
        entry = {"elementId": entity_id, **vqt}
        for sub_id in list(self._watchers.get(entity_id, ())):
            sub = self._subs.get(sub_id)
            if sub is not None:
                self._enqueue(sub, [entry])

    async def _async_todo_follow_up(
        self, entity_id: str, state, typing, cached: list | None
    ) -> None:
        fresh = await self._engine.todo_cache.async_get(entity_id, state)
        if fresh != cached:
            self._fan_out(
                entity_id, state_to_vqt(state, typing, todo_items=fresh)
            )

    def _enqueue(self, sub: Subscription, updates: list[dict]) -> None:
        if len(sub.queue) >= MAX_QUEUED_BATCHES:
            sub.queue.popleft()
            sub.overflow = True
        sub.queue.append({"sequenceNumber": sub.next_seq, "updates": updates})
        sub.next_seq += 1
        sub.data_event.set()

    def _snapshot_batch(self, sub: Subscription) -> None:
        """Stage the monitored objects' current values as one batch."""
        if not sub.monitored:
            return
        snapshot = self._engine.model.snapshot()
        updates = []
        for element_id in sub.monitored:
            obj = snapshot.objects.get(element_id)
            if obj is None:
                continue
            if obj.entity_id is not None and obj.typing is not None:
                state = self._hass.states.get(obj.entity_id)
                if state is None:
                    continue
                if obj.typing.kind == KIND_TODO:
                    vqt = state_to_vqt(
                        state,
                        obj.typing,
                        todo_items=self._engine.todo_cache.peek(obj.entity_id),
                    )
                else:
                    vqt = state_to_vqt(state, obj.typing)
            else:
                vqt = no_data_vqt()
            updates.append({"elementId": element_id, **vqt})
        if updates:
            self._enqueue(sub, updates)

    # ------------------------------------------------------------- accessors
    def _owned(self, client_id: str, sub_id) -> Subscription:
        """Resolve a subscription for this client or raise 404."""
        sub = self._subs.get(sub_id) if isinstance(sub_id, str) else None
        if sub is None or sub.client_id != client_id:
            raise ProblemError(404, "Not Found", f"Subscription not found: {sub_id}")
        sub.touch()
        return sub

    # -------------------------------------------------------------- endpoints
    def create(self, client_id: str, display_name: str | None) -> dict:
        if len(self._subs) >= MAX_SUBSCRIPTIONS_TOTAL:
            raise ProblemError(
                400, "Bad Request", "Server subscription limit reached"
            )
        client_count = sum(
            1 for s in self._subs.values() if s.client_id == client_id
        )
        if client_count >= MAX_SUBSCRIPTIONS_PER_CLIENT:
            raise ProblemError(
                400, "Bad Request", "Per-client subscription limit reached"
            )
        sub = Subscription(
            subscription_id=secrets.token_urlsafe(24),
            client_id=client_id,
            display_name=display_name,
        )
        self._subs[sub.subscription_id] = sub
        return {
            "clientId": client_id,
            "subscriptionId": sub.subscription_id,
            "displayName": display_name,
        }

    def list_items(self, client_id: str, sub_ids: list[str]) -> list[dict]:
        results = []
        for sub_id in sub_ids:
            try:
                sub = self._owned(client_id, sub_id)
            except ProblemError:
                results.append(
                    item_not_found("subscriptionId", sub_id, "Subscription")
                )
                continue
            results.append(
                item_ok(
                    "subscriptionId",
                    sub_id,
                    {
                        "subscriptionId": sub_id,
                        "displayName": sub.display_name,
                        "monitoredObjects": [
                            {"elementId": eid, "maxDepth": depth}
                            for eid, depth in sub.monitored.items()
                        ],
                    },
                )
            )
        return results

    def delete_items(self, client_id: str, sub_ids: list[str]) -> list[dict]:
        results = []
        for sub_id in sub_ids:
            try:
                self._owned(client_id, sub_id)
            except ProblemError:
                results.append(
                    item_not_found("subscriptionId", sub_id, "Subscription")
                )
                continue
            self._drop(sub_id)
            results.append(item_ok("subscriptionId", sub_id, None))
        return results

    def register(
        self, client_id: str, sub_id: str, element_ids: list[str], max_depth: int
    ) -> list[dict]:
        sub = self._owned(client_id, sub_id)
        snapshot = self._engine.model.snapshot()
        results = []
        for element_id in element_ids:
            if element_id not in snapshot.objects:
                results.append(item_not_found("elementId", element_id))
                continue
            if (
                element_id not in sub.monitored
                and len(sub.monitored) >= MAX_MONITORED_PER_SUBSCRIPTION
            ):
                results.append(
                    item_problem(
                        "elementId",
                        element_id,
                        400,
                        "Bad Request",
                        "Monitored-object limit reached for this subscription",
                    )
                )
                continue
            sub.monitored[element_id] = max_depth
            watchers = self._watchers.setdefault(element_id, set())
            watchers.add(sub_id)
            results.append(item_ok("elementId", element_id, None))
        return results

    def unregister(
        self, client_id: str, sub_id: str, element_ids: list[str]
    ) -> list[dict]:
        sub = self._owned(client_id, sub_id)
        results = []
        for element_id in element_ids:
            if element_id not in sub.monitored:
                results.append(
                    item_problem(
                        "elementId",
                        element_id,
                        404,
                        "Not Found",
                        f"Element not registered: {element_id}",
                    )
                )
                continue
            del sub.monitored[element_id]
            watchers = self._watchers.get(element_id)
            if watchers:
                watchers.discard(sub_id)
                if not watchers:
                    del self._watchers[element_id]
            # Already-queued batches for this element are deliberately kept
            # (spec: unregistering stops new updates, it does not purge).
            results.append(item_ok("elementId", element_id, None))
        return results

    def sync(
        self, client_id: str, sub_id: str, last_sequence_number
    ) -> tuple[list[dict], bool]:
        """Apply the ack and return (batches, overflowed)."""
        sub = self._owned(client_id, sub_id)
        if sub.stream_active:
            raise ProblemError(
                400,
                "Bad Request",
                "Subscription has an open SSE stream; close it before calling sync",
            )
        if last_sequence_number == -1:
            sub.queue.clear()
        elif isinstance(last_sequence_number, int) and not isinstance(
            last_sequence_number, bool
        ) and last_sequence_number >= 0:
            while sub.queue and sub.queue[0]["sequenceNumber"] <= last_sequence_number:
                sub.queue.popleft()
        if not sub.queue:
            self._snapshot_batch(sub)
        overflowed = sub.overflow
        sub.overflow = False
        return list(sub.queue), overflowed

    # ------------------------------------------------------------- streaming
    def stream_open(self, client_id: str, sub_id) -> tuple[Subscription, StreamHandle]:
        """Validate scoping and take over the subscription's stream slot."""
        sub = self._owned(client_id, sub_id)
        active = sum(1 for s in self._subs.values() if s.stream is not None)
        if sub.stream is None and active >= MAX_SSE_STREAMS:
            raise ProblemError(
                503, "Service Unavailable", "Too many concurrent SSE streams"
            )
        if sub.stream is not None:
            # Single stream per subscription: the previous stream must end
            # cleanly when a new one opens (spec rule).
            sub.stream.close_event.set()
        handle = StreamHandle()
        sub.stream = handle
        return sub, handle

    def stream_close(self, sub_id: str, handle: StreamHandle) -> None:
        sub = self._subs.get(sub_id)
        if sub is not None and sub.stream is handle:
            sub.stream = None
            sub.touch()

    @staticmethod
    def drain(sub: Subscription) -> list[dict]:
        """Take all queued batches (at-most-once delivery for streaming)."""
        batches = list(sub.queue)
        sub.queue.clear()
        sub.overflow = False
        sub.data_event.clear()
        return batches

    @property
    def count(self) -> int:
        return len(self._subs)
