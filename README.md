<p align="center">
  <img src="assets/banner.png" alt="Logos" width="100%">
</p>

<p align="center">
  <strong>Early alpha</strong> — core gateway, auth, dashboard, and setup wizard work.<br>
  Expect rough edges; breaking changes between releases are likely.
  <a href="https://github.com/GregsGreyCode/Logos/issues">Open an issue</a> if you hit a bug.
</p>

> **Release history note:** v0.4 shipped 57 tagged patch releases (v0.4.26–v0.4.105) before graduating to v0.5. Pre-v0.5 tags have been removed from GitHub to keep the releases page clean; the commit history is fully intact.

---

**A self-hosted platform for agentic AI.**

Logos is a control plane for AI agents — not a single agent, but a platform you run on your own hardware under your own rules. Agent runtimes plug in; you assemble what you need from five dimensions:

> **Soul · Tools · Agent · Model · Policy**

That combination is a **STAMP** — it defines every run Logos records, making every agent interaction observable, reproducible, and auditable. No black-box behaviour you can't inspect.

Run on a $5 VPS, a homelab Kubernetes cluster, or serverless infrastructure. During onboarding you choose your privacy model: local inference, self-hosted endpoints, or cloud providers (Anthropic, OpenAI, OpenRouter).

---

## ⚙️ How it works

```
                        ┌──────────────────────────────────────────┐
                        │              Logos Platform               │
                        │                                          │
  Telegram ─────────►  │  Gateway / Router                        │
  Web Dashboard ──────► │    ├── MCP Gateway ──► MCP Servers       │
  ACP (IDE) ──────────► │    │    (runs inside     (filesystem,    │
                        │    │     gateway)          GitHub, etc.)  │
                        │    ▼                                     │
                        │  Auth & Policy Layer                     │
                        │    │                                     │
                        │    ▼                                     │
                        │  Agent Runtime (e.g. Hermes)             │
                        │    │          │                          │
                        │    ▼          ▼                          │
                        │  Tools    Sub-agents                     │
                        │    │          │    ▲                     │
                        │    └────┬─────┘    │ request_mcp_access  │
                        │         │          │ (HTTP to gateway)   │
                        │         ▼          │                     │
                        │  Model Router      │                     │
                        │    │         │     │                     │
                        └────┼─────────┼─────┼────────────────────┘
                             ▼         ▼
                          Local     Anthropic  OpenRouter
                          (Ollama)  (Claude)   (200+ models)
```

**Request lifecycle:**

1. A message arrives via Telegram, the web dashboard, or an ACP-connected editor.
2. The **Gateway** authenticates the request and applies the per-user policy snapshot.
3. The **Agent Runtime** (currently Hermes) processes the conversation through its tool loop.
4. Tool calls execute inside an **isolated workspace** — scoped to the policy level you've set.
5. The **Model Router** dispatches inference to whichever backend you've configured (local GPU, cloud, or both).
6. The completed run is written to SQLite as a **STAMP record** — full tool trace, approval events, token counts, and outcome — queryable and replayable at any time.

**Key boundaries:**

- `gateway/` — the always-on process: HTTP server, Telegram adapter, auth, routing, web dashboard
- `agent/` — runtime adapters; Hermes is the first, additional runtimes plug in via `logos/agent/interface.py`
- `tools/` — capabilities the agent can call; scoped per session and per policy level
- The platform layer (gateway, router, auth, dashboard) is runtime-agnostic; `hermes_*` modules belong to the Hermes runtime specifically

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

- **Runs agents** — Hermes is the current runtime, with a clean adapter interface for additional runtimes
- **Records everything** — every run captures its full STAMP: agent, model, soul, tools, policy, tool sequence, approval events, token counts, and outcome
- **Enforces policy** — workspace isolation, command approval, filesystem scoping, built-in policy evals
- **Reaches you anywhere** — Telegram and a built-in web dashboard, all from a single gateway process
- **Web dashboard** — full chat UI at `http://localhost:8080`; real-time streaming, per-message stats, voice input, metrics, multiple agent instances, and a live execution panel
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
- **Runs anywhere** — local, Docker, SSH
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

The soul lives in `SOUL.md`, editable without a restart. Tools are scoped per platform and per session. The agent adapter is switchable. The model switches without code changes. Policy is enforced at the workspace and approval layers, not just in the prompt.

