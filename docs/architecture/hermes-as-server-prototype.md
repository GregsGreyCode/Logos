# LOG-44 Phase 1 — Hermes-as-server prototype findings

> Worktree: `logos-log44-hermes-server`, branch `log44-hermes-server-prototype`.
> Scope: read upstream `hermes-agent` to understand the `hermes gateway run` HTTP surface; empirically validate it works inside an existing OpenShell sandbox without any image rebuild.
> Date: 2026-04-17.
> Status: **validated end-to-end against live sandbox** — `/health`, `/v1/models`, `/v1/chat/completions`, `/v1/runs` + SSE, `/api/jobs` all return 200 with expected shapes. See "Empirical validation" section below.

## TL;DR

Upstream Hermes ships **a fully production-grade HTTP agent server** with a much richer API than I'd assumed. The migration is "use what already exists", not "build a daemon ourselves". Key surfaces:

- OpenAI-compat chat (`/v1/chat/completions`) with optional session continuity (`X-Hermes-Session-Id`)
- OpenAI Responses API (`/v1/responses`) — stateful via `previous_response_id`, conversation history is server-side
- **Async runs API** (`POST /v1/runs` → 202 + `run_id`, `GET /v1/runs/{run_id}/events` → SSE) — exactly the autonomous-execution + observability protocol Logos has been partially reinventing
- **Cron jobs CRUD** (`/api/jobs/*`) — per-agent scheduling with pause/resume/run, no Logos-side cron needed for in-sandbox jobs
- Health (`/health`, `/v1/health`), models (`/v1/models`)

Auth model: Bearer token via `API_SERVER_KEY` env var.

Bind: `127.0.0.1:8642` by default. **Upstream bug**: binds 127.0.0.1 regardless of config — NemoClaw works around it with socat (`0.0.0.0:8642 → 127.0.0.1:18642`). For our case (in-sandbox, accessed via `openshell sandbox exec`), 127.0.0.1 binding is FINE — no socat needed.

## Full endpoint inventory

From `knowledge-repos/hermes-agent/gateway/platforms/api_server.py` (lines 1552–1570):

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Health check |
| GET | `/v1/health` | Health check (versioned alias) |
| GET | `/v1/models` | Lists hermes-agent as available model |
| POST | `/v1/chat/completions` | OpenAI Chat Completions. Stateless by default; opt-in continuity via `X-Hermes-Session-Id` header |
| POST | `/v1/responses` | OpenAI Responses API. Stateful — server stores response history under `response_id`, recall via `previous_response_id` |
| GET | `/v1/responses/{response_id}` | Fetch stored response |
| DELETE | `/v1/responses/{response_id}` | Delete stored response |
| POST | `/v1/runs` | **Async run** — returns 202 + `run_id` immediately, agent loop runs in background |
| GET | `/v1/runs/{run_id}/events` | **SSE stream** of structured lifecycle events for a run |
| GET | `/api/jobs` | List cron jobs |
| POST | `/api/jobs` | Create cron job |
| GET | `/api/jobs/{job_id}` | Get cron job |
| PATCH | `/api/jobs/{job_id}` | Update cron job |
| DELETE | `/api/jobs/{job_id}` | Delete cron job |
| POST | `/api/jobs/{job_id}/pause` | Pause cron job |
| POST | `/api/jobs/{job_id}/resume` | Resume cron job |
| POST | `/api/jobs/{job_id}/run` | Trigger cron job immediately |

**Run event types** (from `_make_run_event_callback` in api_server.py:~1320):
- `tool.start` — `{tool, run_id, timestamp}`
- `tool.end` — `{tool, run_id, timestamp, duration, error}`
- `reasoning.available` — `{run_id, timestamp, text}`
- `message.delta` — `{run_id, timestamp, delta}` (streaming text chunks)

NB: `_thinking` and `subagent_progress` are intentionally NOT forwarded over SSE.

**Concurrency:** there's a `_MAX_CONCURRENT_RUNS` cap; over-limit returns 429 with `code: rate_limit_exceeded`.

**Conversation continuity options** (three different mechanisms, pick by transport):
- `X-Hermes-Session-Id` header on `/v1/chat/completions` (lightweight)
- `previous_response_id` body field on `/v1/responses` (full history server-side)
- `session_id` body field on `/v1/runs` (defaults to `run_id` if omitted)

## CLI surface — `hermes gateway` subcommands

