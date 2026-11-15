# Repository Structure and Naming Conventions

> Defines where things live, what they are called, and why — so the repo stays navigable as it grows.

---

## Top-Level Directory Rules

The repo root should contain only:
- Standard project files: `README.md`, `LICENSE`, `pyproject.toml`, `uv.lock`, `Dockerfile`, `.dockerignore`, `.gitignore`
- Agent persona files that must be at root by convention: `SOUL.md`, `AGENTS.md`
- Runtime entry points that cannot be moved without breaking `CMD` or installed scripts: `cli.py`

**Everything else belongs in a named subdirectory.** A cluttered root is a navigation tax paid on every session.

---

## Directory Map

```
logos/
├── README.md                   Standard project readme
├── LICENSE                     MIT license
├── SOUL.md                     Agent persona (root by convention — Claude Code reads it)
├── AGENTS.md                   Agent configuration hints for Claude Code
├── CONTRIBUTING.md             Contribution guide (keep at root — GitHub surfaces it)
├── pyproject.toml              Python package definition + dependencies
├── uv.lock                     Dependency lockfile
├── Dockerfile                  Container build definition
│
├── docs/                       All documentation
│   ├── project/                Internal planning, architecture, ops docs
│   │   ├── REPO_STRUCTURE.md   ← this file
│   │   ├── BUILD_AND_DEPLOY.md Build pipeline, deploy process, improvements
│   │   ├── CRITIQUE.md         Architecture audit, known gaps, improvement backlog
│   │   ├── COMPARISON.md       Logos vs competing projects
│   │   ├── RELEASE_v*.md       Release notes per version
│   │   └── onboarding_plan.md  Setup wizard planning
│   ├── checkpoint-rollback.md  Checkpoint/rollback guide
│   ├── migration/              Migration guides (versioned breaking changes)
│   └── skins/                  Theming documentation
│
├── gateway/                    HTTP API server, auth, setup wizard, web UI
├── agent/                      Agent loop, prompt builder, runtime adapter
├── tools/                      All agent tools (file, shell, approval, workspace, etc.)
├── core/                       Shared domain models and utilities
│
├── k8s/                        Kubernetes manifests (numbered in apply order)
│   ├── 00-namespace.yaml
│   ├── 01-rbac.yaml
│   ├── ...
│   └── 10-logos-canary-deployment.yaml
│
├── scripts/                    Operational scripts
│   ├── install.sh              Bare-metal install
│   ├── smoke-test.sh           Post-deploy smoke test (to be added)
│   └── promote.sh              Canary → production promotion (to be added)
│
├── tests/                      Test suites
│   ├── unit/                   Fast, no-network tests (run in CI)
│   └── integration/            Tests requiring live services (run manually)
│
├── evals/                      Eval framework and suites
│   └── suites/                 Named eval suites
│
├── skills/                     Agent skills (self-contained capability packages)
├── souls/                      Named soul files (agent personas)
├── workflows/                  JSON workflow definitions (DAG engine)
├── cron/                       Cron scheduler and job definitions
│
├── environments/               Benchmark environments and eval harnesses
├── landingpage/                Static marketing landing page (deployed to GitHub Pages root)
├── website/                    Docusaurus docs site (deployed to GitHub Pages /docs/)
│
└── .github/
    └── workflows/
        ├── tests.yml           CI: run unit tests on push/PR
        ├── build.yml           CI: build + push container image (to be added)
        └── deploy-site.yml     CI: build + deploy GitHub Pages site
```

---

## Naming Conventions

### Files

