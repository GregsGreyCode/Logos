# Env Var Refactor: `HERMES_*` → `LOGOS_*` / `AGENT_*`

## Why this matters now

Logos started life as a launcher for one agent: Hermes. Every runtime knob was named `HERMES_*` because there was nothing else to configure. That assumption no longer holds — the gateway now hosts many named agents (Hermes, Jay, Atlas, Mr Legend, …) and is being designed to host many more. `HERMES_*` is wrong for two distinct reasons:

1. **Coupling**: variables that describe the *gateway* (port, JWT secret, runtime mode) carry the name of *one specific agent*. New contributors reasonably ask "is this Hermes-specific?" and have to dig in to find out it isn't.
2. **Conflict**: variables that describe *one agent's behaviour* (model, soul, timeouts) are stored in the gateway's process env as a single global value. When two agents need different models, this breaks. The OpenShell sandbox flow already partially routes around this by uploading per-agent `instance-config.json`, but the in-process code paths still read `HERMES_MODEL` from `os.environ` as if there's only one.

So this isn't just a sed-and-pray rename. The work is:

- **Mechanical part**: pick the correct new name (`LOGOS_*` or `AGENT_*`) for each variable, rename consistently across Python, JS, YAML, shell, docs.
- **Architectural part**: for the variables where there really IS a per-agent value, change the *data flow* — they should come from the agent record (and travel with the dispatch task) rather than from the gateway's process env.

The good news: a precedent already exists. `HERMES_HOME` is being migrated to `LOGOS_HOME` with a clean dual-fallback pattern (`os.getenv("LOGOS_HOME") or os.getenv("HERMES_HOME") or default`), and `HERMES_PORT` has the same shim in `gateway/run.py:4936`. We can copy that pattern wholesale.

## Audit summary

73 distinct `HERMES_*` env var names across ~125 files. Full audit lives at the bottom of this doc as Appendix A. The classification breakdown is:

| Class | Count | New prefix | Owns it |
|-------|------:|------------|---------|
| Gateway/platform-level | ~32 | `LOGOS_*` | The Logos gateway process. Same for every agent that runs on it. |
| Per-agent runtime | ~27 | `AGENT_*` | The individual agent's sandbox. Different per agent. |
| Already migrating (dual support) | 2 | `LOGOS_HOME`, `LOGOS_PORT` | — |
| Deprecated (replace with config.yaml) | 2 | — | `HERMES_TOOL_PROGRESS{,_MODE}` |
| Not env vars at all (Python constants) | 3 | — | `HERMES_VERSION`, `HERMES_AGENT_LOGO`, `HERMES_CADUCEUS` — these are ASCII strings/version constants. **Don't touch.** |
| K8s resource names + image tags | (many) | — | `hermes-secret`, `hermes-canary`, `hermes-sandbox`, etc. **Don't touch — these are identifiers, not configuration.** |

## Naming rule (the only one)

> **A variable is `LOGOS_*` if its value is the same regardless of which agent is running. A variable is `AGENT_*` if its value can differ between Jay and Hermes.**

If you can't decide, ask: "if I have two agents on the same gateway with different values for this, does anything break?" Yes → `AGENT_*`. No → `LOGOS_*`.

A few specific calls from the audit where the answer wasn't obvious:

- **`HERMES_RUNTIME_MODE` / `LOGOS_RUNTIME_MODE`** — **removed.** OpenShell is now the only supported sandbox runtime; there is nothing to select. The env var is still accepted silently but has no effect.
- **`HERMES_INTERACTIVE` / `HERMES_GATEWAY_SESSION`** → **`LOGOS_INTERACTIVE` / `LOGOS_GATEWAY_SESSION`**. These describe the *invocation environment* of the Logos process itself (TTY vs gateway-spawned), not the agent. The audit suggested `AGENT_GATEWAY_SESSION` for the latter — disagree. The gateway-vs-CLI distinction is process-level.
- **`HERMES_REDACT_SECRETS`** → **`LOGOS_REDACT_SECRETS`**. Security policy belongs to the platform, not to individual agents. We don't want a "noisy logging agent" toggle.
- **`HERMES_HUMAN_DELAY_*`** → **`LOGOS_HUMAN_DELAY_*`**. Messaging-platform pacing applies to every reply the gateway sends, not per-agent.
- **`HERMES_SESSION_*`** (PLATFORM, CHAT_ID, CHAT_NAME) → **`AGENT_SESSION_*`**. These are injected per-dispatch into the agent's sandbox env so the `send_message` tool knows where to reply. They are by definition per-run / per-agent.
- **`HERMES_RPC_PORT` / `HERMES_RPC_SOCKET`** → **`AGENT_RPC_PORT` / `AGENT_RPC_SOCKET`**. Per-sandbox IPC channel for code execution.

