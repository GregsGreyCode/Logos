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
- **Self-improves** — the Evolution system lets agents propose code improvements on a configurable schedule; you review, question, or accept each proposal, with optional frontier AI (Claude/GPT) consultation before deciding
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
| **Evolution** | Agents propose platform improvements on a configurable schedule; human reviews and decides; optional frontier AI consultation before committing |

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

### Windows native installer

A native Windows installer (`.exe`) is available on the [GitHub Releases](https://github.com/GregsGreyCode/logos/releases) page. No WSL2 required — download, run, and Logos starts in the system tray.

---

#### ⚠️ Why Windows shows a warning

Logos is currently unsigned. Code signing certificates require an identity validation process that is not yet available to us, and we prefer to invest in development and transparency rather than pay for a certificate we can't fully back.

Windows SmartScreen may show **"Windows protected your PC"** on first run. Click **"More info" → "Run anyway"** to proceed.

You can verify that what you downloaded is exactly what was built using the methods below.

---

#### 🔐 Build transparency

Logos binaries are built exclusively via GitHub Actions — no local machines, no manual steps, no hidden stages.

- Source → build → artifact pipeline is fully public
- Every release links to the exact CI run that produced it
- [View all builds](https://github.com/GregsGreyCode/logos/actions/workflows/build-windows.yml)

---

#### 🔑 SHA256 integrity verification

Every release publishes SHA256 hashes for both the installer and the inner `Logos.exe`. These appear in the **GitHub Release notes**, in `SHA256SUMS.txt`, and in a `.sha256` sidecar file — all produced by the same CI run.

```powershell
# Windows — replace X.Y.Z with the version you downloaded
certutil -hashfile LogosSetup-X.Y.Z.exe SHA256
# Compare the output to the hash in the GitHub Release notes
```

```bash
# macOS / Linux
sha256sum LogosSetup-X.Y.Z.exe
```

---

#### 🧪 VirusTotal scan

Each release is scanned on [VirusTotal](https://www.virustotal.com) — the scan link is included in the GitHub Release notes for that version. You can also drag-and-drop your downloaded file at [virustotal.com](https://www.virustotal.com) to run your own scan.

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

## Local model benchmarking

When you connect a local inference server (Ollama or LM Studio), the setup wizard automatically benchmarks your available models to find the best one for driving the agent. This is the **"Scanning models"** step.

### Candidate selection

Up to 4 candidates are selected by sampling across **size buckets**: small (<5B), mid (5–13B), large (>13B), and unknown (no size in name). One representative is taken from each bucket, then remaining slots are filled from the best of the remaining candidates.

Within each bucket, models are ranked by quality heuristics:
- **Mid**: closest to the 9B sweet spot (large enough to reason, fast enough to use)
- **Small**: largest available (4–5B beats 1–3B)
- **Large**: smallest available (14B beats 70B on throughput)
- **Unknown**: names containing `instruct`, `chat`, `tool`, `assistant` are preferred

This avoids hard-suppressing unknown-size models or well-quantised large models that may outperform a weak 7B.

### Speed benchmark — tokens per second

Two benchmark passes are run per model on different prompt types to capture throughput across workloads:

| Pass | Prompt type | Purpose |
|------|-------------|---------|
| 1 | Natural language prose | Baseline throughput |
| 2 | Structured JSON output | Throughput under formatting constraints |

The two results are averaged. Some models slow significantly on structured output — this matters because agent workloads mix both types.

Token count is taken from `usage.completion_tokens` in the SSE stream (authoritative). If the server does not return usage data, the fallback is `max(SSE_chunk_count, char_count ÷ 4)` — this is shown as "(~approx)" in the debug log.

Time-to-first-token (TTFT) is measured on pass 1 only (model may still be loading into VRAM). Throughput is measured from **first token to last token**, so cold-start latency does not inflate the tok/s figure.

Speed thresholds:

| Label | Tokens/sec | Notes |
|-------|-----------|-------|
| Fast | ≥ 30 | Comfortable for interactive use |
| Good | ≥ 15 | Responsive for most tasks |
| Usable | ≥ 6 | Acceptable; notable latency on long outputs |
| Slow | < 6 | Likely too large for real-time agent use on this hardware |

### Capability evals — 4 tests

After the speed pass, four capability probes determine whether the model is fit for agentic use:

| # | Test | Prompt | Pass condition |
|---|------|--------|---------------|
| 1 | **Instruction following** | 4-step ordered task: ALPHA, 15+27, BETA, letter count of "elephant" | All four outputs present (ALPHA, 42, BETA, 8) |
| 2 | **Arithmetic reasoning** | Two-part: 150 km in 2.5 h → speed; 17×6−14 | Both answers present (60, 88) |
| 3 | **Strict JSON format** | Reply with ONLY a specific JSON object, no surrounding text | Parses cleanly as JSON with exact field values; extra prose fails |
| 4 | **Tool selection (2 scenarios)** | Route "What is Bitcoin price?" and "Write a Python reverse function" to the right tool | `{"A": "search_web", "B": "run_code"}` — both must be correct |

A model passes the capability bar if it scores **≥ 3/4** tests.

Evals 3 and 4 are strict — no regex fallback. A model that outputs valid JSON surrounded by prose fails eval 3; a model whose tool selection JSON is unparseable fails eval 4. This matches the stricter requirements of real agentic use.

### Scoring formula

Each model receives a composite score:

```
score = 0.45 × (eval_tests_passed / 4)
      + 0.30 × min(tok_s, 40) / 40
      + 0.15 × ttft_score              (1.0 at ≤500ms, 0.0 at ≥4s)
      + 0.10 × min(param_count_B, 13) / 13
```

- **Eval quality (45%)** — weighted most heavily; a fast but unreliable model produces poor agent outcomes.
- **Speed (30%)** — capped at 40 tok/s; returns above that have diminishing value for interactive use.
- **TTFT (15%)** — time-to-first-token affects perceived responsiveness, especially for short interactions where a 4s wait before any output is noticeable even if throughput is high afterward.
- **Model size (10%)** — all else equal, a larger model is preferred; capped at 13B.

The highest-scoring model is recommended as the default.

### VRAM management

Between tests, each model is explicitly unloaded from GPU memory to prevent contention from affecting subsequent throughput measurements:

- **LM Studio**: `POST /api/v1/models/unload` with `{"instance_id": model_id}`
- **Ollama**: `POST /api/generate` with `{"model": model_id, "keep_alive": 0, "prompt": ""}`

### Multi-server parallelism

If you connect more than one inference server, each server's models are tested **in parallel with other servers** — but models on the *same* server are tested sequentially to prevent VRAM contention. Results are merged and scored together.

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

Every log line includes a `[session_id]` field — set via a `contextvars.ContextVar` at the start of each request and injected into all log records by a root-logger filter. This lets you `grep` a single session ID across gateway, agent, and tool logs without any thread-local state.

`GET /healthz` returns per-platform success and error counters (`platform_stats`), useful for spotting silent adapter failures across Telegram, Discord, Slack, and other connected platforms.

**Reliability:** the SQLite session store serialises writes through a `threading.Lock` with WAL mode and `wal_autocheckpoint=100` — concurrent gateway coroutines never race on the same connection. Incoming messages are deduplicated by `platform:message_id` against a 500-entry LRU cache, preventing double-processing on network retries. Cron delivery retries up to three times with exponential backoff before marking a job failed.

---

## Evolution — agent self-improvement

The **Evolution** tab gives agents a structured channel to propose improvements to the platform itself, on a schedule you control.

### How it works

1. **Agents analyse your codebase** on the configured interval (default: once a week). Each agent reads the repository, looks for bugs, complexity hotspots, and `TODO`/`FIXME` comments, and drafts a concrete improvement.
2. **A proposal is submitted** — title, summary, a unified diff, and the list of affected files. The proposal appears in the Evolution tab for your review.
3. **You decide:**
   - **Accept** — mark the proposal for implementation; the diff is applied to a branch in your git fork and a PR is opened.
   - **Decline** — reject it with or without explanation.
   - **Ask a question** — send a clarifying question back to the agent. The agent answers and the proposal returns to pending for re-review.
4. **Optionally consult a frontier model** — before deciding, you can ask Claude or GPT-4o to review the proposal and give an independent assessment.

### Your fork as source of truth

Each Logos deployment works against **your own fork** of the repository. Fork the canonical repo into your GitHub account, configure the fork URL in Evolution Settings, and the agent reads from it and opens PRs against it. This means:

- Your deployment's improvement history is yours — isolated from other users.
- You decide when to pull upstream changes from the canonical repo.
- Accepted proposals land as conventional PRs that you can review, diff, and merge (or not) in your normal workflow.

### Setting up

1. **Fork** the canonical Logos repository to your GitHub account.
2. In the **Evolution tab → Settings**, configure:
   - **Fork remote URL** — your fork's HTTPS URL (`https://github.com/you/logos`)
   - **Username** and **Personal access token** — a GitHub PAT with `repo` scope
   - **Base branch** — the branch PRs will target (default: `main`)
   - **Schedule** — how often agents should run the self-improvement skill (1 hour → 1 year)
   - **Frontier model** — which AI to consult for proposal reviews (Claude or GPT-4o)
3. Toggle **Enabled** to start the schedule.

### Permissions

| Role | Can do |
|------|--------|
| Admin / Operator | View proposals, create proposals, accept/decline/question, consult frontier, configure settings |
| User | View proposals |
| Viewer | View proposals |

### API

```
GET    /evolution/proposals              # list (filterable by status)
GET    /evolution/proposals/{id}         # get one
POST   /evolution/proposals             # create (agents use this)
POST   /evolution/proposals/{id}/decide  # accept / decline / question
POST   /evolution/proposals/{id}/answer  # agent answers a question
POST   /evolution/proposals/{id}/consult # consult a frontier model
GET    /evolution/settings               # get settings
PATCH  /evolution/settings               # update settings
```

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

`k8s/16-network-policy.yaml` ships two `NetworkPolicy` resources that lock down pod traffic: the gateway pod accepts ingress only on port 8080 and can reach DNS (53), HTTPS (443), HTTP (80), and the ai-router pod; the ai-router pod accepts ingress only from gateway pods and can reach DNS, HTTPS, HTTP, and local inference ports (Ollama 11434, LM Studio 1234, vLLM 8000). Apply after confirming your ingress controller namespace label matches the policy.

---

## Contributing

```bash
git clone https://github.com/GregsGreyCode/logos.git
cd logos
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
- `mini-swe-agent` — vendored directly into the repo; powers the agent's code-editing toolset

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
- **[Nous Research / hermes-agent](https://github.com/NousResearch/hermes-agent)** — the Hermes agent runtime (`agents/hermes/`) is a heavily extended fork of their open-source hermes-agent. The platform layer (gateway, auth, dashboard, STAMP system, policy enforcement) is original work built on top of it. The [`tinker-atropos`](https://github.com/NousResearch/tinker-atropos) submodule is also theirs.
- **[SWE-agent / mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent)** — vendored directly into the repo (MIT licence), powers the terminal tool's PTY-based shell execution.
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
