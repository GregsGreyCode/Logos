# Outstanding Features

Unimplemented features and requirements identified from planning docs, design critiques, and code TODOs. Ordered roughly by impact. Use this as a backlog reference.

---

## Setup Wizard

### Frontier-first onboarding track
**Source:** `docs/project/onboarding_plan.md`
The wizard's Step 0 shows a "Frontier-first" track (Anthropic, OpenAI, OpenRouter) but it is disabled with a "Coming soon" label. The entire track — cloud model configuration, frontier voice providers, Honcho opt-in, cloud vision — needs to be built. Local-first is the only working track.

### `hermes doctor` system check step
**Source:** `docs/project/onboarding_plan.md`
The plan calls for a system check step early in the wizard that surfaces blockers (missing env vars, unreachable endpoints, insufficient disk) before the user spends time on model/platform config. `logos_cli/doctor.py` exists as a CLI command but is not integrated into the `/setup` wizard flow.

### Messaging platform setup flow
**Source:** `docs/project/onboarding_plan.md`
Telegram, Discord, Slack, and other platform tokens are configured entirely out-of-band via env vars. The wizard should include a post-completion flow for connecting at least one messaging platform with guided token entry and connection testing.

### Tool/policy defaults by track
**Source:** `docs/project/onboarding_plan.md`
After track selection, the wizard should apply a config preset — which toolsets are enabled, which policy rules are active — based on whether the user chose local-first or frontier-first. Currently all users get identical defaults.

### Honcho opt-in step
**Source:** `docs/project/onboarding_plan.md`
Honcho AI memory (`honcho_integration/`) is implemented but has no wizard UI. There should be a privacy-gated opt-in step that explains what data Honcho stores and lets the user enable or skip it during setup.

### Voice provider selection
**Source:** `docs/project/onboarding_plan.md`
A wizard step for choosing speech-to-text provider (local `faster-whisper` vs cloud Groq/OpenAI Whisper) is planned but not built.

### Additional agent runtimes in wizard
**Source:** `docs/project/onboarding_plan.md`, `gateway/http_api.py`
The wizard's Step 3 runtime picker has a disabled "More agents — Coming soon" placeholder. Only Hermes is selectable. Adding support for selecting other agent runtimes here is deferred.

---

## Executor / Instance Management

*LocalProcessExecutor and KubernetesExecutor are fully implemented — moved to Recently Completed.*

---

## MCP

### MCP servers as k8s pods (HTTP transport)
**Source:** design discussion
When running Logos on Kubernetes, the cleanest architecture for MCP servers is to run each as a separate pod with HTTP/StreamableHTTP transport, connected to Logos via internal cluster DNS. The existing k8s executor infrastructure could be extended to manage MCP server pod lifecycle (spawn, health-check, teardown) the same way it manages agent instance pods. No implementation exists yet.

---

## Evolution System

### Auto-update cron job when schedule changes
**Source:** `AGENTS.md`
When evolution settings are saved with a new `schedule`, the old cron job is not automatically cancelled and replaced. This is documented as manual-only. `gateway/evolution_handlers.py` needs to cancel the stored `cron_job_id` and register a new job when `schedule` changes.

---

## Developer / Ops Tooling

### ~~Smoke test script~~ RESOLVED
**Source:** `docs/project/BUILD_AND_DEPLOY.md`
Implemented as `scripts/smoke-test.sh`. Tests: `/health`, `/healthz`, `/health/ready`, `/login`, `/status`, `/souls`, bad-creds rejection, unauthenticated instance list. Pass/fail exit code for CI.

### Canary → production promotion script
**Source:** `docs/project/BUILD_AND_DEPLOY.md`
Promoting from canary to production requires manually re-tagging the image and applying the production deployment manifest. A `scripts/promote.sh` that handles re-tagging and rolling restart would reduce deploy risk.

### ~~Dynamic Ollama model catalog~~ RESOLVED
**Source:** `docs/project/CRITIQUE.md`
Extracted hardcoded model array from `setup.html` into `gateway/model_catalog.yaml`. Served via `GET /api/model-catalog` endpoint. Setup wizard fetches dynamically at init with empty-array fallback. New models can be added by editing the YAML file — no code change needed.

### Network-accessible `logos` CLI
**Source:** `docs/project/CRITIQUE.md`
A `logos-cli` pip package that connects to a remote Logos gateway over HTTP/SSE — equivalent to using the web UI but from a terminal anywhere on the network. The gateway already has the streaming `/chat` and `/status` endpoints. No client package exists.

### `hermes setup --vscode` one-command ACP editor setup
**Source:** `docs/project/CRITIQUE.md`
The ACP adapter is implemented and documented in `docs/acp-setup.md`, but the setup is entirely manual. A `--vscode` flag on `hermes setup` that writes the ACP server URL into the user's VS Code settings JSON would reduce friction significantly.

---

## Security

### Handoff tool: real policy enforcement via toolset filtering
**Source:** `docs/project/CRITIQUE.md`, `tools/handoff_tool.py`
`policy_scope="restricted"` and `"read_only"` are enforced only by appending an instruction to the subagent's system prompt — a polite request, not a hard constraint. The actual toolset passed to the child agent is not filtered, so a restricted agent still has access to write tools. Real enforcement requires filtering the toolset at spawn time.

