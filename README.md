<p align="center">
  <img src="assets/banner.png" alt="Logos" width="100%">
</p>

<p align="center">
  <strong>v1.0</strong> — first stable release. Breaking changes possible between majors; the v1.x surface is the supported one going forward.
  <a href="https://github.com/GregsGreyCode/Logos/issues">Open an issue</a> if you hit a bug.
</p>

<img width="1494" height="945" alt="image" src="https://github.com/user-attachments/assets/3643dc15-8967-42eb-bf9c-751f27412544" />


> **Release history note:** v0.4 shipped 57 tagged patch releases (v0.4.26–v0.4.105) before graduating to v0.5. Pre-v0.5 tags have been removed from GitHub to keep the releases page clean; the commit history is fully intact. **v1.0.0 shipped 2026-04-21** — first 1.x release; cuts over to upstream hermes-as-server dispatch and drops the vendored runtime fork.

---

**A self-hosted platform for agentic AI.**

Logos is a control plane for AI agents — not a single agent, but a platform you run on your own hardware under your own rules. You assemble what you need from five dimensions:

> **Soul · Tools · Agent · Model · Permissions**

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
                        │     ▼                                     │
                        │   Worker Registry (v2)                    │
                        │     │  openshell sandbox exec             │
                        │     │    → curl $HERMES_BASE_URL/v1/runs  │
                        │     │    ← SSE: token / thinking / result │
                        │     ▼                                     │
                        │   ┌─────────────────────────────────────┐ │
                        │   │  OpenShell Sandbox (per agent)      │ │
                        │   │    hermes gateway run (HTTP server) │ │
                        │   │      POST /v1/runs  → tool loop     │ │──► Ollama, LM Studio,
                        │   │      → inference.local ─────────────┼─┼─►  Anthropic, OpenAI,
                        │   └─────────────────────────────────────┘ │    OpenRouter
                        └───────────────────────────────────────────┘
