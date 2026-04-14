<p align="center">
  <img src="assets/banner.png" alt="Logos" width="100%">
</p>

<p align="center">
  <strong>Early alpha</strong> — core gateway, auth, dashboard, and setup wizard work.<br>
  Expect rough edges; breaking changes between releases are likely.
  <a href="https://github.com/GregsGreyCode/Logos/issues">Open an issue</a> if you hit a bug.
</p>

<!-- screenshot: hero — main dashboard with 2–3 agents and live sandboxes panel visible.
     Shown below the tagline; this is the first thing most visitors see. -->

> **Release history note:** v0.4 shipped 57 tagged patch releases (v0.4.26–v0.4.105) before graduating to v0.5. Pre-v0.5 tags have been removed from GitHub to keep the releases page clean; the commit history is fully intact.

---

**A self-hosted platform for agentic AI.**

Logos is a control plane for AI agents — not a single agent, but a platform you run on your own hardware under your own rules. You assemble what you need from five dimensions:

> **Soul · Tools · Agent · Model · Policy**

That combination is a **STAMP** — it defines every run Logos records, making every agent interaction observable, reproducible, and auditable. No black-box behaviour you can't inspect.

Run it on your laptop, a homelab box, or a $5 VPS. During the first-run setup wizard you choose your privacy model: local inference (Ollama, LM Studio), self-hosted endpoints, or cloud providers (Anthropic, OpenAI, OpenRouter).

---

## ⚙️ How it works

```
                        ┌──────────────────────────────────────────┐
                        │              Logos Gateway                │
                        │                                           │
  Telegram ───────────► │   HTTP / SSE / WebSocket entry point      │
  Web Dashboard ──────► │     ├── Auth + per-user policy            │
  ACP (IDE) ──────────► │     ├── MCP Gateway ──► MCP servers       │
                        │     │     (one process,    (filesystem,   │
                        │     │      shared by all    GitHub, etc.) │
                        │     │      sandboxes)                     │
                        │     ▼                                     │
                        │   Worker Registry                         │
                        │     │                                     │
                        │     │  openshell sandbox exec (per task)  │
                        │     │  stdin: task JSON                   │
                        │     │  stdout: token/thinking/result JSON │
                        │     ▼                                     │
                        │   ┌─────────────────────────────────────┐ │
                        │   │  OpenShell Sandbox (per agent)       │ │
                        │   │    sandbox_worker.py ─► inference.local──► Local Ollama
                        │   │      tools, MCP, sandbox FS          │ │     LM Studio
                        │   └─────────────────────────────────────┘ │     Anthropic
                        │                                           │     OpenAI
                        └───────────────────────────────────────────┘     OpenRouter
```

<!-- screenshot: architecture-in-ui — a dashboard view that makes the ASCII boxes
     tangible: gateway process panel, worker registry table, a live sandbox. -->

**Request lifecycle:**

1. A message arrives via Telegram, the web dashboard, or an ACP-connected editor.
2. The **gateway** authenticates the request and applies the per-user policy snapshot.
3. The gateway finds the target agent's existing sandbox via the **worker registry** (reads `~/.logos/openshell_instances.json`; healthy means the sandbox CR is `phase == "ready"`).
4. The gateway spawns a one-shot **`openshell sandbox exec`** into that sandbox, running `docker/sandbox_worker.py`. The task JSON is written to the subprocess's stdin and stdin is closed — OpenShell's exec transport only starts the in-sandbox process once stdin reaches EOF.
5. The worker runs the conversation through its tool loop, calling models via OpenShell's `inference.local` Privacy Router (which strips sandbox credentials and injects the real provider keys outside the isolation boundary).
6. The worker streams `token` / `thinking` / `tool_progress` JSON lines on stdout as they arrive; the gateway forwards them to the dashboard over SSE and collects the terminal `task_result` frame. The subprocess exits; the sandbox stays up for the next task.
7. The completed run is written to SQLite as a **STAMP record** — full tool trace, approval events, token counts, outcome — queryable and replayable later.

**Key boundaries:**

- `gateway/` — the always-on process: HTTP server, Telegram adapter, auth, routing, web dashboard, MCP gateway, worker registry
- `agents/hermes/` — the Hermes runtime that runs *inside* sandbox workers
- `tools/` — capabilities the agent can call; scoped per session and per policy level
- `gateway/executors/` — the two runtime backends: `openshell.py` (default) and `docker.py`
- `docker/sandbox_worker.py` — the lightweight worker that runs inside an OpenShell sandbox, invoked per task by `openshell sandbox exec`

