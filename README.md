<p align="center">
  <img src="assets/banner.png" alt="Logos" width="100%">
</p>

> **Early alpha** — core gateway, auth, dashboard, and setup wizard work. Expect rough edges; breaking changes between releases are likely. If you hit a bug, open an issue.

**A self-hosted platform for agentic AI. Compose agents from first principles on your own hardware, under your rules.**

Logos is a control plane for AI agents — not a single agent, but a platform. Agent runtimes plug in; you assemble what you need from five composable dimensions: **Soul**, **Tools**, **Agent**, **Model**, and **Policy** and you request an instance. 

That combination — a **STAMP** — defines every run executed and Logos records, making every agent interaction observable, reproducible, and auditable.

No black-box behaviour you can't inspect. Run your agents on a  $5 VPS, a homelab Kubernetes cluster, or serverless infrastructure.

During onboarding you choose your privacy approach: route agent requests to local models and take advantage of the compute you already have or point to compute addresses or premium cloud providers (Anthropic, OpenAI, OpenRouter).

---

## How it works

```
                        ┌─────────────────────────────────────┐
                        │             Logos Platform           │
                        │                                      │
  Telegram ─────────►  │  Gateway / Router                    │
  Web Dashboard ──────► │    │                                 │
  ACP (IDE) ──────────► │    ▼                                 │
                        │  Auth & Policy Layer                 │
                        │    │                                 │
                        │    ▼                                 │
                        │  Agent Runtime (e.g. Hermes)         │
                        │    │          │                      │
                        │    ▼          ▼                      │
                        │  Tools     Sub-agents                │
                        │    │          │                      │
                        │    └────┬─────┘                      │
                        │         ▼                            │
                        │  Model Router                        │
                        │    │         │         │             │
                        └────┼─────────┼─────────┼────────────┘
                             ▼         ▼         ▼
                          Local     Anthropic  OpenRouter
                          (Ollama)  (Claude)   (200+ models)
```

**Request lifecycle:**

1. A message arrives via Telegram, the web dashboard, or an ACP-connected editor.
2. The **Gateway** authenticates the request and applies the per-user policy snapshot.
3. The **Agent Runtime** (currently Hermes) processes the conversation through its tool loop.
4. Tool calls are executed inside an **isolated workspace** — scoped to the policy level you've set.
5. The **Model Router** dispatches inference to whichever backend you've configured (local GPU, cloud, or both).
6. The completed run is written to SQLite as a **STAMP record** — full tool trace, approval events, token counts, and outcome — queryable and replayable at any time.

**Key boundaries:**

- `gateway/` — the always-on process: HTTP server, Telegram adapter, auth, routing, web dashboard
- `agent/` — runtime adapters; Hermes is the first, additional runtimes plug in via `logos/agent/interface.py`
- `tools/` — capabilities the agent can call; scoped per session and per policy level
- Platform layer (gateway, router, auth, dashboard) is runtime-agnostic; `hermes_*` modules belong to the Hermes runtime specifically

---

## Who is it for?

**Homelab enthusiasts** who want to run agents-as-a-service at home. Once an agent knows your infrastructure it can query Prometheus, read logs, SSH into machines, inspect containers, and automate deployments.

**Developers** who want a personal AI dev partner with IDE integration that can browse the web, run code, edit files, search a codebase, and remember how you work — without sending code to a third party.

**Families or households** where different people want different AI experiences: different personalities, different model capabilities, different permission levels, all from one deployment.

**Privacy-conscious users** who want a local agentic AI experience without the data sharing.

**Bleeding-edge users** who want to try different agents with the premium models — token manager, secrets, and budgeting coming soon.

**Tinkerers** who want a platform where they can test agentic combinations, then modify, extend, and break the platform and its adapters without worrying about SLAs.

Some things you could ask an agent on Logos to do:

- *"Process the newest Prometheus metric labels and build me alerts and a dashboard that provides oversight of these."*
- *"Send me a report every day at 9am about X, Y, and Z — and ask me for feedback."*
- *"Spin up a research task that reads 20 web pages, cross-references them, and writes a summary — locally, privately."*
- *"The last request failed — investigate your logs and agent code to examine the cause."*