```

**Request lifecycle:**

1. A message arrives via Telegram, the web dashboard, or an ACP-connected editor.
2. The **gateway** authenticates the request and applies the per-user policy snapshot.
3. The gateway finds the target agent's existing sandbox via the **worker registry** (reads `~/.logos/openshell_instances.json`; healthy means the sandbox CR is `phase == "ready"` and the in-sandbox `hermes gateway run` HTTP server is reachable).
4. The gateway invokes `openshell sandbox exec` with a short shell stub that `curl`s the in-sandbox `hermes gateway run` HTTP server at `POST /v1/runs`, passing the task payload + the sandbox's own bearer token. This is the LOG-44 v2 dispatch path — the previous stdin/stdout `sandbox_worker.py` transport has been retired.
5. Inside the sandbox, upstream `hermes-agent` runs the conversation through its tool loop, calling models via OpenShell's `inference.local` Privacy Router (which strips sandbox credentials and injects the real provider keys outside the isolation boundary).
6. `hermes gateway run` streams an SSE response: `token` / `thinking` / `tool_start` / `tool_end` / `task_result` frames flow back through the `openshell sandbox exec` subprocess to the gateway, which forwards them to the dashboard over its own SSE channel.
7. The completed run is written to SQLite as a **STAMP record** — tool sequence with per-tool previews and timings, approval events, token counts, outcome. Query any run via `GET /api/runs/{id}`, or clone one into a fresh chat with `GET /runs/{id}/clone` (returns a prefilled payload — same prompt + model, new dispatch; not a deterministic replay).

**Key boundaries:**

- `gateway/` — the always-on process: HTTP server, auth, routing, web dashboard, MCP gateway, worker registry
- `gateway/channels/` — messaging adapters (Telegram, Discord, Slack, WhatsApp, Signal, Email, Home Assistant); one adapter instance spawns per agent credential row so inbound messages route directly to their owning agent
- `gateway/executors/` — the OpenShell executor (`openshell.py`), the only supported sandbox runtime
- `agent/` — Logos's LLM adapter layer used by the gateway itself (`anthropic_adapter`, `prompt_builder`, `context_compressor`, `insights`)
- `tools/` — capabilities the agent can call; scoped per session and per policy level
- **In-sandbox runtime** — the agent tool loop, model calls, memory curation, and the `/v1/runs` HTTP endpoint are upstream `hermes-agent` running unchanged inside each sandbox. The image `ghcr.io/gregsgreycode/hermes-sandbox` wraps upstream hermes with the `sandbox` user (uid 10001), `iproute2`, and a staged `agent-browser` + Chromium install so OpenShell's sandbox policy is satisfied.

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

- **Runs agents** — upstream `hermes-agent` is the current runtime, running as an HTTP server (`hermes gateway run`) inside each sandbox. The runtime layer is pluggable at the sandbox-image level; alternative runtimes slot in by producing a compatible image.
- **Records every run** — agent, model, soul, tool sequence, approvals, api call count, and outcome land in the `agent_runs` table; token and USD-cost totals are joined in from `cost_log`/`dispatches` at read time (`GET /api/runs/{id}` returns the unified "STAMP" view).
- **Enforces policy** — workspace scoping (realpath-resolved), OpenShell egress policy, Landlock filesystem isolation, dangerous-command gate with regex + Tirith scan. (Dispatch-time ActionPolicy dimensions for write/exec/network/secret are not currently wired; see `gateway/auth/policy.py` for the live set.)
- **Reaches you anywhere** — Telegram, Discord, Slack, WhatsApp, Signal, Email, plus a built-in web dashboard, all from a single gateway process. Each agent owns its own bot tokens (per-agent credentials) so multiple agents can run on the same platform simultaneously without fighting over one token.
- **Web dashboard** — full chat UI at `http://localhost:8091`; real-time streaming, per-message stats, voice input, metrics, multiple named agents, live execution panel, world view with live agent sprites (💭 thought bubble renders while an agent is dispatching).
- **Compare tab** — run the same prompt against two named agents side-by-side. Per-pane target selector (both / left / right), multi-line input, Mind button per pane, parallel/sequential toggle. Backed by transient sessions so probes don't pollute either agent's history.
- **Per-agent cloud-tool credentials** — each agent owns its own API keys for built-in cloud tools (search, news, weather, etc.). Keys live in the agent's sandbox `.env`, survive respawns, and are managed from the in-chat `/setup` card. No more gateway-wide env vars for per-agent secrets.
- **Persistent history** — searchable conversation history in SQLite with full-text search across all past conversations
- **Voice input** — speak via Telegram or the dashboard; faster-whisper transcribes locally when the package is installed (falls back to Groq / OpenAI).
- **Live execution view** — watch in real time which tools the agent calls, its chain of reasoning, and elapsed time per step
- **AI routing layer** — provisions one OpenShell gateway per `(provider, model)` route on the local host, picked per dispatch based on readiness. Today every route is a local OpenShell gateway on its own port; cross-machine / multi-host routing is planned.
- **Parallel sub-agents** — upstream hermes's own `delegate` and `handoff` flows run inside the sandbox; Logos's `mixture_of_agents_tool` adds multi-model fan-out for comparative reasoning.
- **MCP gateway** — centralized Model Context Protocol server management; MCP servers boot once in the gateway, agents request access dynamically with per-category approval tiers (`auto_approve` / `user_approve` / `admin_approve` / `deny`).
- **Memory system** — agent-curated persistent memory, FTS5 full-text session search with LLM summarisation. (Skills under `skills/` are human-authored prompts, not auto-generated.)
- **Dynamic toolset discovery** — toolsets and their live readiness state (API-key status, optional-tool availability) come from the sandbox itself via `GET /v1/toolsets`; the gateway falls back to a local view only when no sandbox is healthy.
- **IDE integration** — ACP protocol for VS Code, Zed, and JetBrains
- **Model support** — Anthropic, OpenAI, OpenRouter (200+ models), Nous Portal, or any OpenAI-compatible endpoint
- **Cancel mid-response** — `POST /chat/{task_id}/cancel` SIGTERMs the in-flight `openshell sandbox exec` subprocess tracked by v2 dispatch's `_INFLIGHT` registry; a "Stop" button in the chat header fires it for you.