---

## 🔒 Security & deployment model

**Understanding the isolation boundary matters before you choose how to run Logos.** Agents can read files, execute code, and make network requests — what they _cannot_ reach depends entirely on your deployment mode.

### Isolation modes at a glance

| Mode | How it's run | Isolation boundary | Network policy | Agents can reach… |
|------|-------------|-------------------|----------------|-------------------|
| **Local process** | `pip install logos` / Windows `.exe` | OS process boundary | None | Your user's home directory and whatever that user can reach on the host |
| **Container sandbox** | Docker Desktop (Windows/macOS) | Docker container boundary | None (outbound unrestricted) | Files inside the container only; host filesystem not mounted |
| **Full sandbox (OpenShell)** | Docker + OpenShell CLI (Linux/macOS) | Docker container + egress policy | OpenShell YAML policy enforcement | Container-only filesystem; outbound restricted to policy allowlist |
| **Docker Compose** | `docker-compose.yml` | Container boundary | None | Files/processes _inside_ the container only; the host filesystem is not mounted |
| **Docker Compose + k3s** | `docker-compose.k3s.yml` | Kubernetes pod boundary | K8s NetworkPolicy capable | A fresh pod with its own filesystem and network namespace per agent |
| **External Kubernetes** | `k8s/` manifests | Kubernetes pod + RBAC boundary | K8s NetworkPolicy capable | Pod resources only; network policies and RBAC apply |

### Local process (Windows / Linux / macOS) — fallback

Agents run as child processes of the gateway, inheriting the same user account. There is **no additional sandbox** beyond normal OS process isolation. An agent that calls the terminal tool can read and write anything your user account can.

This is the **last-resort fallback** when neither Docker nor Kubernetes is available. The setup wizard clearly labels this mode.

**Mitigations:**
- Keep policy level at `WORKSPACE_ONLY` or lower to restrict filesystem access to the working directory.
- Run Logos under a dedicated low-privilege user account, not your daily-driver account.

### Container sandbox (Windows with Docker Desktop)

When Docker Desktop is available but the OpenShell CLI is not (currently the case on **Windows**, where OpenShell does not ship binaries), Logos can still run agents inside Docker containers. Each agent gets its own isolated container with:

- Separate filesystem (host filesystem not mounted)
- `--cap-drop=ALL` (no Linux capabilities)
- `--security-opt=no-new-privileges`
- Localhost-only port binding
- Auto-cleanup on exit (`--rm`)

**What this mode does NOT provide:** egress policy enforcement. Agents can make outbound network requests to any destination. If your use case requires network restriction, use Kubernetes with NetworkPolicy or wait for OpenShell Windows support.

### Full sandbox — OpenShell (Linux / macOS)

The strongest desktop isolation. OpenShell manages Docker containers with a declarative YAML policy layer that controls:

- Which network destinations agents can reach (egress allowlist)
- GPU passthrough for local inference
- Container lifecycle and resource limits

**Platform support:** Linux and macOS only. OpenShell does not currently ship Windows binaries. The setup wizard detects this automatically and offers Container sandbox mode as the Windows alternative.

### Docker Compose — `docker-compose.yml` (recommended for most users)

Logos and all its agents run inside a single Docker container. The host filesystem is **not mounted** — agents can only see what's inside the container. They cannot read your `.env` file, home directory, or other host files.

```
Host OS
└── Docker container  ← isolation boundary
    ├── Logos gateway
    └── Agent processes (child processes inside the same container)
```

**What agents cannot do:** escape the container, read host files, reach host network services (unless you expose ports in `docker-compose.yml`).

**What agents can do:** read/write files inside the container, make outbound HTTP requests, call the tools you've enabled.

**To start:**
```bash
cp .env.example .env   # add your API keys + HERMES_JWT_SECRET
docker compose up -d
```

### Docker Compose + k3s — `docker-compose.k3s.yml` (strongest self-hosted isolation)

