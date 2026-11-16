# Observability — `/metrics` endpoint + Dashboard strip

**Status:** proposal
**Date:** 2026-04-21

## Problem

The Dashboard shows *state* (sandbox phases, route status, agent uptime) but not *rates or resource use*. When an agent feels slow, a route flaps, or a cluster saturates VRAM, there's nowhere in Logos to see it — operators have to SSH and `docker stats` / tail logs / run `openshell sandbox list` manually.

At the same time, none of the systems Logos orchestrates ship a useful `/metrics` endpoint:

| Source | `/metrics`? | What's actually queryable |
|---|---|---|
| OpenShell gateway | No (gRPC on :10100, not HTTP) | `openshell sandbox list` / `gateway info` CLI |
| LM Studio (192.168.1.117:1234) | No — returns `{"error":"Unexpected endpoint"}` for every `/metrics` / `/v1/metrics` / `/api/v0/metrics` path | `/api/v1/models` → model state, `loaded_context_length`, `size_bytes` |
| Hermes (inside each sandbox) | No | `/health`, `/health/detailed`, `/v1/models`, `/api/jobs` |
| Docker | stats socket | `docker stats` for CPU/mem/net per cluster container |
| Logos itself | No | In-memory dispatch counters, `spawn_metrics.json` on disk |

**Logos is the only place that can aggregate these.** Without an observability layer here, any monitoring story (Prometheus scrape, Grafana board, alerts) is a bag of bespoke scripts.

## What we want

Two surfaces, same underlying collector:

1. **`GET /metrics`** — Prometheus text format. Public (same auth posture as `/status`). Scrapable by Prom / Grafana Agent / anything that speaks the format.
2. **`GET /api/admin/metrics-snapshot`** — authenticated JSON snapshot for the UI. Cheaper than parsing Prom text client-side.

Then a new **Observability strip** on the Dashboard reads the snapshot every 5-10s.

Out of scope: persistent TSDB, alerting rules, long-term retention. Logos emits instantaneous values only — Prometheus or similar handles storage/alerting.

## Metric inventory (first cut)

### Logos-native (no external scrape needed)

| Metric | Type | Labels | Source |
|---|---|---|---|
| `logos_dispatch_total` | counter | `agent`, `status`, `origin` | existing in-process counters |
| `logos_dispatch_duration_seconds` | histogram | `agent`, `origin` | wrap `dispatch_task_v2` |
| `logos_spawn_duration_seconds` | histogram | `phase` (pod/agent), `bucket` (warm/cold) | `spawn_metrics.record()` |
| `logos_sse_connections_active` | gauge | — | aiohttp middleware |
| `logos_route_status` | gauge (0/1) | `provider`, `model`, `status` | `auth_db.list_model_routes()` |
| `logos_provisioning_stage_dwell_seconds` | gauge | `stage` | `_SETUP_PROGRESS` |

### Pulled from OpenShell CLI (cached 10s)

| Metric | Type | Labels | Source |
|---|---|---|---|
| `openshell_sandboxes_total` | gauge | `gateway`, `phase` | `openshell -g <gw> sandbox list` |
| `openshell_clusters_healthy` | gauge (0/1) | `cluster` | existing `_handle_clusters_list` |

### Pulled from LM Studio (cached 30s)

| Metric | Type | Labels | Source |
|---|---|---|---|
| `lmstudio_model_loaded` | gauge (0/1) | `machine`, `model` | `/api/v1/models` → `state` field |
| `lmstudio_loaded_context_tokens` | gauge | `machine`, `model` | `/api/v1/models` → `loaded_context_length` |
| `lmstudio_model_size_bytes` | gauge | `machine`, `model` | `/api/v1/models` → `size_bytes` |

### Pulled from per-sandbox hermes (cached 15s)

| Metric | Type | Labels | Source |
|---|---|---|---|
| `hermes_up` | gauge (0/1) | `sandbox` | `GET /health` — 200 vs timeout |
| `hermes_platforms_connected` | gauge | `sandbox`, `platform` | `/health/detailed` |
| `hermes_cron_jobs_active` | gauge | `sandbox` | `/api/jobs` list |

### Pulled from Docker stats (cached 5s)

| Metric | Type | Labels | Source |
|---|---|---|---|
| `cluster_cpu_percent` | gauge | `cluster` | `docker stats --no-stream` |
| `cluster_memory_bytes` | gauge | `cluster` | `docker stats --no-stream` |

## Implementation plan

Three landing zones.

### 1. `gateway/metrics.py` (new)

Thin collector — no `prometheus_client` dependency (Prom text format is a dozen lines to emit).

```python
class Metric:
    name: str
    mtype: str  # "counter" | "gauge" | "histogram"
    help: str
    labels: tuple[str, ...]
    values: dict[tuple, float]  # label-tuple → value

class Registry:
    def counter(name, help, labels=())  # returns a Counter
    def gauge(name, help, labels=(), pull_fn=None)  # pull_fn() returns {label_tuple: value}
    def histogram(name, help, labels=(), buckets=(...))

    def render_prometheus(self) -> str   # Prom text format
    def render_snapshot(self) -> dict    # JSON for the UI
```