---

## 🧬 The STAMP model

Every run in Logos is defined by five dimensions:

| | |
|---|---|
| **S** — Soul | The persona: how the agent communicates, reasons, and behaves |
| **T** — Tools | The capabilities available: what the agent can reach and act on |
| **A** — Agent | The runtime: which adapter processes the conversation |
| **M** — Model | The brain: which LLMs are called to execute functions |
| **P** — Permissions | The granted access: which hosts the sandbox is allowed to reach, plus approval gates for dangerous tools |

Compose these five and you have an AI agent. Change any one dimension and you have a different seeded agent. Every STAMP is recorded in full — compare runs across configurations, replay them exactly, or clone them into new sessions.

The soul lives in `SOUL.md` and is re-read from disk on every message (no cache, no restart required). Tools are scoped per agent and per session. The agent adapter is switchable. The model switches without code changes. Permissions are enforced at three layers: workspace scoping in the agent loop, OpenShell kernel-level sandbox isolation + egress allowlist (granted permissions turn into per-host network rules), and a dangerous-command regex + Tirith gate with approval prompts.

---

## 🔒 Security & deployment model

**Understanding the isolation boundary matters before you choose how to run Logos.** Agents can read files, execute code, and make network requests — what they _cannot_ reach is enforced by OpenShell (the sandbox runtime Logos depends on). Logos itself is a control plane; the isolation primitives described below — Landlock filesystem policy, per-binary egress allowlists, the credential-stripping Privacy Router — are provided by OpenShell and Logos composes with them. Replace OpenShell with a weaker sandbox and most of this section doesn't apply.

### Runtime modes at a glance
<img width="1268" height="1261" alt="image" src="https://github.com/user-attachments/assets/571806a6-b1cf-444d-bd7a-23f4f31d4783" />
OpenShell is the only supported sandbox runtime — `gateway/executors/build_executor()` always returns `OpenShellExecutor`.

| Mode | Default? | How it spawns | Isolation boundary | Egress policy | Platform |
|------|---|---------------|-------------------|---------------|----------|
| **`openshell`** | ✅ only | OpenShell CLI provisions a persistent sandbox per agent; the gateway dispatches each task via a one-shot `openshell sandbox exec` subprocess, piping task JSON on stdin and reading event JSON on stdout. Inference egress goes through OpenShell's HTTP CONNECT proxy to the `inference.local` Privacy Router. | Kernel Landlock LSM (filesystem) + OpenShell egress allowlist (network) + container | Per-binary YAML egress policy (`gateway/policies/openshell_default.yaml`) | Linux, macOS |

> **What happened to the Kubernetes, Docker, and local-process executors?** The Kubernetes pod-per-agent executor was deleted in commit `f6f0972`; the `DockerSandboxExecutor`, the k3s install flow, and the `LocalProcessExecutor` were removed in a later cleanup pass once OpenShell became the canonical sandbox runtime exposed in `/setup`. The `k8s/` manifests still work for **deploying the gateway itself** as a Kubernetes Deployment — see [`k8s/README.md`](k8s/README.md) — but agents inside that gateway use OpenShell.

### Defense layers

Agent security is defense-in-depth — multiple independent layers, not a single boundary:

| Layer | What it does | Where it runs |
|-------|-------------|---------------|
| **Workspace scoping** | Restricts file read/write to the agent's workspace directory. Symlink-safe (`realpath` before access check). | All modes |
| **Toolset enforcement** | Agents can only call tools in their enabled toolset. Validated at agent init and registry dispatch. | All modes |
| **API key filtering** | Sandbox workers never receive provider API keys. They call `inference.local`, and the OpenShell Privacy Router (running outside the sandbox) injects the real credentials. | OpenShell only |
| **Command review** | Regex patterns catch common destructive shell commands (`rm -rf /`, `DROP TABLE`, `chmod 777`, etc.). Prompts for approval before execution. | All modes |
| **Filesystem isolation** | Landlock LSM declarative read-only / read-write policy enforced by the kernel. | OpenShell only |
| **Egress policy** | Per-binary YAML allowlist (`network_policies` in OpenShell policy). | OpenShell only |
| **Container isolation** | Docker container with `--cap-drop=ALL`, `--security-opt=no-new-privileges`, no host filesystem mounts. | OpenShell |

