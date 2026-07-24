# i3X for Home Assistant

Home Assistant integration for [CESMII i3X 1.0](https://github.com/cesmii/i3X), the open, vendor-agnostic API for contextualized information interoperability. It works in both directions:

- **Server**: exposes Home Assistant as an i3X server. Any i3X client (the i3X Explorer, the official Python client, the i3X MCP server, AI applications, historians) can browse your home as a typed, hierarchical address space and read live values, history, and subscriptions, using the same API it would use against an industrial manufacturing platform.
- **Client**: connects Home Assistant to external i3X servers. Remote objects become sensor entities (live via SSE or sync polling), their history imports into long-term statistics, and the `i3x.write` service writes values back.

**Conformance: `Full 1.0 Compliance`.** Every MUST and every declared optional feature of the official [i3X Conformance Test Suite](https://github.com/cesmii/i3X/tree/1.0/conformance-tests) passes, with a rich structured type system (verdict from `i3x-test` v1.0.0, re-verified in CI on every push).

```
                                serve /api/i3x/v1
┌─────────────────────────────┐ ───────────────▶ ┌──────────────────────────────┐
│        Home Assistant       │                  │      the i3X ecosystem       │
│                             │                  │                              │
│  areas ─ devices ─ entities │                  │  i3X Explorer, Python SDK,   │
│  recorder + statistics      │                  │  MCP server, AI apps,        │
│  state-change events        │                  │  historians, i3X servers     │
│                             │ ◀─────────────── │                              │
└─────────────────────────────┘  consume: sensors,└──────────────────────────────┘
                                 statistics, writes
```

## The server half

- **Model browsing**: a single-rooted hierarchy (`home`, then areas, devices, and entities) served through `GET /objects`, `POST /objects/list`, and `POST /objects/related` with bidirectional edges. Devices are compositions: they carry `HasComponent`/`ComponentOf` edges to their entities, and value or history reads with `maxDepth > 1` (or `0`) fold the component entities into a `components` map.
- **Rich object types**: JSON-Schema types generated per domain, device class, and unit. Structured schemas cover `light`, `climate`, `cover`, `media_player`, `weather`, and more; sensors get scalar leaf types (`type:sensor.temperature.degc` is `{"type": "number"}`).
- **Current values**: `POST /objects/value` returns VQT records (value, quality, timestamp) mapped from live HA states. `unavailable` maps to `Bad` and `unknown` to `GoodNoData`, per the spec's null-pairing rules.
- **Todo lists**: `todo` entities expose a structured value carrying both the open-item count and the actual list items (fetched live through `todo.get_items` with a freshness cache keyed on the entity's last update).
- **History**: `POST /objects/history` serves the recorder's data as VQT arrays. For spans older than the recorder's purge window, numeric entities are backfilled from long-term statistics (hourly points, kept forever), so history requests reach years back instead of about 10 days. Responses are row-capped with an honest HTTP 206 on truncation.
- **Subscriptions**: create, register, and sync with monotonic sequence numbers, ack semantics (`lastSequenceNumber`; `-1` clears), poll-style snapshot capture, queue-overflow 206s, and mandatory idle-TTL expiry. Fed live from HA's event bus.
- **SSE streaming**: `POST /subscriptions/stream` pushes state changes as they happen, with a single stream per subscription, clean takeover, and keep-alive heartbeats for proxies.
- **Writes**: `PUT /objects/value` maps to Home Assistant service calls per domain (switch, light, number, select, cover, climate, and more). Two tiers: idempotent echo writes (writing a value equal to the current one) always succeed as no-ops, while value-changing writes require the write toggle plus a per-entity allowlist. `PUT /objects/history` accepts idempotent replacements only, because HA's recorder is append-only.

## The client half

Add a "Connect to an i3X server" config entry pointing at any i3X 1.0 endpoint, using a Bearer token, HTTP Basic, a custom header, or no auth. Then, via the entry's options:

- **Live sensors**: listed remote elementIds become sensor entities under one service device. Updates arrive by SSE push when the server declares `subscribe.stream`, otherwise by `/sync` polling with sequence-number acks. Expired subscriptions (404) are recreated automatically.
- **Statistics import**: remote history becomes native long-term statistics (`i3x:*`). Measurements import as hourly mean/min/max; counters import as meter-reset-aware state plus sum, resuming from the last stored row so re-imports never double-count.
- **`i3x.write`**: write a VQT to any remote object from automations or scripts. The call is refused cleanly if the remote does not declare `update.current`.

Interop is CI-tested against the conformance suite's reference mock server and verified against CESMII's public demo at `api.i3x.dev`.

## Security

The security boundary is Home Assistant's own bearer-token auth: every endpoint except the spec-mandated `GET /info` requires a valid HA access token, and a token holder gains nothing beyond what HA's native API already grants. On top of that:

- **`local_only` (default on)**: all i3X endpoints, `/info` included, refuse requests from non-private addresses (evaluated after HA's `trusted_proxies` handling).
- `/info` is **rate limited** per IP for non-local clients and discloses no identifying details (generic server name, no HA version).
- Resource caps throughout: bulk requests of at most 500 ids, 20 subscriptions per client and 100 total, 500 monitored objects each, 10 concurrent SSE streams, bounded queues, history row limits, and recorder reads off the event loop.
- **Value-changing writes are off by default.** Out of the box, the only writes that succeed are idempotent echoes: writing a value identical to the current one, which changes nothing (and is exactly what the conformance suite's non-destructive write tests do). The master toggle in the options enables real writes for all exposed entities; the optional write allowlist narrows that to specific entities (an empty allowlist means all). **Locks are never writable**, and history writes can never invent or alter data points.

Exposing the API beyond your LAN is a deliberate opt-out of `local_only`. Prefer VPN or Tailscale reach over a public route, and note that SSO forward-auth proxies generally cannot sit in front of headless i3X API clients.

## Installation

1. **HACS** (custom repository): HACS > Integrations > Custom repositories, then add `https://github.com/rdgerken/ha-i3x` as an Integration. Alternatively, copy `custom_components/i3x/` into your config's `custom_components/`.
2. Restart Home Assistant.
3. Settings > Devices & Services > Add Integration > **i3X**, then pick the server or client flavor.
4. Create a long-lived access token (your profile > Security) for i3X clients.

Your server is now at `http://<ha-host>:8123/api/i3x/v1`. Check `GET /info`:

```bash
curl http://homeassistant.local:8123/api/i3x/v1/info
curl -H "Authorization: Bearer <token>" \
     -X POST http://homeassistant.local:8123/api/i3x/v1/objects/value \
     -H "Content-Type: application/json" \
     -d '{"elementIds": ["sensor.living_room_temperature"]}'
```

### Server options

| Option | Default | Meaning |
| --- | --- | --- |
| Server name | `Home Assistant i3X` | Shown on the unauthenticated `/info` endpoint |
| Local only | on | Refuse non-private client addresses on every endpoint |
| Include domains / include globs / exclude globs | all | Which entities are exposed (uses HA's entity-filter syntax) |
| Subscription TTL | 600 s | Idle time before a subscription is expired |
| Allow value-changing writes | off | Master switch for writes that actually change entity state |
| Writable entity globs | empty (= all) | Optional allowlist narrowing writes to specific entities (e.g. `switch.*`, `input_number.pool_*`) |

### Client options

| Option | Default | Meaning |
| --- | --- | --- |
| Remote elementIds to surface as sensors | empty | Each becomes a sensor entity with live updates |
| Measurement statistics imports | empty | Remote elementIds imported as hourly mean/min/max statistics |
| Counter statistics imports | empty | Remote elementIds imported as meter-style state and sum statistics |

## Verifying conformance yourself

```bash
git clone --branch 1.0 https://github.com/cesmii/i3X
cd i3X/conformance-tests
node bin/i3x-test.js run http://<ha-host>:8123/api/i3x/v1 --token <long-lived-token>
```

The write tests are non-destructive (they echo current values back), so they are safe to run against a live home even with value-changing writes disabled. Note that the "rich type system" part of the verdict requires at least one structured-domain entity (a light, climate, cover, and so on) to be exposed; an address space of only scalar sensors is honestly reported as an immature type system.

## Known limitations

- Full-fidelity history equals your recorder purge window (default 10 days); older spans come from long-term statistics, meaning hourly resolution and numeric entities only.
- History *writes* are idempotent replacements only. HA's recorder is append-only, so novel or altered past points are refused per-item.
- Composition depth is one level (device to entities); `maxDepth` beyond that is trivially satisfied.
- Subscriptions are in-memory: an HA restart drops them (spec-legal; clients recreate on 404).
- Browser-based clients need their origin added to HA's `http: cors_allowed_origins`. This does not apply to the i3X Explorer, which is a desktop application.

## Roadmap

- [x] SSE streaming (`POST /subscriptions/stream`) (v0.2)
- [x] Writes mapped to service calls, default-off behind an entity allowlist (v0.2)
- [x] **Full 1.0 Compliance** verdict (v0.2)
- [x] i3X client half: consume external i3X servers into HA entities and long-term statistics (v0.3)
- [x] Long-term statistics as a deep-history source (v0.4)
- [x] Composition objects (devices fold their entities into `components`) (v0.4)

## Disclaimer

Not affiliated with CESMII. i3X™ is a trademark of CESMII, the Smart Manufacturing Institute. MIT licensed; use at your own risk.