From `knowledge-repos/hermes-agent/hermes_cli/main.py:4332`:

| Subcommand | Purpose | Notes |
|---|---|---|
| `run` | Run gateway in **foreground** | What we want for sandboxes. Flags: `-v` / `-vv` (log verbosity), `-q` (silent), `--replace` (stomp existing instance) |
| `start` | Start as a service | systemd-style; not relevant inside an OpenShell sandbox |
| `stop` / `restart` / `status` | Service mgmt | Not relevant inside sandbox |
| `install` / `uninstall` | systemd install | Not relevant inside sandbox |
| `setup` | Configure messaging platforms | Interactive — would need scripting if we use it from build |

For sandbox use: `hermes gateway run -v` is the entrypoint. Foreground, stays attached, exits cleanly on SIGTERM.

## Mapping Logos current dispatch → upstream APIs

### Today (Plan A-prime)
```
Logos gateway _handle_chat
  → WorkerRegistry.dispatch_task(sandbox_name, {type: task, message, history, ...})
    → openshell sandbox exec --no-tty --name {sandbox} -- python3 /tmp/sandbox_worker.py
      → AIAgent(...).run_conversation(history, message, stream_callback, reasoning_callback)
      → emits JSON-line frames on stdout: {ready}, {thinking}, {token}, {task_result}
  ← parse stdout frames, stream back as SSE to web client
```

### Target (Phase 1 minimum)
```
Logos gateway _handle_chat
  → WorkerRegistry.dispatch_task(sandbox_name, body)
    → openshell sandbox exec --no-tty --name {sandbox} -- curl -sN \
        -H "Authorization: Bearer ${API_SERVER_KEY}" \
        -H "Content-Type: application/json" \
        -d '{...}' \
        http://127.0.0.1:8642/v1/runs
      → returns 202 + {run_id}
    → openshell sandbox exec --no-tty --name {sandbox} -- curl -sN \
        -H "Authorization: Bearer ${API_SERVER_KEY}" \
        http://127.0.0.1:8642/v1/runs/{run_id}/events
      → SSE stream of message.delta / tool.start / tool.end / reasoning.available
  ← parse SSE events, re-emit as Logos's existing SSE shape to web client
```

The `openshell sandbox exec curl` invocation keeps the existing transport (no port-forward, no new attack surface). The dispatch shape changes from "parse JSON-lines" to "parse SSE" — both line-delimited.

**Cleaner alternative (Phase 1+):** mount a unix socket in the sandbox at `/tmp/hermes-api.sock`, have hermes bind there (if supported — needs investigation; if not, socat shim it). Then `openshell sandbox exec` spawns a thin Python client that reads from the unix socket. Either way the transport is unchanged from Logos's perspective.

## NemoClaw's Dockerfile — what to copy, what to skip

From `knowledge-repos/NemoClaw/agents/hermes/Dockerfile`:

**Worth copying:**
- `apt-get remove gcc g++ cpp make netcat ncat` hardening (line 14–17)
- The TelegramFallbackTransport patch (line 27–39) — disables a code path that breaks under L7 proxy. We'll need this when we move TG into per-sandbox.
- The `socat` forwarder in `start.sh` if we go TCP route (line 222–240)
- The `decode-proxy.py` URL-decoder for OpenShell placeholder tokens (line 247–263) — needed for httpx URL-encoding of `%3A` → `:`
- Config integrity check via SHA-256 (`verify_config_integrity` in start.sh)
- Capability drop via capsh (`cap_net_raw`, `cap_dac_override`, etc.) — defense in depth

**Worth SKIPPING for our prototype:**
- The full `gosu` privilege separation (`gateway` user) — useful eventually but adds complexity. Phase 1 can run as `sandbox` user.
- The Landlock immutable config dirs — defense in depth, not Phase 1 essential.
- The chattr `+i` immutable hardening — same.
- NemoClaw plugin install — that's their custom plugin, we'll write our own (or skip plugins entirely for Phase 1).
- The configure_messaging_channels env var dance — Phase 1 doesn't enable channels yet.

**Phase 1 minimal Dockerfile shape** (to write next):
```dockerfile
ARG BASE_IMAGE=hermes-upstream:latest  # or our own derived base
FROM ${BASE_IMAGE}

# Hermes binary should already be in the upstream image at /usr/local/bin/hermes
# If not: RUN curl -fsSL https://hermes.nousresearch.com/install.sh | bash

# Minimal config — Phase 1 just proves /health and /v1/chat/completions work
RUN mkdir -p /sandbox/.hermes /sandbox/.hermes-data
COPY phase1-config.yaml /sandbox/.hermes/config.yaml
COPY phase1.env /sandbox/.hermes/.env

# Stay-alive entrypoint runs `hermes gateway run` in foreground
ENTRYPOINT ["hermes", "gateway", "run", "-v"]
```

