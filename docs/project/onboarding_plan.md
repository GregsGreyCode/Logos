# Logos Admin Onboarding Plan

First-time setup flow for administrators. The onboarding splits early into two tracks based on a single founding decision: **how much data are you willing to send outside your network?**

---

## The Two Tracks

| | Local-first | Frontier-first |
|---|---|---|
| **Models** | Ollama / LM Studio (on-device) | Anthropic, OpenAI, OpenRouter |
| **Voice** | faster-whisper (local) | Groq or OpenAI Whisper (cloud) |
| **Vision** | Disabled | Enabled via OpenRouter |
| **User modelling** | Local memory files only | Honcho (cloud, opt-in) |
| **Multi-model reasoning** | Disabled | Mixture-of-Agents enabled |
| **Data leaves network** | Never | For AI calls and enabled integrations |
| **Cost** | Hardware only | Pay-per-token for cloud APIs |
| **Privacy guarantee** | Total | Depends on what you enable |

Both tracks have full access to the gateway (Telegram, Discord, Slack, etc.), run auditability, policy enforcement, cron scheduling, A2A handoffs, and editor integrations. The track only governs what leaves your network.

---

## Onboarding Flow (v0.3.53 — browser wizard at /setup)

The setup wizard runs entirely in the browser. Wizard state is persisted to `localStorage` under the key `logos_setup_progress_v2` (1-hour TTL), so refreshing the page resumes from the current step without re-running benchmarks.

### Step 0 — Track Selection

The founding decision. Two options are presented:

- **Local-first** — active and fully supported
- **Frontier-first** — visible but marked "coming soon"

A collapsible intro panel explains the full 7-step flow with STAMP context before the user commits to a track.

---

### Step 1 — Connect Model Servers

Auto-scans the local network for:
- Ollama at `:11434`
- LM Studio at `:1234`

Scan results are cached in `localStorage` under `logos_setup_scan` for 10 minutes. Manual add is supported. Setup guides for both Ollama and LM Studio are always visible as collapsible panels.

---

### Step 2 — Benchmark Models

SSE streaming benchmark. Each candidate model runs:

1. A warmup pass (discarded, avoids cold-start penalty)
2. Three scored passes: prose × 2 + structured

Four capability evaluations per model:
- Instruction following
- Reasoning
- JSON format
- Tool selection

Results display: tok/s, TTFT, eval breakdown. Rows are clickable to expand details and see why a model was or was not recommended. The wizard pre-selects the best balanced model; also highlights the fastest acceptable model if it differs.

---

### Step 3 — Agent Runtime

Runtime picker. Hermes is available; other runtimes are shown as "coming soon". Selection drives the Kubernetes namespace value used in step 5.

---

### Step 4 — Execution Target

Two options:
- **This machine** (local)
- **Kubernetes** — in-cluster (auto-detected) or kubeconfig (textarea input)

A connectivity test is available. Namespace is auto-derived from the agent runtime chosen in step 4.

---

### Step 5 — Soul

A 4×2 grid of 8 soul presets. Only the selected card hue-cycles. Each card shows an icon, name, description, and tool hints.

---

### Step 6 — Your Account

Email, username, password, and confirm password. All fields are required. On submit, the backend updates the admin user and replaces the default seed credentials (`admin@example.com` / `admin`).

---

### Step 7 — Review & Launch

Summary of all configuration from steps 0–6. Inline error display (no `alert()` calls). An endpoint reachability check warns if the model server is unreachable rather than blocking launch. Back navigation is available from this step.

On `complete()`, `logos_setup_progress_v2` and `logos_setup_scan` are cleared from `localStorage`. The chosen model and endpoint are written to `config.yaml` (`HERMES_MODEL`, `OPENAI_BASE_URL`) so the agent uses them on next start — skipped if those env vars are already set (respects pre-configured k8s deployments). All selected inference servers are registered as machines in the routing database.

---

## Implementation Status

| Feature | Status |
|---|---|
| Track selection (step 0) | Done |
| Step intro panel with STAMP context | Done |
| Connect model servers — auto-scan Ollama + LM Studio | Done |
| Connect model servers — manual add | Done |
| Scan result caching (10 min, localStorage) | Done |
| Benchmark — SSE streaming, warmup pass, 3-pass median | Done |
| Benchmark — 4 capability evals | Done |
| Benchmark — pre-selects best balanced + fastest | Done |
| Verify model step (removed — folded into benchmark + reachability check) | Removed in v0.3.44 |
| Agent runtime picker | Done |
| Execution target — local and Kubernetes | Done |
| Soul preset grid (8 presets) | Done |
| Admin account creation (step 7) | Done |
| Review & launch with reachability check | Done |
| Wizard state persistence (1hr, localStorage, key v2) | Done |
| Multi-server benchmark + registration | Done — all selected servers benchmarked and registered |
| Model written to config.yaml on complete | Done — agent picks up chosen model on next start |
| Back navigation from all steps | Done |
| Frontier-first track | Not built — coming soon |
| System check / hermes doctor step | Not built |
| Messaging platform setup in wizard | Not built — configured post-setup from dashboard |
| Tool/policy defaults by track | Not built |
| Honcho opt-in | Not built |
| Voice provider selection | Not built |
| Validation run step (separate) | Not built — folded into step 3 and step 8 reachability check |

---

## What Needs to Be Built

| Component | Notes |
|---|---|
| Frontier-first track | Full track behind the "coming soon" gate |
| Messaging platform setup | Add post-setup dashboard flow; not in wizard |
| Tool/policy defaults by track | Config presets per track applied at launch |
| Honcho opt-in with privacy gate | Wrapper around existing integration |
| Voice provider selection | Wrapper around transcription config |
| `hermes doctor` system check | Surface blockers before configuration |

---

## Design Principles

- **Never surprise the user with a cloud call.** Every step that touches a third-party service shows a notice before prompting for credentials.
- **Sensible defaults, not locked defaults.** Both tracks suggest defaults but allow customisation in the same flow.
- **Fail loudly on system checks.** Do not let a broken environment get through to model or platform configuration.
- **One decision per screen.** The track choice is the only decision that shapes what follows. Everything else is additive.
- **Resumable.** Wizard state is persisted so a page refresh never forces the user to re-run benchmarks.
