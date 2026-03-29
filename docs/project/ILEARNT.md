# I Learnt

> Bullet-point log of technical lessons from building Logos.

---

## Docker / Build

- **Copy `pyproject.toml` before source code** — installing deps before `COPY . /app/` means the 156-package layer caches on code-only changes. Saved ~80s per build.
- **`kubectl set image` with the same tag is a no-op** — Kubernetes sees no spec change, doesn't restart. Use `kubectl rollout restart` to force a fresh pull, or use immutable SHA tags.
- **Bake the git SHA at build time** — `ARG BUILD_SHA` + `--build-arg BUILD_SHA=$(git rev-parse --short HEAD)` → readable in the UI at runtime. Proves exactly what commit is deployed.
- **`docker buildx` pushes a manifest list** — the digest you see in the build output is the multi-arch manifest, not the amd64 image digest. The pod's `Image ID` will be different.

## Kubernetes

- **Downward API for node LAN IP** — a pod's CNI IP (`10.244.x.x`) is not the host's LAN IP (`192.168.1.x`). `fieldRef: status.hostIP` injects the node's real IP without any cluster permissions.
- **PVC with `local-path` is node-pinned** — if that node goes down, the PVC is gone. Fine for homelab, a surprise for anyone expecting K8s-level HA.
- **NodePort services route by named port** — `targetPort: http` resolves to the container port named `http`, not necessarily port 80. Easy to get wrong.
- **`imagePullPolicy: Always` still needs a restart to re-pull** — the policy controls *when* it pulls, not whether it notices a tag has a new underlying image.
- **Pod IPs are not the node's LAN IP — `_own_ips()` must include `NODE_IP`** — pod cluster IPs (`10.42.x.x`) differ from the node LAN IP (`192.168.x.x`). The subnet scan skips `own_port` (8080) only on IPs in `_own_ips()`. Without `NODE_IP`, port 8080 on the host node is never excluded and Logos appears as a duplicate LM Studio server in its own scan results. Fix: inject `NODE_IP` via downward API (`status.hostIP`) and add it to `_own_ips()`.
- **Logos pod egress must explicitly allow inference ports** — a NetworkPolicy that only permits ports 80/443 silently blocks machine setup probes. Setup health checks and model discovery run from the logos pod directly (not via the ai-router), so ports 1234 (LM Studio), 11434 (Ollama), and 8000 (vLLM) must be in the logos pod's egress rules or the setup wizard shows no model servers.
- **`HERMES_RUNTIME_MODE` must be set in the k8s configmap** — `SessionContext` reads it from env to build the agent system prompt. If missing it defaults to `"local"` and the agent incorrectly reports "Local (Linux)" instead of "Kubernetes (Linux)". Add `HERMES_RUNTIME_MODE: "kubernetes"` to the configmap and wire it into the deployment env.

## Python / aiohttp

- **`X-Accel-Buffering: no` is mandatory for SSE through nginx** — without it nginx buffers the whole response before sending. The client gets nothing until the model finishes.
- **`asyncio.Semaphore` for bounded concurrency** — scanning 254 hosts × 2 ports concurrently but capped at 40 in-flight: `async with sem:`. Completes in ~3s instead of ~8 minutes.
- **SSE is simpler than WebSockets for server→client streams** — works over HTTP/1.1, auto-reconnects in the browser, no upgrade handshake. Use it for one-way streaming.
- **aiohttp `StreamResponse` must be `prepare()`d before writing** — `await response.prepare(request)` must come before any `response.write()` calls or you get an error.

## LLM / AI

- **LM Studio adopted Ollama's `/api/tags` endpoint** — this broke probe-based server classification. Fixed by making the probe strategy explicit (`prefer=ollama|lmstudio`) so callers can skip misclassifying endpoints.
- **TTFT (time to first token) matters more than total latency for UX** — users perceive a model as fast if the first token arrives quickly, even if the full response takes 15s. Track and show both.
- **T-shirt size models by parameter count** — extract `Xb` from model ID → map to xs/s/m/l/xl → show RAM hint. Prevents users from downloading a 70B model onto a 16 GB machine.
- **CPU+GPU hybrid inference is viable** — 70B model (40 GB Q4) on 64 GB RAM + 16 GB VRAM: Ollama splits layers automatically. Expect 2–5 tok/s. Usable for non-interactive agent tasks.

## Frontend (Alpine.js)