---

## 👥 Who is it for?

### 🏠 Homelab enthusiasts
Run agents-as-a-service across your infrastructure. Once an agent knows your setup it can query Prometheus, read logs, SSH into machines, inspect containers, and automate deployments.

### 👨‍💻 Developers
A personal AI dev partner with IDE integration that browses the web, runs code, edits files, searches codebases, and remembers how you work — without sending code to a third party.

### 🏡 Households
Different people, different agents: different personalities, different model capabilities, different permission levels — all from one deployment.

### 🔒 Privacy-conscious users
Local-first agentic AI. Your data stays on your hardware.

### 🧪 Tinkerers
Test agentic combinations, then modify, extend, and break the platform and its adapters without worrying about SLAs.

**Some things you could ask an agent on Logos:**

- *"Process the newest Prometheus metric labels and build me alerts and a dashboard."*
- *"Send me a report every day at 9am about X, Y, and Z — and ask me for feedback."*
- *"Spin up a research task that reads 20 web pages, cross-references them, and writes a summary — locally, privately."*
- *"The last request failed — investigate your logs and agent code to examine the cause."*

---

## 🚀 What Logos does

- **Runs agents** — Hermes is the current runtime. The runtime layer is pluggable; additional runtimes can register as alternative sandbox worker images.
- **Records everything** — every run captures its full STAMP: agent, model, soul, tools, policy, tool sequence, approval events, token counts, and outcome
- **Enforces policy** — workspace isolation, command approval, filesystem scoping, OpenShell egress policy, built-in policy evals
- **Reaches you anywhere** — Telegram and a built-in web dashboard, all from a single gateway process
- **Web dashboard** — full chat UI at `http://localhost:8091`; real-time streaming, per-message stats, voice input, metrics, multiple named agents, world view with live agent sprites, live execution panel
- **Persistent history** — searchable conversation history in SQLite with full-text search across all past conversations
- **Voice input** — speak via Telegram or the dashboard; faster-whisper transcribes locally by default
- **Image support** — send images directly; the vision pipeline enriches context before passing it to the model
- **Live execution view** — watch in real time which tools the agent calls, its chain of reasoning, and elapsed time per step
- **AI routing layer** — routes requests across machines based on model class, availability, and per-user priority profiles
- **Parallel sub-agents** — spawn sub-agents via delegation or Mixture-of-Agents, each with independent tool policies and model selection
- **MCP gateway** — centralized Model Context Protocol server management; MCP servers boot once in the gateway, agents request access dynamically with per-category approval tiers
- **Memory system** — agent-curated persistent memory, FTS5 session search with LLM summarisation, autonomous skill creation
- **Scheduling** — cron jobs with Telegram delivery
- **Workflow engine** — JSON-defined task graphs with DAG execution, parallel steps, conditional branching, and human approval gates
- **Self-improvement** — the Evolution system lets agents propose code improvements on a schedule; you review, question, or accept each proposal
- **IDE integration** — ACP protocol for VS Code, Zed, and JetBrains
- **Model support** — Anthropic, OpenAI, OpenRouter (200+ models), Nous Portal, or any OpenAI-compatible endpoint
- **Cancel mid-response** — abort any in-flight request without waiting for it to finish

---

## 🧬 The STAMP model

Every run in Logos is defined by five dimensions:

| | |
|---|---|
| **S** — Soul | The persona: how the agent communicates, reasons, and behaves |
| **T** — Tools | The capabilities available: what the agent can reach and act on |
| **A** — Agent | The runtime: which adapter processes the conversation |
| **M** — Model | The brain: which LLMs are called to execute functions |
| **P** — Policy | The rules: what the agent is allowed to do, approve, or refuse |

Compose these five and you have an AI agent. Change any one dimension and you have a different seeded agent. Every STAMP is recorded in full — compare runs across configurations, replay them exactly, or clone them into new sessions.

The soul lives in `SOUL.md`, editable without a restart. Tools are scoped per agent and per session. The agent adapter is switchable. The model switches without code changes. Policy is enforced at the workspace, OpenShell sandbox, and approval layers — not just in the prompt.

---