## The architectural part

For most `LOGOS_*` renames, the work is purely mechanical: change `HERMES_FOO` to `LOGOS_FOO` everywhere with a fallback. Done.

For the `AGENT_*` renames there's a real conceptual change. Today:

```
gateway process env: HERMES_MODEL=qwen/qwen3.5-9b   ← single global
                            ↓
         executor.spawn() reads HERMES_MODEL via os.getenv
                            ↓
         instance-config.json gets "model": "qwen/qwen3.5-9b" baked in
                            ↓
            sandbox worker reads model from config
```

After:

```
gateway process env: LOGOS_DEFAULT_MODEL=qwen/qwen3.5-9b  ← still global, but it's the *default*
                            ↓
       admin/agents create form: pick model OR leave "auto"
                            ↓
         agent record in DB:  model = "" (auto)  OR  "claude-opus-4.6"
                            ↓
       executor.spawn(agent_record):
         resolved_model = agent.model or LOGOS_DEFAULT_MODEL
                            ↓
       sandbox env: AGENT_MODEL=claude-opus-4.6  ← set inside the sandbox
                            ↓
       sandbox worker reads AGENT_MODEL  (no longer reads HERMES_MODEL)
```

The key shift: `AGENT_*` variables only ever exist *inside a spawned sandbox*. The gateway's own process env never has `AGENT_MODEL` set, because the gateway itself doesn't run any single agent. If gateway code needs the "current default model", it reads `LOGOS_DEFAULT_MODEL`. If sandbox code needs "my model", it reads `AGENT_MODEL` from the env that was injected when the sandbox started.

This rules out one tempting shortcut: we cannot just `AGENT_MODEL = HERMES_MODEL` at the top of the gateway. They're not the same thing. A gateway with `LOGOS_DEFAULT_MODEL=qwen` should still happily host an agent whose `AGENT_MODEL=claude-opus`.

Same logic applies to `HERMES_SERVER_TYPE`, `HERMES_INFERENCE_PROVIDER`, `HERMES_MAX_ITERATIONS`, `HERMES_API_TIMEOUT`, etc. — they all become per-spawn values pulled from the agent record, defaulted from `LOGOS_DEFAULT_*` if the agent didn't override.

## Phasing

This is too big for a single commit. Three phases:

### Phase 1 — `LOGOS_*` (gateway-scope, mechanical)

Pure rename, dual-fallback for one release. ~32 variables.

Steps:

1. **Add `LOGOS_*` reads with `HERMES_*` fallback** at every read site. Example:

   ```python
   port = int(
       os.getenv("LOGOS_PORT")
       or os.getenv("HERMES_PORT")
       or "8091"
   )
   ```

   This is non-breaking — a deployment with only `HERMES_PORT` set keeps working.

2. **Update every WRITE site** (`os.environ["HERMES_FOO"] = ...`, `_cfg["HERMES_FOO"] = ...`, `export HERMES_FOO=...`) to write `LOGOS_FOO` instead. Old readers still see the value because Phase 1 readers check both.

3. **Update config files** to use the new names everywhere fresh setups will see them:
   - `.env.example`
   - `k8s/01-configmap-env.yaml`, `k8s/dev/01-configmap-env.yaml`
   - `k8s/06-deployment.yaml`, `k8s/10-logos-canary-deployment.yaml`, `k8s/13-logos-setup-test-deployment.yaml`, `k8s/dev/06-deployment.yaml`
   - `docker-compose.k3s.yml`
   - `docker/Dockerfile.docker-sandbox` ENV directives
   - `docker/entrypoint-hermes.sh` shell exports
   - `gateway/setup_handlers.py` — when the wizard writes `~/.logos/config.yaml`, write `LOGOS_*` keys.
   - `gateway/run.py` lines 100-200 — the config.yaml → env bridge writes to `os.environ` with the legacy names; switch to `LOGOS_*`.

