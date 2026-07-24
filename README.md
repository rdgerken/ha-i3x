# i3X for Home Assistant

Expose Home Assistant as a **[CESMII i3X 1.0](https://github.com/cesmii/i3X)** server — the open, vendor-agnostic API for contextualized information interoperability. Any i3X client (the i3X Explorer, the official Python client, the i3X MCP server, AI applications, historians) can browse your home as a typed, hierarchical address space and read live values, history, and subscriptions — using the same API it would use against an industrial manufacturing platform.

**Conformance: `Full 1.0 Compliance`** 🎉 — every MUST *and* every declared optional feature of the official [i3X Conformance Test Suite](https://github.com/cesmii/i3X/tree/1.0/conformance-tests) passes, with a rich structured type system (verdict from `i3x-test` v1.0.0, re-verified in CI on every push).

```
┌─────────────────────────────┐          ┌──────────────────────────────┐
│        Home Assistant       │          │         i3X clients          │
│                             │          │                              │
│  areas ─ devices ─ entities │──────────▶  i3X Explorer, Python SDK,   │
│  recorder history           │ /api/i3x │  MCP server, AI apps, ...    │
│  state-change events        │   /v1    │                              │
└─────────────────────────────┘          └──────────────────────────────┘
```

## What it does

- **Model browsing** — a single-rooted hierarchy (`home` → areas → devices → entities) served through `GET /objects`, `POST /objects/list`, and `POST /objects/related` with bidirectional `HasParent`/`HasChildren` edges.
- **Rich object types** — JSON-Schema types generated per domain/device-class/unit: structured schemas for `light`, `climate`, `cover`, `media_player`, `weather`, and more; scalar leaf types for sensors (`type:sensor.temperature.degc` → `{"type": "number"}`).
- **Current values** — `POST /objects/value` returns VQT records (value/quality/timestamp) mapped from live HA states; `unavailable` → `Bad`, `unknown` → `GoodNoData`, per the spec's null-pairing rules.
- **History** — `POST /objects/history` serves the recorder's data as VQT arrays (row-capped with honest HTTP 206 on truncation).
- **Subscriptions** — create/register/sync with monotonic sequence numbers, ack semantics (`lastSequenceNumber`, `-1` clears), poll-style snapshot capture, queue-overflow 206s, and mandatory idle-TTL expiry. Fed live from HA's event bus.
- **SSE streaming** — `POST /subscriptions/stream` pushes state changes as they happen (single stream per subscription with clean takeover, keep-alive heartbeats for proxies).
- **Writes** — `PUT /objects/value` maps to Home Assistant service calls per domain (switch/light/number/select/cover/climate/…). Two tiers: *idempotent echo writes* (writing a value that equals the current one) always succeed as no-ops; *value-changing* writes require the write toggle plus a per-entity allowlist. `PUT /objects/history` accepts idempotent replacements only — HA's recorder is append-only.

## Security

The security boundary is Home Assistant's own bearer-token auth: every endpoint except the spec-mandated `GET /info` requires a valid HA access token, and a token holder gains nothing beyond what HA's native API already grants. On top of that:

- **`local_only` (default on)** — all i3X endpoints, `/info` included, refuse requests from non-private addresses (evaluated after HA's `trusted_proxies` handling).
- `/info` is **rate limited** per IP for non-local clients and discloses no identifying details (generic server name, no HA version).
- Resource caps throughout: bulk requests ≤ 500 ids, ≤ 20 subscriptions per client / 100 total, ≤ 500 monitored objects each, ≤ 10 concurrent SSE streams, bounded queues, history row limits, recorder reads off the event loop.
- **Value-changing writes are off by default.** Out of the box, the only writes that succeed are idempotent echoes — writing a value identical to the current one, which changes nothing (this is also exactly what the conformance suite's non-destructive write tests do). Turning anything on requires enabling writes *and* adding the entity to the write allowlist in the options. **Locks are never writable**, and history writes can never invent or alter data points.

Exposing the API beyond your LAN is a deliberate opt-out of `local_only` — prefer VPN/Tailscale reach over a public route, and note that SSO forward-auth proxies generally can't sit in front of headless API clients.

## Installation

1. **HACS** (custom repository): HACS → Integrations → ⋮ → Custom repositories → add `https://github.com/rdgerken/ha-i3x` as an Integration — or copy `custom_components/i3x/` into your config's `custom_components/`.
2. Restart Home Assistant.
3. Settings → Devices & Services → Add Integration → **i3X**.
4. Create a long-lived access token (your profile → Security) for i3X clients.

Your server is now at `http://<ha-host>:8123/api/i3x/v1` — check `GET /info`.

```bash
curl http://homeassistant.local:8123/api/i3x/v1/info
curl -H "Authorization: Bearer <token>" \
     -X POST http://homeassistant.local:8123/api/i3x/v1/objects/value \
     -H "Content-Type: application/json" \
     -d '{"elementIds": ["sensor.living_room_temperature"]}'
```

### Options

| Option | Default | Meaning |
| --- | --- | --- |
| Server name | `Home Assistant i3X` | Shown on the unauthenticated `/info` endpoint |
| Local only | on | Refuse non-private client addresses on every endpoint |
| Include domains / include globs / exclude globs | all | Which entities are exposed (uses HA's entity-filter syntax) |
| Subscription TTL | 600 s | Idle time before a subscription is expired |
| Allow value-changing writes | off | Master switch for writes that actually change entity state |
| Writable entity globs | empty | Allowlist for value-changing writes (e.g. `switch.*`, `input_number.pool_*`) |

## Verifying conformance yourself

```bash
git clone --branch 1.0 https://github.com/cesmii/i3X
cd i3X/conformance-tests
node bin/i3x-test.js run http://<ha-host>:8123/api/i3x/v1 --token <long-lived-token>
```

The write tests are non-destructive (they echo current values back), so they are safe to run against a live home even with value-changing writes disabled. Note the "rich type system" part of the verdict requires at least one structured-domain entity (a light, climate, cover, …) to be exposed — an address space of only scalar sensors is honestly reported as an immature type system.

## Known limitations

- History depth equals your recorder purge window (default 10 days). Long-term statistics as an i3X history source is on the roadmap.
- History *writes* are idempotent replacements only — HA's recorder is append-only, so novel or altered past points are refused per-item.
- No composition objects yet (`isComposition` is always `false`; `maxDepth` is accepted and trivially satisfied).
- Subscriptions are in-memory: an HA restart drops them (spec-legal — clients recreate on 404).
- Browser-based clients (i3X Explorer) need their origin added to HA's `http: cors_allowed_origins`.

## Roadmap

- [x] SSE streaming (`POST /subscriptions/stream`) — v0.2
- [x] Writes mapped to service calls, default-off behind an entity allowlist — v0.2
- [x] **Full 1.0 Compliance** verdict — v0.2
- [ ] i3X **client** half: consume external i3X servers into HA entities and long-term statistics
- [ ] Long-term statistics as a deep-history source

## Disclaimer

Not affiliated with CESMII. i3X™ is a trademark of CESMII, the Smart Manufacturing Institute. MIT licensed; use at your own risk.