## Required Hermes config (Phase 1 prototype)

Hermes needs at minimum:
- `HERMES_HOME` env var pointing to a writable dir (NemoClaw uses `/sandbox/.hermes-data`)
- `config.yaml` with at minimum a `model` block (provider, base_url, default model name)
- `.env` with `API_SERVER_KEY` for bearer auth
- An `OPENAI_API_KEY` (or equivalent) routed through `inference.local`

Reference: `knowledge-repos/NemoClaw/agents/hermes/generate-config.ts` shows the config generation logic — it's TypeScript, but the YAML output shape is what we need.

## Open decisions before Phase 1 implementation

| Decision | Options | Recommendation |
|---|---|---|
| Transport from Logos → in-sandbox Hermes | (a) `openshell sandbox exec curl localhost:8642`, (b) OpenShell port-forward 8642→host, (c) unix socket | **(a)** for Phase 1 prototype — zero new surface area, keeps Plan A-prime's exec primitive. Move to (c) after if there's a measurable cost. |
| Auth | (a) Skip auth in Phase 1, (b) `API_SERVER_KEY` from start | **(b)** — set a per-sandbox secret at spawn time, pass it to `openshell sandbox exec curl`. Trivial cost, prevents future regressions. |
| Conversation continuity | (a) `/v1/chat/completions` + `X-Hermes-Session-Id`, (b) `/v1/responses` + `previous_response_id`, (c) `/v1/runs` + `session_id` | **(c)** for Phase 1 — gives us SSE event stream which matches what Logos's web UI already expects. |
| Hermes binary install | (a) curl install.sh, (b) bake into derived base image | **(b)** — derive `hermes-server-base:m13` from `hermes-upstream:latest` with hermes binary baked in. Avoids per-spawn install. |
| Where Phase 1 prototype Dockerfile lives | (a) `docker/Dockerfile.hermes-server-test` in repo root, (b) `prototypes/log44-phase1/` | **(b)** — clearly throwaway, don't pollute `docker/`. |
| Should Phase 1 swap WorkerRegistry, or run alongside? | (a) Replace `dispatch_task` in worktree, (b) New `dispatch_task_v2` next to old | **(b)** — keeps the v1 dispatch path working while we test v2. Easy A/B. |

## Concrete next steps (Monday morning)

In the worktree (`logos-log44-hermes-server`):

1. **Build a derived base image** at `prototypes/log44-phase1/Dockerfile.hermes-server-base`:
   - `FROM hermes-upstream:latest`
   - Install hermes binary if not present
   - Confirm `/usr/local/bin/hermes` exists, `hermes --version` works

2. **Build a test sandbox image** at `prototypes/log44-phase1/Dockerfile.hermes-server-test`:
   - `FROM hermes-server-base`
   - Drop in a minimal `config.yaml` and `.env`
   - `ENTRYPOINT ["hermes", "gateway", "run", "-v"]`

3. **Run it locally** (no OpenShell needed for the first prototype):
   ```bash
   docker run -d --name hermes-test -p 8642:8642 hermes-server-test
   docker logs -f hermes-test  # confirm "API server listening on 127.0.0.1:8642"
   curl http://localhost:8642/health
   curl -X POST http://localhost:8642/v1/chat/completions \
        -H "Authorization: Bearer ${API_SERVER_KEY}" \
        -H "Content-Type: application/json" \
        -d '{"model":"hermes-agent","messages":[{"role":"user","content":"Say hi in 5 words"}]}'
   ```
   - If chat completes: foundation is sound, move to Step 4.
   - If chat fails: read `docker logs`, debug, document the gotcha here.

4. **Test the runs API**:
   ```bash
   curl -X POST http://localhost:8642/v1/runs \
        -H "Authorization: Bearer ${API_SERVER_KEY}" \
        -H "Content-Type: application/json" \
        -d '{"input":"Multiply 7 by 6","instructions":"Be concise"}' \
   # Should return 202 + {"run_id": "run_..."}
   curl -N http://localhost:8642/v1/runs/{run_id}/events \
        -H "Authorization: Bearer ${API_SERVER_KEY}"
   # Should stream SSE: message.delta, tool.start/end, etc.
   ```

