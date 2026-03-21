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

## Onboarding Flow

### Step 0 — Welcome

Display on first admin login.

```
Welcome to Logos.

Before we configure anything, one decision shapes everything else:

  [1] Local-first   — your data never leaves this machine.
                      Models run on your hardware. Slower, private, free.

  [2] Frontier-first — use the best available models via cloud APIs.
                       Faster and more capable. Requires API keys.
                       Conversations are processed by third-party providers.

You can change this later, but it affects which features are available.

Which setup suits you?  [1/2]:
```

**This choice is persisted to config as `setup.track: local | frontier`.**

---

### Step 1 — System Check (`hermes doctor`)

Run automatically on both tracks. Surface blockers before anything is configured.

Checks:
- Python version ≥ 3.11
- Node.js ≥ 18 (for dashboard)
- Git available
- `~/.hermes/` directory writable
- SQLite accessible
- **Local track only:** Ollama or LM Studio reachable at default endpoint
- **Frontier track only:** Outbound HTTPS connectivity

Output:
```
System check
  ✓ Python 3.11.4
  ✓ Node.js 20.1.0
  ✓ Git 2.43.0
  ✓ ~/.hermes/ writable
  ✓ SQLite OK
  ✓ Ollama reachable at localhost:11434    ← local track

All checks passed. Continuing.
```

Hard-stop on failure. No partial configuration.

---

### Step 2 — Model Setup

**Local track**

```
Local model setup

Logos will use Ollama (or LM Studio) running on this machine.

Ollama endpoint [http://localhost:11434]:

Fetching available models...
  [1] llama3.2:3b      (2.0 GB)  — fast, lightweight
  [2] llama3.3:70b     (43 GB)   — strong reasoning, needs GPU
  [3] mistral:7b       (4.1 GB)  — balanced
  [4] qwen2.5:14b      (9.0 GB)  — good for coding
  [5] Enter custom model name

Select a model [1]:
```

If the chosen model is not pulled, offer to pull it now.

Saves to config:
```yaml
provider: ollama
model: llama3.2:3b
openai_base_url: http://localhost:11434/v1
openai_api_key: ollama
```

---

**Frontier track**

```
Cloud model setup

Choose your primary provider:

  [1] Anthropic     — Claude models (recommended for reasoning and coding)
  [2] OpenRouter    — 200+ models, single API key, pay-per-use
  [3] OpenAI        — GPT models

Provider [1]:
```

After provider selection, prompt for API key. Validate it with a test call before proceeding. Show the model list for the chosen provider and let the admin pick a default.

Note displayed for any cloud provider selection:
```
⚠  Messages sent to this provider leave your network and are processed
   under their terms of service. Check their data retention policy.
```

Saves provider, API key (to `.env`), and selected model to config.

---

### Step 3 — Admin Account

Set credentials for the dashboard and API.

```
Admin account setup

These credentials protect the dashboard and API. Store them somewhere safe —
there is no password reset without editing config directly.

Admin email:
Admin password: (hidden)
Confirm:        (hidden)
```

Generates `HERMES_JWT_SECRET` automatically via `openssl rand -hex 32`.

Writes to `k8s/02-secret.yaml` (or `.env` for non-Kubernetes installs) with clear instruction not to commit the filled file.

---

### Step 4 — Soul

The soul is the agent's persona — how it communicates, its name, its defaults.

```
Soul setup

The soul defines how your agent behaves. It lives in ~/.hermes/SOUL.md
and can be edited at any time without restarting.

Choose a starting point:

  [1] Minimal         — no persona, just a capable assistant
  [2] Homelab ops     — focused on infrastructure, direct, no filler
  [3] Dev partner     — coding-oriented, technical, remembers your stack
  [4] Family-friendly — approachable, patient, avoids jargon
  [5] Write my own    — open editor now

Selection [1]:
```

The selected preset is written to `~/.hermes/SOUL.md`. The admin can edit it immediately or skip.