## 🔒 Security & deployment model

**Understanding the isolation boundary matters before you choose how to run Logos.** Agents can read files, execute code, and make network requests — what they _cannot_ reach depends entirely on which runtime mode you pick.

### Runtime modes at a glance

Logos has two runtime modes selected by `runtime.mode` in `~/.logos/config.yaml` (or via the setup wizard). They are the two branches of `gateway/executors/build_executor()`:

| Mode | Default? | How it spawns | Isolation boundary | Egress policy | Platform |
|------|---|---------------|-------------------|---------------|----------|
| **`openshell`** | ✅ default | OpenShell CLI provisions a persistent sandbox per agent; the gateway dispatches each task via a one-shot `openshell sandbox exec` subprocess, piping task JSON on stdin and reading event JSON on stdout. Inference egress goes through OpenShell's HTTP CONNECT proxy to the `inference.local` Privacy Router. | Kernel Landlock LSM (filesystem) + OpenShell egress allowlist (network) + container | Per-binary YAML egress policy (`gateway/policies/openshell_default.yaml`) | Linux, macOS |
| **`docker`** | fallback | Plain Docker container — `--cap-drop=ALL`, `--security-opt=no-new-privileges`, no host filesystem mounts | Docker container | None (outbound unrestricted) | Linux, macOS, Windows (via Docker Desktop) |

> **What happened to the Kubernetes and local-process executors?** The Kubernetes pod-per-agent executor was deleted in commit `f6f0972 chore: drop legacy k8s pod-per-agent executor + mini-swe-agent terminal backends`. The `LocalProcessExecutor` (running agents as subprocesses of the gateway) was removed at the same time once OpenShell became the only sandbox runtime exposed in `/setup`. The `k8s/` manifests still work for **deploying the gateway itself** as a Kubernetes Deployment — see [`k8s/README.md`](k8s/README.md) — but agents inside that gateway use OpenShell or Docker just like everywhere else. Any legacy `runtime.mode` value other than `docker` now coerces to OpenShell.

### Defense layers

Agent security is defense-in-depth — multiple independent layers, not a single boundary:

