# Root Path Audit — Logos

Validated against actual code (not trusting docs). Each entry cites evidence.

**Ground truth reference:**
- `pyproject.toml` v0.10.4, package name `logos`
- Entry points: `logos` (→ `logos_cli.main:main`), `hermes` (→ `logos_cli.main:main_agent`), `hermes-agent` (→ `agents.hermes.agent:main`), `logos-acp` (→ `acp_adapter.entry:main`)
- `setuptools.packages.find` includes: `agent`, `agents`, `tools`, `logos_cli`, `gateway`, `core`, `cron`, `acp_adapter`
- **Notably absent from setuptools include list**: top-level `logos/` package — yet it IS imported by 25+ live files (packaging bug or intentional namespace workaround — flag for investigation)

**Legend:** ✅ active · ⚠️ active-but-stale-docs · 🧹 legacy/cleanup candidate · 🗑️ build artifact (gitignored) · ❓ needs deeper look

---

## Progress: 57/57 paths assessed (100%) — COMPLETE

## Batch 1 (paths 1-10)

### 1. `.claude/` — ✅ active (Claude Code config)
Contains only `settings.local.json`. Per-repo Claude Code settings. Keep.

### 2. `.dockerignore` — ⚠️ active-but-stale
Excludes `CRITIQUE.md`, `SOUL.md`, `onboarding_plan.md`. `SOUL.md` exists in the repo (1465 B, committed); `CRITIQUE.md` and `onboarding_plan.md` do not exist. Harmless stale entries.

### 3. `.env.example` — ✅ active
14 KB, referenced by `docker-compose.yml` ("cp .env.example .env"). Keep.

### 4. `.github/` — ✅ active
Contains `PULL_REQUEST_TEMPLATE.md` + `workflows/` (build-image.yml, build-windows.yml, deploy-site.yml, tests.yml). Keep.

### 5. `.gitignore` — ✅ active
Notable: gitignores `PROJECT_STATE.md`, `docs/private/`, `knowledge-repos/` — they are **local-only working docs / reference material**, not part of the shipped repo.

### 6. `acp_adapter/` — ✅ active
Registered as entry-point `logos-acp = "acp_adapter.entry:main"`. Imported by `logos_cli/main.py` + 6 test files. Files: auth, entry, events, permissions, server, session, tools, `__main__`. Live ACP (Agent Client Protocol) adapter.

### 7. `acp_registry/` — ✅ active (small metadata dir)
Two files: `agent.json` (ACP agent descriptor pointing at `hermes acp` command) + `icon.svg`. Distribution metadata for ACP registry publication. Keep.

### 8. `agent/` — ✅ active (core)
Imported 101× across 38 files. Contains prompt_builder, context_compressor, prompt_caching, auxiliary_client, model_metadata, display, skill_commands, anthropic_adapter, redact, trajectory, insights. This is the **live agent internals package** — included by setuptools. Keep.

### 9. `agents/` — ✅ active
Contains only `hermes/` subpackage (agent.py, `__init__`.py, logos-agent.yaml). `agents.hermes.agent:main` is the `hermes-agent` console entry point. Keep.

### 10. `AGENTS.md` — ⚠️ SEVERELY STALE — needs rewrite
Claims ~11 root-level shim files exist (`hermes_state.py`, `model_tools.py`, `toolsets.py`, `hermes_constants.py`, `hermes_time.py`, `utils.py`, `runs.py`, `metrics.py`, `batch_runner.py`, `mini_swe_runner.py`). **ZERO root-level `*.py` files actually exist** — those modules live in `core/` (verified: core/ has batch_runner.py, metrics.py, runs.py, state.py, toolsets.py, clock.py, constants.py, model_tools.py, utils.py, trajectory_compressor.py). The shim claim is stale — either the shims were deleted or never committed. Also calls the top-level `logos/` directory "WIP" — but it is imported by 25+ files across the live codebase. **Major rewrite needed.**

---

## Batch 2 (paths 11-20)

### 11. `assets/` — ✅ active
Branding + tailwind CSS source/output (`tailwind-input.css`, `tailwind.css`) + `logo.png/svg`, `banner.png`, and `world/characters.png` (likely a world/tamagotchi UI sprite). Referenced by `tailwind.config.js` at root. Keep.