| Type | Convention | Example |
|---|---|---|
| Python modules | `snake_case.py` | `session_manager.py` |
| Python packages | `snake_case/` with `__init__.py` | `gateway/` |
| K8s manifests | `NN-descriptive-name.yaml` (two-digit prefix for apply order) | `06-deployment.yaml` |
| Documentation | `SCREAMING_SNAKE.md` for important project docs; `kebab-case.md` for guides | `CRITIQUE.md`, `acp-setup.md` |
| Release notes | `RELEASE_v{semver}.md` | `RELEASE_v0.2.0.md` |
| Shell scripts | `kebab-case.sh` | `smoke-test.sh` |
| Workflow definitions | `kebab-case.json` | `daily-digest.json` |
| Soul files | `kebab-case.md` in `souls/` | `souls/focused-dev.md` |

### Branches

| Branch | Purpose |
|---|---|
| `main` | Source of truth — always deployable |
| `feat/description` | Feature branches (short-lived, merge to main via PR) |
| `fix/description` | Bug fix branches |

Currently everything merges direct to main. As the team grows, use short-lived feature branches and PRs to keep main always green.

### Docker image tags

| Tag | Meaning |
|---|---|
| `:canary` | Latest canary build (mutable — tracks main) |
| `:latest` | Latest production build (mutable — tracks promoted releases) |
| `:{git-sha}` | Immutable build tied to a specific commit — use for rollbacks |

The `:canary` and `:latest` tags are mutable and should never be used for rollback. Always use the SHA tag to pin to a specific build.

---

## What Does Not Belong at Root

Move these if they appear at root:

| File/pattern | Move to |
|---|---|
| `*_plan.md`, `*_spec.md`, `*_notes.md` | `docs/project/` |
| `RELEASE_*.md` | `docs/project/` |
| `CRITIQUE.md`, `COMPARISON.md`, `BUILD_AND_DEPLOY.md` | `docs/project/` |
| One-off scripts (`batch_runner.py`, etc.) | `scripts/` or delete if unused |
| Temporary output files | `.gitignore` them; never commit |
| `*.example` config files | `docs/` or `scripts/` with a README |
| `node_modules/` | Should be in `.gitignore` — never committed |
| `venv/` | Should be in `.gitignore` — never committed |
| `logs/`, `temp_*/` | Should be in `.gitignore` — never committed |
| `__pycache__/`, `*.egg-info/` | Should be in `.gitignore` — never committed |

---

## Current Root Cleanup Backlog

These files currently sit at root and should be moved or reviewed:

| File | Action |
|---|---|
| `batch_runner.py` | Move to a `runners/` package or `scripts/` |
| `mini_swe_runner.py` | Move to a `runners/` package or `scripts/` |
| `rl_cli.py` | Move to a `runners/` package or `scripts/` |
| `metrics.py` | Move to `core/` |
| `runs.py` | Move to `core/` |
| `utils.py` | Move to `core/` |
| `cli-config.yaml.example` | Move to `docs/` with explanation |
| `minisweagent_path.py` | Move to `scripts/` or delete |
| `data/`, `logs/`, `temp_vision_images/` | Verify `.gitignore` coverage |

**Already completed:**
- `run_agent.py` — deleted; production runtime is now `agents/hermes/agent.py` (entrypoint `agents.hermes.agent:main`)
- `hermes_state.py`, `hermes_time.py`, `hermes_constants.py`, `model_tools.py`, `toolsets.py`, `toolset_distributions.py`, `trajectory_compressor.py` — `core/` is now canonical; root-level files are thin re-export shims
- `setup-hermes.sh` — archived to `archive/`

---

## docs/project/ — What Goes Here

Planning docs, architecture decisions, operational runbooks, and project-level notes that are:
- Written for the development team, not end users
- Not surfaced in the public Docusaurus site
- Likely to be referenced across multiple sessions

| File | Purpose |
|---|---|
| `REPO_STRUCTURE.md` | This file — naming conventions and layout |
| `BUILD_AND_DEPLOY.md` | Build pipeline, deploy process, CI improvements |
| `CRITIQUE.md` | Architecture audit, known gaps, improvement backlog |
| `COMPARISON.md` | Logos vs competing projects |
| `RELEASE_v*.md` | Release notes per version |
| `onboarding_plan.md` | Setup wizard planning notes |