| Layer | What it does | Where it runs |
|-------|-------------|---------------|
| **Workspace scoping** | Restricts file read/write to the agent's workspace directory. Symlink-safe (`realpath` before access check). | All modes |
| **Toolset enforcement** | Agents can only call tools in their enabled toolset. Validated at agent init and registry dispatch. | All modes |
| **API key filtering** | Sandbox workers never receive provider API keys. They call `inference.local`, and the OpenShell Privacy Router (running outside the sandbox) injects the real credentials. | OpenShell only |
| **Command review** | Regex patterns catch common destructive shell commands (`rm -rf /`, `DROP TABLE`, `chmod 777`, etc.). Prompts for approval before execution. | All modes |
| **Tirith scanning** | Pre-execution semantic analysis of shell commands for content-level threats (homograph URLs, pipe-to-interpreter, terminal injection). Auto-installed from [GitHub releases](https://github.com/sheeki03/tirith). | Linux, macOS |
| **Filesystem isolation** | Landlock LSM declarative read-only / read-write policy enforced by the kernel. | OpenShell only |
| **Egress policy** | Per-binary YAML allowlist (`network_policies` in OpenShell policy). | OpenShell only |
| **Container isolation** | Docker container with `--cap-drop=ALL`, `--security-opt=no-new-privileges`, no host filesystem mounts. | OpenShell, Docker |

**Command review** catches obvious destructive patterns but is bypassable with interpreter one-liners (e.g. `python -c "import shutil; ..."`). It is a convenience layer, not a security boundary. The real protection comes from workspace scoping (all modes), kernel-level filesystem and egress policy (OpenShell), and container isolation (OpenShell, Docker).

**Tirith** is not available on Windows. When absent, the command review regex patterns are the only pre-execution check. On Linux/macOS, Tirith is auto-downloaded at startup and provides deeper analysis.

### Secrets and auth

**`LOGOS_JWT_SECRET`** _(legacy alias: `HERMES_JWT_SECRET` — still accepted)_
All session tokens are signed with this secret. Generate it once with `openssl rand -hex 32` and store it somewhere safe.
- If you lose it, all active sessions are invalidated on next restart (users will need to log in again — no data is lost).
- Rotating it intentionally: change the value, restart Logos.
- Never commit it to version control.

**`LOGOS_COOKIE_SECURE`** _(legacy alias: `HERMES_COOKIE_SECURE`)_
Set to `true` if Logos is behind an HTTPS reverse proxy (nginx, Caddy, Traefik). This adds the `Secure` flag to auth cookies so they are only sent over HTTPS.
- Leave empty for plain HTTP (local or development).
- **Do not expose Logos directly on the internet without TLS.**

> **Env var note:** as of `848a6db refactor: rename HERMES_* env vars to LOGOS_*`, the canonical prefix is `LOGOS_*`. The old `HERMES_*` names still work as fallbacks during the migration window, but new config and docs should use `LOGOS_*`.

**Provider API keys**
In OpenShell mode, provider API keys are never exposed to the sandbox at all — they live in the gateway's environment and are injected at the Privacy Router boundary. In Docker mode, the sandbox container inherits the API keys needed to reach the model endpoint; a sufficiently capable agent could read them via `os.environ`. This is the strongest argument for running OpenShell mode whenever you can.

### Network exposure

By default Logos binds to `0.0.0.0:8091`, making the dashboard reachable from any interface. In a homelab or VPS deployment:

- Put it behind a reverse proxy (nginx, Caddy) with TLS.
- Use firewall rules to restrict access to trusted IPs if you don't have a proxy.
- The Telegram integration lets you reach your agent without exposing the web UI at all.

---

## ⚡ Quick install

### Linux / WSL2 — one-shot installer (recommended)

Three steps. The install script handles everything else — uv, venv, deps, ~/.logos layout, CLI symlinks, and optionally OpenShell + sysctl bumps.

```bash
# 1. Docker (required if you want the default OpenShell sandboxed multi-agent mode)
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER

# 2. Node.js ≥20 (required for browser automation tools + WhatsApp bridge —
#    skip this step only if you pass SKIP_NPM=1 below)
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install -y nodejs

# 3. Log out and log back in so the new 'docker' group membership applies.

# 4. Run the installer (prompts once for sudo to bump inotify limits)
curl -fsSL https://raw.githubusercontent.com/GregsGreyCode/Logos/main/scripts/fresh-install.sh \
  | INSTALL_OPENSHELL=1 BUMP_INOTIFY=1 bash
```

Afterwards:

```bash
logos gateway start
logos status          # confirm Process + Port 8091 are green

# open http://<host>:8091/setup — the wizard provisions model routes
# and creates your first agent. On first install the wizard's
# "Complete" step runs for 1–3 minutes (cold sandbox-image pull +
# k3s boot + agent spawn). Subsequent agents spin up much faster.

# Optional — keep the gateway running after logout / reboot:
logos gateway install                   # installs a systemd user unit
sudo loginctl enable-linger $USER        # service survives logout
```

<!-- screenshot: setup-wizard-landing — the /setup page as a new user first sees it -->
<!-- screenshot: setup-benchmark — benchmark results page with several scored models -->
<!-- screenshot: setup-complete — the final provisioning spinner (shows 1-3 minute copy) -->

Env flags for the installer:

| Flag | Default | What it does |
| --- | --- | --- |
| `INSTALL_OPENSHELL=1` | off | Fetches the OpenShell static binary into `~/.local/bin/openshell` |
| `BUMP_INOTIFY=1` | off | Raises `fs.inotify.max_user_instances` to 8192 (needed for ≥8 OpenShell routes) |
| `SKIP_NPM=1` | off | Skips `npm install` (browser tools + WhatsApp bridge won't work) |
| `START_AFTER=1` | off | Launches `logos gateway start` at the end |
| `LOGOS_REPO_DIR=/path` | `$HOME/logos` | Where to clone the repo |
| `PYTHON_VERSION=<ver>` | `3.12` | Pins the venv's Python version (3.11 also supported) |

The script is idempotent — safe to re-run as a repair tool.

### Linux / macOS — manual install

> ⚠ **Not recommended for first-time users.** This path skips several things the one-shot installer does for you — the `~/.logos/` directory scaffold, the `logos` CLI symlink, `npm install` for browser tools + WhatsApp, and the OpenShell binary download. Use only if you have a specific reason (e.g. packaging Logos yourself, or you already have uv + an unusual Python pinned).

```bash
git clone https://github.com/GregsGreyCode/Logos.git
cd Logos
curl -LsSf https://astral.sh/uv/install.sh | sh
uv venv venv --python 3.12
source venv/bin/activate
uv pip install -e ".[all]"

# The gateway expects ~/.logos/ to exist with a specific layout.
mkdir -p ~/.logos/{agents,sessions,memories,skills,cron,logs,pairing,hooks,image_cache,audio_cache,whatsapp/session}
cp docs/cli-config.yaml.example ~/.logos/config.yaml
touch ~/.logos/.env

# Put the `logos` command on PATH.
mkdir -p ~/.local/bin
ln -sf "$PWD/venv/bin/logos" ~/.local/bin/logos

# Optional but needed for full functionality:
#   npm ci                     # browser automation tools + WhatsApp bridge
#   Install the OpenShell CLI  # https://github.com/NVIDIA/OpenShell/releases
#                              # (or re-run fresh-install.sh with INSTALL_OPENSHELL=1)

logos gateway start
```

Then open <http://localhost:8091/setup> — the **setup wizard launches automatically on first run** (whenever `auth.db` doesn't have the `setup_completed` feature flag).

> **OpenShell:** to use the default `openshell` runtime mode, install the [OpenShell CLI](https://github.com/NVIDIA/OpenShell) first (the one-shot installer does this for you with `INSTALL_OPENSHELL=1`). The setup wizard will detect it. If OpenShell is missing, you'll be steered to the `docker` fallback.

### Windows installer

A native Windows installer (`.exe`) is published on the [GitHub Releases](https://github.com/GregsGreyCode/Logos/releases) page. No WSL2 required.

**What the installer does:**
1. Installs a self-contained Python + Node.js environment under `%LOCALAPPDATA%\Logos`
2. Creates a start menu entry and system tray icon
3. Starts the Logos gateway automatically

**After installation:**
1. Logos opens in the system tray — right-click the icon to open the dashboard
2. Navigate to `http://localhost:8091` in your browser
3. The setup wizard launches automatically — it will prompt you for any API keys it needs
4. Your configuration is saved to `%USERPROFILE%\.logos\config.yaml`

**Sandbox on Windows:** OpenShell does not yet ship Windows binaries. The setup wizard offers **`docker` mode** when Docker Desktop is installed. Without Docker Desktop there is no supported runtime on Windows — install Docker Desktop (or run Logos inside WSL2 where OpenShell works) before proceeding.

#### ⚠️ Why Windows shows a warning

Logos is currently unsigned. Windows SmartScreen may show **"Windows protected your PC"** on first run. Click **"More info" → "Run anyway"** to proceed. See the build-transparency / SHA256 verification section under [Releases](https://github.com/GregsGreyCode/Logos/releases) for how to verify what you downloaded.

---

## 🏁 Getting started

On first run, the setup wizard at `/setup` walks you through:

1. **Model provider** — local inference (Ollama or LM Studio), or a cloud provider (Anthropic, OpenAI, OpenRouter)
2. **Inference servers** — Logos scans your local network automatically for Ollama / LM Studio endpoints
3. **Benchmarking** — quick TTFT + tok/s + capability evals on candidate models, scoring them so you can pick the best fit for your hardware
4. **Runtime mode** — OpenShell (preferred), Docker, or local
5. **Soul + first agent** — pick a starting persona; you can edit it later
6. **Telegram (optional)** — connect a bot token if you want to chat from your phone

Your configuration lives in `~/.logos/config.yaml` (Linux/macOS/WSL2) or `%USERPROFILE%\.logos\config.yaml` (Windows). Per-user state and auth live in `~/.logos/auth.db`. Sessions and per-agent memory are under `~/.logos/sessions/` and `~/.logos/memories/`.

To re-run the setup wizard, an admin user can hit `POST /api/setup/reset` (or just delete `auth.db` to start completely fresh).

---

## ⏱️ Your first 10 minutes

> 📹 *[Video walkthrough coming soon]*

**0:00 — Install and start**

Run the installer (or `python -m gateway.run` from source) and open `http://localhost:8091`. You should see the setup wizard.

<!-- screenshot: wizard-step1-providers — provider picker at the start of /setup -->

**2:00 — Complete the setup wizard**

Pick a model (cloud API key or local Ollama/LM Studio endpoint), let the benchmark run, choose a runtime mode (OpenShell if it's available — otherwise Docker), and leave policy at the default. You can change everything later.

<!-- screenshot: wizard-benchmark-results — the benchmark scoreboard with tok/s + eval columns -->

**4:00 — Send your first message**

Open the dashboard's **Agents** tab, create an agent, then jump to **Chats** and send something simple. Watch the **live execution panel** — you'll see exactly which tools the agent calls, in order, and how long each step takes. This is the STAMP model in action.

<!-- screenshot: first-chat — chat view with live execution panel showing tool calls streaming in -->

> *Try: "What can you see about the machine you're running on?"*

**6:00 — Edit your soul**

Open `~/.logos/SOUL.md` in any editor. Change the agent's name, tone, or give it a specific focus. Save — no restart needed. Send another message and notice the difference.

> *Try adding: "Always respond concisely. You are a homelab assistant named Atlas."*

**8:00 — Inspect a run**

From the dashboard's **Settings** tab (admins only) you can browse recent runs. Each entry has the full tool trace, token counts, and outcome.

**10:00 — Where to go next**

- Connect Telegram so you can reach your agent from anywhere
- Swap the model — try a smaller local model for routine work and a frontier model for hard tasks
- Try a more complex prompt — ask it to read a log file, query a URL, or write and run a script
- Explore `workflows/examples/` for pre-built task graphs

---

## 📊 Local model benchmarking

When you connect a local inference server (Ollama or LM Studio), the setup wizard automatically benchmarks your available models to find the best fit for driving the agent.

### Candidate selection

Up to 4 candidates are selected by sampling across **size buckets**: small (<5B), mid (5–13B), large (>13B), and unknown. One representative per bucket, then remaining slots filled from the best of the rest.

Within each bucket, models are ranked by quality heuristics:
- **Mid**: closest to the 9B sweet spot (large enough to reason, fast enough to use)
- **Small**: largest available (4–5B beats 1–3B)
- **Large**: smallest available (14B beats 70B on throughput)
- **Unknown**: names containing `instruct`, `chat`, `tool`, `assistant` are preferred

### Speed benchmark

Two passes per model on different prompt types. Results are averaged. Time-to-first-token (TTFT) is measured on pass 1. Throughput is measured from first token to last so cold-start latency doesn't inflate the tok/s figure.

| Label | Tokens/sec | Notes |
|-------|-----------|-------|
| Fast | ≥ 30 | Comfortable for interactive use |
| Good | ≥ 15 | Responsive for most tasks |
| Usable | ≥ 6 | Acceptable; notable latency on long outputs |
| Slow | < 6 | Likely too large for real-time agent use on this hardware |

### Capability evals

| # | Test | Pass condition |
|---|------|---------------|
| 1 | **Instruction following** | 4-step ordered task: all four outputs present |
| 2 | **Arithmetic reasoning** | Two-part maths problem: both answers correct |
| 3 | **Strict JSON format** | Output parses cleanly as JSON with exact field values; extra prose fails |
| 4 | **Tool selection** | Routes two scenarios to the right tool; both must be correct |

A model passes the capability bar at **≥ 3/4** tests.

### Scoring formula

```
score = 0.45 × (eval_tests_passed / 4)
      + 0.30 × min(tok_s, 40) / 40
      + 0.15 × ttft_score              (1.0 at ≤500ms, 0.0 at ≥4s)
      + 0.10 × min(param_count_B, 13) / 13
```

Eval quality dominates. Speed is capped at 40 tok/s — diminishing returns for interactive use above that.

<!-- screenshot: benchmark-scoreboard — full benchmark UI with multiple models scored,
     showing tok/s, ttft, ctx, and eval pass/fail columns. Illustrates this section. -->

---

## 🎛️ Customising your STAMP

**Soul** — edit `~/.logos/SOUL.md` at any time. Changes take effect on the next message; no restart needed.

**Tools** — enable or disable per agent via the Agents tab in the dashboard, or by editing `toolsets` on the agent record.

**Agent** — choose which runtime processes your conversation. Currently available: **Hermes** (general-purpose, full tool loop). ACP clients (VS Code, Zed, JetBrains) connect through the ACP adapter.

**Model** — switch via the dashboard's model picker (shown in the chat header), or set `HERMES_MODEL` / `LOGOS_MODEL` directly in `~/.logos/config.yaml`.

**Policy** — set the action policy for an agent via the dashboard's Admin tab, or assign a policy ID per session at chat-start time.

<!-- screenshot: stamp-editor — agent-edit view showing the five STAMP dimensions
     (Soul picker, Tools toggles, Agent runtime, Model picker, Policy selector). -->

---

## 🔭 Observability

Every log line includes a `[session_id]` field set via a `contextvars.ContextVar` at the start of each request — grep a single session ID across gateway, sandbox worker, and tool logs without any thread-local state.

`GET /healthz` returns per-platform success and error counters (`platform_stats`), useful for spotting silent adapter failures across Telegram, Discord, Slack, and other connected platforms.

`GET /api/runs` and the **Settings → Runs** view in the dashboard expose the per-run STAMP records — model, soul, tool sequence, token counts, outcome.

<!-- screenshot: observability-run-detail — a single STAMP run detail view with the
     tool trace, token counts, outcome; pair with a sandboxes-table shot showing
     the live Phase / Uptime columns. -->

---

## 🧠 Evolution — agent self-improvement

The **Evolution** view (under the Settings tab) gives agents a structured channel to propose improvements to the platform itself, on a schedule you control.

1. **Agents analyse your codebase** on the configured interval. Each agent reads the repository, looks for bugs and complexity hotspots, and drafts a concrete improvement.
2. **A proposal is submitted** — title, summary, a unified diff, and the list of affected files — and appears in the Evolution view for your review.
3. **You decide:** Accept, Decline, or Ask a question back to the agent.
4. **Optionally consult a frontier model** — ask Claude or GPT-4o to review the proposal before you decide.

Each Logos deployment works against **your own fork** of the repository. Fork the canonical repo, configure the fork URL in Evolution Settings, and the agent reads from it and opens PRs against it.

---

## 🔌 MCP Gateway

Logos runs a **centralized MCP (Model Context Protocol) gateway** inside the gateway process. MCP servers boot once at startup and are shared across all agent sandboxes — no per-agent subprocess spawning, no config duplication into sandboxes.

### Why centralized?

The per-agent subprocess model breaks for OpenShell sandboxes: the sandbox container has no access to `~/.logos/config.yaml` and no way to spawn `npx` / `pipx` MCP server processes. The centralized gateway solves this — agents connect over HTTP to `http://host.openshell.internal:{mcp_port}/mcp/{server-name}` regardless of where they're running.

### How it works

```
Sandbox worker (any executor)
    │
    │  tools: request_mcp_access("filesystem")
    │
    ▼
Gateway policy check
    │
    ├─ auto_approve category  → granted immediately
    ├─ user_approve category  → approval prompt sent to user
    └─ admin_approve category → requires admin to approve
    │
    ▼
Grant issued → agent receives MCP tools for that server
    │
    │  tool calls routed via HTTP
    ▼
/mcp/{server-name}  (JSON-RPC proxy in gateway)
    │
    ▼
MCP server subprocess (boots once at gateway start)
```

### Configuration (`~/.logos/config.yaml`)

```yaml
mcp_servers:
  filesystem:
    command: npx
    args: ["-y", "@modelcontextprotocol/server-filesystem", "/home/user/projects"]
    category: local            # controls approval tier
    description: "Read and write files in ~/projects"

  github:
    command: npx
    args: ["-y", "@modelcontextprotocol/server-github"]
    env:
      GITHUB_PERSONAL_ACCESS_TOKEN: "${GITHUB_TOKEN}"
    category: external
    description: "GitHub issue and PR management"

mcp_policy:
  auto_approve:  [local]       # granted without prompting the user
  user_approve:  [external]    # user sees an approval request
  admin_approve: [privileged]  # only an admin account can approve
  # deny:        [dangerous]   # always blocked
```

`category` is a free-form label you assign to each server — the `mcp_policy` block maps categories to approval tiers. Any server whose category isn't listed defaults to `user_approve`.

The MCP port defaults to `8081` and can be overridden with `LOGOS_MCP_PORT` (alias `HERMES_MCP_PORT`).

---

## 🛠️ Developer reference

Source in `gateway/`, `tools/`, and `agents/hermes/`. See [`AGENTS.md`](AGENTS.md) for internals, local dev setup, gateway architecture, and how to add tools.

**Runtime support:**

| Backend | Status | Notes |
|---------|--------|-------|
| OpenShell sandbox (Linux / macOS) | ✅ Default | Strongest isolation; required for inference credential separation |
| Docker sandbox | ✅ Tested | Container isolation; no per-binary egress policy |
| Local process | ⚠️ Unsafe fallback | No isolation; only when nothing else is available |
| Local model serving (Ollama / LM Studio) | ✅ Tested | Auto-discovered by setup wizard scan |
| Cloud providers (Anthropic, OpenAI, OpenRouter) | ✅ Tested | Configured in setup wizard |
| Kubernetes pod-per-agent | ❌ Removed | Deleted in `f6f0972`. The `k8s/` manifests still deploy the gateway itself; agent runtime uses OpenShell. |

---

## 📦 Building & deploying

```bash
docker buildx build \
  --platform linux/amd64 \
  --build-arg BUILD_SHA=$(git rev-parse --short HEAD) \
  -t ghcr.io/gregsgreycode/logos:canary \
  --push .
```

> **`--build-arg BUILD_SHA=...` is required** — omit it and the version footer displays `unknown` instead of the actual commit SHA.

---

## 🖼️ Gallery

<!-- screenshot-grid: a 2×2 of views not already shown above:
       • messaging-telegram — a Telegram DM conversation with an agent
       • cost-tracker — per-model / per-session spend breakdown
       • mcp-gateway — MCP server config + readiness checks
       • pairing-users — admin view approving / revoking pairing codes
     Render as four small thumbnails side-by-side. -->

---

## 🤝 Contributing

```bash
git clone https://github.com/GregsGreyCode/Logos.git
cd Logos
curl -LsSf https://astral.sh/uv/install.sh | sh
uv venv venv --python 3.11
source venv/bin/activate
uv pip install -e ".[all,dev]"
./scripts/test.sh
```

**Why these choices:**
- `uv` — significantly faster than pip for dependency resolution; the project uses it throughout
- Python 3.11 — minimum supported version; 3.12+ untested

**Test script options:**

```bash
./scripts/test.sh                  # unit tests only — mirrors CI (default)
./scripts/test.sh --integration    # unit + integration tests (requires API keys)
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

## 📜 License

MIT — see [LICENSE](LICENSE).

---

## 🙏 Thanks

This project would not exist without the open-source work it stands on:

- **[Anthropic / Claude](https://www.anthropic.com)** — Claude wrote a significant portion of the gateway, UI, tooling, and this documentation.
- **[Nous Research / hermes-agent](https://github.com/NousResearch/hermes-agent)** — the Hermes agent runtime (`agents/hermes/`) is a heavily extended fork of their open-source hermes-agent. The platform layer (gateway, auth, dashboard, STAMP system, policy enforcement) is original work built on top of it. The `tinker-atropos` RL submodule combines [Atropos](https://github.com/NousResearch/atropos) (Nous Research) and [Tinker](https://github.com/thinking-machines-lab/tinker) (Thinking Machines Lab).
- **[NVIDIA OpenShell](https://github.com/openshell-ai/openshell)** — the sandbox runtime that gives Logos its strongest isolation mode: kernel-level Landlock filesystem policy, per-binary egress allowlists, and the Privacy Router that keeps inference credentials out of the sandbox entirely.
- **[Ollama](https://github.com/ollama/ollama)** — makes running local LLMs approachable. Powers the homelab GPU machines that handle inference.
- **[LM Studio](https://lmstudio.ai)** — excellent local model serving, especially for experimentation and first-time model setup.
- **[faster-whisper](https://github.com/SYSTRAN/faster-whisper)** — powers in-pod voice transcription without any cloud dependency.
- **[aiohttp](https://github.com/aio-libs/aiohttp)** — the async web framework underpinning the entire gateway and HTTP API.
- **[Alpine.js](https://alpinejs.dev)** — the reactive UI layer for the dashboard. Lightweight and pleasant to work with for a single-file SPA.
- **[Tailwind CSS](https://tailwindcss.com)** — makes the dashboard look polished without writing custom CSS.
- **[Phaser](https://phaser.io)** — powers the world view and agent sprites in the Agents tab.
- **[marked.js](https://github.com/markedjs/marked)** — client-side Markdown rendering for chat messages.
- **[python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot)** — the Telegram adapter that makes Logos available anywhere.
- **[SQLite](https://www.sqlite.org)** — server-side chat persistence and full-text search. Quietly does everything.
