<p align="center">
  <img src="assets/banner.png" alt="Logos" width="100%">
</p>

# Logos

**A self-hosted platform for agentic AI. Compose agents from first principles — on your hardware, under your rules.**

Logos is a control plane for AI agents. Not a single agent — a platform. Agent runtimes (Hermes by default, others via the adapter interface) plug into the platform; you assemble what you need from five composable dimensions: **Soul**, **Tools**, **Agent**, **Model**, and **Policy**. That combination — a **STAMP** — defines every run Logos records, making every interaction observable, reproducible, and auditable.

Nothing leaves your network by default. No telemetry. No cloud dependency. No black-box behaviour you can't inspect. Run it on a $5 VPS, a homelab Kubernetes cluster, or serverless infrastructure that costs nothing when idle.

---

Cloud AI assistants are opaque by design — you can't inspect what happened, swap the model, change the persona, or restrict what the agent is allowed to do. Single-agent tools give you one thing and call it a platform. Logos gives you actual control: observable runs, pluggable runtimes, explicit policies, and a soul you define and own. Every tool call is recorded. Every approval is logged. Any run can be replayed or cloned. The policy layer enforces hard limits — not just hopes in a system prompt.

---

## Who is it for?

**Homelab enthusiasts** who already run a cluster and want an AI that knows their infrastructure. It can query Prometheus, read logs, SSH into machines, inspect containers, and automate deployments.

**Developers** who want a personal AI dev partner that can browse the web, run code, edit files, search a codebase, and remember how you like to work — without sending your code to a third party.

**Families or households** where different people want different AI experiences: different personalities, different model capabilities, different permission levels, all from one deployment.

**Privacy-conscious users** who want the capability of frontier AI without the data exposure. Local models by default, cloud models as an opt-in with configurable sanitisation.

**Tinkerers** who want a platform they can actually modify, extend, and break without worrying about someone else's SLA.

You could use Logos to:
- Ask "what's causing the high CPU on my NAS?" and get a real answer that checked Prometheus and read the actual logs
- Have a voice conversation on your phone via Telegram while commuting, that remembers your context from last week
- Spin up a research agent that reads 20 web pages, cross-references them, and writes a summary — locally, privately
- Let a household member chat with a friendly, limited-capability assistant while you use the full operator mode
- Automate a canary deploy, smoke test it, and roll back — all by asking in plain English

---

## What Logos does

- **Runs agents** — Hermes (default adapter), with a clean interface for additional runtimes
- **Records everything** — every run captures its full STAMP: agent, model, soul, tools, policy, tool sequence, approval events, token counts, and outcome
- **Enforces policy** — workspace isolation, command approval, filesystem scoping, built-in policy evals
- **Reaches you anywhere** — Telegram, Discord, Slack, WhatsApp, Signal, email, Home Assistant, and CLI — all from a single gateway process
- **Multi-platform chat** — persistent, searchable conversation history stored server-side in SQLite with full-text search across all past conversations
- **Voice input** — speak into the dashboard or Telegram; faster-whisper transcribes locally, no cloud required
- **Image attach** — send images directly in chat; the vision pipeline auto-describes them and passes enriched context to the model
- **Live execution view** — watch in real time what tools the agent is calling, the chain of reasoning, and elapsed time per step
- **AI routing layer** — smart proxy routes requests across machines based on model class, availability, and per-user priority profiles; machine claiming lets users set a preferred inference target
- **Multi-instance spawning** — spin up parallel sub-agents with different soul presets and tool policies for specific tasks
- **Learns and remembers** — agent-curated memory, FTS5 session search with LLM summarisation, Honcho user modelling, autonomous skill creation and self-improvement
- **Runs on your schedule** — first-class cron scheduling with delivery to any connected platform
- **Delegates with structure** — A2A handoffs with explicit contracts, structured I/O validation, and full run lineage
- **Integrates editors** — ACP protocol support for VS Code, Zed, and JetBrains
- **Connects any model** — Anthropic, OpenAI, OpenRouter (200+ models), Nous Portal, or any OpenAI-compatible endpoint; switch with `hermes model`, no code changes
- **Runs anywhere** — local, Docker, SSH, Modal, Daytona, Singularity
- **Cancel mid-response** — abort any in-flight request without waiting for it to finish

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

**Agent** — Hermes is the default runtime. Additional adapters register via `logos/agent/interface.py`. ACP clients (VS Code, Zed, JetBrains) connect through the ACP adapter. The planned agent types below each bring different reasoning strategies and tool policies:

| Agent | Best for |
|-------|----------|
| **Hermes** (current) | General-purpose assistant — full tool loop, memory, skill creation, MCP integrations |
| **Research** | Deep web search, source citation, long-horizon synthesis |
| **Coder** | Code execution sandbox, file editing, git ops, test runner |
| **Concierge** | Minimal tools, fast responses — lightweight Q&A, family-friendly |
| **Operator** | Full infra access, autonomous multi-step execution, no babysitting |
| **Analyst** | Grafana/Prometheus query, data tools, chart generation |

**Model** — `hermes model` to switch. Any OpenAI-compatible endpoint works; set `openai.base_url` in config.

**Policy** — workspace isolation mode (`FULL_ACCESS`, `WORKSPACE_ONLY`, `REPO_SCOPED`, `READ_ONLY`), command approval callbacks, and policy enforcement evals (`hermes evals run policy_enforcement`).

Combining all three gives you precise control over behaviour, cost, and capability. For example:
- **Research agent** + neutral soul + Llama 3.3 70B → local deep research, private, free
- **Coder agent** + your soul + Claude Opus → personal dev partner with full context
- **Concierge agent** + friendly soul + Mistral 7B → always-on quick answers, low resource cost
- **Operator agent** + operator soul + Claude → trusted homelab automation with maximum tool access

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

Per-session state is tracked while running. The dashboard's live execution block shows a badge in its top-right corner: ✅ running (under 180 seconds), ⚠️ slow (over 3 minutes), or 🔴 stuck (backend-flagged). Status syncs from the backend every 4 seconds. Separately, after each tool completes, if it took longer than **30 seconds** a slow-tool warning is logged with the tool name, elapsed time, and thread pool queue depth. Runs that remain in `status='running'` for more than **1 hour** are surfaced as stuck via the metrics DB query and appear in `hermes metrics` / the Prometheus export.

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

## Developer reference

Source lives in `gateway/`, `tools/`, and `agent/`. See [`AGENTS.md`](AGENTS.md) for internals, local dev setup, gateway architecture, and how to add tools.

**On module naming:** internal packages use the `hermes_` prefix (e.g. `hermes_cli`, `hermes_constants`) because [hermes-agent](https://github.com/NousResearch/hermes-agent) is the first agent running on this platform. This is intentional. When additional agent runtimes land, each will bring its own module namespace and plug into the Logos platform layer. The `hermes_` modules belong to the hermes-agent; Logos owns the gateway, router, auth, and dashboard.

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

This project would not exist without the incredible open-source work it stands on:

- **[Nous Research](https://github.com/NousResearch/hermes-agent)** — the hermes-agent that forms the core of this system. Their work on self-improving agents, skill creation, and multi-platform adapters is the foundation everything here is built on.
- **[Anthropic / Claude](https://www.anthropic.com)** — Claude wrote a significant portion of the gateway, UI, tooling, and this documentation.
- **[Ollama](https://github.com/ollama/ollama)** — makes running local LLMs approachable. Powers the homelab GPU machines that handle inference.
- **[LM Studio](https://lmstudio.ai)** — excellent local model serving, especially for experimentation and first-time model setup.
- **[faster-whisper](https://github.com/SYSTRAN/faster-whisper)** — powers in-pod voice transcription without any cloud dependency.
- **[aiohttp](https://github.com/aio-libs/aiohttp)** — the async web framework underpinning the entire gateway and HTTP API.
- **[Alpine.js](https://alpinejs.dev)** — the reactive UI layer for the dashboard. Lightweight and pleasant to work with for a single-file SPA.
- **[Tailwind CSS](https://tailwindcss.com)** — makes the dashboard look polished without writing custom CSS.
- **[marked.js](https://github.com/markedjs/marked)** — client-side Markdown rendering for chat messages.
- **[Talos Linux](https://www.talos.dev)** — the immutable, secure Kubernetes OS running the homelab cluster.
- **[python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot)** — the Telegram adapter that makes Hermes available anywhere.
- **[SQLite](https://www.sqlite.org)** — server-side chat persistence and vector search. Quietly does everything.

---

## Final Words

This project isn't perfect — it was built fast, under pressure, by someone who wanted something that didn't exist yet. Take the good parts and make them better. If you integrate a new agent runtime, open a PR or an issue — that's exactly the kind of thing this project wants to grow toward.