4. **Documentation**: update `README.md`, `k8s/README.md`, anything in `docs/` that mentions the old vars. Add a one-paragraph migration note pointing at this doc.

5. **Test**:
   - Spin up a fresh gateway with only `LOGOS_*` set → boots, /status returns the right model, sandbox dispatch works.
   - Spin up a gateway with only `HERMES_*` set (legacy install) → boots and works identically. Watch for a deprecation warning in logs.
   - Spin up with both set → `LOGOS_*` wins (new precedence).

6. **Add a one-time deprecation warning** in `gateway/run.py` startup: scan `os.environ` for any `HERMES_*` key that has a `LOGOS_*` equivalent and log `WARNING: HERMES_FOO is deprecated, use LOGOS_FOO instead`. This is the only feedback users get; make it noisy but only fire once per process.

7. **Don't push to main yet** — let Phase 1 bake on `develop` for at least one full setup-test cycle before merging.

### Phase 2 — `AGENT_*` (per-agent, architectural)

This is the real work. ~27 variables. Two sub-phases:

**Phase 2a — agent record fields**

For every per-agent variable, decide whether the agent record in `auth.db` should grow a column to store it explicitly. The current schema has:

```
agents (id, name, soul_slug, model, description, creator_id, shared, toolsets, char_index, created_at, updated_at)
```

Most `AGENT_*` variables don't need a column — they should default from `LOGOS_DEFAULT_*` and only override at spawn time when the user explicitly configures them. But the high-traffic ones probably do:

| Variable | New column? | Reason |
|----------|------------|--------|
| `HERMES_MODEL` | already exists (`agents.model`) | — |
| `HERMES_SERVER_TYPE` | **add `agents.server_type`** | model + server_type need to match; users will want to lock both. |
| `HERMES_INFERENCE_PROVIDER` | **add `agents.provider`** | derived from cloud_providers join; column makes the query trivial. |
| `HERMES_MAX_ITERATIONS` | no | rare override; pass from request body if needed. |
| `HERMES_TIMEZONE` | no | per-user, not per-agent. Belongs on `users` table eventually, fall back to `LOGOS_DEFAULT_TIMEZONE` for now. |
| `HERMES_REASONING_EFFORT` | no | per-call override, set via UI per chat. |
| `HERMES_AGENT_TIMEOUT` | no | platform default is fine. |
| `HERMES_API_TIMEOUT` | no | platform default. |
| `HERMES_DUMP_REQUESTS` / `HERMES_DUMP_REQUEST_STDOUT` | no | debug flags, gateway-wide. → `LOGOS_*` actually, not `AGENT_*`. (Audit had these wrong.) |
| `HERMES_RPC_PORT` / `HERMES_RPC_SOCKET` | no | dynamically allocated per code-execution sandbox. |
| `HERMES_SESSION_*` | no | injected per-dispatch from session context. |
| `HERMES_YOLO_MODE` | no | per-call override. |
| `HERMES_PREFILL_MESSAGES_FILE` | no | per-call override. |
| `HERMES_EPHEMERAL_SYSTEM_PROMPT` | no | per-call override. |

So the only schema change is two new columns: `agents.server_type` and `agents.provider`. Both nullable (NULL means "use the platform default"). Migration is a single `ALTER TABLE` with backfill from current `HERMES_SERVER_TYPE` env.

**Phase 2b — dispatch-time env injection**

The `OpenShellExecutor.spawn()` flow currently builds `instance_config.json` with a few keys (`worker_id`, `gateway_url`, `soul`, `toolsets`, `model`). Extend it with everything an `AGENT_*` variable used to provide:

```python
instance_config = {
    "worker_id":      worker_id,
    "instance_name":  config.name,
    "gateway_url":    f"http://host.openshell.internal:{LOGOS_PORT}",
    "soul":           config.soul_name or "general",
    "toolsets":       config.toolsets or [],
    "agent_env": {
        "AGENT_MODEL":              resolved_model,
        "AGENT_SERVER_TYPE":        resolved_server_type,
        "AGENT_INFERENCE_PROVIDER": resolved_provider,
        "AGENT_TIMEZONE":           resolved_tz,
        "AGENT_MAX_ITERATIONS":     resolved_iters,
        # …etc
    },
}
```