---

### Step 5 — Messaging Platforms

```
Gateway setup

Which platforms should the agent be reachable on?
You can add more later with: hermes gateway add <platform>

  [ ] Telegram       — recommended, best mobile experience
  [ ] Discord
  [ ] Slack
  [ ] WhatsApp       — requires unofficial bridge (see docs)
  [ ] Signal         — requires signal-cli running locally
  [ ] Email          — IMAP/SMTP
  [ ] Home Assistant
  [ ] CLI only       — no gateway, just the terminal

Toggle with numbers, confirm with Enter:
```

For each selected platform, prompt only for the required credentials (bot token, webhook URL, etc.). Skip platforms not selected.

**WhatsApp and Signal** show an additional note:
```
⚠  WhatsApp uses an unofficial bridge and may violate Meta's ToS.
   Signal requires signal-cli running as a local daemon.
   See docs/platforms.md for setup instructions.
```

---

### Step 6 — Tools & Policy

**Local track defaults:**

```
Tool configuration (Local-first defaults)

The following toolsets will be enabled:
  ✓ terminal       — run commands (with approval required)
  ✓ filesystem     — read/write files within workspace
  ✓ web_search     — search the web
  ✓ memory         — persistent notes across sessions
  ✓ session_search — search past conversations (local FTS5)
  ✗ vision         — requires OpenRouter API key (disabled)
  ✗ moa            — Mixture-of-Agents, requires cloud APIs (disabled)
  ✗ honcho         — cloud user modelling (disabled)

Workspace isolation: WORKSPACE_ONLY
Command approval:    required for dangerous commands

[C]ustomise or [A]ccept defaults:
```

**Frontier track defaults:**

```
Tool configuration (Frontier-first defaults)

The following toolsets will be enabled:
  ✓ terminal       — run commands (with approval required)
  ✓ filesystem     — read/write files within workspace
  ✓ web_search     — search the web
  ✓ memory         — persistent notes across sessions
  ✓ session_search — search past conversations
  ✓ vision         — image analysis via OpenRouter
  ✓ moa            — Mixture-of-Agents (uses 4 cloud models per call — expensive)

Workspace isolation: WORKSPACE_ONLY
Command approval:    required for dangerous commands

[C]ustomise or [A]ccept defaults:
```

If customising, show each toolset with a toggle and brief description.

**Workspace isolation — explained at selection:**

```
Workspace isolation controls what the agent can access on disk.

  [1] FULL_ACCESS     — no restrictions (not recommended)
  [2] WORKSPACE_ONLY  — reads/writes confined to the run workspace (recommended)
  [3] REPO_SCOPED     — confined to the current git repository
  [4] READ_ONLY       — no writes permitted

Note: all modes are Python-level enforcement. For stronger isolation,
use a container backend (Docker, Modal) — see docs/isolation.md.

Isolation mode [2]:
```

---

### Step 7 — Optional Cloud Integrations (Frontier track only)

Shown only on the frontier track. Each integration is individually opt-in with a privacy notice before prompting for credentials.

**Honcho (user modelling)**

```
Optional: Honcho user modelling

Honcho builds a persistent model of each user across conversations —
tracking preferences, communication style, and context — and injects
that back into the agent on future sessions.

⚠  Privacy: Honcho is a managed cloud service (app.honcho.dev).
   When enabled, every conversation message is sent to their servers.
   Your MEMORY.md, USER.md, and SOUL.md are also uploaded on activation.
   There is no way to sanitise this data — the service requires actual
   conversation content to function.

   Do not enable this if data privacy is a requirement.

Enable Honcho? [y/N]:
```

If yes, prompt for `HONCHO_API_KEY`.

**Voice transcription provider**

