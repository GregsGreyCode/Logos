# NVIDIA OpenShell Integration

**Branch:** `feature/openshell-integration`
**Status:** All 5 phases complete
**Date:** 2026-04-06

---

## Overview

Deeper integration of NVIDIA OpenShell as the canonical sandbox runtime for Logos agent instances. OpenShell provides kernel-level Landlock filesystem isolation, per-binary network egress control, and managed inference routing via Privacy Router — capabilities that Logos's existing Docker sandbox mode lacks.

This work is structured in 5 phases, each independently shippable. Existing deployment modes (local, Docker, k8s) are unaffected.

See also: `docs/migration/logos-openshell-migration.md` for the full architectural rationale.

---

## What Was Already Built (pre-existing)

| Component | File | Notes |
|-----------|------|-------|
| OpenShellExecutor | `gateway/executors/openshell.py` | Full lifecycle: spawn, SSH tunnel, health check, delete, resource tracking |
| Factory registration | `gateway/executors/__init__.py` | `build_executor("openshell")` returns OpenShellExecutor |
| Default egress policy | `gateway/policies/openshell_default.yaml` | Allows local model endpoints + DNS, blocks everything else |
| Setup wizard | `gateway/html/setup.html` + `gateway/setup_handlers.py` | OpenShell is a step-4 option |
| Policy framework | `gateway/auth/policy.py` | 6-dimensional ActionPolicy with merge semantics |
| Sandbox Dockerfiles | `docker/Dockerfile.openshell-sandbox`, `docker/Dockerfile.docker-sandbox` | OCI images for both modes |

---

## Phase 1: Foundation (complete)

### 1A. Unit tests for OpenShellExecutor

**File:** `tests/unit/test_openshell_executor.py` (31 tests)

Covers: factory returns correct executor, protocol conformance, spawn lifecycle (sandbox create args, env var passing, policy application, unhealthy reporting, CLI failure, tunnel failure fallback), port allocation, SSH config parsing, delete (tunnel kill + sandbox destroy), list instance pruning, headroom/resources, CLI-not-found error.

All OpenShell CLI calls are mocked via `unittest.mock.patch` — no openshell binary required.

### 1B. SOUL.md mounting

Both the OpenShell and Docker executors now resolve the soul from `get_soul_registry()`, write `soul.soul_md` to a temp file, and bind-mount it read-only at `/hermes/SOUL.md` inside the sandbox. The env var `HERMES_SOUL_PATH=/hermes/SOUL.md` tells the agent runtime where to find it. Temp files are cleaned up on instance delete.

**Files modified:**
- `gateway/executors/openshell.py` — soul resolution + `--volume` mount + cleanup
- `gateway/executors/docker.py` — same pattern with Docker `-v` flag

This brings parity with the k8s executor, which already mounts SOUL.md via ConfigMap.

### 1C. Sandbox image CI workflow

**File:** `.github/workflows/build-sandbox-image.yml`

Matrix build for both sandbox Dockerfiles. Pushes to:
- `ghcr.io/gregsgreycode/logos-sandbox:{version|latest|canary}` (Docker sandbox)
- `ghcr.io/gregsgreycode/logos-openshell-sandbox:{version|latest|canary}` (OpenShell sandbox)

Same channel logic as the main gateway image: pre-release tags (`v*-rc*`) push `:canary` only, stable tags push versioned + `:latest` + `:canary`.

### 1D. Dockerfile hardening

**File:** `docker/Dockerfile.openshell-sandbox`

- Added non-root `hermes` user (UID 10001), matching the Docker sandbox
- Switched from `pip install` to `uv` for faster dependency resolution
- Added `git`, `ca-certificates` to system deps
- Proper directory structure and permissions for `/hermes` mount point

---

## Phase 2: Policy Presets + Compiler (complete)

### 2A. Four policy preset YAML files

**Files in `gateway/policies/`:**

| Preset | Filesystem | Network | Use case |
|--------|-----------|---------|----------|
| `full_access.yaml` | rw: /sandbox, /tmp, /home | Package registries, GitHub, model endpoints | Dev/exploration |
| `workspace_only.yaml` | rw: /sandbox, /tmp | Model endpoints only | Default |
| `repo_scoped.yaml` | rw: /sandbox/repo, /tmp | inference.local + MCP gateway | Production |
| `read_only.yaml` | rw: /tmp only | inference.local only, no MCP | Audit/review |

All presets enforce:
- Non-root execution (`run_as_user: hermes`)
- SOUL.md read-only protection
- DNS resolution allowed
- Deny-all as the final rule

### 2B. Policy compiler

**File:** `gateway/policies/compiler.py`

`compile_openshell_policy(action_policy, ...)` translates a Logos `ActionPolicy` into a complete OpenShell policy YAML string. The compiler:

1. Selects a base preset from `action_policy.filesystem_policy`
2. Injects dynamic network rules for additional model endpoints
3. Injects MCP gateway rules scoped to `POST /mcp/*` (omitted for READ_ONLY)
4. Injects network allowlist domains
5. For `LOCAL_ONLY` network policy, strips all non-local destinations

Also provides:
- `policy_hash(yaml_str)` — 16-char SHA-256 prefix for STAMP recording
- `validate_openshell_policy(yaml_str)` — structural validation (version, network rules, deny-all terminator)

Deterministic: same inputs always produce the same YAML and hash.

### 2C. Compiler wired into OpenShellExecutor

`InstanceConfig` gained two optional fields: `action_policy` (an `ActionPolicy` object) and `mcp_servers` (list of server names). When `action_policy` is set, `OpenShellExecutor.spawn()` compiles a dynamic policy YAML and applies it instead of the static default. Falls back to `openshell_default.yaml` if compilation fails.

Compiled policy files are written to `~/.hermes/openshell_souls/{name}-policy.yaml` and cleaned up on instance delete.

### 2D. Tests

**File:** `tests/unit/test_policy_compiler.py` (39 tests)

Covers: preset selection for all 4 filesystem levels + unknown fallback, model endpoint injection (single, multiple, empty, empty-host), MCP rules (add, port, host, READ_ONLY omission, empty), LOCAL_ONLY network filtering (strips external, preserves DNS, preserves deny-all), INTERNET_ENABLED keeps everything, network allowlist (add, whitespace strip, empty skip), validation (invalid YAML, missing version, missing network, missing action/destination, no deny-all), determinism (same hash, different hash, hash format), SOUL.md read-only in all presets, non-root user in all presets.

---

## Remaining Phases

### Phase 3: Inference Routing via Privacy Router (complete)

**What was done:**
- Added `_configure_provider()` method to OpenShellExecutor — calls `openshell provider set --type {openai|anthropic} --endpoint {url} --api-key {key}`
- Auto-detects provider type from endpoint URL (anthropic vs openai-compatible)
- When Privacy Router is configured, sandbox gets `HERMES_BASE_URL=https://inference.local` — no API keys inside the sandbox
- Graceful fallback: if `openshell provider set` fails, passes credentials via env vars directly (less secure but functional)
- `HERMES_MCP_GATEWAY_URL` injected into all sandbox env vars
- `api_key` field added to `InstanceConfig`
- 9 new tests covering provider config, type detection, inference.local routing, credential fallback

### Phase 4: MCP Cross-Sandbox Routing (complete)

**What was done:**
- `http_api.py` now resolves the user's `ActionPolicy`, configured MCP server names, and machine API key at spawn time, passing them to `InstanceConfig` (openshell mode only)
- The policy compiler (Phase 2) generates scoped `POST /mcp/*` network rules per MCP server
- `HERMES_MCP_GATEWAY_URL` is injected into every sandbox env (Phase 3)
- For `READ_ONLY` policies, MCP rules are omitted (agent can't use tools)
- 3 new tests for compiled policy with MCP rules

### Phase 5: Dashboard Preview + Documentation (complete)

**What was done:**
- New endpoint `POST /action-policies/{id}/preview-openshell` — compiles the ActionPolicy with current MCP config and returns YAML + hash + validation status
- Dashboard "Preview YAML" button on each action policy card — opens a collapsible panel showing the rendered OpenShell YAML, policy hash, and validation badge
- Documentation finalized

---

## Test Summary

| Test file | Count | Status |
|-----------|-------|--------|
| `tests/unit/test_executors.py` | 102 | Passing (pre-existing) |
| `tests/unit/test_openshell_executor.py` | 43 | Passing (new) |
| `tests/unit/test_policy_compiler.py` | 39 | Passing (new) |
| **Total** | **184** | **All passing** |

---

## Files Changed (Phases 1-5)

| Action | File |
|--------|------|
| New | `tests/unit/test_openshell_executor.py` |
| New | `tests/unit/test_policy_compiler.py` |
| New | `gateway/policies/compiler.py` |
| New | `gateway/policies/full_access.yaml` |
| New | `gateway/policies/workspace_only.yaml` |
| New | `gateway/policies/repo_scoped.yaml` |
| New | `gateway/policies/read_only.yaml` |
| New | `.github/workflows/build-sandbox-image.yml` |
| New | `docs/openshell-integration.md` |
| Modified | `gateway/executors/openshell.py` |
| Modified | `gateway/executors/docker.py` |
| Modified | `gateway/executors/base.py` |
| Modified | `gateway/http_api.py` |
| Modified | `docker/Dockerfile.openshell-sandbox` |