**Command review** catches obvious destructive patterns but is bypassable with interpreter one-liners (e.g. `python -c "import shutil; ..."`). It is a convenience layer, not a security boundary. The real protection comes from workspace scoping, kernel-level filesystem and egress policy, and container isolation — all provided by OpenShell.


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
Provider API keys are never exposed to the sandbox. They live in the gateway's environment (and, for per-agent cloud-tool keys, in each agent's own sandbox `.env` for only the tools that agent has been granted). Inference calls go through OpenShell's `inference.local` Privacy Router, which strips any sandbox-supplied credentials and injects the real provider key outside the isolation boundary.

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

# 2. Log out and log back in so the new 'docker' group membership applies.

# 3. Run the installer. Takes ~10-15 minutes on a fresh box — most of
#    that is the one-time `docker build` of the hermes-sandbox image
#    (~5-10 min, cached after). Prompts once for sudo to bump inotify
#    limits and, if Node.js ≥20 isn't already installed, to auto-
#    install it via nodesource (needed for browser automation +
#    WhatsApp). Pass SKIP_NPM=1 to skip Node entirely;
#    LOGOS_SKIP_SANDBOX_BUILD=1 to skip the image build (agents
#    won't be able to spawn sandboxes until you build it manually).
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

# Keeping Logos current:
logos gateway update --check            # is origin ahead of local HEAD?
logos gateway update                    # ff-only git pull + restart
# …or click the "⬆ update" pill in the dashboard when new commits land.
```

<!-- screenshot: setup-wizard-landing — the /setup page as a new user first sees it -->
<!-- screenshot: setup-benchmark — benchmark results page with several scored models -->
<!-- screenshot: setup-complete — the final provisioning spinner (shows 1-3 minute copy) -->

**Published images** (listed so you can inspect or pin them — the installer only pulls `hermes-sandbox` by default):

| Image | What it is | Installer behaviour |
| --- | --- | --- |
| `ghcr.io/gregsgreycode/hermes-sandbox:v1.0.17` (also `:latest`) | The sandbox runtime image that every agent runs inside. Wraps upstream hermes with the sandbox user, iproute2, and agent-browser. | Pulled automatically. Override with `LOGOS_SANDBOX_IMAGE=…` or skip with `LOGOS_SKIP_SANDBOX_BUILD=1`. |
| `ghcr.io/gregsgreycode/hermes-upstream:v1.0.17` (also `:latest`) | The upstream `hermes-agent` base image, rebuilt in our registry for determinism. The sandbox image `FROM`s this. | Not pulled separately — its layers arrive baked into `hermes-sandbox`. Only fetched directly when you rebuild from source (`LOGOS_FORCE_SANDBOX_BUILD=1`, or GHCR unreachable). |
| `ghcr.io/gregsgreycode/logos:1.0.17` (also `:latest`, `:canary`) | The gateway — used when deploying Logos itself in a container. | Not pulled. The installer runs Logos from source. Only relevant if you're running the gateway as a container (e.g. via the `k8s/` manifests). |

Env flags for the installer:

| Flag | Default | What it does |
| --- | --- | --- |
| `INSTALL_OPENSHELL=1` | off | Fetches the OpenShell static binary into `~/.local/bin/openshell` and builds the `hermes-sandbox` Docker image (first build: ~5-10 min) |
| `BUMP_INOTIFY=1` | off | Raises `fs.inotify.max_user_instances` to 8192 (needed for ≥8 OpenShell routes) |
| `SKIP_NPM=1` | off | Skips `npm install` (browser tools + WhatsApp bridge won't work) |
| `LOGOS_SKIP_SANDBOX_BUILD=1` | off | Skips the local `docker build` of the sandbox image. Use when pulling from a pre-built registry |
| `LOGOS_FORCE_SANDBOX_BUILD=1` | off | Forces a rebuild of the sandbox image even when it already exists locally (use after editing `docker/Dockerfile.hermes-upstream` or anything else baked into the sandbox image) |
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

> **Windows:** there is no native Windows build at this time. Run Logos under WSL2 using the Linux installer path above.

---

## 🏁 Getting started

On first run, the setup wizard at `/setup` walks you through:

1. **Model provider** — local inference (Ollama or LM Studio), or a cloud provider (Anthropic, OpenAI, OpenRouter)
2. **Inference servers** — Logos scans your local network automatically for Ollama / LM Studio endpoints via `GET /api/setup/scan` (ports 11434, 1234, 8080)
3. **Benchmarking** — TTFT + tok/s + 6 capability evals (instruction following, 2-part reasoning, strict JSON, tool selection, nested JSON, multi-step reasoning), plus a 7-test hard tier (4 advanced static tests + 3 agent-loop tests) if a model passes ≥5/6 standard evals
4. **Sandbox runtime** — OpenShell is the only supported runtime. The CLI setup also asks about a `terminal` backend (local / SSH / Daytona / Singularity) — that's where shell commands *executed by the agent's terminal tool* run, not where the agent itself is sandboxed.
5. **Soul + first agent** — pick a starting persona; you can edit it later
6. **Messaging (optional)** — connect a Telegram / Discord / Slack / WhatsApp bot token to a specific agent so you can chat from your phone. Tokens live per-agent (different agents can own different bots) and can be added or rotated later from **Config → Messaging** without a gateway restart.

Your configuration lives in `~/.logos/config.yaml` (Linux/macOS/WSL2). Per-user state and auth live in `~/.logos/auth.db`. Sessions and per-agent memory are under `~/.logos/sessions/` and `~/.logos/memories/`.

To re-run the setup wizard, an admin user can hit `POST /api/setup/reset` (or just delete `auth.db` to start completely fresh).

---

## ⏱️ Your first 10 minutes

> 📹 *[Video walkthrough coming soon]*

**0:00 — Install and start**

Run `logos gateway start` (or `python -m gateway.run` from source) and open `http://localhost:8091`. You should see the setup wizard.