```
Optional: Cloud voice transcription

Logos transcribes voice locally using faster-whisper by default (free, private).
Cloud providers are faster and more accurate on low-end hardware.

  [1] faster-whisper  — local, free, private (default)
  [2] Groq Whisper    — cloud, free tier, fast
  [3] OpenAI Whisper  — cloud, paid

⚠  Cloud options (2/3) send audio data to a third-party provider.

Transcription provider [1]:
```

---

### Step 8 — Validation Run

Run automatically after configuration is complete.

```
Running validation...

  ✓ Config written to ~/.hermes/config.yaml
  ✓ Model reachable — llama3.2:3b responded in 1.2s
  ✓ Gateway started — Telegram connected
  ✓ Policy eval passed — hermes evals run policy_enforcement
  ✓ Database initialised — sessions.db OK
  ✓ Test run completed — 1 run recorded

Setup complete.
```

Any failure here surfaces the specific error and offers to retry that step or skip.

---

### Step 9 — Summary & Next Steps

```
Logos is configured and ready.

Track:        Local-first
Model:        llama3.2:3b (Ollama)
Platforms:    Telegram, CLI
Isolation:    WORKSPACE_ONLY
Cloud:        None enabled

Start the agent:
  hermes                    — interactive CLI
  hermes gateway            — start Telegram + other platforms

Useful commands:
  hermes model              — switch model
  hermes tools              — adjust toolsets
  hermes runs list          — view run history
  hermes doctor             — diagnose issues
  hermes setup              — re-run this wizard at any time

Edit your soul at any time:
  ~/.hermes/SOUL.md         — changes take effect on the next message

Documentation:
  AGENTS.md                 — architecture and internals
  docs/platforms.md         — platform-specific setup
  docs/isolation.md         — workspace and container backends
```

---

## Implementation Notes

### What needs to be built

The onboarding flow does not yet exist as a unified wizard. `hermes setup` is the entry point but currently handles sections independently. The following work is required:

| Component | Status | Notes |
|---|---|---|
| Track selection screen | Not built | New |
| `hermes doctor` system check | Exists | Extend with local/frontier checks |
| Model setup (local) | Exists via `hermes model` | Wrap into wizard step |
| Model setup (frontier) | Exists via `hermes model` | Add validation call before saving |
| Admin account creation | Not built | Writes to K8s secret / `.env` |
| Soul preset selection | Not built | Presets need writing |
| Platform toggle UI | Partially exists | Needs multi-select and conditional prompts |
| Tool/policy defaults by track | Not built | Config presets per track |
| Honcho opt-in with privacy gate | Not built | Wrapper around existing integration |
| Voice provider selection | Not built | Wrapper around transcription config |
| Validation run | Partially exists via `hermes doctor` | Extend with post-config checks |
| Summary screen | Not built | New |

### Config keys written by onboarding

```yaml
# ~/.hermes/config.yaml
setup:
  track: local             # or: frontier
  completed: true
  version: 1

provider: ollama           # or: anthropic, openrouter, openai
model: llama3.2:3b
openai_base_url: http://localhost:11434/v1

workspace_isolation: workspace_only
toolsets:
  - terminal
  - filesystem
  - web_search
  - memory
  - session_search

gateway:
  platforms:
    - telegram
    - cli

honcho:
  enabled: false

voice:
  provider: faster-whisper
```

### Guard rails

- Onboarding must be re-runnable at any time via `hermes setup` without destroying existing config
- Each step should be skippable (admin presses Enter to keep current value)
- Track selection should warn before switching tracks if credentials are already configured for the other track
- Onboarding state (`setup.completed`) allows the platform to detect first-run and redirect to setup
- Validation step failures must not mark setup as complete

### Design principles

- **Never surprise the user with a cloud call.** Every step that touches a third-party service shows a notice before prompting for credentials.
- **Sensible defaults, not locked defaults.** Both tracks suggest defaults but allow customisation in the same flow.
- **Fail loudly on Step 1.** Do not let a broken environment get through to model or platform configuration.
- **One decision per screen.** The track choice is the only decision that shapes what follows. Everything else is additive.
