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
- **VRAM state between tests must be explicit** — LM Studio: `POST /api/v1/models/unload` with `{"instance_id": model_id}`. Ollama: `POST /api/generate` with `{"model": model_id, "keep_alive": 0, "prompt": ""}`. Without this, each model loads on top of the previous one and throughput measurements for later models are degraded.
- **`x-transition` on Alpine `x-show` can interfere with `max-height` overflow** — transitions using opacity/scale don't affect max-height, but the interaction with certain CSS classes can cause unexpected layout. Use explicit inline `style="max-height:Xrem;overflow-y:auto"` rather than Tailwind's `max-h-*` on transitioning elements.

## Security

- **CSRF tokens required on all state-changing requests** — cookie auth alone is not enough. Every POST/PATCH needs a `X-CSRF-Token` header validated server-side.
- **`stringData` in K8s Secrets means plaintext in the YAML** — template files with `REPLACE_WITH_*` values are safe to commit; filled-in files are not. Seal Secrets or SOPS before committing real values.
- **`npx -y` MCP server invocations are a supply chain risk** — unpinned packages download and run at runtime with user permissions. Always pin to a version.
