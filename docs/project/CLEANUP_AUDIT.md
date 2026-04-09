# Logos Codebase Audit & Cleanup Notes

**Date:** 2026-04-06
**Scope:** Full codebase (~212K lines source, ~68K lines tests)
**Purpose:** Identify dead code, bugs, broken features, redundancies, and cleanup opportunities
**Status:** AUDIT ONLY - changes to be executed in a follow-up pass

---

## Codebase Sections (by size)

| # | Section | Lines | Status |
|---|---------|-------|--------|
| 1 | gateway/ (Python) | 31,950 | Audited |
| 2 | tools/ | 31,715 | Audited |
| 3 | logos_cli/ | 25,064 | Audited |
| 4 | gateway/ (HTML/JS) | 12,449 | Audited |
| 5 | agents/hermes/ + agent/ | 11,073 | Audited |
| 6 | core/ | 6,577 | Audited |
| 7 | environments/ | 6,870 | Audited |
| 8 | logos/ (framework) | 1,472 | Audited |
| 9 | integrations (cron/acp) | 2,949 | Audited |
| 10 | infra (k8s/docker/CI) | ~2,000 | Audited |

---

## PRIORITY 0 - BUGS (Fix Immediately)

### P0-1: CSRF Token Undefined in Update Buttons
- **File:** `gateway/html/main_app.html` lines 380, 394
- **Bug:** Uses `csrfToken` (undefined variable) instead of `getCsrfToken()` function call
- **Impact:** Launcher update buttons silently fail
- **Fix:** Replace `csrfToken` with `getCsrfToken()` in both locations

### P0-2: Dead chat_handlers.py References Non-Existent DB Functions
- **File:** `gateway/chat_handlers.py` (129 lines)
- **Bug:** Imports `list_chats()`, `create_chat()`, `get_chat()` etc. from auth/db.py - NONE EXIST
- **Impact:** Module would crash if ever imported. Complete dead code.
- **Fix:** Delete entire file

### P0-3: World Manager Memory Leak - Watchers Never Cleaned Up
- **File:** `gateway/html/main_app.html` lines 5299-5341
- **Bug:** `initWorld()` creates 3 Alpine `$watch` watchers that are never destroyed. `destroyWorld()` exists but is never called.
- **Impact:** Memory leak - watchers accumulate, PixiJS ticker runs even when hidden
- **Fix:** Add `$watch('tab', val => { if (val !== 'agents') this.destroyWorld(); })` and pause ticker

### P0-4: Client Null Pointer in Fallback Activation
- **File:** `agents/hermes/agent.py` line 536, 2784
- **Bug:** When fallback switches from OpenAI to Anthropic, `self.client` is set to `None` but line 2784 uses `self.client.chat.completions.create()` without null check
- **Impact:** Crash during fallback if api_mode routing doesn't match
- **Fix:** Add explicit null check before `self.client` usage

### ~~P0-5: OpenShell Executor — Beta Integration, Needs Testing~~ ✅ Resolved
- *Resolved by the OpenShell integration rewrite — agent-only sandboxes with reverse-connection worker. See `48c6135 feat: OpenShell integration rewrite`, `0cc3446 feat: OpenShell integration Phase 5`, and the new `Dockerfile.hermes-sandbox` + `gateway/policies/openshell_default.yaml`. OpenShell is now the default first-class runtime, not a beta integration.*

### P0-6: Context Length Not Updated on Model Switch
- **File:** `agents/hermes/agent.py` lines 2991-3020
- **Bug:** When fallback model activates, `self.context_compressor.context_length` still uses original model's value
- **Impact:** If fallback has smaller context window, API calls fail with "context exceeded"
- **Fix:** Update `self.context_compressor.context_length` after model switch

---

## PRIORITY 1 - HIGH (Fix Soon)

### P1-1: Hardcoded Internal k8s URLs
- **File:** `gateway/http_api.py` lines 60, 62
- **Details:** `AI_ROUTER_BASE = "http://ai-router.hermes.svc.cluster.local:9001"` and canary health URL
- **Fix:** Make fully configurable via env vars (defaults already use env, but fallback is hardcoded k8s)

### P1-2: Hardcoded Grafana IP
- **File:** `gateway/http_api.py` line 620
- **Details:** `"grafana_url": "http://192.168.1.253:3200"` - leaks internal network topology
- **Fix:** Remove or make configurable

### P1-3: Model Name Fuzzy Matching Too Permissive
- **File:** `agent/model_metadata.py` lines 332-334
- **Bug:** `if default_model in model or model in default_model` matches incorrectly (e.g., `gpt-4` matches `gpt-4o`)
- **Impact:** Wrong context window sizes returned, causing premature compression or context exceeded errors
- **Fix:** Use exact match or stricter prefix matching