---

## What Logos does

- **Runs agents** — Hermes is the current runtime, with a clean adapter interface for additional runtimes
- **Records everything** — every run captures its full STAMP: agent, model, soul, tools, policy, tool sequence, approval events, token counts, and outcome
- **Enforces policy** — workspace isolation, command approval, filesystem scoping, built-in policy evals
- **Reaches you anywhere** — Telegram and a built-in web dashboard, all from a single gateway process
- **Web dashboard** — full chat UI at `http://localhost:8080`; real-time streaming, per-message stats, copy button, voice input, metrics, multiple agent instances, and a live execution panel
- **Persistent history** — searchable conversation history stored server-side in SQLite with full-text search across all past conversations
- **Voice input** — speak via Telegram or the dashboard; faster-whisper transcribes locally by default
- **Image attach** — send images directly in chat; the vision pipeline describes them and passes enriched context to the model
- **Live execution view** — watch in real time what tools the agent is calling, the chain of reasoning, and elapsed time per step
- **AI routing layer** — smart proxy routes requests across machines based on model class, availability, and per-user priority profiles; machine claiming lets users set a preferred inference target
- **Parallel sub-agents** — spawn parallel sub-agents via delegation or Mixture-of-Agents, each with independent tool policies and model selection
- **Learns and remembers** — agent-curated persistent memory, FTS5 session search with LLM summarisation, autonomous skill creation
- **Runs on your schedule** — cron scheduling with delivery to Telegram
- **Delegates with structure** — A2A handoffs with explicit contracts, structured I/O validation, and full run lineage
- **Workflow engine** — JSON-defined task graphs with DAG execution, parallel steps, conditional branching, and human approval gates; examples in `workflows/examples/`
- **Integrates editors** — ACP protocol support for VS Code, Zed, and JetBrains
- **Connects any model** — Anthropic, OpenAI, OpenRouter (200+ models), Nous Portal, or any OpenAI-compatible endpoint
- **Runs anywhere** — local, Docker, SSH, Modal, Daytona, Singularity
- **Cancel mid-response** — abort any in-flight request without waiting for it to finish

---

## Platform pillars

| Pillar | What it does |
|--------|-------------|
| **Chat / Gateway** | Conversational agent over Telegram and HTTP; multi-session, streaming, always-on concurrent input |
| **Policy & Trust** | Per-user action policies (write, exec, filesystem, provider, network, secret) with approval gates and provider trust enforcement |
| **Run Auditability** | Every agent request produces a run record: tool timeline, policy snapshot, model used, output summary, clone-to-chat replay |
| **Workspace Isolation** | Ephemeral per-run workspaces, filesystem path enforcement (Python-level), dry-run simulation for safe rehearsal. True OS-level sandboxing requires container backends (Docker, Modal, etc.) |

---

## The STAMP model

Every run in Logos is defined by five dimensions:

| | |
|---|---|
| **S** — Soul | The persona: how the agent communicates, reasons, and behaves |
| **T** — Tools | The capabilities available: what the agent can reach and act on |
| **A** — Agent | The runtime: which adapter processes the conversation |
| **M** — Model | The brain: which LLMs are called to execute functions |
| **P** — Policy | The rules: what the agent is allowed to do, approve, or refuse |

Compose these five and you have an AI agent. Change any one dimension and you have a different seeded agent. Every STAMP is recorded in full — compare runs across configurations, replay them exactly, or clone them into new sessions.

The soul lives in `SOUL.md`, editable without a restart. Tools are scoped per platform and per session. The agent adapter is switchable. The model switches without code changes. Policy is enforced at the workspace and approval layers, not just in the prompt.

---

## Quick install

> **Before running:** you can inspect the installer first:
> ```bash
> curl -fsSL https://raw.githubusercontent.com/GregsGreyCode/logos/main/scripts/install.sh | less
> ```

```bash
curl -fsSL https://raw.githubusercontent.com/GregsGreyCode/logos/main/scripts/install.sh | bash
```

