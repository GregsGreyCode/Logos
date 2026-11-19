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
├── .env.example              gateway settings (LOGOS_* variables)
├── tailwind.config.js        Tailwind build config for the login and setup stylesheet
│
├── agents/                   agent definitions, one YAML per agent
│                             (<name>.local.yaml files are untracked machine overrides)
│
├── infra/
│   ├── agent-image/          Dockerfile for logos-agent:<hermes version>, sandbox policy, README
│   ├── openshell/            OpenShell 0.1.8 gateway config, providers, install README
│   ├── brain-template/       starting layout and rules of a brain vault
│   ├── agent-jobs/           jobs that run inside a sandbox (consolidation, brain index, commit)
│   └── systemd/              user units: the gateway, the nightly backup export
│
├── scripts/                  commands a person (or the dashboard's Update button) runs
│   ├── logos-agent           create, inspect and remove agents
│   ├── logos-deploy          bring a machine up to date with v2 (plan, rollback, history)
│   ├── logos-models          the model gateway and LM Studio
│   ├── logos-calendar        the calendar server and people
│   ├── logos-sync            Syncthing for the brains (Obsidian on the phones)
│   ├── logos-eval            agent evals against the bench agent
│   └── logos-backup-export   the nightly export for the offsite backup
│
├── souls/
│   ├── general/              the person agents' persona (Greg, Béré, dev, bench)
│   ├── household/            the household agent's persona
│   └── _shared/              rules added to every soul: brain, noticeboard, voice, calendar, web,
│                             house (lights; household agent only)
│
├── gateway/                  the Logos gateway (aiohttp) and the services it runs
│   ├── run.py                process entry point (`logos gateway run`): env, logs, HTTP server
│   ├── http_api.py           app setup and routes (sign-in, setup, dashboard, chat)
│   ├── dashboard_api.py      the dashboard's /api/v2 endpoints
│   ├── html/, ui/            the pages (login, setup, app) and the dashboard's modules
│   ├── auth/                 accounts, passwords, JWT cookies, middleware, roles
│   ├── agent_runtime.py      builds sandboxes from agents/<name>.yaml (shared with scripts)
│   ├── *_host.py             one per service container: model gateway, noticeboard, voice,
│   │                         calendar, Matrix, web reader, house
│   ├── calendar_service.py, web_service.py, house_service.py, model_guard.py
│   │                         the code those containers run (stdlib only)
│   └── lib/                  shared helpers (state DB, redaction)
│
├── logos_cli/                the `logos` command: gateway run/start/stop/status, doctor, version
├── evals/                    eval cases for scripts/logos-eval
├── tests/                    pytest suite (what CI runs)
├── assets/                   logo, icons, web app manifest, login stylesheet
│
├── docs/
│   ├── *.md                  one page per service (calendar, matrix, web-reader, house, messages, backups, …)
│   ├── adr/, architecture/   decisions and architecture notes
│   ├── project/              this file
│   └── archive/v1/           v1 planning, design and migration documents, history only
│
└── .github/workflows/
    └── tests.yml             pytest on pushes and pull requests
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

## Still from v1

The auth database (`~/.logos/auth.db`) still has v1 tables that nothing reads (channel
credentials, MCP servers, approvals, evolution, dispatches). They are left in place: dropping
them is a migration on live data, worth doing only alongside some other schema change.
