# Logos Repo Cleanup & Restructuring Report

> Generated: 2026-03-23
> Status: Phase 1–5 complete (analysis only — no files changed)

---

## 1. Inventory Summary

### Repository Identity

| Item | Value |
|------|-------|
| Package name (pyproject.toml) | `logos` |
| Installed CLI commands | `hermes`, `hermes-agent`, `hermes-acp` |
| Egg-info name | `hermes_agent` |
| Primary entry point | `hermes_cli.main:main` → `hermes gateway run` |
| Submodules | `mini-swe-agent`, `tinker-atropos` |
| Python version | 3.11+ |

### Classification Table

| Path | Classification | Notes |
|------|---------------|-------|
| `gateway/` | **Definitely Logos** | HTTP API, auth, web UI, session management |
| `tools/` | **Definitely Logos** | All agent tools (approval, file, terminal, etc.) |
| `hermes_cli/` | **Definitely Logos** | Main platform CLI (named hermes, but is Logos's CLI) |
| `cron/` | **Definitely Logos** | Cron scheduler for agent jobs |
| `honcho_integration/` | **Definitely Logos** | Honcho AI memory integration |
| `acp_adapter/` | **Definitely Logos** | ACP protocol server (VS Code, Zed, JetBrains) |
| `acp_registry/` | **Definitely Logos** | ACP agent registry metadata |
| `agent/` | **Definitely Logos** | Runtime adapters: anthropic, display, context compression |
| `logos/` | **Definitely Logos** | New platform abstraction layer (interface, policy, registry, blueprints) |
| `evals/` | **Definitely Logos** | Eval framework and suites |
| `workflows/` | **Definitely Logos** | DAG workflow engine |
| `k8s/` | **Definitely Logos** | Kubernetes deployment manifests |
| `tests/` | **Definitely Logos** | Full test suite |
| `docs/` | **Definitely Logos** | Documentation (with some hermes-branded content to update) |
| `assets/` | **Definitely Logos** | Logo, banner (Logos-branded) |
| `website/` | **Definitely Logos** | Docusaurus documentation site |
| `skills/` | **Definitely Logos** | Agent skill packages |
| `souls/` | **Definitely Logos** | Named agent personas |
| `optional-skills/` | **Definitely Logos** | Optional skill packages |
| `scripts/` | **Definitely Logos** | Operational scripts (install, release, etc.) |
| `run_agent.py` | **Definitely hermes-agent** | Active production agent runtime — hermes-agent core loop |
| `hermes_state.py` | **Definitely hermes-agent** | SQLite state store (v8, canonical, still heavily used) |
| `hermes_constants.py` | **Definitely hermes-agent** | Shared constants (OPENROUTER, NOUS API URLs) |
| `hermes_time.py` | **Definitely hermes-agent** | Timezone-aware clock |
| `model_tools.py` | **Definitely hermes-agent** | Tool definitions & dispatch (used by run_agent.py) |
| `toolsets.py` | **Definitely hermes-agent** | Toolset definitions |
| `toolset_distributions.py` | **Definitely hermes-agent** | Toolset sampling distributions |
| `trajectory_compressor.py` | **Definitely hermes-agent** | Conversation trajectory compressor |
| `utils.py` | **Definitely hermes-agent** | Shared utilities (atomic writes, etc.) |
| `minisweagent_path.py` | **Definitely hermes-agent** | mini-swe-agent path discovery |
| `runs.py` | **Likely Logos** | Run audit trail (imports hermes_state, used by run_agent.py) |
| `metrics.py` | **Likely Logos** | Metrics engine (imports hermes_state) |
| `batch_runner.py` | **Likely hermes-agent** | Batch agent runner |
| `mini_swe_runner.py` | **Likely hermes-agent** | mini-swe-agent runner |
| `rl_cli.py` | **Likely hermes-agent** | RL training CLI runner |
| `core/` | **STALE migration copies** | ⚠️ See Section 2.4 — core/ is OLDER than root-level files |
| `agents/hermes/` | **Work-in-progress** | ⚠️ Refactored agent importing stale core.* — see Section 2.4 |
| `environments/` | **Likely hermes-agent** | RL/Atropos training environments |
| `hermes` (root script) | **Legacy** | Old launcher: `from cli import main; fire.Fire(main)` — superseded |
| `cli.py` | **Legacy / Ambiguous** | Old standalone CLI — `hermes_cli/main.py` is the successor |
| `hermes_agent.egg-info/` | **Build artifact** | Should be gitignored |
| `datagen-config-examples/` | **Likely hermes-agent** | Data generation scripts/configs |
| `landingpage/nous-logo.png` | **Legacy** | Old Nous Research logo |
| `landingpage/hermes-agent-banner.png` | **Legacy** | Old hermes-agent banner |
| `temp_vision_images/` | **Runtime artifact** | Temp files — should be gitignored |
| `logs/` | **Runtime artifact** | Log files — should be gitignored |
| `plans/` | **Likely Logos** | Implementation planning docs |
| `AGENTS.md` | **Needs update** | Says "Hermes Agent - Development Guide" |
| `CONTRIBUTING.md` | **Needs update** | Says "Contributing to Hermes Agent" |
| `setup-hermes.sh` | **Likely hermes-agent** | Developer setup script |
| `cli-config.yaml.example` | **Ambiguous** | Config example — check if it matches hermes_cli config |

---

## 2. Runtime-Critical Logos Components

### 2.1 Primary Runtime Path

```
CMD: hermes gateway run
  → hermes_cli/main.py:main()
    → gateway.run.GatewayRunner.start()
      → (per platform) Telegram / Discord / Slack / Web adapters
        → gateway/run.py: from run_agent import AIAgent
          → run_agent.py: AIAgent.run_conversation()
            → tools/*.py (all tools)
            → hermes_state.py (session DB, v8)
            → model_tools.py (tool dispatch)
            → toolsets.py / toolset_distributions.py
```

### 2.2 CLI Path (interactive chat)

```
hermes chat / hermes [no args]
  → hermes_cli/main.py:main()
    → hermes_cli/runtime_provider.py
      → run_agent.py: AIAgent (same runtime)
        → hermes_state.py
        → model_tools.py
        → toolsets.py
```

### 2.3 ACP Path (IDE integration)

```
hermes acp / hermes-acp
  → acp_adapter/entry.py
    → acp_adapter/server.py
      → run_agent.py: AIAgent (same runtime)
```

### 2.4 Critical Finding: The core/ Migration is INCOMPLETE and STALE

**This is the most important finding in this report.**

The `core/` directory was created as part of an in-progress migration to make hermes-agent a proper Logos subsystem. However:

| File | Root version | core/ version | Status |
|------|-------------|---------------|--------|
| `hermes_state.py` | SCHEMA_VERSION **8** (canonical) | `core/state.py` SCHEMA_VERSION **4** | ⚠️ core/ is **4 versions stale** |
| `hermes_constants.py` | Canonical | `core/constants.py` | Identical (safe) |
| `hermes_time.py` | Canonical | `core/clock.py` | Identical (safe) |
| `utils.py` | Canonical | `core/utils.py` | Identical (safe) |
| `toolsets.py` | Has `bug_notes` toolset | `core/toolsets.py` | Missing `bug_notes`; uses `core.toolsets` import |
| `model_tools.py` | Canonical | `core/model_tools.py` | Unknown diff — assume stale |
| `trajectory_compressor.py` | Canonical | `core/trajectory_compressor.py` | Unknown diff — assume stale |
| `toolset_distributions.py` | Canonical | `core/toolset_distributions.py` | Unknown diff — assume stale |
| `minisweagent_path.py` | Canonical | `core/minisweagent_path.py` | Unknown diff — assume stale |

**`agents/hermes/agent.py` imports from `core.*` which are stale.** It therefore runs against schema v4 state logic and a toolset missing `bug_notes`. This file should NOT be used as the production runtime until `core/` is synced.

**`gateway/run.py` correctly imports `from run_agent import AIAgent`** — so production is NOT affected by the stale `core/` issue.

---

## 3. Hermes-Agent Derived Components

These are files that originated directly from the hermes-agent codebase. They remain active and are depended on by the Logos runtime, but belong conceptually to the "hermes agent" layer.

### 3.1 Active hermes-agent runtime files (still needed, messy location)

| File | Why it's hermes-agent origin | Current usage |
|------|------------------------------|--------------|
| `run_agent.py` | Core AIAgent loop — the hermes-agent runtime | Imported by `gateway/run.py` (5 places) and `acp_adapter` |
| `hermes_state.py` | SQLite state store, named "Hermes" | Imported across gateway, hermes_cli, tools, tests (50+ sites) |
| `hermes_constants.py` | Constants file named "Hermes" | Imported by hermes_cli |
| `hermes_time.py` | Clock named "Hermes" | Used by run_agent.py |
| `model_tools.py` | Tool definitions for hermes agent loop | Used by run_agent.py |
| `toolsets.py` | Toolset definitions | Used by run_agent.py |
| `toolset_distributions.py` | Toolset sampling | Used by run_agent.py |
| `trajectory_compressor.py` | Trajectory management | Used at runtime |
| `utils.py` | Shared utilities | Used broadly |
| `minisweagent_path.py` | mini-swe-agent path resolver | Used by run_agent.py |
| `runs.py` | Run audit records | Used by run_agent.py |
| `batch_runner.py` | Batch agent runs | Entry point `hermes-agent` workflows |
| `mini_swe_runner.py` | mini-swe-agent runner | Standalone runner |
| `rl_cli.py` | RL training CLI | Optional RL feature |
| `environments/` | RL training environments (Atropos) | Optional RL feature |

### 3.2 hermes-agent legacy files (no longer needed)

| File | Why it's legacy |
|------|----------------|
| `hermes` (root script) | 13-line launcher: `from cli import main; fire.Fire(main)`. Superseded by pyproject.toml `hermes = "hermes_cli.main:main"` |
| `cli.py` | Old standalone CLI ("Hermes Agent CLI"). `hermes_cli/main.py` is the current successor. Used by the `hermes` root script but not by the installed `hermes` command. |
| `core/state.py` | Stale v4 copy of `hermes_state.py` (canonical is v8) |
| `core/toolsets.py` | Stale copy missing `bug_notes` toolset |
| `agents/hermes/agent.py` | Partially-refactored agent using stale `core.*` — not yet production-ready |
| `setup-hermes.sh` | Developer setup script for hermes-agent origin; should become `scripts/setup.sh` |

### 3.3 Documentation with hermes-agent branding (needs update)

| File | Issue |
|------|-------|
| `AGENTS.md` | Title: "Hermes Agent - Development Guide" |
| `CONTRIBUTING.md` | Title: "Contributing to Hermes Agent" |
| `landingpage/hermes-agent-banner.png` | Old branding |
| `landingpage/nous-logo.png` | Nous Research logo (no longer relevant) |
| `hermes_agent.egg-info/PKG-INFO` | Stale package metadata |

---

## 4. Unclear Items Requiring Review

| Path | Why unclear | Recommended action |
|------|-------------|-------------------|
| `cli.py` | Described as CLI, but may have features not yet in `hermes_cli/main.py`. Large file. | Deep-read both; diff capabilities before archiving |
| `agents/hermes/` | Work-in-progress refactor — partially useful, partially broken | Review after `core/` is synced; may become the future production agent |
| `datagen-config-examples/` | Data generation examples — are these used? | Check if referenced by scripts or docs |
| `plans/checkpoint-rollback.md` | Implementation plan — is this done or pending? | Check if `tools/checkpoint_manager.py` covers this |
| `cli-config.yaml.example` | Config example — does it match current `hermes_cli` config schema? | Verify schema match |
| `data/` | Directory exists but appeared empty in listing | Verify contents; may be gitignored data |
| `environments/benchmarks` | Benchmark environments — active or abandoned? | Check if used in CI or by Atropos |
| `environments/terminal_test_env` | Test environment — active or archived? | Check if referenced by tests |

---

## 5. Likely Irrelevant / Legacy / Duplicate Files

### 5.1 Build artifacts (should be gitignored, never committed)

- `hermes_agent.egg-info/` — setuptools build artifact
- `__pycache__/` directories — Python bytecode
- `venv/` — virtual environment
- `temp_vision_images/` — runtime-generated temp files
- `logs/` — runtime log files (if committed)

### 5.2 Stale migration artifacts

- `core/state.py` — v4, stale by 4 schema migrations; the root `hermes_state.py` is canonical
- `core/toolsets.py` — missing `bug_notes` toolset; stale copy

### 5.3 Legacy launchers

- `hermes` (root script) — superseded by pyproject.toml entry point
- `cli.py` — likely superseded by `hermes_cli/main.py`

### 5.4 Old branding

- `landingpage/nous-logo.png`
- `landingpage/hermes-agent-banner.png`
- `hermes_agent.egg-info/PKG-INFO` (contains old metadata)

---

## 6. Proposed New Directory Structure

This is the **target state** — not all of this requires immediate changes.

```
logos/                              ← repo root
├── README.md
├── LICENSE
├── SOUL.md                         ← keep (Claude Code reads it)
├── AGENTS.md                       ← UPDATE: rename to Logos Dev Guide
├── CONTRIBUTING.md                 ← UPDATE: rename to Logos Contributing
├── pyproject.toml                  ← UPDATE: rename scripts hermes→logos
├── uv.lock
├── Dockerfile
├── .gitmodules
│
├── logos/                          ← Logos platform abstractions
│   ├── agent/                      ← Agent interface (ABC, runner)
│   ├── adapters/                   ← Adapter implementations
│   ├── blueprints/                 ← STAMP blueprint schema/loader
│   ├── audit/                      ← Run audit trail (runs.py → here)
│   ├── policy/                     ← Policy enforcement
│   ├── registry/                   ← Agent/tool registry
│   ├── souls/                      ← Soul loader
│   ├── models/                     ← Model provider abstraction
│   └── tools/                      ← Tool registry (logos-level)
│
├── gateway/                        ← HTTP API, auth, web UI (keep as-is)
├── agent/                          ← Runtime adapters (keep as-is)
├── tools/                          ← All agent tools (keep as-is)
├── hermes_cli/                     ← Platform CLI (keep; rename to logos_cli/ eventually)
├── cron/                           ← Cron scheduler (keep as-is)
├── honcho_integration/             ← Honcho memory (keep as-is)
├── acp_adapter/                    ← ACP protocol adapter (keep as-is)
├── acp_registry/                   ← ACP registry metadata (keep as-is)
├── evals/                          ← Eval framework (keep as-is)
├── workflows/                      ← Workflow engine (keep as-is)
│
├── agents/                         ← Agent runtime implementations
│   └── hermes/                     ← Hermes agent (future submodule)
│       ├── agent.py                ← Refactored runtime (update core.* imports)
│       ├── logos-agent.yaml        ← Agent descriptor
│       └── __init__.py
│
├── core/                           ← Shared domain modules (SYNC with root files)
│   ├── state.py                    ← SYNC to v8 (from hermes_state.py)
│   ├── constants.py                ← Already identical
│   ├── clock.py                    ← Already identical
│   ├── utils.py                    ← Already identical
│   ├── model_tools.py              ← SYNC from model_tools.py
│   ├── toolsets.py                 ← SYNC (add bug_notes)
│   ├── toolset_distributions.py    ← SYNC
│   ├── trajectory_compressor.py    ← SYNC
│   └── minisweagent_path.py        ← SYNC
│
├── k8s/                            ← Kubernetes manifests (keep as-is)
├── tests/                          ← All tests (keep as-is)
├── docs/                           ← Documentation (keep as-is)
├── website/                        ← Docusaurus site (keep as-is)
├── landingpage/                    ← Marketing page (remove nous/hermes branding)
├── assets/                         ← Logos branding (keep as-is)
├── skills/                         ← Agent skills (keep as-is)
├── optional-skills/                ← Optional skills (keep as-is)
├── souls/                          ← Agent personas (keep as-is)
├── scripts/                        ← Operational scripts (keep as-is)
│
├── mini-swe-agent/                 ← Git submodule (already configured)
├── tinker-atropos/                 ← Git submodule (already configured)
│
└── archive/                        ← Files pending review before deletion
    ├── hermes-origin/              ← Old hermes-agent files
    │   ├── hermes                  ← Old root launcher script
    │   ├── cli.py                  ← Old standalone CLI
    │   └── setup-hermes.sh         ← Old setup script
    └── stale-core/                 ← Stale core/ copies (after sync)
```

### Root-level files to KEEP in place (runtime dependency)

These root-level files are imported by `gateway/run.py`, `hermes_cli/`, and the full runtime. They should stay until all import sites are updated to use `core.*`:

- `run_agent.py`
- `hermes_state.py`
- `hermes_constants.py`
- `hermes_time.py`
- `model_tools.py`
- `toolsets.py`
- `toolset_distributions.py`
- `trajectory_compressor.py`
- `utils.py`
- `minisweagent_path.py`
- `runs.py`
- `metrics.py`
- `batch_runner.py`
- `mini_swe_runner.py`
- `rl_cli.py`

---

## 7. Safe Cleanup Plan

### Phase A — Immediate safe changes (no import updates needed)

**Step A1: Gitignore cleanup** (zero risk)
- Add to `.gitignore`: `hermes_agent.egg-info/`, `temp_vision_images/`, `__pycache__/`, `logs/`, `venv/`
- If `hermes_agent.egg-info/` is committed, remove it: `git rm -r hermes_agent.egg-info/`

**Step A2: Archive legacy root scripts** (minimal risk — they aren't imported by production)
- Move `hermes` (root script) → `archive/hermes-origin/hermes`
- Move `setup-hermes.sh` → `scripts/setup.sh` (rename for Logos)
- CONFIRM: run `grep -r "from hermes import\|import hermes$" .` — should return nothing

**Step A3: Branding cleanup in docs**
- Update `AGENTS.md`: replace "Hermes Agent" → "Logos Platform" in title/header
- Update `CONTRIBUTING.md`: replace "Hermes Agent" in title/header
- Remove `landingpage/nous-logo.png` (Nous Research branding)
- Remove `landingpage/hermes-agent-banner.png` (old branding)

**Step A4: Move documentation**
- Move `plans/checkpoint-rollback.md` → `docs/project/plans/checkpoint-rollback.md` (per REPO_STRUCTURE.md)

### Phase B — Sync core/ to root-level canonical files (medium risk)

This is the most important correctness fix. The `core/` copies are stale.

**Step B1: Sync `core/state.py` from `hermes_state.py`**
```
cp hermes_state.py core/state.py
```
⚠️ After this, `agents/hermes/agent.py` will need testing. But it doesn't import `core.state` directly (it uses `hermes_state` lazily in places), so impact is likely low.

**Step B2: Sync remaining core/ files**
```
cp model_tools.py core/model_tools.py
cp toolsets.py core/toolsets.py          # Then re-add "from core.toolsets import" fix
cp toolset_distributions.py core/toolset_distributions.py
cp trajectory_compressor.py core/trajectory_compressor.py
cp minisweagent_path.py core/minisweagent_path.py
```

After syncing, update `core/toolsets.py` line 17 to use `from core.toolsets import` (the core.* self-reference) — or use relative imports.

**Step B3: Run tests**
```
pytest tests/ -x -q
```

### Phase C — Migration of import sites to core.* (higher risk, do in batches)

Once `core/` is synced, migrate import sites in this order (least risky first):

1. `tests/` — update test imports from `hermes_state` → `core.state`, etc.
2. `tools/` — update tool imports
3. `cron/` — update cron imports
4. `hermes_cli/` — update CLI imports
5. `gateway/` — update gateway imports (most risk — test after each)
6. `run_agent.py` — update agent imports (test the full agent loop after)

After each batch: run `pytest tests/ -x -q`

### Phase D — Wire up agents/hermes/agent.py as production runtime (future)

Once `core/` is fully synced and import sites are migrated:

1. Update `agents/hermes/agent.py` to import run recorder from `logos.audit.runs`
2. Update `pyproject.toml`: `hermes-agent = "agents.hermes.agent:main"`
3. Update `gateway/run.py`: `from agents.hermes.agent import AIAgent`
4. Archive `run_agent.py` (keep as `archive/hermes-origin/run_agent.py`)

### Phase E — Hermes-agent submodule preparation (future)

1. Move all active hermes-agent runtime code under `agents/hermes/`:
   - `run_agent.py` → `agents/hermes/run_agent.py` (or keep as `agent.py`)
   - `hermes_state.py` → `core/state.py` (already synced by then)
   - `model_tools.py` → `core/model_tools.py` (already synced by then)
   - `toolsets.py` → `core/toolsets.py` (already synced by then)
   - etc.
2. Update pyproject.toml `py-modules` to remove now-moved files
3. Create `agents/hermes/pyproject.toml` for future submodule extraction
4. Consider renaming `hermes_cli/` → `logos_cli/` and entry `hermes` → `logos`

---

## 8. Reports Created

- `docs/project/CLEANUP_REPORT.md` — this file (full analysis)
- `docs/project/SUSPICIOUS_FILES.md` — per-file confidence table for review

---

## Appendix: Core Execution Path Map

Minimum files for Logos to **build and run**:

```
pyproject.toml          Package definition
Dockerfile              Container build
gateway/                Full gateway server
  run.py                Main gateway runner
  session.py            Session management
  http_api.py           Web dashboard API
  chat_handlers.py      Message processing
  auth/                 Auth + policy
  platforms/            Telegram, Discord, Slack, Web
hermes_cli/             CLI entry point
  main.py               `hermes` command
  runtime_provider.py   Provider resolution
  ...
run_agent.py            AIAgent runtime
hermes_state.py         Session + run + workspace DB (v8)
model_tools.py          Tool dispatch
toolsets.py             Toolset definitions
toolset_distributions.py Toolset sampling
utils.py                Atomic writes
hermes_constants.py     API URLs
hermes_time.py          Timezone clock
tools/                  All agent tools
cron/                   Cron scheduler
honcho_integration/     Memory (optional)
acp_adapter/            ACP server (optional)
logos/                  Platform abstractions (policy enforcement)
```

Files NOT needed for basic operation (can be absent):
- `cli.py` (superseded)
- `hermes` root script (superseded)
- `core/` (stale copies — runtime uses root-level canonical files)
- `agents/hermes/agent.py` (not wired into production)
- `environments/` (only for RL training)
- `evals/` (only for eval runs)
- `workflows/` (only for workflow runs)
- `datagen-config-examples/` (documentation/examples only)
- `rl_cli.py`, `batch_runner.py`, `mini_swe_runner.py` (optional runners)