Works on Linux, macOS, and WSL2. The installer handles everything — Python, Node.js, and dependencies. No prerequisites except git.

> **Windows:** Native Windows is not supported. Please install [WSL2](https://learn.microsoft.com/en-us/windows/wsl/install) and run the command above.

Once installed, start the gateway:

```bash
source ~/.bashrc    # reload shell (or: source ~/.zshrc)
hermes gateway      # start the gateway + web dashboard
```

Then open `http://localhost:8080` in your browser. That's your primary interface.

---

## Getting started

The primary interfaces are the **web dashboard** (`http://localhost:8080`) and **Telegram**. Both are served from the same gateway process.

On first run, the setup wizard walks you through:

1. Choosing your LLM provider and model (local, Anthropic, OpenAI, or OpenRouter)
2. Choosing your agent
3. Enabling the tools you want available to the agent
4. Setting your policy level, workspace isolation mode and soul.md
5. Optionally connecting Telegram

Your configuration lives in `~/.hermes/config.yaml`. You can edit it directly or use the setup wizard again at any time.

---

## Your first 10 minutes

> 📹 *[Video walkthrough coming soon]*

A suggested first session to get a feel for what Logos can do.

**0:00 — Install and start**

Run the installer and start the gateway. Open `http://localhost:8080`. You should see the Logos dashboard.

```bash
curl -fsSL https://raw.githubusercontent.com/GregsGreyCode/logos/main/scripts/install.sh | bash
source ~/.bashrc
hermes gateway
```

**2:00 — Complete the setup wizard**

The wizard launches automatically on first run. Choose a model (cloud API key or local Ollama endpoint), enable a basic toolset, and leave policy at `WORKSPACE_ONLY` for now. You can change everything later.

**4:00 — Send your first message**

Ask the agent something simple. Watch the **live execution panel** on the right — you'll see exactly which tools it calls, in order, and how long each step takes. This is the STAMP model in action.

> *Try: "What can you see about the machine you're running on?"*

**6:00 — Edit your soul**

Open `~/.hermes/SOUL.md` in any editor. Change the agent's name, tone, or give it a specific focus. Save the file — no restart needed. Send another message and notice the difference.

> *Try adding: "Always respond concisely. You are a homelab assistant named Atlas."*

**8:00 — Inspect a run**

From the dashboard, open **Runs**. Click on the run you just created. You'll see the full tool trace, token counts, and outcome. Hit **Clone** to open a new session seeded from that exact configuration.

**10:00 — Where to go next**

- Connect Telegram so you can reach your agent from anywhere
- Swap the model to something local if you haven't already
- Try a more complex prompt — ask it to read a log file, query a URL, or write and run a script
- Explore `workflows/examples/` for pre-built task graphs

---

## Customising your STAMP

**Soul** — edit `~/.hermes/SOUL.md` at any time. Changes take effect on the next message; no restart needed. The soul is the fastest way to change how the agent behaves without touching config or code.

**Tools** — enable or disable per platform via the web dashboard or by editing the `toolsets` key in `~/.hermes/config.yaml`.

**Agent** — choose which runtime processes your conversation. Currently available: **Hermes** (general-purpose, full tool loop). Additional agents register via `logos/agent/interface.py`. ACP clients (VS Code, Zed, JetBrains) connect through the ACP adapter.

What many people think of as "agent types" — researcher, coder, concierge — aren't separate runtimes. They're what you get when you give any agent a purpose-built soul. A research-focused `SOUL.md` plus a web-browsing toolset turns Hermes into a research agent. A minimal soul plus restricted tools gives you a lightweight concierge.

**Model** — switch model via the web dashboard or by setting `openai.base_url` in config for any OpenAI-compatible endpoint. Note: a small number of specialised tools (vision analysis, Mixture-of-Agents) use their own fixed model selections and are not affected by this setting.

**Policy** — set workspace isolation mode (`FULL_ACCESS`, `WORKSPACE_ONLY`, `REPO_SCOPED`, `READ_ONLY`), configure command approval callbacks, and run policy enforcement evals from the dashboard.

---

## Observability

Runs, evals, and metrics are accessible from the web dashboard and as slash commands inside the interactive shell:

```
/runs list          # recent runs with status and token counts
/runs detail <id>   # full tool trace, approval events, outcome
/runs replay <id>   # re-run a message in the same session
/runs clone <id>    # seed a new session from a prior run
/evals run <suite>  # execute an eval suite
/evals results      # view past eval results
/metrics            # usage dashboard
/metrics prometheus # Prometheus export for scraping
```

Per-session state is tracked while running. The live execution view shows what tools the agent is calling and how long each step takes. After each tool completes, if it took longer than **30 seconds** a slow-tool warning is logged with the tool name, elapsed time, and thread pool queue depth. Runs that remain in `status='running'` for more than **1 hour** are surfaced as stuck in `/metrics` and the Prometheus export.

---

## Migrating from OpenClaw

If you have an existing OpenClaw installation, Logos can import your configuration, memories, and skills.

From the web dashboard, navigate to **Settings → Migration**, or use the interactive migration wizard:

```bash
hermes claw migrate              # interactive migration (full preset)
hermes claw migrate --dry-run    # preview what would be migrated
hermes claw migrate --preset user-data   # migrate without secrets
hermes claw migrate --overwrite  # overwrite existing conflicts
```

What gets imported: `SOUL.md`, memories, skills, command allowlist, messaging settings, API keys, TTS assets, workspace instructions.

---

## Optional cloud integrations

These integrations are **disabled by default** and require explicit configuration. Enabling them sends data to third-party cloud providers.

### Honcho (user modelling)

[Honcho](https://app.honcho.dev) is a cloud service by Plastic Labs that builds a persistent model of each user across conversations — tracking preferences, communication style, and context — and feeds that back into the agent on future sessions.

**What it does when enabled:**
- Syncs every conversation message to Honcho's cloud API in a background thread
- Uploads your `MEMORY.md`, `USER.md`, and `SOUL.md` to Honcho on first activation
- Runs inference on Honcho's backend to generate user insights injected into context
- Maintains a "peer card" — a curated fact list about the user inferred over time

**Privacy:** There is no meaningful way to sanitise data sent to Honcho. The service works by reading actual conversation content. If you enable it, your conversations leave your network and are processed by a third party. Do not enable this if data privacy is a requirement.

**To enable:** set `HONCHO_API_KEY` in your environment or `~/.honcho/config.json`. The integration activates automatically when the key is present and does nothing otherwise.

---

## Developer reference

Source lives in `gateway/`, `tools/`, and `agent/`. See [`AGENTS.md`](AGENTS.md) for internals, local dev setup, gateway architecture, and how to add tools.

**On module naming:** internal packages use the `hermes_` prefix (e.g. `hermes_cli`, `hermes_constants`) because Hermes is the first agent runtime on this platform. When additional runtimes land, each will bring its own module namespace and plug into the Logos platform layer. The `hermes_` modules belong to the Hermes runtime; Logos owns the gateway, router, auth, and dashboard.

**Runtime support:**

| Backend | Status |
|---------|--------|
| Local (Ollama) | ✅ First-class |
| Docker | ✅ First-class |
| SSH | ✅ First-class |
| Modal | 🧪 Experimental |
| Daytona | 🧪 Experimental |
| Singularity | 🧪 Experimental |

---

## Building & deploying

Logos is distributed as a container image. The `BUILD_SHA` build argument bakes the git commit short SHA into the image — it appears in the login screen version footer.

**Build and push:**

```bash
docker buildx build \
  --platform linux/amd64 \
  --build-arg BUILD_SHA=$(git rev-parse --short HEAD) \
  -t ghcr.io/gregsgreycode/logos:canary \
  --push \
  .
```

> **Why `--build-arg BUILD_SHA=...` is required:** the Dockerfile defaults to `ARG BUILD_SHA=unknown`. Omit this flag and the version footer will display `unknown` instead of the actual commit SHA.

After pushing, roll out the updated image:

```bash
kubectl rollout restart deployment/logos -n logos
kubectl rollout status  deployment/logos -n logos
```

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
./scripts/test.sh
```

**Why these choices:**
- `uv` — significantly faster than pip for dependency resolution; the project uses it throughout
- Python 3.11 — minimum supported version; 3.12+ untested
- `mini-swe-agent` — a submodule used by the agent's code-editing toolset; required for full functionality

**Test script options:**

```bash
./scripts/test.sh                  # unit tests only — mirrors CI (default)
./scripts/test.sh --integration    # unit + integration tests (requires API keys)
./scripts/test.sh --mini           # mini-swe-agent tests only
./scripts/test.sh --everything     # all suites
./scripts/test.sh --coverage       # generate HTML coverage report in htmlcov/
./scripts/test.sh --no-parallel    # serial output — easier to read tracebacks
./scripts/test.sh -k "test_foo"    # pass extra args through to pytest
```

Integration tests require live API keys (`OPENROUTER_API_KEY`, `OPENAI_API_KEY`, etc.) and hit real external services. Unit tests blank all keys automatically and never make network calls.

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

This project would not exist without the open-source work it stands on:

- **[Anthropic / Claude](https://www.anthropic.com)** — Claude wrote a significant portion of the gateway, UI, tooling, and this documentation.
- **[Nous Research](https://nousresearch.com)** — inspiration and influence on agentic patterns, skill creation, and self-improvement loops.
- **[Ollama](https://github.com/ollama/ollama)** — makes running local LLMs approachable. Powers the homelab GPU machines that handle inference.
- **[LM Studio](https://lmstudio.ai)** — excellent local model serving, especially for experimentation and first-time model setup.
- **[faster-whisper](https://github.com/SYSTRAN/faster-whisper)** — powers in-pod voice transcription without any cloud dependency.
- **[aiohttp](https://github.com/aio-libs/aiohttp)** — the async web framework underpinning the entire gateway and HTTP API.
- **[Alpine.js](https://alpinejs.dev)** — the reactive UI layer for the dashboard. Lightweight and pleasant to work with for a single-file SPA.
- **[Tailwind CSS](https://tailwindcss.com)** — makes the dashboard look polished without writing custom CSS.
- **[marked.js](https://github.com/markedjs/marked)** — client-side Markdown rendering for chat messages.
- **[Talos Linux](https://www.talos.dev)** — the immutable, secure Kubernetes OS running the homelab cluster.
- **[python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot)** — the Telegram adapter that makes Hermes available anywhere.
- **[SQLite](https://www.sqlite.org)** — server-side chat persistence and full-text search. Quietly does everything.

Cloud AI assistants are opaque by design — you can't inspect what happened, swap the model, change the persona, or directly control what the agent is allowed to do. 

Single-agent tools give you one thing and call it a platform/operating system.

Logos gives observable runs, pluggable runtimes, explicit policies, and a soul you define and own. Every tool call is recorded. Every approval is logged. Any run can be replayed or cloned. The policy layer enforces hard limits — not just hopes in a system prompt.

---

## Who is it for?

**Homelab enthusiasts** who want to run agents-as-a-service at home. There's quite a lot of observability to be achieved agentically once the agent knows their infrastructure. It can query Prometheus, read logs, SSH into machines, inspect containers, and automate deployments.

**Developers** who want a personal IDE integrated AI dev partner that can browse the web, run code, edit files, search a codebase, and remember how you work — without sending code to a third party.

**Families or households** where different people want different AI experiences: different personalities, different model capabilities, different permission levels, all from one deployment.

**Privacy-conscious users** who want a local agentic ai exprience without the data sharing. 

**Bleeding-edge users** who want to try different agents out with the top paid for models - token manager, secrets and budgeting coming soon. 

**Tinkerers** who want a platform where they can test out agentic combinations, and improve, modify, update, extend, and break the platform and it's adapters without worrying about SLAs.

You could ask an agent on Logos to:

- "process the newest prom metric labels and build me alerts and a dashboard that provides oversight of these."

- "provide me a report every day at x about y, z and a and ask me for feedback." 

 - "Spin up a research task that reads 20 web pages, cross-references them, and writes a summary — locally, privately

- "the last request failed, investigate your logs and agent code using mcps/socrati code to examine the cause." 

---

## What Logos does

- **Runs agents** — Hermes is the current runtime, with a clean adapter interface for additional runtimes
- **Records everything** — every run captures its full STAMP: agent, model, soul, tools, policy, tool sequence, approval events, token counts, and outcome
- **Enforces policy** — workspace isolation, command approval, filesystem scoping, built-in policy evals
- **Reaches you anywhere** — Telegram and a built-in web dashboard — all from a single gateway process
- **Web dashboard** — full chat UI served directly by the gateway at `http://localhost:8080`; real-time streaming, per-message stats, copy button, voice input, multiple agent instances, and a live execution panel
- **Multi-platform chat** — persistent, searchable conversation history stored server-side in SQLite with full-text search across all past conversations
- **Voice input** — speak via Telegram or the dashboard; faster-whisper transcribes locally by default. 
- **Image attach** — send images directly in chat; the vision pipeline describes them and passes enriched context to the model
- **Live execution view** — watch in real time what tools the agent is calling, the chain of reasoning, and elapsed time per step
- **AI routing layer** — smart proxy routes requests across machines based on model class, availability, and per-user priority profiles; machine claiming lets users set a preferred inference target
- **Parallel sub-agents** — spawn parallel sub-agents via delegation or Mixture-of-Agents, each with independent tool policies and model selection
- **Learns and remembers** — agent-curated persistent memory, FTS5 session search with LLM summarisation, autonomous skill creation
- **Runs on your schedule** — cron scheduling with delivery to Telegram. 
- **Delegates with structure** — A2A handoffs with explicit contracts, structured I/O validation, and full run lineage
- **Workflow engine** — JSON-defined task graphs with DAG execution, parallel steps, conditional branching, and human approval gates; examples in `workflows/examples/`
- **Integrates editors** — ACP protocol support for VS Code, Zed, and JetBrains
- **Connects any model** — Anthropic, OpenAI, OpenRouter (200+ models), Nous Portal, or any OpenAI-compatible endpoint
- **Runs anywhere** — local, Docker, SSH, Modal, Daytona, Singularity
- **Cancel mid-response** — abort any in-flight request without waiting for it to finish

---

## Platform pillars

Four functional layers that make up the platform:

| Pillar | What it does |
|--------|-------------|
| **Chat / Gateway** | Conversational agent over Telegram and HTTP; multi-session, streaming, always-on concurrent input |
| **Policy & Trust** | Per-user action policies (write, exec, filesystem, provider, network, secret) with approval gates and provider trust enforcement |
| **Run Auditability** | Every agent request produces a run record: tool timeline, policy snapshot, model used, output summary, clone-to-chat replay |
| **Workspace Isolation** | Ephemeral per-run workspaces, filesystem path enforcement (Python-level), dry-run simulation for safe rehearsal. True OS-level sandboxing requires container backends (Docker, Modal, etc.) |

---

## The STAMP model

Every run in Logos is defined by five dimensions:

| | |
|---|---|
| **S** — Soul | The persona: how the agent communicates, reasons, and behaves |
| **T** — Tools | The capabilities available: what the agent can reach and act on |
| **A** — Agent | The runtime: which adapter processes the conversation |
| **M** — Model | The brain: which LLMs are called to execute functions |
| **P** — Policy | The rules: what the agent is allowed to do, approve, or refuse |

Compose these five and you have an AI agent. Change any one dimension and you have a different seeded agent. Every STAMP is recorded in full — compare runs across configurations, replay them exactly, or clone them into new sessions. The soul lives in `SOUL.md`, editable without a restart. Tools are scoped per platform and per session. The agent adapter is switchable. The model switches without code changes. Policy is enforced at the workspace and approval layers, not just in the prompt.

---

## Quick Install

> **Before running:** you can inspect the installer first: `curl -fsSL https://raw.githubusercontent.com/GregsGreyCode/logos/main/scripts/install.sh | less`

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

The primary interfaces are the **web dashboard** (`http://localhost:8080` once the gateway is running) and **Telegram** (or any other configured platform adapter). The `hermes` CLI is for configuration and admin operations.

```bash
hermes gateway      # Start the gateway + web dashboard (primary entry point)
hermes              # Open the local interactive shell
hermes model        # Choose your LLM provider and model
hermes tools        # Configure which tools are enabled
hermes config set   # Set individual config values
hermes setup        # Run the full setup wizard (configures everything at once)
hermes claw migrate # Migrate from OpenClaw (predecessor project)
hermes update       # Update to the latest version
hermes doctor       # Diagnose any issues
```

---

## Customising your STAMP

**Soul** — edit `~/.hermes/SOUL.md` at any time. Changes take effect on the next message; no restart needed. The soul is the fastest way to change how the agent behaves without touching config or code.

**Tools** — `hermes tools` to enable or disable per platform. The `toolsets` key in `~/.hermes/config.yaml` sets the default.

**Agent** — choose which agent runs your conversation. Currently available: **Hermes** (general-purpose, full tool loop). Additional agents register via `logos/agent/interface.py`. ACP clients (VS Code, Zed, JetBrains) connect through the ACP adapter.

What many people think of as "agent types" — researcher, coder, concierge — aren't separate runtimes. They're what you get when you give any agent a purpose-built soul. A research-focused `SOUL.md` + a web-browsing toolset turns Hermes into a research agent. A minimal soul + restricted tools gives you a lightweight concierge.

**Model** — `hermes model` to switch. Any OpenAI-compatible endpoint works; set `openai.base_url` in config. Note: a small number of specialised tools (vision analysis, Mixture-of-Agents) use their own fixed model selections and are not affected by this setting.

**Policy** — workspace isolation mode (`FULL_ACCESS`, `WORKSPACE_ONLY`, `REPO_SCOPED`, `READ_ONLY`), command approval callbacks, and policy enforcement evals (`/evals run policy_enforcement`).

---

## Observability

Runs, evals, and metrics are slash commands inside the interactive shell (`hermes`) and are also accessible from the web dashboard:

```bash
hermes              # open the interactive shell, then use slash commands:
/runs list          # recent runs with status and token counts
/runs detail <id>   # full tool trace, approval events, outcome
/runs replay <id>   # re-run a message in the same session
/runs clone <id>    # seed a new session from a prior run
/evals run <suite>  # execute an eval suite
/evals results      # view past eval results
/metrics            # usage dashboard
/metrics prometheus # Prometheus export for scraping
```

Per-session state is tracked while running. The live execution view shows what tools the agent is calling and how long each step takes. After each tool completes, if it took longer than **30 seconds** a slow-tool warning is logged with the tool name, elapsed time, and thread pool queue depth. Runs that remain in `status='running'` for more than **1 hour** are surfaced as stuck in `/metrics` and the Prometheus export.

---

## Migrating from OpenClaw

OpenClaw improvements to skills, souls and tools are worth migrating. If you have an existing OpenClaw installation, Logos can import your configuration, memories, and skills.

```bash
hermes claw migrate              # interactive migration (full preset)
hermes claw migrate --dry-run    # preview what would be migrated
hermes claw migrate --preset user-data   # migrate without secrets
hermes claw migrate --overwrite  # overwrite existing conflicts
```

What gets imported: SOUL.md, memories, skills, command allowlist, messaging settings, API keys, TTS assets, workspace instructions.

---

## Optional cloud integrations

These integrations are **disabled by default** and require explicit configuration. Enabling them sends data to third-party cloud providers. 

### Honcho (user modelling)

[Honcho](https://app.honcho.dev) is a cloud service by Plastic Labs that builds a persistent model of each user across conversations — tracking preferences, communication style, and context — and feeds that back into the agent on future sessions.

**What it does when enabled:**
- Syncs every conversation message to Honcho's cloud API in a background thread
- Uploads your `MEMORY.md`, `USER.md`, and `SOUL.md` to Honcho on first activation
- Runs inference on Honcho's backend to generate user insights injected into context
- Maintains a "peer card" — a curated fact list about the user inferred over time

**Privacy:** There is no meaningful way to sanitise data sent to Honcho. The service works by reading actual conversation content. If you enable it, your conversations leave your network and are processed by a third party. Do not enable this if data privacy is a requirement.

**To enable:** set `HONCHO_API_KEY` in your environment or `~/.honcho/config.json`. The integration activates automatically when the key is present and does nothing otherwise.

---

## Developer reference

Source lives in `gateway/`, `tools/`, and `agent/`. See [`AGENTS.md`](AGENTS.md) for internals, local dev setup, gateway architecture, and how to add tools.

**On module naming:** internal packages use the `hermes_` prefix (e.g. `hermes_cli`, `hermes_constants`) because Hermes is the first agent runtime on this platform. When additional runtimes land, each will bring its own module namespace and plug into the Logos platform layer. The `hermes_` modules belong to the Hermes runtime; Logos owns the gateway, router, auth, and dashboard.

---

## Building & Deploying

Logos is distributed as a container image. The `BUILD_SHA` build argument bakes the git commit short SHA into the image — it appears in the login screen version footer.

**Build and push:**

```bash
docker buildx build \
  --platform linux/amd64 \
  --build-arg BUILD_SHA=$(git rev-parse --short HEAD) \
  -t ghcr.io/gregsgreycode/logos:canary \
  --push \
  .
```

> **Why `--build-arg BUILD_SHA=...` is required:** the Dockerfile defaults to `ARG BUILD_SHA=unknown`. If you omit this flag, the version footer in the login screen will display `unknown` instead of the actual commit SHA.

After pushing, roll out the updated image to your cluster:

```bash
kubectl rollout restart deployment/logos -n logos
kubectl rollout status  deployment/logos -n logos
```

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
./scripts/test.sh
```

**Test script options:**

```bash
./scripts/test.sh                  # unit tests only — mirrors CI (default)
./scripts/test.sh --integration    # unit + integration tests (requires API keys)
./scripts/test.sh --mini           # mini-swe-agent tests only
./scripts/test.sh --everything     # all suites
./scripts/test.sh --coverage       # generate HTML coverage report in htmlcov/
./scripts/test.sh --no-parallel    # serial output — easier to read tracebacks
./scripts/test.sh -k "test_foo"    # pass extra args through to pytest
```

Integration tests require live API keys (`OPENROUTER_API_KEY`, `OPENAI_API_KEY`, etc.) and hit real external services. Unit tests blank all keys automatically and never make network calls.

> **RL Training (optional):** To work on the RL/Tinker-Atropos integration:
> ```bash
> git submodule update --init tinker-atropos
> uv pip install -e "./tinker-atropos"
> ```

---

---

## License

MIT — see [LICENSE](LICENSE).

---

## Thanks

This project would not exist without the open-source work it stands on:

- **[Anthropic / Claude](https://www.anthropic.com)** — Claude wrote a significant portion of the gateway, UI, tooling, and this documentation.
- **[Nous Research](https://nousresearch.com)** — inspiration and influence on agentic patterns, skill creation, and self-improvement loops.
- **[Ollama](https://github.com/ollama/ollama)** — makes running local LLMs approachable. Powers the homelab GPU machines that handle inference.
- **[LM Studio](https://lmstudio.ai)** — excellent local model serving, especially for experimentation and first-time model setup.
- **[faster-whisper](https://github.com/SYSTRAN/faster-whisper)** — powers in-pod voice transcription without any cloud dependency.
- **[aiohttp](https://github.com/aio-libs/aiohttp)** — the async web framework underpinning the entire gateway and HTTP API.
- **[Alpine.js](https://alpinejs.dev)** — the reactive UI layer for the dashboard. Lightweight and pleasant to work with for a single-file SPA.
- **[Tailwind CSS](https://tailwindcss.com)** — makes the dashboard look polished without writing custom CSS.
- **[marked.js](https://github.com/markedjs/marked)** — client-side Markdown rendering for chat messages.
- **[Talos Linux](https://www.talos.dev)** — the immutable, secure Kubernetes OS running the homelab cluster.
- **[python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot)** — the Telegram adapter that makes Hermes available anywhere.
- **[SQLite](https://www.sqlite.org)** — server-side chat persistence and full-text search. Quietly does everything.