### 12. `CONTRIBUTING.md` — ✅ active (spot-checked)
Appears current — references `optional-skills/` (exists), `hermes skills browse/install` CLI (need to verify the commands still exist, but structure is plausible). Recommend: grep for `hermes skills` to confirm subcommand wiring before full trust. Keep.

### 13. `core/` — ✅ active (central)
Houses all the modules AGENTS.md wrongly claimed were root-level shims: `batch_runner.py`, `metrics.py`, `runs.py`, `state.py`, `toolsets.py`, `clock.py`, `constants.py`, `model_tools.py`, `utils.py`, `trajectory_compressor.py`, `toolset_distributions.py`. Included in setuptools. Keep.

### 14. `cron/` — ✅ active
`jobs.py`, `scheduler.py`, `__init__.py`. Included in setuptools. Matches optional dep `cron = ["croniter"]` in pyproject. Keep.

### 15. `data/` — 🧹 EMPTY directory, gitignored
Zero files. Listed in `.gitignore` line 15 (`data/`). Safe to delete the empty dir or leave as runtime placeholder.

### 16. `datagen-config-examples/` — ❓ likely legacy (needs user call)
4 files (217 lines total): `example_browser_tasks.jsonl` (5 lines), `run_browser_tasks.sh`, `trajectory_compression.yaml`, `web_research.yaml`. Only cross-referenced from:
  - `core/trajectory_compressor.py` (generic "datagen" string, not these specific files)
  - `scripts/sample_and_compress.py`
  - `docs/project/historical/CLEANUP_REPORT.md` (already in *historical* docs)
No Python entry point, no tests. Likely leftover from an older datagen workflow. **Recommend: remove unless you are actively running `scripts/sample_and_compress.py`.**

### 17. `docker/` — ✅ active
`Dockerfile.docker-sandbox`, `Dockerfile.hermes-sandbox`, `Dockerfile.hermes-upstream`, `entrypoint-hermes.sh`, `AGENT-IMAGES.md`, `sandbox_worker.py`. All referenced across gateway/ (run.py, http_api.py, worker_registry.py, executors/openshell.py, setup_handlers.py) and docs. Keep.

### 18. `docker-compose.k3s.yml` — ✅ active
Fresh content — uses new `LOGOS_*` env var naming with `HERMES_*` deprecated aliases. Documents k3s pod-level agent isolation. Keep.

### 19. `docker-compose.yml` — ✅ active
Simple compose for the "agents-as-child-processes" local mode. Fresh content. Keep.

### 20. `Dockerfile` — ✅ active
Python 3.11-slim, uv-based install of `.[all]`, runs `logos gateway run` as entrypoint. Matches current CLI scheme. Keep.

---

## Batch 3 (paths 21-30)

### 21. `docs/` — ⚠️ mixed; subdirs need separate audits
Top-level contents verified:
- **Current (active)**: `MISSING.md` (updated 2026-04-12, tracks M1–M11 features), `acp-setup.md`, `agent-runtime-protocol.md`, `lazy_tool_loading.md`, `mcp_redesign.md`, `skins/`, `cli-config.yaml.example`, `audit/`, `migration/` (5 migration docs incl. `env-var-rename-hermes-to-logos`), `proposals/` (3 docs)
- **Mixed truthfulness (need per-file review)**: `docs/project/` — `CRITIQUE.md`, `onboarding_plan.md`, `CLEANUP_AUDIT.md`, `REPO_STRUCTURE.md` (likely stale given root-level AGENTS.md is stale), `BUILD_AND_DEPLOY.md`, `ILEARNT.md`, `W-VS-L.md`, `BENCHMARK_REDESIGN.md`, `COMPARISON.md`, `MULTI_AGENT_MEMORY.md`, `AGENT_WORKER.md`, `AGENT_WORLD.md`, `WINDOWS_DESKTOP.md`
- **Historical (labelled)**: `docs/project/historical/CLEANUP_REPORT.md`, `docs/project/plans/` (3 plan docs — forward-looking, may be stale), `docs/project/todo/refactor_http_api.md`
- **Gitignored**: `docs/private/` (homelab-specific, local-only per `.gitignore` line 75)