5. **Move to OpenShell sandbox**: launch a sandbox using the new image, confirm `openshell sandbox exec curl localhost:8642/health` returns 200.

6. **Wire `dispatch_task_v2`** in `gateway/worker_registry.py`: takes the same task shape, internally does the curl-via-exec + SSE parsing instead of stdin/stdout subprocess.

7. **Add a feature flag** (e.g. `LOGOS_DISPATCH_V2=1` env var) that flips between v1 and v2 in `_handle_chat`. Side-by-side test on a real sandbox.

## Risks / known gotchas (from upstream code reading)

- **Hermes binds 127.0.0.1 regardless of config** (upstream bug). Inside our sandbox this is fine because `openshell sandbox exec` joins the sandbox's namespace; we don't need external port access. Only matters if we ever decide to port-forward.
- **`HERMES_HOME` must be writable** — Hermes writes state files (PID, state.db, .channel_directory) directly into HERMES_HOME. Cannot be mounted read-only. NemoClaw separates immutable config (`/sandbox/.hermes`) from writable state (`/sandbox/.hermes-data`) via symlinks.
- **`API_SERVER_KEY` is opt-in** — if env var is empty, the API server runs unauthenticated. Production: always set it.
- **Concurrent runs are capped** at `_MAX_CONCURRENT_RUNS` — for one-agent-per-sandbox this is fine, but if we ever cram multiple agents into one sandbox we'll hit it.
- **Hermes uses `httpx` which URL-encodes `%3A` → `:`** — breaks OpenShell's placeholder pattern. NemoClaw works around with `decode-proxy.py`. We'll need it once we route inference through `inference.local` placeholders.

## What this DOESN'T cover (deferred)

- Per-agent config push from Logos at spawn time → Phase 44.2
- Channel adapter migration (TG/Discord/Slack into sandbox) → Phase 44.3
- Sessions/memory reconciliation → Phase 44.4
- Tool/skill plugin migration → Phase 44.5
- Cleanup of `sandbox_worker.py` → Phase 44.6

## Empirical validation (2026-04-17)

Ran `hermes gateway run` inside the existing `hermes-henry` OpenShell sandbox with no image rebuild — the `hermes-sandbox:m12` image already ships with the hermes binary (`/usr/local/bin/hermes`, v0.7.0 from 2026.4.3). This alone is a significant simplification over the doc's original plan of deriving a new base image.

### Setup used

```yaml
# /tmp/hermes-proto-home/config.yaml
model:
  default: gpt-oss-20b
  provider: custom
  base_url: https://inference.local/v1
api_server:
  enabled: true
  host: 127.0.0.1
  port: 8642
```

```bash
# /tmp/hermes-proto-home/.env
API_SERVER_KEY=proto-test-key-abc123
OPENAI_API_KEY=lm-studio
OPENAI_BASE_URL=https://inference.local/v1
```

Launch: `HERMES_HOME=/tmp/hermes-proto-home nohup hermes gateway run -v > /tmp/hermes-gw.log 2>&1 &`

Log confirmed clean startup:
- `[Api_Server] API server listening on http://127.0.0.1:8642` ✓
- `✓ api_server connected` ✓
- `1 hook(s) loaded` (boot hooks wired) ✓
- `Cron ticker started (interval=60s)` ✓

### Endpoints validated

| Endpoint | Request | Result |
|---|---|---|
| `GET /health` | no body | 200 `{"status": "ok", "platform": "hermes-agent"}` |
| `GET /v1/models` | Bearer auth | 200 `{"object":"list","data":[{"id":"hermes-agent",...}]}` |
| `POST /v1/chat/completions` | `{model:"hermes-agent",messages:[{role:"user",content:"Say hi in 3 words"}]}` | 200 `{"choices":[{"message":{"role":"assistant","content":"Hello there!"}}], usage:{prompt_tokens:9308,completion_tokens:4}}` — **inference.local routing works** |
| `POST /v1/runs` | `{"input":"What is 7*6?","instructions":"Be concise."}` | 202 `{"run_id":"run_698b...","status":"started"}` |
| `GET /v1/runs/{run_id}/events` | SSE stream | `text/event-stream`, events in order: `message.delta` ("4"), `message.delta` ("2"), `reasoning.available` ("42"), `run.completed` (output:"42", usage), then `: stream closed` |
| `GET /api/jobs` | Bearer auth | 200 `{"jobs": []}` |

