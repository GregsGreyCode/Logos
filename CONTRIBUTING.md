# Contributing to Logos

Logos v2 is a private project for two people, rebuilt on the `v2` branch. The plan, epics and
stories are in the spec: https://claude.ai/artifact/JpSGvsY25zM7VHPdce6v6P

Read [README.md](README.md) first for what exists today, and
[docs/project/REPO_STRUCTURE.md](docs/project/REPO_STRUCTURE.md) for where things live.

## Ground rules

- **Agents are upstream hermes, unmodified.** Do not patch hermes or copy its code in. Change
  behaviour through the agent definition (`agents/<name>.yaml`), the soul, skills, the sandbox
  policy or the jobs in `infra/agent-jobs/`.
- **The brain is the durable part.** Sandboxes and containers are disposable. Anything that
  must survive belongs on the agent's home or brain volume
  ([ADR 0001](docs/adr/0001-agent-brains-on-docker-volumes.md)).
- **Keep it small.** There are two users and three agents. Do not add multi-tenant features,
  roles, routing layers or provider abstractions.
- **Naming.** Gateway settings are `LOGOS_*` environment variables. `HERMES_*` names are
  reserved for upstream hermes (for example `HERMES_HOME=/opt/data` inside a sandbox).
- **No secrets in git.** Tokens and personal IDs go in the environment or in untracked
  `agents/<name>.local.yaml` files.

## Development setup

Requirements: Python 3.11 or later. To create real agents you also need Docker Engine 28 or
later and OpenShell 0.1.8 ([infra/openshell/README.md](infra/openshell/README.md)).

```shell
git clone https://github.com/GregsGreyCode/Logos.git
cd Logos
git checkout v2
python3 -m venv venv && . venv/bin/activate
pip install -e ".[all,dev]"
```

Run the gateway against a throwaway home so you never touch a real `~/.logos`:

```shell
export LOGOS_HOME="$(mktemp -d)" LOGOS_PORT=18091
logos doctor
logos gateway run
```

## Tests

```shell
python -m pytest -q tests
```

- The suite must be green before a change is merged. CI runs it on pushes and pull requests
  to `main` and `v2`.
- Tests never touch the real `~/.logos` or `~/.hermes`: `tests/conftest.py` points
  `LOGOS_HOME` and `HERMES_HOME` at a temp directory.
- Delete a test only when the code it tests is deleted. Never weaken a test of code that
  remains.
- `evals/` holds checks that need a live sandbox. They are run by hand, not by CI.

## Where to make a change

| You want to change | Look at |
| --- | --- |
| How a sandbox is created or configured | `gateway/agent_runtime.py` (used by `scripts/logos-agent` and the gateway) |
| An agent's model, schedule or Telegram settings | `agents/<name>.yaml` |
| What an agent may read, write or reach | `infra/agent-image/policy.yaml` |
| The agent image | `infra/agent-image/Dockerfile` |
| Brain layout and rules | `infra/brain-template/`, `infra/agent-jobs/` |
| Brain sync to devices | `scripts/logos-sync`, `docs/brain-sync.md` |
| Gateway HTTP API | `gateway/http_api.py` and the `*_handlers.py` modules |
| Login, accounts, sessions | `gateway/auth/` |
| Dashboard | `gateway/html/main_app.html` (one Alpine.js file) |
| How the gateway finds and controls sandboxes | `gateway/sandbox_backend.py` |
| The `logos` command | `logos_cli/` |

Record a decision that is hard to reverse as an ADR in `docs/adr/`.

## Pull requests

- Branch from `v2`. Name the branch after the story, for example `s1.11-rename-docs`.
- One story or one logical change per pull request.
- Commit messages follow Conventional Commits with the story as scope:

  ```
  <type>(sN.N): <summary>
  ```

  Types: `feat`, `fix`, `refactor`, `docs`, `test`, `chore`.
- In the description say what changed, why, and how you checked it. Call out anything that
  affects security (sandbox policy, auth, secrets, network exposure).

## Reporting issues

Use [GitHub Issues](https://github.com/GregsGreyCode/Logos/issues). For a security problem see
[SECURITY.md](SECURITY.md).

## License

By contributing, you agree that your contributions are licensed under the
[MIT License](LICENSE).