Each agent session spawns a dedicated Kubernetes pod inside an embedded [k3s](https://k3s.io) cluster. Pods have their own filesystem, network namespace, and resource limits — agents are isolated from each other _and_ from the Logos gateway container.

```
Host OS
└── Docker network
    ├── k3s container (privileged)  ← runs the Kubernetes control plane
    └── logos container
        └── Agent pods (k8s pods in the hermes namespace)  ← per-agent isolation boundary
```

**Requirements:** Linux host only. k3s requires a privileged container and Linux namespaces/cgroups — this does **not** work on Docker Desktop for Mac or Windows without extra configuration.

**To start:**
```bash
cp .env.example .env   # add HERMES_JWT_SECRET (required)
docker compose -f docker-compose.k3s.yml up -d
# In setup wizard step 4: choose Kubernetes → "Logos is outside the cluster"
# Leave kubeconfig empty — it is mounted automatically.
```

### External Kubernetes (`k8s/` manifests)

Deploy Logos to an existing cluster. Agent pods run in the `hermes` namespace with full Kubernetes isolation. You can layer network policies and RBAC on top for production deployments.

**Quick start:**
```bash
# 1. Create the namespace and apply manifests
kubectl apply -f k8s/

# 2. Set required secrets
kubectl create secret generic logos-secrets -n logos \
  --from-literal=HERMES_JWT_SECRET=$(openssl rand -hex 32) \
  --from-literal=OPENAI_API_KEY=<your-key>   # or whichever provider you use

# 3. Check rollout
kubectl rollout status deployment/logos -n logos
```

In the setup wizard, step 4: choose **Kubernetes → "Logos is inside the cluster"** and leave kubeconfig empty — the in-cluster service account is used automatically.

See [`k8s/README.md`](k8s/README.md) for full manifest reference, ingress setup, and persistent volume configuration.

---

### 🔑 Secrets and auth

**`HERMES_JWT_SECRET`**
All session tokens are signed with this secret. Generate it once with `openssl rand -hex 32` and store it somewhere safe.
- If you lose it, all active sessions are invalidated on next restart (users will need to log in again — no data is lost).
- Rotating it intentionally: change the value, restart Logos.
- Never commit it to version control.

**`HERMES_COOKIE_SECURE`**
Set to `true` if Logos is behind an HTTPS reverse proxy (nginx, Caddy, Traefik). This adds the `Secure` flag to auth cookies so they are only sent over HTTPS.
- Leave empty for plain HTTP (local or docker-compose without TLS termination).
- **Do not expose Logos directly on the internet without TLS.**

**API keys in `.env`**
In Docker deployments, API keys live in the `.env` file on the host — they are passed as environment variables to the container and are **not visible to agents** (agents cannot read the host `.env` file or process environment of the gateway). On bare metal, agents run in the same process environment, so a sufficiently capable agent could read them via `os.environ`.

---

### 🌐 Network exposure

By default Logos binds to `0.0.0.0:8080`, making the dashboard reachable from any interface. In a homelab or VPS deployment:

- Put it behind a reverse proxy (nginx, Caddy) with TLS.
- Use firewall rules to restrict access to trusted IPs if you don't have a proxy.
- The Telegram integration lets you reach your agent without exposing the web UI at all.

---

## ⚡ Quick install

**Which path is for me?**

| I want to… | Use |
|---|---|
| Try Logos on Linux / macOS / WSL2 | [Bash installer](#bash-installer) |
| Run on Windows without WSL2 | [Windows installer](#-windows-installer) |
| Strongest self-hosted isolation (Linux host) | [Docker Compose + k3s](#docker-compose--k3s----docker-composek3syml-strongest-self-hosted-isolation) |
| Standard Docker deployment | [Docker Compose](#docker-compose----docker-composeyml-recommended-for-most-users) |
| Deploy to an existing Kubernetes cluster | [External Kubernetes](#external-kubernetes-k8s-manifests) |

---

### Bash installer

> **Before running:** you can inspect the installer first:
> ```bash
> curl -fsSL https://raw.githubusercontent.com/GregsGreyCode/logos/main/scripts/install.sh | less
> ```

```bash
curl -fsSL https://raw.githubusercontent.com/GregsGreyCode/logos/main/scripts/install.sh | bash
```

Works on Linux, macOS, and WSL2. Handles Python, Node.js, and dependencies automatically. No prerequisites except git.

After the installer finishes:

```bash
# Add your API keys / JWT secret (open in any editor)
nano ~/.logos/config.yaml   # or edit via the setup wizard at http://localhost:8080

# Start Logos
logos                       # or: python -m gateway.http_api
```

Open `http://localhost:8080` — the setup wizard launches automatically on first run.

### 🪟 Windows installer

A native Windows installer (`.exe`) is available on the [GitHub Releases](https://github.com/GregsGreyCode/logos/releases) page. No WSL2 required.

**What the installer does:**
1. Installs a self-contained Python + Node.js environment under `%LOCALAPPDATA%\Logos`
2. Creates a start menu entry and system tray icon
3. Starts the Logos gateway automatically

**After installation:**
1. Logos opens in the system tray — right-click the icon to open the dashboard
2. Navigate to `http://localhost:8080` in your browser
3. The setup wizard launches automatically — it will prompt you for any API keys it needs
4. Your configuration is saved to `%USERPROFILE%\.logos\config.yaml`

**Sandbox on Windows:** If Docker Desktop is installed, the setup wizard offers **Container sandbox** mode — each agent runs in its own isolated Docker container. Without Docker, agents run in-process (local process mode). OpenShell is not yet available on Windows.

---

#### ⚠️ Why Windows shows a warning

Logos is currently unsigned. Windows SmartScreen may show **"Windows protected your PC"** on first run. Click **"More info" → "Run anyway"** to proceed.

You can verify that what you downloaded is exactly what was built using the methods below.

#### 🔐 Build transparency

Logos binaries are built exclusively via GitHub Actions — no local machines, no manual steps, no hidden stages.

- Source → build → artifact pipeline is fully public
- Every release links to the exact CI run that produced it
- [View all builds](https://github.com/GregsGreyCode/logos/actions/workflows/build-windows.yml)

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

#### 🧪 VirusTotal scan

Each release is scanned on [VirusTotal](https://www.virustotal.com) — the scan link is included in the GitHub Release notes. You can also drag-and-drop your downloaded file at [virustotal.com](https://www.virustotal.com) to run your own scan.

---

## 🏁 Getting started

On first run, the setup wizard walks you through:

1. Choosing your LLM provider and model (local, Anthropic, OpenAI, or OpenRouter)
2. Connecting inference servers — Logos scans your local network automatically
3. Benchmarking candidates to find the best model for your hardware
4. Choosing your agent runtime and soul
5. Setting your policy level and workspace isolation mode
6. Optionally connecting Telegram

**Platform-specific choices in the wizard:**

| Platform | Step 4 — Execution backend |
|---|---|
| Bare metal (Linux / macOS / Windows) | **Local** (default) |
| Docker Compose | **Local** (agents run inside the container) |
| Docker Compose + k3s | **Kubernetes → "Logos is outside the cluster"** |
| External Kubernetes | **Kubernetes → "Logos is inside the cluster"** |

Your configuration lives in `~/.logos/config.yaml` (Linux/macOS/WSL2) or `%USERPROFILE%\.logos\config.yaml` (Windows).

---

## ⏱️ Your first 10 minutes

> 📹 *[Video walkthrough coming soon]*

**0:00 — Install and start**

Run the installer and open `http://localhost:8080`. You should see the Logos dashboard.

**2:00 — Complete the setup wizard**

The wizard launches automatically on first run. Choose a model (cloud API key or local Ollama endpoint), run the benchmark, and leave policy at `WORKSPACE_ONLY` for now. You can change everything later.

**4:00 — Send your first message**

Ask the agent something simple. Watch the **live execution panel** — you'll see exactly which tools it calls, in order, and how long each step takes. This is the STAMP model in action.

> *Try: "What can you see about the machine you're running on?"*

**6:00 — Edit your soul**

Open `~/.logos/SOUL.md` in any editor. Change the agent's name, tone, or give it a specific focus. Save — no restart needed. Send another message and notice the difference.

> *Try adding: "Always respond concisely. You are a homelab assistant named Atlas."*

**8:00 — Inspect a run**

From the dashboard, open **Runs**. Click the run you just created. You'll see the full tool trace, token counts, and outcome. Hit **Clone** to open a new session seeded from that exact configuration.

**10:00 — Where to go next**

- Connect Telegram so you can reach your agent from anywhere
- Swap the model to something local if you haven't already
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

Two passes per model on different prompt types:

| Pass | Prompt type | Purpose |
|------|-------------|---------|
| 1 | Natural language prose | Baseline throughput |
| 2 | Structured JSON output | Throughput under formatting constraints |

The two results are averaged. Time-to-first-token (TTFT) is measured on pass 1. Throughput is measured from first token to last, so cold-start latency doesn't inflate the tok/s figure.

| Label | Tokens/sec | Notes |
|-------|-----------|-------|
| Fast | ≥ 30 | Comfortable for interactive use |
| Good | ≥ 15 | Responsive for most tasks |
| Usable | ≥ 6 | Acceptable; notable latency on long outputs |
| Slow | < 6 | Likely too large for real-time agent use on this hardware |

### Capability evals — 4 tests

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

- **Eval quality (45%)** — weighted most heavily; a fast but unreliable model produces poor agent outcomes
- **Speed (30%)** — capped at 40 tok/s; returns above that have diminishing value for interactive use
- **TTFT (15%)** — time-to-first-token affects perceived responsiveness
- **Model size (10%)** — all else equal, a larger model is preferred; capped at 13B

### VRAM management

Between tests, each model is explicitly unloaded from GPU memory to prevent contention. If you connect more than one inference server, each server's models are tested **in parallel with other servers** — but models on the *same* server run sequentially to prevent VRAM contention.

---

## 🎛️ Customising your STAMP

**Soul** — edit `~/.logos/SOUL.md` at any time. Changes take effect on the next message; no restart needed.

**Tools** — enable or disable per platform via the web dashboard or by editing the `toolsets` key in `~/.logos/config.yaml`.

**Agent** — choose which runtime processes your conversation. Currently available: **Hermes** (general-purpose, full tool loop). Additional agents register via `logos/agent/interface.py`. ACP clients (VS Code, Zed, JetBrains) connect through the ACP adapter.

**Model** — switch via the web dashboard or by setting `openai.base_url` in config for any OpenAI-compatible endpoint.

**Policy** — set workspace isolation mode (`FULL_ACCESS`, `WORKSPACE_ONLY`, `REPO_SCOPED`, `READ_ONLY`), configure command approval callbacks, and run policy enforcement evals from the dashboard.

---

## 🔭 Observability

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

Every log line includes a `[session_id]` field set via a `contextvars.ContextVar` at the start of each request — grep a single session ID across gateway, agent, and tool logs without any thread-local state.

`GET /healthz` returns per-platform success and error counters (`platform_stats`), useful for spotting silent adapter failures across Telegram, Discord, Slack, and other connected platforms.

---

## 🧠 Evolution — agent self-improvement

The **Evolution** tab gives agents a structured channel to propose improvements to the platform itself, on a schedule you control.

1. **Agents analyse your codebase** on the configured interval. Each agent reads the repository, looks for bugs and complexity hotspots, and drafts a concrete improvement.
2. **A proposal is submitted** — title, summary, a unified diff, and the list of affected files — and appears in the Evolution tab for your review.
3. **You decide:** Accept, Decline, or Ask a question back to the agent.
4. **Optionally consult a frontier model** — ask Claude or GPT-4o to review the proposal before you decide.

Each Logos deployment works against **your own fork** of the repository. Fork the canonical repo, configure the fork URL in Evolution Settings, and the agent reads from it and opens PRs against it.

**Setup:** Fork → Evolution Settings → configure fork URL, PAT, base branch, schedule, frontier model → Enable.

**API:**
```
GET    /evolution/proposals              # list (filterable by status)
POST   /evolution/proposals             # create (agents use this)
POST   /evolution/proposals/{id}/decide  # accept / decline / question
POST   /evolution/proposals/{id}/consult # consult a frontier model
PATCH  /evolution/settings               # update settings
```

---

## 🔌 MCP Gateway

Logos runs a **centralized MCP (Model Context Protocol) gateway** inside the gateway process. MCP servers boot once at startup and are shared across all agent sessions — no per-agent subprocess spawning, no config duplication into sandboxes.

### Why centralized?

The per-agent subprocess model breaks in two critical scenarios:
- **OpenShell sandbox**: the sandbox container has no access to `~/.logos/config.yaml`, so MCP servers can never start inside it.
- **k8s pods**: each pod would need its own MCP server subprocesses, multiplying resource use by session count.

The centralized gateway solves both: agents connect over HTTP regardless of where they're running.

### How it works

```
Agent (any executor)
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

**`category`** is a free-form label you assign to each server — the `mcp_policy` block maps categories to approval tiers. Any server whose category isn't listed defaults to `user_approve`.

### How agents request access

Agents have two MCP tools always available (in the `mcp-gateway` toolset):

| Tool | What it does |
|------|-------------|
| `get_mcp_catalogue` | Lists all configured MCP servers, their descriptions, and the approval tier |
| `request_mcp_access` | Requests access to a specific server by name |

When `request_mcp_access("filesystem")` is called:
1. The gateway checks the server's category against `mcp_policy`.
2. **`auto_approve`**: access granted immediately; MCP tools injected into the session.
3. **`user_approve`**: an approval card appears in the web dashboard or Telegram; the agent pauses and resumes when you respond.
4. **`admin_approve`**: same flow, but only an admin-role account can approve.

### Cross-platform URL routing

The gateway automatically resolves the right endpoint URL based on where the agent is running:

| Execution mode | Agent connects to |
|---|---|
| Local process (bare metal / Docker) | `http://127.0.0.1:{mcp_port}/mcp/{name}` |
| OpenShell sandbox | `http://host.docker.internal:{mcp_port}/mcp/{name}` |
| Kubernetes pod | `http://logos-gateway.{ns}.svc.cluster.local:{mcp_port}/mcp/{name}` |

The port defaults to `8081` and can be overridden with `HERMES_MCP_PORT`.

### Management API

```
GET    /api/mcp/catalogue                  # list configured servers + policy tiers
GET    /api/mcp/status                     # which servers are running, which failed
POST   /api/mcp/grants/{session_id}/{srv}  # admin: manually grant access
DELETE /api/mcp/grants/{session_id}/{srv}  # admin: revoke access
```

Grants are in-memory and reset on gateway restart.

---

## 🔄 Migrating from OpenClaw

```bash
hermes claw migrate              # interactive migration (full preset)
hermes claw migrate --dry-run    # preview what would be migrated
hermes claw migrate --preset user-data   # migrate without secrets
```

What gets imported: `SOUL.md`, memories, skills, command allowlist, messaging settings, API keys, TTS assets, workspace instructions.

---

## ☁️ Optional cloud integrations

These are **disabled by default** and require explicit configuration. Enabling them sends data to third-party providers.

### Honcho (user modelling)

[Honcho](https://app.honcho.dev) builds a persistent model of each user across conversations and feeds it back into the agent context on future sessions.

**What it does when enabled:** syncs conversation messages to Honcho's cloud API, uploads `MEMORY.md`/`USER.md`/`SOUL.md`, runs inference on Honcho's backend to inject user insights into context.

**Privacy:** your conversations leave your network and are processed by a third party. Do not enable this if data privacy is a requirement.

**To enable:** set `HONCHO_API_KEY` in your environment or `~/.honcho/config.json`.

---

## 🛠️ Developer reference

Source in `gateway/`, `tools/`, and `agent/`. See [`AGENTS.md`](AGENTS.md) for internals, local dev setup, gateway architecture, and how to add tools.

**Runtime support:**

| Backend | Status |
|---------|--------|
| Local (Ollama / LM Studio) | ✅ Tested |
| Docker | ✅ Tested |
| Kubernetes (k3s / external) | ✅ Tested |
| SSH | ✅ Tested |
| Modal | ⚠️ Not yet tested |
| Daytona | ⚠️ Not yet tested |
| Singularity | ⚠️ Not yet tested |

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

After pushing, roll out:

```bash
kubectl rollout restart deployment/logos -n logos
kubectl rollout status  deployment/logos -n logos
```

---

## 🤝 Contributing

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

## 📜 License

MIT — see [LICENSE](LICENSE).

---

## 🙏 Thanks

This project would not exist without the open-source work it stands on:

- **[Anthropic / Claude](https://www.anthropic.com)** — Claude wrote a significant portion of the gateway, UI, tooling, and this documentation.
- **[Nous Research / hermes-agent](https://github.com/NousResearch/hermes-agent)** — the Hermes agent runtime (`agents/hermes/`) is a heavily extended fork of their open-source hermes-agent. The platform layer (gateway, auth, dashboard, STAMP system, policy enforcement) is original work built on top of it. The `tinker-atropos` RL submodule combines [Atropos](https://github.com/NousResearch/atropos) (Nous Research) and [Tinker](https://github.com/thinking-machines-lab/tinker) (Thinking Machines Lab).
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