<!-- screenshot: wizard-step1-providers — provider picker at the start of /setup -->

**2:00 — Complete the setup wizard**

Pick a model (cloud API key or local Ollama/LM Studio endpoint), let the benchmark run, and leave policy at the default. You can change everything later. (OpenShell is the only supported sandbox runtime — the wizard confirms it's installed before letting you proceed.)

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
- Create a second agent with a different soul and use the **Compare** tab to run the same prompt through both

---

## 📊 Local model benchmarking

When you connect a local inference server (Ollama or LM Studio), the setup wizard automatically benchmarks your available models to find the best fit for driving the agent.
<img width="1098" height="1042" alt="image" src="https://github.com/user-attachments/assets/63f5b294-d821-44c9-bb74-edf8210f48f5" />

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
| 5 | **Nested JSON schema** | JSON with required nesting, an array, and mixed types — no surrounding prose |
| 6 | **Multi-step reasoning** | Three chained multiplications in one word problem; single numeric answer |

A model passes the capability bar at **≥ 4/6** tests. Passing ≥ 5/6 unlocks a hard-tier run — advanced tool routing across 5 tools, deep-nested JSON, a harder reasoning problem, plus agent-loop tests (post-tool summarisation, error recovery, multi-turn grounding).

### Scoring formula

```
score = 0.40 × (eval_tests_passed / 6)
      + 0.15 × min(tok_s, 40) / 40
      + 0.25 × advanced_eval_score     (hard + agent-loop tiers; only runs if model passed ≥ 5/6 standard)
      +        param_size_bonus        (≤ 0.05, peaks near ~13B)
      +        capability_bonus        (+0.035 tool_use, +0.015 vision)
      +        penalties               (−0.15 specialised models, context viability, slow greeting-latency)
```

Eval quality and advanced-tier performance dominate. Speed is capped at 40 tok/s — diminishing returns for interactive use above that. Weights rebalanced 2026-04-13 so agent-loop failures can actually move a model's ranking.

<img width="1674" height="258" alt="image" src="https://github.com/user-attachments/assets/f84afbc9-1930-4818-83a4-8467deb48dd9" />


---

## 🎛️ Customising your STAMP

**Soul** — edit `~/.logos/SOUL.md` at any time. Changes take effect on the next message; no restart needed.

**Tools** — enable or disable per agent via the Agents tab in the dashboard, or by editing `toolsets` on the agent record.

**Agent** — choose which runtime processes your conversation. Currently available: **Hermes** (general-purpose, full tool loop). ACP clients (VS Code, Zed, JetBrains) connect through the ACP adapter.

**Model** — switch via the dashboard's model picker (shown in the chat header), or set `HERMES_MODEL` / `LOGOS_MODEL` directly in `~/.logos/config.yaml`.

**Policy** — set the action policy for an agent via the dashboard's **Config → Permissions** tab (renamed from "Sandbox Policies"), or assign a policy ID per session at chat-start time.



---

## 🔭 Observability

Every log line includes a `[session_id]` field set via a `contextvars.ContextVar` at the start of each request — grep a single session ID across gateway, sandbox worker, and tool logs without any thread-local state.

`GET /healthz` returns per-platform success and error counters (`platform_stats`), useful for spotting silent adapter failures across Telegram, Discord, Slack, and other connected platforms.

`GET /api/runs` (and the `Activity → Runs` tab in the dashboard) lists STAMP records. `GET /api/runs/{id}` returns the full enrichment — `agent_runs` row plus `dispatches` chain, `cost_log` entries, and `approval_requests` joined on `session_id`. Open the drawer on a row to see model, soul, tool sequence, token totals, USD cost, and approval history.

<!-- screenshot: observability-run-detail — a single STAMP run detail view with the
     tool trace, token counts, outcome; pair with a sandboxes-table shot showing
     the live Phase / Uptime columns. -->

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

Source in `gateway/`, `tools/`, and `agent/` (Logos's LLM adapter layer). The agent runtime itself is upstream `hermes-agent` running inside each sandbox — see `docker/Dockerfile.hermes-upstream` for how the sandbox image is assembled. For gateway architecture, local dev setup, and how to add tools, see [`AGENTS.md`](AGENTS.md).

**Runtime support:**

| Backend | Status | Notes |
|---------|--------|-------|
| OpenShell sandbox (Linux / macOS) | ✅ Only supported runtime | Strongest isolation; required for inference credential separation |
| Local model serving (Ollama / LM Studio) | ✅ Tested | Auto-discovered by setup wizard scan |
| Cloud providers (Anthropic, OpenAI, OpenRouter) | ✅ Tested | Configured in setup wizard |


---

## 📦 Building & deploying

Local dev build — tag as `:canary` so the dashboard's "canary" slot picks it up:

```bash
docker buildx build \
  --platform linux/amd64 \
  --build-arg BUILD_SHA=$(git rev-parse --short HEAD) \
  -t ghcr.io/gregsgreycode/logos:canary \
  --push .
```

> **`--build-arg BUILD_SHA=...` is required** — omit it and the version footer displays `unknown` instead of the actual commit SHA.

Stable release builds happen automatically via `.github/workflows/build-image.yml` whenever a `v*` tag is pushed: the workflow builds the gateway image, logs in to GHCR with `GITHUB_TOKEN`, and publishes `:vX.Y.Z`, `:latest`, and `:canary` tags off the same SHA. A sibling workflow (`publish-sandbox-image.yml`) produces the matching `hermes-sandbox` + `hermes-upstream` images on the same trigger.

---

## 🧱 Architectural limitations

### One k3s cluster per OpenShell model route

In the default (sandboxed) deployment, each *model route* — i.e. each (provider, model) pair you provision — is backed by its own full OpenShell gateway, which internally runs a full k3s cluster in a Docker container. This is an OpenShell design choice, not a Logos one: OpenShell's privacy router pins a single forced model per gateway via `openshell inference set --provider <p> --model <m>` and overwrites the `model` field on every inbound request. Multiple Logos agents pinned to the **same** model share a gateway and add no extra overhead; agents on **different** models each require their own gateway. (See `gateway/openshell_routes.py` for the full rationale.)

The practical consequences:

- **RAM**: each gateway costs ~200–500 MB idle. Five routes ≈ 1–2.5 GB of overhead before any agent runs.
- **inotify instances**: each k3s cluster consumes roughly 70 kernel `inotify` *instances* (kubelet + containerd + CoreDNS + CNI + API server, etc.). With the Linux default of `fs.inotify.max_user_instances=128`, the host can only reliably host **one** OpenShell route — the second or third will fail to provision, usually surfacing as *"Gateway failed to start"* or *"underlying gateway is unreachable"* with no useful container logs.
- **Fix**: raise the ceiling once, on the host, to ~8192 instances. The installer does this automatically when run with `BUMP_INOTIFY=1`; otherwise run it manually:

  ```bash
  sudo sysctl -w fs.inotify.max_user_instances=8192
  echo 'fs.inotify.max_user_instances=8192' | sudo tee -a /etc/sysctl.d/99-openshell.conf
  echo 'fs.inotify.max_user_watches=1048576'  | sudo tee -a /etc/sysctl.d/99-openshell.conf
  sudo sysctl --system
  ```

  The gateway preflight (`gateway/http_api.py`) logs a warning on startup when the ceiling is below safe thresholds, and the gateway-start error handler surfaces the same fix command in the UI toast when a route fails to come up for this reason.
- **Can't be applied from the UI**: bumping a kernel tunable requires root, and the Logos gateway runs as your user. The UI can detect and surface the command, but you have to paste it into a terminal on the host.

OpenShell is the only supported runtime mode (see [Runtime modes at a glance](#runtime-modes-at-a-glance)), so this limitation applies to every Logos deployment that provisions more than one model route.

---

## 🖼️ Gallery

<img width="1670" height="1262" alt="image" src="https://github.com/user-attachments/assets/4653c61c-240c-48ca-aace-af8c08e5f50a" />

<img width="1668" height="1259" alt="image" src="https://github.com/user-attachments/assets/b68bbaf2-66ae-4f26-91d2-121041ec0bd2" />



## 🤝 Contributing

```bash
git clone https://github.com/GregsGreyCode/Logos.git
cd Logos
curl -LsSf https://astral.sh/uv/install.sh | sh
uv venv venv --python 3.12
source venv/bin/activate
uv pip install -e ".[all,dev]"
./scripts/test.sh
```

**Why these choices:**
- `uv` — significantly faster than pip for dependency resolution; the project uses it throughout
- Python 3.12 — the canonical dev version. 3.11 is the minimum (`requires-python = ">=3.11"`) and what CI runs against, so either works

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

---

## 📜 License

MIT — see [LICENSE](LICENSE).

---

## 🙏 Thanks

This project would not exist without the open-source work it stands on:

- **[Anthropic / Claude](https://www.anthropic.com)** — Claude wrote a significant portion of the gateway, UI, tooling, and this documentation.
- **[Nous Research / hermes-agent](https://github.com/NousResearch/hermes-agent)** — Logos runs upstream `hermes-agent` unchanged as the in-sandbox runtime (the previously vendored fork was retired at v1.0.0). The platform layer (gateway, auth, dashboard, STAMP system, per-agent credential management, policy enforcement) is original work that composes around it. The `tinker-atropos` RL submodule combines [Atropos](https://github.com/NousResearch/atropos) (Nous Research) and [Tinker](https://github.com/thinking-machines-lab/tinker) (Thinking Machines Lab).
- **[NVIDIA OpenShell](https://github.com/NVIDIA/OpenShell)** — the sandbox runtime that gives Logos its strongest isolation mode: kernel-level Landlock filesystem policy, per-binary egress allowlists, and the Privacy Router that keeps inference credentials out of the sandbox entirely.
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