### P1-4: Race Condition in Interrupt Handler
- **File:** `agents/hermes/agent.py` lines 2788-2813
- **Bug:** Main thread calls `self.client.close()` while background thread is executing `self.client.chat.completions.create()`
- **Impact:** Unpredictable errors when interrupt timing aligns with API response
- **Fix:** Use threading lock around client access

### P1-5: Fallback Provider is One-Shot Only
- **File:** `agents/hermes/agent.py` lines 2961-2996
- **Bug:** `if self._fallback_activated: return False` - can never retry even if fallback itself fails
- **Impact:** When both primary and fallback fail, agent gives up silently
- **Fix:** Track fallback attempt separately from success

### P1-6: Tool Arguments Silently Default to Empty Dict
- **File:** `agents/hermes/agent.py` lines 3770-3774
- **Bug:** Malformed JSON in tool_call.function.arguments → `function_args = {}` with no error logged
- **Impact:** Tools called with wrong arguments, results unpredictable
- **Fix:** Log malformed arguments, report to model as tool error

### P1-7: Exception Swallowing (75+ instances)
- **Files:** `gateway/http_api.py`, `gateway/setup_handlers.py`, `gateway/admin_handlers.py`, `agents/hermes/agent.py`
- **Pattern:** `except Exception: pass` or `except Exception as e: logger.debug(...)` everywhere
- **Impact:** Silent failures, extremely hard to debug in production
- **Fix:** Replace with proper logging at WARNING/ERROR level, at minimum

### P1-8: Prompt Cache Invalidation on Context Compression
- **File:** `agents/hermes/agent.py` lines 4592-4620, 4657
- **Bug:** Memory reload during compression changes system prompt → Anthropic cache prefix no longer matches → cache miss
- **Impact:** Anthropic prompt caching savings (up to 75%) lost after compression
- **Fix:** Track whether memory has changed and only invalidate cache if so

### P1-9: Duplicate `_PROVIDER_MODELS` Dictionary Shadowing
- **File:** `logos_cli/main.py` lines 1366-1391
- **Bug:** Local incomplete dict (4 providers) is defined before the full version is imported from models.py (line 1668)
- **Impact:** If code runs before import, returns empty model lists
- **Fix:** Remove local dict, import at module level

### P1-10: API Key Exposure in Warning Logs
- **File:** `agents/hermes/agent.py` lines 600-604
- **Bug:** Warning branch prints up to 20 chars of API key
- **Impact:** Credential leak in logs
- **Fix:** Print "key present" or "key missing" without showing the key

---

## PRIORITY 2 - MEDIUM (Cleanup Pass)

### P2-1: Delete Dead Code
- [ ] `gateway/chat_handlers.py` — 129 lines, completely unused (P0-2)
- [ ] `gateway/html/main_app.html` line 4494: `agentsTab: 'running'` — legacy compat variable, never referenced
- [ ] `logos_cli/auth.py` lines 1723-1728: Deprecated `login_command()` still registered but says "has been removed"

### P2-2: Consolidate Duplicate Logic
- [ ] Model selection: `logos_cli/setup.py` and `gateway/setup_handlers.py` both probe `/models` endpoint with different error handling
- [ ] Provider validation: `logos_cli/main.py` and `gateway/setup_handlers.py` duplicate endpoint probing
- [ ] Provider resolution: Two separate chains (main agent in `runtime_provider.py` vs auxiliary in `agent/auxiliary_client.py`)
- [ ] Reasoning extraction: Two paths (agent.py `_extract_reasoning()` vs `anthropic_adapter.py normalize_anthropic_response()`)

### P2-3: Replace `asyncio.get_event_loop()` with `asyncio.get_running_loop()`
- **Files:** `gateway/http_api.py` (6 instances), `gateway/setup_handlers.py` (3 instances)
- **Reason:** Deprecated in Python 3.10+, may break in future versions

### P2-4: Consolidate HTTP Client Libraries
- **Issue:** Mix of `aiohttp` and `httpx` across platforms
- **aiohttp:** discord.py, homeassistant.py, whatsapp.py, http_api.py (global)
- **httpx:** base.py, signal.py, slack.py, telegram.py
- **Fix:** Pick one (httpx preferred for async + sync support)

### P2-5: Missing Null Checks in UI Templates
- **File:** `gateway/html/main_app.html`
- **Lines:** 310 (`authUser.display_name`), 322-323 (`authUser.role`), 664 (`status.current_model`)
- **Fix:** Add `?.` optional chaining where appropriate