The sandbox `entrypoint.sh` reads `agent_env` from the config file and exports each key into the worker's process env *before* `python sandbox_worker.py` starts. From the worker's perspective, every `AGENT_*` variable is just a normal env var — it never reads from `instance_config.json` directly except for orchestration metadata.

This has the nice property that **Tools that already read `HERMES_RPC_PORT` etc. need only a one-line rename**, because the variable is still in `os.environ` — just under a new name.

For the gateway-side resolution helper, add:

```python
# gateway/agent_env.py  (new file)
def build_agent_env(agent_record: dict) -> dict[str, str]:
    """Resolve every AGENT_* env var for a sandbox spawn from the agent
    record + LOGOS_DEFAULT_* fallbacks. Returns a dict ready to upload
    via OpenShell's instance-config.json."""
    return {
        "AGENT_MODEL":              agent_record.get("model")        or os.getenv("LOGOS_DEFAULT_MODEL", ""),
        "AGENT_SERVER_TYPE":        agent_record.get("server_type")  or os.getenv("LOGOS_DEFAULT_SERVER_TYPE", ""),
        "AGENT_INFERENCE_PROVIDER": agent_record.get("provider")     or os.getenv("LOGOS_DEFAULT_INFERENCE_PROVIDER", ""),
        "AGENT_TIMEZONE":           os.getenv("LOGOS_DEFAULT_TIMEZONE", "UTC"),
        "AGENT_MAX_ITERATIONS":     str(os.getenv("LOGOS_DEFAULT_MAX_ITERATIONS", "90")),
        # …
    }
```

`OpenShellExecutor.spawn()` calls this and embeds the result in the upload. The existing one-off `resolved_model` block in `executors/openshell.py` (added this session) collapses into this helper.

**Phase 2c — sandbox worker**

`docker/sandbox_worker.py` currently reads `os.environ.get("HERMES_MODEL")`. After Phase 2b, the entrypoint script exports `AGENT_MODEL` so `os.environ.get("AGENT_MODEL")` works. One-line rename. Same for `tools/code_execution_tool.py` (RPC vars) and any other tool the worker invokes.

**Phase 2d — backwards-compat shim inside the sandbox**

The legacy `HERMES_*` reads in tools/agent code can stay for one release as fallbacks, same dual-pattern as Phase 1. After the deprecation window they're removed.

### Phase 3 — Cleanup (one release later)

1. Remove the `HERMES_*` fallback chains everywhere — readers only check `LOGOS_*` / `AGENT_*`.
2. Remove `HERMES_TOOL_PROGRESS{,_MODE}` deprecated bridges entirely.
3. Update setup wizard to scrub legacy `HERMES_*` keys from existing `~/.logos/config.yaml` files on next run, replacing them with the new keys.
4. Drop the deprecation warning from Phase 1 step 6 (no longer triggers).
5. Final sweep with `git grep "HERMES_"` and ensure every remaining hit is a non-env-var usage (k8s resource name, image tag, Python module, brand reference). Annotate any survivors with a comment explaining why they're allowed.

## Things explicitly OUT of scope

To keep this from sprawling:

- **Renaming Python modules**: `agents/hermes/`, `logos_cli/hermes_launcher.py`. These have nothing to do with env vars, and renaming Python modules has its own blast radius.
- **Renaming K8s resources**: `hermes-secret`, `hermes-canary`, `hermes-config`, `hermes-agent-pods` (NetworkPolicy selector). These are stable resource identifiers; renaming them is a cluster migration, not an env var rename.
- **Renaming Docker images**: `hermes-sandbox` is a container image name and a build target. Touch later if we want, not now.
- **Brand strings**: "the Hermes agent", "Hermes default agent", error messages saying "Hermes". These are user-facing references to the *first agent* and are correct as-is. They will become alongside other agents' names ("the Jay agent") naturally.
- **`HERMES_VERSION`, `HERMES_AGENT_LOGO`, `HERMES_CADUCEUS`**: not env vars, Python constants. Out of scope for this work — if anyone wants to rename them later (`_AGENT_LOGO_ASCII` etc.), that's a separate `refactor:` commit.

## Risks & mitigations