**Pull-fn pattern** — gauges that need external calls register a function that's invoked on scrape. Wrap with an asyncio lock + timestamp-based cache so rapid scrapes don't hammer LM Studio. Each pull-fn declares its `cache_ttl_seconds`.

**Cost budget** — at 10s scrape interval with the TTLs above, one scrape triggers: 0-1 OpenShell CLI call, 0-1 LM Studio fetch, N hermes pings (parallel, 2s timeout each, capped at active sandbox count), one `docker stats --no-stream`. Worst case ~2s per scrape; typical ~100ms.

### 2. `gateway/http_api.py`

Two routes:

- `app.router.add_get("/metrics", _handle_metrics)` — public, text/plain, Prometheus format
- `app.router.add_get("/api/admin/metrics-snapshot", _handle_metrics_snapshot)` — auth'd, JSON

Public `/metrics` has no secrets in labels (never put `api_key` / agent user content into label values). Auth'd snapshot can be richer.

### 3. `gateway/html/main_app.html`

New strip inside `x-show="tab==='activity' && activityTab==='dashboards'"`, placed above Sandboxes:

```
┌─ Observability ─────────────────────────────────────────┐
│ ┌─Dispatch──┐ ┌─LM Studio──┐ ┌─Clusters──┐ ┌─Agents──┐ │
│ │12/min     │ │qwen3.5-9b  │ │openshell  │ │tony ✓   │ │
│ │p50 1.2s   │ │  loaded    │ │  42% cpu  │ │sally ✓  │ │
│ │p90 3.4s   │ │  64K ctx   │ │  2.1/8 GB │ │         │ │
│ └───────────┘ └────────────┘ └───────────┘ └─────────┘ │
└──────────────────────────────────────────────────────────┘
```

Polls `/api/admin/metrics-snapshot` every 5s when the tab is active.

## Critical files

- `gateway/metrics.py` — new module (~200 lines)
- `gateway/http_api.py` — two new routes + handlers (~40 lines)
- `gateway/html/main_app.html` — Observability strip (~120 lines) + Alpine state/poller (~30 lines)

## Reused existing code

- `spawn_metrics.record()` — hook the histogram off this
- `auth_db.list_model_routes()` — route-status gauge source
- `openshell_routes.list_clusters()` — cluster count source
- `_handle_clusters_list` — already does `docker ps` parsing; reuse the container-name resolution
- `worker_registry_v2.fetch_toolsets_from_sandbox` — pattern for per-sandbox probes via `openshell sandbox exec`

## Verification

1. **Prom format**: `curl http://localhost:8091/metrics | promtool check metrics` should accept the output
2. **Snapshot freshness**: Hit `/api/admin/metrics-snapshot` twice in under the TTL of a pull-fn — second call should be served from cache (measurable via a debug log line)
3. **Cost**: A single scrape with 2 active sandboxes + 1 LM Studio + 2 clusters should complete in < 500ms on warm cache, < 2s cold
4. **UI**: On the Dashboard, stopping LM Studio makes the "LM Studio" card flip to `unreachable` within one poll cycle (5s)
5. **Security**: `curl http://localhost:8091/metrics` from the LAN must not return any label containing `api_key`, session tokens, user content, or credentials

## Incremental delivery

The four metric categories are independent. Cheapest first:

1. **Phase 1**: Logos-native counters + `/metrics` skeleton. Already-computed data, no external I/O. One day of work.
2. **Phase 2**: Docker stats + OpenShell sandbox counts. Both come from CLI / local daemon; no network. Cheap and high-signal.
3. **Phase 3**: LM Studio + hermes pulls. Adds LAN I/O per scrape; caching matters.
4. **Phase 4**: Dashboard Observability strip. Needs Phases 1-3 populated to look useful.

Ship each phase as a separate PR — each is independently valuable.

## Out of scope

- TSDB / persistent retention — use Prometheus for that
- Alerting / paging — Prometheus Alertmanager
- Per-tool-call tracing — that's an OpenTelemetry story, separate proposal
- Dashboards beyond the single Observability strip — Grafana reads `/metrics`, we don't replicate Grafana

## Open questions

1. **Authentication for `/metrics`** — Prometheus scrape is conventionally unauthenticated on an internal network. Do we want that posture, or require a bearer token? (Same-LAN posture today matches `/status`.)
2. **Label cardinality** — `agent` label on dispatch metrics is bounded by agent count (small). `session` would explode cardinality; must stay out of labels.
3. **Histogram bucket choices** — default to Prom exponential buckets, or tune to our observed dispatch latency distribution (5ms / 50ms / 500ms / 5s / 50s)?