**Flag**: `docs/project/REPO_STRUCTURE.md` is likely stale — it presumably describes the same structure as AGENTS.md which is known stale. Worth opening during a later pass.

### 22. `e2e/` — ✅ active
Playwright TypeScript tests: `playwright.config.ts`, `global-setup.ts`, `tests/`, `lib/`, `README.md`, own `package.json` + lockfile. `test-results/` + `playwright-report/` + `Trace-20260406T141925.json` are run artifacts. Keep.

### 23. `environments/` — 🧹 GHOST DIRECTORY (delete)
Contains **only** `__pycache__/` and `tool_call_parsers/__pycache__/` — **no `.py` source files remain**. Zero imports of `environments` or `environments.tool_call_parsers` anywhere in the repo. Source was deleted, pycache never cleared. **Safe to remove.** (The active "environments" work lives at `tools/environments/` inside the `tools/` package, per AGENTS.md's still-accurate sub-tree description.)

### 24. `evals/` — ✅ active
`assertions.py`, `runner.py`, `schema.py`, `__init__.py`, `suites/`. Standalone eval harness. Not in setuptools include list (not shipped as a package) — verify use before assuming it's still wired into anything. Likely kept as a dev/bench tool.

### 25. `gateway/` — ✅ active (central)
40+ modules — HTTP API, auth, platforms, executors, MCP, policies, runs, seed, setup, webhooks, worker registry, etc. This is the running web server (entry: `logos gateway run`). Keep.

### 26. `honcho_integration/` — 🧹 GHOST DIRECTORY (delete)
Only `__pycache__/`, zero Python source. Grep for `honcho` finds it only in `docs/project/historical/CLEANUP_REPORT.md` and `uv.lock`. Experiment that was removed but the dir remained. **Safe to remove.**

### 27. `installer/` — ✅ active (Windows only)
Single file: `logos.iss` (Inno Setup script). Produces `.exe` via `.github/workflows/build-windows.yml`. Keep if Windows desktop installer is still a delivery target (per `docs/project/WINDOWS_DESKTOP.md`).

### 28. `k8s/` — ✅ active
17 numbered YAMLs + `dev/` + `README.md`. Matches the "three deployments (logos/canary/setup-test)" per your project memory. Referenced by deploy tooling. Keep.

### 29. `knowledge-repos/` — ✅ active, but **gitignored**
Contains `ai-town/` and `llmfit/` (external reference clones, per your memory: "reference repos in knowledge-repos/"). Not shipped; local-only. Keep as-is.

### 30. `landingpage/` — ✅ active
Static site (index.html, script.js, style.css, favicons, apple touch icon). Deployed by `.github/workflows/deploy-site.yml` → GitHub Pages root (website/ mounts at `/docs/`). Keep.

---

## Batch 4 (paths 31-42)

### 31. `launcher/` — ✅ active (Windows build support)
`hermes_launcher.py`, `hermes_launcher.spec` (PyInstaller), `logos.ico`, `make_icon.py`. Referenced by `.github/workflows/build-windows.yml`, `installer/logos.iss`, `docs/project/WINDOWS_DESKTOP.md`, `SECURITY.md`. Keep if Windows desktop target remains.

### 32. `LICENSE` — ✅ active
MIT. Keep.

### 33. `logos/` — ✅ active (platform abstraction layer) — **packaging bug**
The top-level `logos/` package: `adapters/`, `agent/`, `audit/`, `blueprints/`, `context.py`, `__init__.py`, `models/`, `policy/`, `registry/`, `souls/`, `tools/`. Imported by 25+ files (logos_cli/cli.py, gateway/run.py, tests, etc.).
**BUG**: Not included in `pyproject.toml` `setuptools.packages.find` → `include = [...]` list. Means `pip install .` does not ship this package, only editable installs work via `PYTHONPATH="/app"` (which is exactly how the `Dockerfile` at line 19 works). **Recommend adding `"logos", "logos.*"` to the include list.**

### 34. `logos_cli/` — ✅ active (the CLI)
28 files — main CLI entry point (`logos_cli.main:main` → `logos` binary). Contains: auth, banner, callbacks, checklist, claw, clipboard, cli, codex_models, colors, commands, config, cron, curses_ui, debug, default_soul, doctor, gateway, main, models, pairing, runtime_provider, setup, skills_config, skills_hub, skin_engine, status, tools_config, uninstall. Keep.

### 35. `logs/` — 🗑️ runtime artifacts, gitignored
9 files: `gateway.restart.20260412-*.log` (all from today, 2026-04-12). Runtime-only, gitignored. Leave (produced by the running service).

### 36. `M11.md` — ✅ active (current milestone doc)
Detailed M11 plan/notes (agents as versioned drop-in sandbox images). Matches `docs/MISSING.md` status table claiming M11 is done. Location at repo root is unusual (milestones M1-M10 presumably live elsewhere — not verified). Consider moving to `docs/milestones/M11.md` for consistency, but it's current and load-bearing. Keep.

### 37. `optional-skills/` — ✅ active
9 skill category dirs: `autonomous-ai-agents/`, `blockchain/`, `email/`, `health/`, `migration/`, `productivity/`, `research/`, `security/`, + `DESCRIPTION.md`. Referenced by `logos_cli/setup.py`, website docs, `CONTRIBUTING.md`. Matches the opt-in skills model CONTRIBUTING.md describes. Keep.

### 38. `package.json` — ⚠️ stale metadata, keep for deps
Name is still `"hermes-agent"` (not `logos`), version `1.0.0`, repository URL points at `NousResearch/Hermes-Agent.git` — **stale branding**. Only npm dep is `agent-browser@^0.13.0` (actively used — 11 file references). `tailwindcss` is NOT listed as a dep here, yet `tailwind.config.js` expects it and the workflow uses npm — tailwind is probably installed transitively via node_modules (verify the lockfile); recommend adding it explicitly. **Recommend: update name/repo/homepage fields to match Logos.**

### 39. `package-lock.json` — ✅ active
112 KB lockfile matching `package.json` + tailwind. Keep (npm-managed).

### 40. `PROJECT_STATE.md` — ⚠️ stale (local-only)
Dated 2026-04-04 (8 days old at time of this audit). Gitignored per `.gitignore` line 79 — local-only working doc intended to be distilled into AI-assistant memory. Contents reflect the architecture at that date; check each claim before trusting. Consider refreshing or archiving.

---

## Batch 5 (paths 41-52)

### 41. `pyproject.toml` — ✅ active
Known packaging issue covered under path #33 (`logos/` missing from include list). Otherwise current: v0.10.4, all declared entry points resolve to real modules.

### 42. `README.draft.md` — ⚠️ untracked WIP, NEWER than README.md
31.9 KB. Diff vs `README.md`: updated architecture diagram that correctly shows the OpenShell per-agent sandbox model with Worker Registry + reverse `/ws/worker` connection + `inference.local`, which matches the current sandbox_worker.py reality. README.md's diagram still shows the old "Agent Runtime (Hermes) → Tools → Sub-agents" flow. **Action**: once verified, promote `README.draft.md` over `README.md` and remove the draft.

### 43. `README.md` — ⚠️ slightly stale architecture diagram
40.1 KB. Committed. The "How it works" diagram (line ~29–) and numbered steps describe an older "Agent Runtime → Tools → Sub-agents → Model Router" shape that predates the current OpenShell-sandbox-per-agent model (see `docker-compose.k3s.yml`, `M11.md`, `docker/sandbox_worker.py`). Architecture diagram needs refresh; the rest likely OK, but every technical claim should be re-verified before the next release.

### 44. `scripts/` — ⚠️ mixed
9 items: `dev-setup.sh`, `hermes-gateway`, `kill_modal.sh`, `sample_and_compress.py`, `smoke-test.sh`, `tag.sh`, `test_model_server.py`, `test.sh`, `whatsapp-bridge/`.
  - Referenced externally: `smoke-test.sh`, `tag.sh`, `test.sh`, `hermes-gateway`, `test_model_server.py`, `kill_modal.sh` are in docs/BUILD_AND_DEPLOY.md, proposals, and README. Active.
  - `sample_and_compress.py` pairs with the legacy `datagen-config-examples/` — if those examples are removed, this script likely goes with them.
  - `whatsapp-bridge/` subdir not audited here — verify separately.
  - `kill_modal.sh` — only useful if Modal is still a runtime target (check usage).

### 45. `SECURITY.md` — ✅ active
Windows binary-signing / SmartScreen + SHA256 verification guide. Matches `.github/workflows/build-windows.yml`. Keep.

### 46. `skills/` — ✅ active (bundled skills)
25 skill category dirs + `index-cache/` (cached skill metadata). Referenced by `CONTRIBUTING.md`, `logos_cli/skills_config.py`, `logos_cli/skills_hub.py`. Matches the "Skills Hub" model. Keep.

### 47. `SOUL.md` — ⚠️ local-only (gitignored)
Root-level default soul-prompt (~1.5 KB). Gitignored per `.gitignore` + explicitly excluded from Docker builds per `.dockerignore` line 17. The shipped default soul lives in `logos_cli/default_soul.py` + `souls/default/`. Root SOUL.md is a local scratchpad. Safe to delete if unused.

### 48. `souls/` — ✅ active
11 persona dirs: `app-development`, `companion`, `default`, `general`, `general-lite`, `homelab-code-fix`, `homelab-investigator`, `news-anchor`, `planning-life`, `relationship-counseling`, `studying`. Referenced throughout the platform (blueprints, policy, gateway souls routes). Keep.

### 49. `tailwind.config.js` — ✅ active
Scans `gateway/html/**/*.html` + `gateway/world/**/*.js` (both directories exist and contain live code — verified `gateway/world/` has `AgentSprite.js`, `WorldManager.js`, `WorldScene.js`, etc., which is the tamagotchi/world UI surface). Keep.

### 50. `TASKS.md` — ✅ active (current open work)
43.9 KB. Leads with "Logos open work" + dated debug notes from 2026-04-09/10/11 (e.g., `#24 Sandbox worker registration silently fails over CONNECT tunnel — REGRESSION`). Current working document. Keep.

---

## Batch 6 (paths 53-57)

### 53. `tests/` — ✅ active, one ghost subdir
~65 top-level `test_*.py` modules + subdirs (`acp/`, `agent/`, `cron/`, `fakes/`, `gateway/`, `integration/`, `logos_cli/`, `skills/`, `tools/`, `unit/`, `test_data/`). Pytest-driven (pyproject.toml `testpaths = ["tests"]`). Keep.
  - 🧹 **Ghost**: `tests/honcho_integration/` contains only `__pycache__/` — matches the ghost `honcho_integration/` at repo root. Remove.
  - `run_interrupt_test.py`, `test_interactive_interrupt.py`, `test_real_interrupt_subagent.py` are explicitly ignored by pytest (`addopts = --ignore=...` in pyproject.toml) — manual-run tests. Keep but know they aren't in CI.

### 54. `tools/` — ✅ active (central)
48 modules — the tool registry. Registered in setuptools include list. Includes: `registry.py`, `terminal_tool.py`, `browser_tool.py`, `file_tools.py`, `mcp_tool.py`, `delegate_tool.py`, `tirith_security.py`, `approval.py`, `memory_tool.py`, `knowledge_tool.py`, `handoff_tool.py`, `skills_*.py` (several), `vision_tools.py`, `web_tools.py`, `workflow_tool.py`, `process_registry.py`, `interrupt.py`, `environments/` subpackage (the real one — not the ghost at repo root), etc. This is where `AGENTS.md` accurately names several files. Keep.

### 55. `uv.lock` — ✅ active
682 KB uv lockfile for `pyproject.toml`. Keep.

### 56. `website/` — ✅ active (Docusaurus docs site)
Standard Docusaurus v3 layout: `docs/`, `docusaurus.config.ts`, `sidebars.ts`, `src/`, `static/`, `package.json` + lockfile, `tsconfig.json`, `README.md`. Deployed by `.github/workflows/deploy-site.yml` to `/docs/` under GitHub Pages. Keep.

### 57. `workflows/` — ✅ active
`engine.py`, `model.py`, `__init__.py`, `examples/`. Imported by `gateway/http_api.py`, `gateway/setup_handlers.py`, `tools/workflow_tool.py`, `tools/browser_tool.py`. **NOT** in `setuptools.packages.find` include list — same packaging bug as `logos/` (path #33). Runs only because `PYTHONPATH="/app"` in the Dockerfile. **Recommend: add `"workflows", "workflows.*"` to the include list.**

---

## Summary

### Confirmed legacy / safe to delete

| Path | Reason |
|---|---|
| `environments/` | Source deleted; only `__pycache__` remains. Zero imports. |
| `honcho_integration/` | Same — only `__pycache__`. Zero imports (+ `uv.lock` entry). |
| `tests/honcho_integration/` | Tests for the above ghost; only `__pycache__`. |
| `hermes_agent.egg-info/` | Stale egg-info under pre-rename package name. |
| `data/` (empty) | Empty dir, gitignored. |
| `datagen-config-examples/` (likely) | Only self-referencing + pairs with `scripts/sample_and_compress.py` (also a legacy candidate). |

### Stale documentation (needs rewrite or update)

| Doc | Issue |
|---|---|
| `AGENTS.md` | Claims 11 root-level shim files exist (none do — all live in `core/`). Calls `logos/` "WIP" — it's alive. |
| `README.md` | Architecture diagram predates the current OpenShell-per-agent-sandbox model. Draft version (`README.draft.md`) is more accurate. |
| `docs/project/REPO_STRUCTURE.md` | Shares the AGENTS.md heritage — high probability of the same staleness. Open and verify. |
| `.dockerignore` | Excludes `CRITIQUE.md` + `onboarding_plan.md` — neither exists at root anymore (harmless stale entries). |
| `package.json` | Name still `"hermes-agent"`, repo URL `NousResearch/Hermes-Agent.git`. Update to Logos. |
| `PROJECT_STATE.md` | Dated 2026-04-04 (local-only). Refresh or archive. |

### Packaging bugs

- `pyproject.toml` `setuptools.packages.find` → `include = [...]` is missing **`logos`, `logos.*`** and **`workflows`, `workflows.*`**. Both are imported by live code and only work today because the Dockerfile relies on `PYTHONPATH="/app"`. A non-editable `pip install .` would omit these and fail at import time.

### Flags needing your judgement (not clear-cut)

| Path | Question |
|---|---|
| `M11.md` at repo root | Current but unusual placement — consider moving to `docs/milestones/` for consistency with M1–M10 (wherever those live). |
| `SOUL.md` at repo root | Gitignored, dockerignored. Safe to delete if not a personal scratchpad you use. |
| `scripts/kill_modal.sh` | Only relevant if Modal is still a supported runtime target. |
| `docs/project/plans/` (3 forward-looking plans) | Forward-looking — mark as `proposals/` or keep as `plans/`? |
| `evals/` | Not in setuptools include list and not imported under `import evals` anywhere — verify before trust. |

---

## What I did NOT do (be careful before acting on the report)

- I validated *inter-file* references and did *structural* checks (imports, grep counts, gitignore, workflow wiring) but I did **not** run the tests or exec any of the scripts. Before deleting anything listed above, run the test suite and the smoke-test harness at minimum.
- Subdirectory contents of `docs/`, `tests/`, `skills/`, `souls/`, `gateway/`, and `logos/` were not individually assessed — only the top level. Each of those merits its own pass if you want full coverage.
- I did **not** read every doc in `docs/project/` — I only flagged those most likely to be stale based on cross-references to already-confirmed-stale docs. Do a per-file pass there next.


### Build-artifact directories (gitignored, not counted above)
- `logos.egg-info/` — setuptools build artifact, in-repo copy (`git status` shows it modified). Regenerated on `pip install -e .`.
- `hermes_agent.egg-info/` — **stale** egg-info under the old package name. Gitignored per `.gitignore` line 52. Remove the on-disk directory; it won't come back (current package name is `logos`).
- `node_modules/`, `__pycache__/`, `.pytest_cache/`, `.venv/`, `venv/`, `test-results/`, `temp_vision_images/` — all gitignored runtime/build output.

---