| Risk | Mitigation |
|------|-----------|
| Existing user installs have `~/.logos/.env` or `~/.logos/config.yaml` with `HERMES_*` keys | Phase 1 dual-fallback keeps them working transparently. Phase 1 deprecation warning tells them to update. Phase 3 setup wizard rewrites keys on next run. |
| Production k8s deployments break on rolling update | Phase 1 ConfigMaps still write old names; new code reads both. Operators migrate at their own pace. |
| External tooling (CI, ops scripts, dotfiles) sets `HERMES_*` | Same fallback chain. We log a deprecation warning so operators see it in their pipelines. |
| Tests assume `HERMES_*` is in `os.environ` | Audit `tests/` for direct `os.environ["HERMES_FOO"] = ...` and `monkeypatch.setenv("HERMES_FOO", ...)`. Update all of them in Phase 1 to use the new names. The fallback chain in production code means tests can use *either*, but consistency is better. |
| Sandbox image rebuild needed for Phase 2c worker rename | Bump the sandbox image tag. The resurrect-on-startup pass already handles "agent record exists, sandbox does not" — operators can trigger a re-spawn by deleting and re-creating the OpenShell sandbox via the dashboard. |
| `HERMES_JWT_SECRET` is security-critical and has no fallback today | Phase 1 reads `LOGOS_JWT_SECRET or HERMES_JWT_SECRET`. The persistent secret file at `~/.logos/.jwt_secret` (`gateway/http_api.py:2879-2886`) is the source of truth anyway — env vars are bootstrap-only. Tests should rotate cleanly. |

## Test plan

These tests should be run at the end of each phase against a fresh gateway:

1. **Fresh setup wizard**: nothing in `~/.logos/`. Run setup, complete it, verify config.yaml has `LOGOS_*` keys (not `HERMES_*`). Verify gateway starts on `LOGOS_PORT=8091`.
2. **Legacy install upgrade**: copy a Phase-0-era `~/.logos/.env` with `HERMES_*` keys into a fresh home, start gateway, verify it boots, verify deprecation warnings fire once per legacy var.
3. **Mixed**: both old and new keys present. New wins.
4. **Per-agent model override**: create two agents — Jay with model `qwen/qwen3.5-9b`, Atlas with model `claude-opus-4.6`. Spawn both. Verify each sandbox's `AGENT_MODEL` is correct. Send a chat to each, verify the right model responds.
5. **Auto model**: create an agent with model="", verify it picks up `LOGOS_DEFAULT_MODEL`.
6. **Sandbox env injection**: shell into a running sandbox via `openshell sandbox exec hermes-jay -- env | grep AGENT_`, verify all expected `AGENT_*` are set.
7. **K8s deployment**: apply `k8s/06-deployment.yaml` to a test cluster, verify gateway pod boots and health-checks.
8. **Tool that reads `HERMES_RPC_PORT`**: invoke code execution tool, verify the new `AGENT_RPC_PORT` flow works on Linux.

## Implementation ordering checklist

- [ ] **P1.1** Add `LOGOS_*` read sites with `HERMES_*` fallback (~32 vars across ~80 files)
- [ ] **P1.2** Switch every write site (`os.environ[X]=...`, config.yaml writes, exports) to `LOGOS_*`
- [ ] **P1.3** Update `.env.example`, k8s manifests, docker-compose, Dockerfiles, entrypoint scripts
- [ ] **P1.4** Update `README.md`, `k8s/README.md`, docs
- [ ] **P1.5** Add one-time deprecation warning loop in gateway startup
- [ ] **P1.6** Update tests that hardcode `HERMES_*`
- [ ] **P1.7** Full setup-and-test cycle, fix anything that breaks
- [ ] **P1.8** Commit `feat: rename gateway env vars HERMES_* → LOGOS_* (dual-fallback)`
- [ ] **P2.1** Schema migration: add `agents.server_type`, `agents.provider` columns
- [ ] **P2.2** New `gateway/agent_env.py` helper with `build_agent_env(agent_record)`
- [ ] **P2.3** Update `OpenShellExecutor.spawn()` to embed `agent_env` dict in instance-config.json
- [ ] **P2.4** Update `docker/entrypoint-hermes.sh` to export every key from `agent_env`
- [ ] **P2.5** Rename `HERMES_*` reads → `AGENT_*` in `docker/sandbox_worker.py`, `tools/`, `agents/hermes/agent.py`
- [ ] **P2.6** Add per-agent overrides to the create-agent UI form (model already exists; add server_type, provider)
- [ ] **P2.7** Test: two agents with different models on the same gateway
- [ ] **P2.8** Commit `feat: per-agent AGENT_* env vars via dispatch-time injection`
- [ ] **P3.1** Remove all `HERMES_*` fallback chains (Phase 1 + Phase 2)
- [ ] **P3.2** Remove `HERMES_TOOL_PROGRESS{,_MODE}` deprecated handling
- [ ] **P3.3** Setup wizard scrubs legacy keys from existing config.yaml
- [ ] **P3.4** Final `git grep HERMES_` review, annotate any allowed survivors
- [ ] **P3.5** Commit `chore: drop HERMES_* env var compatibility shims`

