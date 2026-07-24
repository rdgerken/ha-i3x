# i3X for Home Assistant

Expose Home Assistant as a **[CESMII i3X 1.0](https://github.com/cesmii/i3X)** server — the open, vendor-agnostic API for contextualized information interoperability. Any i3X client (the i3X Explorer, the official Python client, the i3X MCP server, AI applications, historians) can browse your home as a typed, hierarchical address space and read live values, history, and subscriptions — using the same API it would use against an industrial manufacturing platform.

**Conformance: `1.0 Compatible`** — all 39 MUST-level tests of the official [i3X Conformance Test Suite](https://github.com/cesmii/i3X/tree/1.0/conformance-tests) pass (verdict from `i3x-test` v1.0.0; optional write/SSE features are on the roadmap toward Full 1.0 Compliance).

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
- **Honest capabilities** — `GET /info` declares exactly what works; undeclared optional endpoints return 501.

## Security

The security boundary is Home Assistant's own bearer-token auth: every endpoint except the spec-mandated `GET /info` requires a valid HA access token, and a token holder gains nothing beyond what HA's native API already grants. On top of that:

- **`local_only` (default on)** — all i3X endpoints, `/info` included, refuse requests from non-private addresses (evaluated after HA's `trusted_proxies` handling).
- `/info` is **rate limited** per IP for non-local clients and discloses no identifying details (generic server name, no HA version).
- Resource caps throughout: bulk requests ≤ 500 ids, ≤ 20 subscriptions per client / 100 total, ≤ 500 monitored objects each, bounded queues, history row limits, recorder reads off the event loop.
- Writes are not implemented in this version (`update.current: false`); when they land they will default off behind an entity allowlist.

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

## Verifying conformance yourself

```bash
git clone --branch 1.0 https://github.com/cesmii/i3X
cd i3X/conformance-tests
node bin/i3x-test.js run http://<ha-host>:8123/api/i3x/v1 --token <long-lived-token> --no-writes
```

## Known limitations

- History depth equals your recorder purge window (default 10 days). Long-term statistics as an i3X history source is on the roadmap.
- No composition objects yet (`isComposition` is always `false`; `maxDepth` is accepted and trivially satisfied).
- Subscriptions are in-memory: an HA restart drops them (spec-legal — clients recreate on 404).
- Browser-based clients (i3X Explorer) need their origin added to HA's `http: cors_allowed_origins`.

## Roadmap

- [ ] SSE streaming (`POST /subscriptions/stream`) — HA's event bus is push-native
- [ ] Writes (`PUT /objects/value`) mapped to service calls, default-off behind an entity allowlist
- [ ] Target: **Full 1.0 Compliance** verdict
- [ ] i3X **client** half: consume external i3X servers into HA entities and long-term statistics
- [ ] Long-term statistics as a deep-history source

## Disclaimer

Not affiliated with CESMII. i3X™ is a trademark of CESMII, the Smart Manufacturing Institute. MIT licensed; use at your own risk.