### P2-6: Add Missing Validation
- [ ] `gateway/session.py` line 90-100: `from_dict()` doesn't validate Platform enum
- [ ] `logos_cli/config.py` line 127: DEFAULT_CONFIG uses string "model" but code expects dict format

### P2-7: Fix Inconsistent Model Config Format
- **Issue:** Model config stored as string `"anthropic/claude-opus-4.6"` in DEFAULT_CONFIG but setup wizard writes dict `{"default": "...", "provider": "..."}`
- **Fix:** Standardize on dict format everywhere

### P2-8: skills/index-cache/ May Be Stale
- **Files:** `skills/index-cache/*.json`
- **Last modified:** 2026-03-14 (23 days ago)
- **Fix:** Refresh or add auto-refresh mechanism

---

## PRIORITY 3 - LOW (Nice to Have)

### P3-1: Architectural Improvements
- [ ] Centralized route registry for HTTP API (routes scattered across http_api.py, mcp_management.py, admin_handlers.py)
- [ ] Abstract LLMAdapter base class with OpenAI/Anthropic subclasses sharing test suite
- [ ] Integrate batch_runner.py with logos.agent.runner (currently bypasses framework)

### P3-2: Code Quality
- [ ] `gateway/http_api.py` line 1950: Missing space before `or` operator
- [ ] Duplicate `import sys` in `logos_cli/config.py` lines 19 and 21
- [ ] `logos_cli/config.py` line 1203-1209: `save_env_value_secure()` is a no-op wrapper
- [ ] Add return type hints to logos_cli/ functions
- [ ] Move provider aliases to module-level constant in auth.py (currently local in resolve_provider)

### P3-3: Security Hardening
- [ ] `logos_cli/config.py` line 1181-1186: `.env` chmod(0o600) silently passes on failure
- [ ] Agent phone redaction regex may be too aggressive (agent/redact.py line 82)
- [ ] Token estimation assumes 4 chars/token (incorrect for CJK languages)

### P3-4: k8s Manifests Cleanup
- [ ] `k8s/13-logos-setup-test-deployment.yaml` and `k8s/14-logos-setup-test-service.yaml` - possibly stale, not referenced
- [ ] `k8s/15-logos-generic-config.yaml` - possibly stale
- [ ] Document canary deployment process (k8s/10-12 are temporary resources)

### P3-5: Configuration
- [ ] Workspace config (`workspace_base_dir`, `workspace_ttl_hours`) is read but never set during setup
- [ ] Session reset config not exposed in setup wizard
- [ ] Database timeout hardcoded at 10s in auth/db.py (not configurable)
- [ ] OAuth client IDs hardcoded in auth.py (no env var override)

---

## SECTIONS VERIFIED AS CLEAN

These sections had minimal or no issues:

| Section | Verdict |
|---------|---------|
| **tools/** | Production-grade. Excellent security posture, proper optional dep handling |
| **core/state.py** | Clean schema, 8 migrations applied correctly, zero orphaned columns |
| **core/toolsets.py** | Source of truth, all toolset→tool mappings verified |
| **environments/** | Fully functional RL training infrastructure |
| **evals/** | Working evaluation framework with DB persistence |
| **workflows/** | Well-engineered async engine with approval gates |
| **cron/** | Functional scheduler with cross-platform locking |
| **acp_adapter/** | Complete, matches agent.json spec |
| **gateway/platforms/** | All 7 platforms functional (telegram/discord/slack/whatsapp/signal/email/homeassistant) |
| **gateway/executors/** | All current executors verified (local/docker/openshell). The legacy KubernetesExecutor was deleted in `f6f0972`. |
| **souls/** | All 10 soul manifests valid YAML, reference existing toolsets |
| **CI/CD workflows** | All functional, proper gating |
| **Docker configs** | Both modes (simple + k3s) properly configured |

---

## Execution Plan

### Pass 1 (This Session): COMPLETE
- [x] Map codebase structure and sizes
- [x] Audit all 10 sections
- [x] Document findings in this file

### Pass 2 (Next Session): Execute Fixes
1. **P0 fixes** (5 items) — immediate bug fixes
2. **P1 fixes** (10 items) — high-impact improvements
3. **P2 cleanup** (8 items) — code quality and consistency
4. Run full test suite after each batch
5. Deploy and verify

### Pass 3 (Future): Architecture
- P3 items as time permits
- Framework unification (logos/ ↔ core/ ↔ agents/)
- Test coverage gaps

---

## Test Validation

Current test status: **4376 passed, 156 skipped, 27 warnings** (all clean)

After executing fixes, re-run:
```bash
python3 -m pytest tests/ -x -q
```

E2E tests (Playwright):
```bash
cd e2e && npx playwright test
```
Last result: 47/51 passing (92%) — 4 failures related to agent creation DB column and user form selectors