### Frame shape confirmed

SSE events are `data: <json>` lines. Event shapes confirmed:
- `{event: "message.delta", run_id, timestamp, delta: "<chunk>"}`
- `{event: "reasoning.available", run_id, timestamp, text: "<preview>"}`
- `{event: "run.completed", run_id, timestamp, output: "<final>", usage: {input_tokens, output_tokens, total_tokens}}`
- NOT observed in this test but documented in code: `tool.start`, `tool.end`
- Terminator: `: stream closed` (standard SSE comment)

### Gotchas observed (that the pre-empirical doc didn't flag)

1. **`openshell sandbox exec` rejects command args containing literal newlines** (gRPC error: *"command argument 2 contains newline or carriage return characters"*). Multi-line scripts must be base64-encoded and decoded inside: `sh -c "echo $B64 | base64 -d | sh"`. This is a real constraint on how Logos's `dispatch_task_v2` will construct its invocation.
2. **`curl` is NOT in the sandbox image.** The dispatch script must use `python3 -c "import urllib.request..."` or ship a curl binary. Python3 is present.
3. **`openshell sandbox exec` hangs without explicit stdin close** (`< /dev/null`). Match what `WorkerRegistry.dispatch_task` already does — pipe stdin explicitly.
4. **Hermes probes model context length at startup** and fails gracefully to 128K default with a `probe-down` log line. Not a functional issue; slight startup cost on first chat. Can pre-populate by setting `model.context_length` in config.yaml.
5. **OpenRouter metadata fetch fails** with "Tunnel connection failed: 403 Forbidden" — blocked by OpenShell policy. Non-fatal, just noise in the log.
6. **Config schema has `model:` as a nested block, not flat.** My first pass with flat `provider: custom, model: gpt-oss-20b` got "Active profile: custom" (profile name lookup, not model config). Must be `model: {default, provider, base_url}`.
7. **`API_SERVER_KEY` auth works correctly** (correcting earlier misread — I was sending the header in my first test). Matrix confirmed empirically: `/health` is public (for probes); `/v1/models`, `/v1/chat/completions`, `/api/jobs`, `/v1/runs` all return 401 without the Bearer header or with a wrong key. Env var in `.env` is sufficient.

### What this changes about the Monday plan

The doc's original "build derived base image" step is **not needed** — `hermes-sandbox:m12` already has everything required. Phase 1 implementation can go straight to:

1. Update `OpenShellExecutor.spawn` to:
   - Write `/tmp/hermes-proto-home/config.yaml` and `/tmp/hermes-proto-home/.env` via `openshell sandbox upload`
   - After sandbox Ready, launch `HERMES_HOME=/tmp/hermes-proto-home nohup hermes gateway run -v > /tmp/hermes-gw.log 2>&1 &` via `sandbox exec` (stdin=/dev/null)
   - Wait for `/health` to return 200 (probe via `sandbox exec python3 -c "..."`)
2. Add `WorkerRegistry.dispatch_task_v2` that:
   - Builds base64-encoded Python script that POSTs to `/v1/runs` and reads `/v1/runs/{id}/events` SSE
   - Translates SSE frames to the existing Logos SSE shape (message.delta → token, reasoning.available → thinking, run.completed → task_result, tool.start/end → tool_start/tool_end)
3. Feature flag `LOGOS_DISPATCH_V2=1` in `_handle_chat` to route to the new path.

Estimated Phase 1 effort revised **down** from M–L to **S–M** based on empirical findings: ~1 day, not 2–3.

---

## Files referenced (for whoever picks this up)

- `knowledge-repos/hermes-agent/gateway/platforms/api_server.py` — the API surface
- `knowledge-repos/hermes-agent/gateway/run.py` — gateway runner entry
- `knowledge-repos/hermes-agent/hermes_cli/main.py:4332` — CLI subcommand defs
- `knowledge-repos/NemoClaw/agents/hermes/Dockerfile` — packaging reference
- `knowledge-repos/NemoClaw/agents/hermes/start.sh` — entrypoint reference
- `knowledge-repos/NemoClaw/agents/hermes/manifest.yaml` — integration contract spec
- `gateway/worker_registry.py:275` — current `dispatch_task` (the function to replace/duplicate as v2)
- `docker/sandbox_worker.py` — the file Phase 44.6 deletes