## Appendix A — full audit table

(73 vars; classification column is the recommended target name. See the audit report for line numbers and read/write sites.)

### Gateway → `LOGOS_*`

| Old | New | Notes |
|-----|-----|-------|
| `HERMES_JWT_SECRET` | `LOGOS_JWT_SECRET` | persistent file at `~/.logos/.jwt_secret` is source of truth; env is bootstrap |
| `HERMES_COOKIE_SECURE` | `LOGOS_COOKIE_SECURE` | HTTPS-behind-proxy flag |
| `HERMES_INTERNAL_TOKEN` | `LOGOS_INTERNAL_TOKEN` | inter-service auth |
| `HERMES_INSTANCE_NAME` | `LOGOS_INSTANCE_NAME` | display name in UI |
| `HERMES_IS_CANARY` | `LOGOS_IS_CANARY` | canary indicator |
| `HERMES_PORT` | `LOGOS_PORT` | already has fallback in `run.py:4936` |
| `HERMES_MCP_PORT` | `LOGOS_MCP_PORT` | in-gateway MCP server port |
| `HERMES_LOG_LEVEL` | `LOGOS_LOG_LEVEL` | log verbosity |
| `HERMES_ADMIN_EMAIL` | `LOGOS_ADMIN_EMAIL` | first-run admin seed |
| `HERMES_ADMIN_PASSWORD` | `LOGOS_ADMIN_PASSWORD` | first-run admin seed |
| `HERMES_ADMIN_NAME` | `LOGOS_ADMIN_NAME` | first-run admin seed |
| `HERMES_WIPE_ON_START` | `LOGOS_WIPE_ON_START` | test/setup-test deployment |
| `HERMES_WORKSPACE_TTL_HOURS` | `LOGOS_WORKSPACE_TTL_HOURS` | ephemeral workspace TTL |
| `HERMES_WORKSPACE_CLEANUP_INTERVAL_HOURS` | `LOGOS_WORKSPACE_CLEANUP_INTERVAL_HOURS` | cleanup scan interval |
| `HERMES_REPO_ROOTS` | `LOGOS_REPO_ROOTS` | filesystem security policy |
| `HERMES_GATEWAY_MCP` | `LOGOS_GATEWAY_MCP` | internal flag |
| `HERMES_QUIET` | `LOGOS_QUIET` | output suppression |
| `HERMES_INTERACTIVE` | `LOGOS_INTERACTIVE` | TTY-present flag |
| `HERMES_EXEC_ASK` | `LOGOS_EXEC_ASK` | force approval prompts |
| `HERMES_GATEWAY_SESSION` | `LOGOS_GATEWAY_SESSION` | "process is gateway-spawned" indicator (audit suggested AGENT_; reclassified) |
| `HERMES_REDACT_SECRETS` | `LOGOS_REDACT_SECRETS` | log redaction policy |
| `HERMES_HUMAN_DELAY_MODE` | `LOGOS_HUMAN_DELAY_MODE` | messaging pacing |
| `HERMES_HUMAN_DELAY_MIN_MS` | `LOGOS_HUMAN_DELAY_MIN_MS` | messaging pacing |
| `HERMES_HUMAN_DELAY_MAX_MS` | `LOGOS_HUMAN_DELAY_MAX_MS` | messaging pacing |
| `HERMES_OAUTH_TRACE` | `LOGOS_OAUTH_TRACE` | auth integration debug |
| `HERMES_CODEX_BASE_URL` | `LOGOS_CODEX_BASE_URL` | auth service URL |
| `HERMES_PORTAL_BASE_URL` | `LOGOS_PORTAL_BASE_URL` | auth service URL |
| `HERMES_CA_BUNDLE` | `LOGOS_CA_BUNDLE` | custom TLS CA |
| `HERMES_DUMP_REQUESTS` | `LOGOS_DUMP_REQUESTS` | gateway-wide debug (audit had as AGENT_; reclassified) |
| `HERMES_DUMP_REQUEST_STDOUT` | `LOGOS_DUMP_REQUEST_STDOUT` | gateway-wide debug |
| `HERMES_HOME` | `LOGOS_HOME` | already migrating; complete the job |

