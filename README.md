<p align="center">
  <img src="assets/banner.png" alt="Logos" width="100%">
</p>

# Logos ◆

**A self-hosted platform for agentic AI. Compose agents from first principles — on your hardware, under your rules.**

Logos is a control plane for AI agents. Not a single agent — a platform. Agent runtimes (Hermes by default, others via the adapter interface) plug into the platform; you assemble what you need from five composable dimensions: **Soul**, **Tools**, **Agent**, **Model**, and **Policy**. That combination — a **STAMP** — defines every run Logos records, making every interaction observable, reproducible, and auditable.

Nothing leaves your network by default. No telemetry. No cloud dependency. No black-box behaviour you can't inspect. Run it on a $5 VPS, a homelab Kubernetes cluster, or serverless infrastructure that costs nothing when idle.

---

Cloud AI assistants are opaque by design — you can't inspect what happened, swap the model, change the persona, or restrict what the agent is allowed to do. Single-agent tools give you one thing and call it a platform. Logos gives you actual control: observable runs, pluggable runtimes, explicit policies, and a soul you define and own. Every tool call is recorded. Every approval is logged. Any run can be replayed or cloned. The policy layer enforces hard limits — not just hopes in a system prompt.

---

## What Logos does

- **Runs agents** — Hermes (default adapter), with a clean interface for additional runtimes
- **Records everything** — every run captures its full STAMP: agent, model, soul, tools, policy, tool sequence, approval events, token counts, and outcome
- **Enforces policy** — workspace isolation, command approval, filesystem scoping, built-in policy evals
- **Reaches you anywhere** — Telegram, Discord, Slack, WhatsApp, Signal, email, Home Assistant, and CLI — all from a single gateway process
- **Learns and remembers** — agent-curated memory, FTS5 session search with LLM summarisation, Honcho user modelling, autonomous skill creation and self-improvement
- **Runs on your schedule** — first-class cron scheduling with delivery to any connected platform
- **Delegates with structure** — A2A handoffs with explicit contracts, structured I/O validation, and full run lineage
- **Integrates editors** — ACP protocol support for VS Code, Zed, and JetBrains
- **Connects any model** — Anthropic, OpenAI, OpenRouter (200+ models), Nous Portal, or any OpenAI-compatible endpoint; switch with `hermes model`, no code changes
- **Runs anywhere** — local, Docker, SSH, Modal, Daytona, Singularity

---

## Platform pillars

Five functional layers that make up the platform:

| Pillar | What it does |
|--------|-------------|
| **Chat / Gateway** | Conversational agent over Telegram, Discord, Slack, WhatsApp, Signal, email, and HTTP; multi-session, streaming, always-on concurrent input |
| **Policy & Trust** | Per-user action policies (write, exec, filesystem, provider, network, secret) with approval gates and provider trust enforcement |
| **Workflows** | JSON-defined task graphs — DAG execution, parallel steps, conditional branching, human approval gates |
| **Run Auditability** | Every agent request produces a run record: tool timeline, policy snapshot, model used, output summary, clone-to-chat replay |
| **Sandboxed Execution** | Ephemeral per-run workspaces, filesystem path enforcement, dry-run simulation for safe rehearsal |

---

## The STAMP model

Every run in Logos is defined by five dimensions:

| | |
|---|---|
| **S** — Soul | The persona: how the agent communicates, reasons, and behaves |
| **T** — Tools | The capabilities available: what the agent can reach and act on |
| **A** — Agent | The runtime: which adapter processes the conversation |
| **M** — Model | The brain: which LLM drives the agent |
| **P** — Policy | The rules: what the agent is allowed to do, approve, or refuse |

Compose these five and you have an agent. Change any one dimension and you have a different agent. Every STAMP is recorded in full — compare runs across configurations, replay them exactly, or clone them into new sessions. The soul lives in `SOUL.md`, editable without a restart. Tools are scoped per platform and per session. The agent adapter is switchable. The model switches without code changes. Policy is enforced at the workspace and approval layers, not just in the prompt.

---

## Quick Install

```bash
curl -fsSL https://raw.githubusercontent.com/GregsGreyCode/logos/main/scripts/install.sh | bash
```

Works on Linux, macOS, and WSL2. The installer handles everything — Python, Node.js, dependencies, and the `hermes` command. No prerequisites except git.

> **Windows:** Native Windows is not supported. Please install [WSL2](https://learn.microsoft.com/en-us/windows/wsl/install) and run the command above.

After installation:

```bash
source ~/.bashrc    # reload shell (or: source ~/.zshrc)
hermes              # start chatting
```

---

## Getting Started

```bash
hermes              # Interactive CLI — start a conversation
hermes model        # Choose your LLM provider and model
hermes tools        # Configure which tools are enabled
hermes config set   # Set individual config values
hermes gateway      # Start the messaging gateway (Telegram, Discord, etc.)
hermes setup        # Run the full setup wizard (configures everything at once)
hermes claw migrate # Migrate from OpenClaw
hermes update       # Update to the latest version
hermes doctor       # Diagnose any issues
```

---

## Customising your STAMP

**Soul** — edit `~/.hermes/SOUL.md` at any time. Changes take effect on the next message; no restart needed. The soul is the fastest way to change how the agent behaves without touching config or code.

**Tools** — `hermes tools` to enable or disable per platform. The `toolsets` key in `~/.hermes/config.yaml` sets the default.

**Agent** — Hermes is the default runtime. Additional adapters register via `logos/agent/interface.py`. ACP clients (VS Code, Zed, JetBrains) connect through the ACP adapter.

**Model** — `hermes model` to switch. Any OpenAI-compatible endpoint works; set `openai.base_url` in config.

**Policy** — workspace isolation mode (`FULL_ACCESS`, `WORKSPACE_ONLY`, `REPO_SCOPED`, `READ_ONLY`), command approval callbacks, and policy enforcement evals (`hermes evals run policy_enforcement`).

---

## Observability

```bash
hermes runs list                        # recent runs with status and token counts
hermes runs detail <run_id>             # full tool trace, approval events, outcome
hermes runs replay <run_id>             # re-run a message in the same session
hermes runs clone <run_id>              # seed a new session from a prior run
hermes evals run <suite>                # execute an eval suite
hermes evals results                    # view past eval results
hermes metrics                          # usage dashboard
hermes metrics prometheus               # Prometheus export for scraping
```

---

## Migrating from OpenClaw

```bash
hermes claw migrate              # interactive migration (full preset)
hermes claw migrate --dry-run    # preview what would be migrated
hermes claw migrate --preset user-data   # migrate without secrets
hermes claw migrate --overwrite  # overwrite existing conflicts
```

What gets imported: SOUL.md, memories, skills, command allowlist, messaging settings, API keys, TTS assets, workspace instructions.

---

## Contributing

```bash
git clone https://github.com/GregsGreyCode/logos.git
cd logos
git submodule update --init mini-swe-agent
curl -LsSf https://astral.sh/uv/install.sh | sh
uv venv .venv --python 3.11
source .venv/bin/activate
uv pip install -e ".[all,dev]"
uv pip install -e "./mini-swe-agent"
python -m pytest tests/ -q
```

> **RL Training (optional):** To work on the RL/Tinker-Atropos integration:
> ```bash
> git submodule update --init tinker-atropos
> uv pip install -e "./tinker-atropos"
> ```

---

## License

MIT — see [LICENSE](LICENSE).

---

## Thanks

<!-- Add your thanks here -->

---

## Final Words

<!-- Add your closing words here -->