- **`$nextTick` before touching DOM after state change** — Alpine batches DOM updates; code that runs immediately after `this.step = 2` may find the step-2 DOM doesn't exist yet.
- **CSS `@keyframes` can hold a value across a range** — `0%, 18% { … }` creates a pause before transitioning. Useful for theme-cycling animations that feel deliberate, not jittery.
- **Radial gradient must reach `transparent` before the element edge** — if the gradient doesn't fade to transparent inside the bounding box, the box edge becomes a visible cutoff line.

## Architecture / Design

- **STAMP model** — every agent run records Soul + Tools + Agent + Model + Policy. Makes runs replayable, comparable, and auditable. Different from infra observability (which just tracks latency).
- **SQLite is a deliberate choice, not a stopgap** — WAL mode, FTS5, zero infrastructure. Right for homelab/personal scale. Wrong for hundreds of concurrent writers or vector search at scale. Own the tradeoff.
- **Soft guardrails are not sandboxes** — path boundary enforcement in Python can be bypassed via subshells, heredocs, eval. A real sandbox needs a kernel boundary. Document the limitation honestly.
- **Dual persistence without a source of truth causes subtle bugs** — SQLite + JSONL for session state means divergence on crash. Pick one. SQLite wins.

## Benchmarking / Model Selection

- **tok/s from SSE chunk count is almost always wrong** — SSE chunks ≠ tokens. A 120-token response may arrive in 3 chunks, giving 3/10s = 0 tok/s. Always prefer `usage.completion_tokens` from the stream; fall back to `char_count ÷ 4` with an "~approx" label, not silently.
- **TTFT matters separately from throughput** — a model at 40 tok/s with 4s TTFT feels slower than 28 tok/s with 0.5s TTFT for short agent interactions. Track both and include TTFT (15%) in the ranking score.
- **Single benchmark prompt understates structured-output slowdown** — some models run 20–30% slower on JSON/tool-call prompts than plain prose. Running two passes (prose + structured) and averaging gives a better proxy for real agent workloads.
- **7–13B hard preference suppresses good candidates** — a 4B model may outscore a weak 7B on real evals, and a 14B Q4 quant may still be fast enough. Sample across size buckets (small/mid/large/unknown) instead of sorting by distance from a sweet spot.
- **JSON format and tool-call selection are mandatory gates, not just score components** — a model that can't reliably produce structured output or select the right tool will break agent loops regardless of speed. Rank within a gated pool; fall back to ungated only if no model passes.
- **One recommendation is not enough** — "best balanced" and "fastest acceptable" serve different user preferences. Always surface both if they differ (balanced = highest composite score; fastest = highest tok/s in the gated pool).
- **Multiple prompts per capability improve discrimination** — a single arithmetic prompt or single tool-call prompt is too easy; mediocre models pass while strong ones don't separate. Future: 2–3 prompts per eval category with a pass-rate threshold (e.g. ≥ 0.67) instead of pass/fail on one shot.
- **Benchmark candidate picker must deduplicate by base model name** — spawned instances register under a suffixed name (e.g. `qwen/qwen3.5-9b:2`). The picker treats this as a distinct model, wasting a slot and producing a redundant result. Fix: strip `:N` suffixes with `re.sub(r":\d+$", "", mid)` before bucketing in `_pick_compare_candidates`.
- **VRAM state between tests must be explicit** — LM Studio: `POST /api/v1/models/unload` with `{"instance_id": model_id}`. Ollama: `POST /api/generate` with `{"model": model_id, "keep_alive": 0, "prompt": ""}`. Without this, each model loads on top of the previous one and throughput measurements for later models are degraded.
- **LM Studio returns HTTP 200 for loads that degrade to red-state** — When VRAM is insufficient for the requested context size, LM Studio returns HTTP 200 from `POST /api/v1/models/load` but loads the model in a degraded state (red indicator in UI). Short prompts still succeed because they never fill the KV cache. A probe must send a completion request with a payload sized to the target context (`max_tokens: 1`, filler ≈ `ctx_size × 3` chars) to verify the KV cache actually fits in VRAM.
- **`x-transition` on Alpine `x-show` can interfere with `max-height` overflow** — transitions using opacity/scale don't affect max-height, but the interaction with certain CSS classes can cause unexpected layout. Use explicit inline `style="max-height:Xrem;overflow-y:auto"` rather than Tailwind's `max-h-*` on transitioning elements.

## Tool & Context Management