### Per-agent → `AGENT_*`

| Old | New | Default sourced from |
|-----|-----|---------------------|
| `HERMES_MODEL` | `AGENT_MODEL` | `LOGOS_DEFAULT_MODEL` |
| `HERMES_SERVER_TYPE` | `AGENT_SERVER_TYPE` | `LOGOS_DEFAULT_SERVER_TYPE` |
| `HERMES_INFERENCE_PROVIDER` | `AGENT_INFERENCE_PROVIDER` | `LOGOS_DEFAULT_INFERENCE_PROVIDER` |
| `HERMES_MAX_ITERATIONS` | `AGENT_MAX_ITERATIONS` | `LOGOS_DEFAULT_MAX_ITERATIONS` (90) |
| `HERMES_TIMEZONE` | `AGENT_TIMEZONE` | `LOGOS_DEFAULT_TIMEZONE` (UTC) |
| `HERMES_REASONING_EFFORT` | `AGENT_REASONING_EFFORT` | per-call override |
| `HERMES_PREFILL_MESSAGES_FILE` | `AGENT_PREFILL_MESSAGES_FILE` | per-call override |
| `HERMES_EPHEMERAL_SYSTEM_PROMPT` | `AGENT_EPHEMERAL_SYSTEM_PROMPT` | per-call override |
| `HERMES_BACKGROUND_NOTIFICATIONS` | `AGENT_BACKGROUND_NOTIFICATIONS` | platform default |
| `HERMES_NOUS_MIN_KEY_TTL_SECONDS` | `AGENT_NOUS_MIN_KEY_TTL_SECONDS` | platform default (1800) |
| `HERMES_NOUS_TIMEOUT_SECONDS` | `AGENT_NOUS_TIMEOUT_SECONDS` | platform default (15) |
| `HERMES_API_TIMEOUT` | `AGENT_API_TIMEOUT` | platform default (900) |
| `HERMES_AGENT_TIMEOUT` | `AGENT_RUN_TIMEOUT` | rename: "agent agent" was awkward |
| `HERMES_CHECKPOINT_TIMEOUT` | `AGENT_CHECKPOINT_TIMEOUT` | platform default (30) |
| `HERMES_RPC_PORT` | `AGENT_RPC_PORT` | dynamic per code-exec |
| `HERMES_RPC_SOCKET` | `AGENT_RPC_SOCKET` | dynamic per code-exec |
| `HERMES_SESSION_PLATFORM` | `AGENT_SESSION_PLATFORM` | dispatch-time injection |
| `HERMES_SESSION_CHAT_ID` | `AGENT_SESSION_CHAT_ID` | dispatch-time injection |
| `HERMES_SESSION_CHAT_NAME` | `AGENT_SESSION_CHAT_NAME` | dispatch-time injection |
| `HERMES_SESSION_KEY` | `AGENT_SESSION_KEY` | approval cache key |
| `HERMES_YOLO_MODE` | `AGENT_YOLO_MODE` | per-call bypass |
| `HERMES_SPINNER_PAUSE` | `AGENT_SPINNER_PAUSE` | TUI internal |

### Phase out (replace with config.yaml)

| Old | Replace with |
|-----|------|
| `HERMES_TOOL_PROGRESS` | `display.tool_progress` in config.yaml |
| `HERMES_TOOL_PROGRESS_MODE` | `display.tool_progress` in config.yaml |

### Don't touch

- `HERMES_VERSION` — Python version constant
- `HERMES_AGENT_LOGO` — ASCII art constant
- `HERMES_CADUCEUS` — ASCII art constant
- `hermes-secret`, `hermes-config`, `hermes-canary`, `hermes-agent-pods`, `hermes-sandbox`, etc. — k8s/docker resource identifiers
- `agents/hermes/`, `logos_cli/hermes_launcher.py` — Python module paths
- "Hermes" as the name of the first shipped agent — brand reference
