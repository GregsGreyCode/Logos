# Repository structure

Where things live on the `v2` branch. Update this file in the same change that adds, moves or
removes a top-level directory.

## Directory map

```
logos/
├── README.md                 what Logos v2 is, status, quick start
├── CONTRIBUTING.md           how to work on the repo
├── SECURITY.md               how to report a vulnerability
├── LICENSE                   MIT
├── pyproject.toml            Python package (`logos`), dependencies, pytest config
├── uv.lock                   dependency lockfile (CI does not use it)
├── .env.example              gateway settings (LOGOS_* variables)
├── package.json              Node dependencies left from v1 (see "Leftovers")
├── tailwind.config.js        Tailwind build config for the dashboard stylesheet
│
├── agents/                   agent definitions, one YAML per agent
│   ├── greg.yaml             Greg's agent
│   └── bench.yaml            benchmark agent
│                             (<name>.local.yaml files are untracked personal overrides)
│
├── infra/
│   ├── agent-image/          Dockerfile for logos-agent:<hermes version>, sandbox policy, README
│   ├── openshell/            OpenShell 0.1.8 gateway config, providers, install README
│   ├── brain-template/       starting layout and rules of a brain vault
│   └── agent-jobs/           jobs that run inside a sandbox (consolidation, brain index, commit)
│
├── scripts/
│   ├── logos-agent           create, inspect and remove agents (up / down / status / exec)
│   ├── logos-sync            Syncthing for the brains, for opening one in Obsidian
│   └── ...                   v1 helper scripts (see "Leftovers")
│
├── souls/                    persona files; an agent definition points at one
├── skills/                   skills available to agents
├── optional-skills/          skills that are off by default
│
├── gateway/                  the Logos gateway (aiohttp)
│   ├── run.py                process entry point (`logos gateway run`)
│   ├── http_api.py           app setup, routes, chat endpoints
│   ├── *_handlers.py         admin, chat, setup, MCP and other route handlers
│   ├── auth/                 accounts, passwords, JWT cookies, middleware
│   ├── html/                 login.html, setup.html, main_app.html (Alpine.js dashboard)
│   ├── agent_runtime.py      builds sandboxes from agents/<name>.yaml (shared with scripts/logos-agent)
│   ├── sandbox_backend.py    backend seam: NullBackend (default) and OpenShellBackend
│   ├── hermes_client.py      client for the upstream hermes API server
│   ├── sandbox_hooks/        hermes hooks installed into a sandbox
│   ├── mcp_logos/            in-process MCP server exposing Logos capabilities
│   └── lib/                  shared helpers (state DB, redaction, transcription, MCP client)
│
├── logos_cli/                the `logos` command: gateway run/start/stop/status, doctor, version
│
├── evals/
│   ├── agent_writes/         does an agent write its brain correctly (needs a live sandbox)
│   └── brain_checks/         brain rule checks (needs a live sandbox)
│
├── tests/                    pytest suite (what CI runs)
│   ├── gateway/              gateway and auth
│   ├── logos_cli/            CLI
│   ├── unit/                 agent runtime, sandbox backend, brain index
│   ├── skills/               skill scripts
│   └── integration/          excluded from CI
│
├── e2e/                      Playwright tests written for the v1 dashboard (see "Leftovers")
├── assets/                   logo, banner, dashboard stylesheet (tailwind.css)
├── docker/                   v1 sandbox Dockerfiles and an MCP test image (see "Leftovers")
│
├── docs/
│   ├── adr/                  architecture decision records
│   ├── architecture/         architecture notes (hermes API client)
│   ├── brain-sync.md         opening a brain in Obsidian
│   ├── project/              this file
│   └── archive/v1/           v1 planning, design and migration documents, history only
│
└── .github/workflows/
    └── tests.yml             pytest on pushes and pull requests to main and v2
```

## Rules

- The repo root holds only standard project files. Everything else goes in a named directory.
- Host-side tooling goes in `scripts/` (commands a person runs) or `infra/` (things that are
  built, installed or copied into a sandbox).
- Runtime state never lives in the repo. The gateway keeps its state in `~/.logos`
  (`LOGOS_HOME`); an agent keeps its state on its two Docker volumes.
- Gateway settings are `LOGOS_*` environment variables. `HERMES_*` names belong to upstream
  hermes.
- New documents go in `docs/`. Documents about removed v1 features go in `docs/archive/v1/`.

## Leftovers

These survive from v1 and have not been reviewed for v2 yet. Do not build on them.

- `docker/Dockerfile.hermes-sandbox`, `docker/Dockerfile.hermes-upstream`,
  `docker/entrypoint-hermes.sh`, `docker/AGENT-IMAGES.md`: the v1 sandbox image, replaced by
  `infra/agent-image/`. `docker/testing/mcp-echo/` is still referenced by
  `gateway/mcp_catalogue.py`.
- `scripts/dev-setup.sh`, `scripts/test.sh`, `scripts/smoke-test.sh`, `scripts/tag.sh`,
  `scripts/hermes-gateway`, `scripts/whatsapp-bridge/`: v1 developer and release helpers.
- `e2e/`: Playwright tests for dashboard tabs that were removed.
- `package.json`, `package-lock.json`: Node dependencies for v1 browser tools.