### ~~Kubernetes NetworkPolicy~~ RESOLVED
**Source:** `docs/project/CRITIQUE.md`
NetworkPolicy manifest exists at `k8s/16-network-policy.yaml` covering both `logos` and `hermes` namespaces. Restricts egress to DNS, HTTPS, and local inference ports. Apply with `kubectl apply -f k8s/16-network-policy.yaml`.

### Windows code signing
**Source:** `.github/workflows/build-windows.yml`, `SECURITY.md`
Installers ship unsigned. Identity validation for code signing certificates is not available to the project in the UK at this time. SmartScreen warns all Windows users on first run. Deferred until a viable signing path exists.

---

## Web Tools

### Page persistence and search
**Source:** `tools/web_tools.py` (explicit TODOs)
Web tools fetch and extract pages on demand but store nothing. Three TODOs at the top of the module: store scraped pages, search over stored pages, and expose a tool to list what has been saved. An accumulated knowledge base from web research would make repeated queries far more efficient.

---

## Honcho Integration

### Hermes-side Honcho enhancements
**Source:** `docs/honcho-integration-spec.md`
The integration spec documents several patterns from `openclaw-honcho` that Hermes should adopt: `lastSavedIndex` deduplication for message writes, platform metadata stripping before storage, multi-agent parent observer hierarchy, `peerPerspective` on `context()` calls, and tiered tool surfaces. None are implemented in `honcho_integration/`.

---

## Codebase Health

### Root-level file cleanup
**Source:** `docs/project/REPO_STRUCTURE.md`
The following files are listed in the cleanup backlog and are still at repo root rather than in an appropriate package: `batch_runner.py`, `mini_swe_runner.py`, `rl_cli.py`, `metrics.py`, `runs.py`, `utils.py`, `minisweagent_path.py`.

### Import migration: `hermes_*` shims → `core.*`
**Source:** `docs/project/REPO_STRUCTURE.md`, `AGENTS.md`
Multiple production files still import from root-level compatibility shims (`hermes_state`, `hermes_constants`, `hermes_time`) rather than `core.*`. Known sites include `gateway/mirror.py`, `gateway/session.py`, `gateway/run.py`, `metrics.py`, `tools/code_execution_tool.py`, `tools/mcp_tool.py`, `tools/session_search_tool.py`, `cli.py`. In progress but not complete.

---

## Recently Completed (2026-04-03)

### DockerSandboxExecutor test coverage and file locking
**Source:** `docs/project/priority_issues.md` Issue #1
Added cross-platform file locking to `gateway/executors/docker.py` to prevent race conditions on concurrent spawns. Added 32 unit tests covering spawn, list, delete, port allocation, container detection, headroom, and lock verification. Docker executor is no longer zero-coverage.

### Multi-instance agent support
**Source:** `docs/project/MULTI_AGENT_MEMORY.md` Phase 1
Users can now spawn multiple named agent instances. Each instance gets an `instance_label` (defaults to soul slug), producing unique k8s names like `hermes-greg-researcher`. Per-user instance limit of 5. Each agent has isolated storage via per-instance `HERMES_HOME`.

### Per-agent knowledge base (RAG)
**Source:** `docs/project/MULTI_AGENT_MEMORY.md` Phase 2
Each agent instance has a persistent knowledge base with semantic search. Documents are chunked, embedded via Ollama (`nomic-embed-text`), and stored as JSONL. Search uses in-process numpy cosine similarity. Three tools: `knowledge_ingest`, `knowledge_search`, `knowledge_manage` in the `knowledge` toolset.

### Instance management UI and API
**Source:** `docs/project/MULTI_AGENT_MEMORY.md` Phase 3
REST API endpoints for reading/writing agent memory (`MEMORY.md`, `USER.md`, `bug_notes.md`), knowledge base CRUD, and semantic search preview. Web UI inspector panel with tabs for Memory, User Profile, Knowledge (with ingest + search), and Bug Notes. Accessible via "inspect" button on each instance card.

### Session auto-ingest to knowledge base
**Source:** `docs/project/MULTI_AGENT_MEMORY.md` Phase 2e
When `knowledge.auto_ingest_sessions` is enabled in config, agent session transcripts are automatically chunked, embedded, and ingested into the agent's knowledge base when the session expires. Only substantive assistant responses (>50 chars) are kept. Gated by minimum message count (`auto_ingest_min_messages`, default 8).

### LocalProcessExecutor and KubernetesExecutor — fully implemented
**Source:** `docs/project/WINDOWS_DESKTOP.md`
Both executors are complete with spawn, list, delete, get_headroom, and get_resources. LocalProcessExecutor has full unit test coverage. KubernetesExecutor uses `config.name` from the API layer. The outstanding features doc entries were stale.

### Benchmark redesign — native LM Studio API
**Source:** `docs/project/BENCHMARK_REDESIGN.md`
Replaced the 12-call legacy benchmark with 2-3 calls using LM Studio's native `/api/v1/chat` (returns tok/s and TTFT in response stats) and `/api/v1/models` (returns type, size_bytes, max_context_length, tool_use, vision). Models filtered by real metadata instead of name heuristics. Combined eval prompt tests 6 capabilities in a single call. Falls back to OpenAI-compatible path for non-LM Studio servers.

### Memory transfer / fork agent
**Source:** `docs/project/MULTI_AGENT_MEMORY.md` Phase 3c
Fork an agent's memory and/or knowledge base to another instance via `POST /instances/{name}/fork`. Copies `MEMORY.md` and the entire `knowledge/` directory. Available from the inspector panel's Memory tab. Source instance is not modified.