- **Tool schemas are the biggest token tax in every request** — 31 tools × ~300 tokens each = ~9.5K tokens of JSON schema sent in every API call. On a 16K context model, the system prompt (8K) + tools (9.5K) exceeds the context for a "Hi!" message. Solution: lazy tool loading — start with 11 core tools (~3.5K), let the agent `request_tools("web")` to load more on demand.
- **LM Studio divides total context across parallel slots** — `n_ctx_slot = total_context / n_parallel`. With 4 parallel slots and 65K model context, each request only gets 16K. For 1-2 users, set `n_parallel: 2` to get 32K per request. The setup benchmark stores the total model context but the usable context per request is `total / n_parallel`.
- **Always pass `n_parallel` when loading a model via LM Studio API** — `POST /api/v1/models/load` accepts `{"model": "...", "context_length": 65536, "n_parallel": 2}`. Without it, LM Studio uses its global default (often 4), silently quartering the per-request context.
- **The benchmark stored total context but runtime needs per-slot context** — `lmstudio_context_lengths` in config.yaml should store `total / n_parallel`, not total. The context compressor uses this value to decide when to compress — if it thinks it has 65K but only has 16K, it sends requests that are too large and fails.
- **Error messages from inference servers contain the real context limit** — LM Studio: `"exceeds the available context size (16384 tokens)"`. Parse this with regex and cache it. The error-driven probe is the most reliable way to learn the per-slot context at runtime.
- **`requires_env` check in tool schemas saves tokens AND prevents confusion** — tools with `requires_env=["FIRECRAWL_API_KEY"]` are excluded from the schema when the key isn't set. The agent never sees tools it can't use, saving tokens and preventing useless tool call attempts.
- **Soul manifests already declare tool categories but the gateway didn't use them** — `compute_effective_toolsets()` was imported but never called. The `enforced` + `default_enabled` vs `optional` split was designed for exactly this purpose — optional tools should be lazy-loaded, not included by default.
- **The `request_tools` pattern mirrors `request_mcp_access`** — both are meta-tools where the agent starts lean and requests capabilities on demand. The gateway tracks per-session grants. Same architecture, same approval model potential.

## Windows / Desktop Packaging

- **`sys.executable` IS the frozen `.exe` — spawning it re-launches the launcher** — In a PyInstaller bundle, `sys.executable` is `Logos.exe`, not a Python interpreter. Calling `subprocess.Popen([sys.executable, "-m", "gateway.run", ...])` re-runs the full launcher (new browser window, new tray icon) instead of the gateway. Fix: add an `--agent-mode` flag the launcher detects early and handles without any UI; pass the port via `HERMES_PORT` env var instead of a `--port` CLI arg the subprocess won't parse.
- **`sys.stdout` and `sys.stderr` are `None` on Windows GUI builds** — Windows desktop apps have no console, so Python sets both streams to `None`. Any `print()` call raises `AttributeError: 'NoneType' object has no attribute 'write'`. Must redirect to `io.TextIOWrapper(open(os.devnull, "wb"))` at process startup *before* any imported code can print — not just inside the agent wrapper.
- **`CREATE_DETACHED_PROCESS` survives parent exit — must scan WMIC on quit** — Agent instances spawned with this flag are fully detached from the parent's job object. `taskkill /F /PID` via `instances.json` is the primary cleanup, but if a process was spawned between `Popen` and `_save_instances` (race on quit), the PID is never recorded. Scan `wmic process where "name='Logos.exe' and commandline like '%--agent-mode%'" get ProcessId /format:list` as a fallback to catch orphans.
- **Child loggers bypass the root logger's filter chain** — Python's `callHandlers` walks up the logger hierarchy calling each logger's *handlers*, not each logger's *filters*. Filters on a parent/root logger are only run for records logged directly to that logger. Always add `addFilter()` to the *handler* (not the root logger) when the format string depends on an injected field like `session_id`.
- **LM Studio "cookie auth" is separate from "API key auth"** — Disabling the API key toggle in LM Studio's Local Server settings does not disable cookie-based authentication. Server-side requests (no browser cookie) fail with `No cookie auth credentials found` even with no key configured. Both modes are controlled by a single "Enable Auth" toggle — ensure it is fully off and restart LM Studio.

## Security

- **CSRF tokens required on all state-changing requests** — cookie auth alone is not enough. Every POST/PATCH needs a `X-CSRF-Token` header validated server-side.
- **`stringData` in K8s Secrets means plaintext in the YAML** — template files with `REPLACE_WITH_*` values are safe to commit; filled-in files are not. Seal Secrets or SOPS before committing real values.
- **`npx -y` MCP server invocations are a supply chain risk** — unpinned packages download and run at runtime with user permissions. Always pin to a version.
