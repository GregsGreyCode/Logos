---
sidebar_position: 2
title: "Installation"
description: "Install Logos on Linux, macOS, WSL2, or Windows"
---

# Installation

Get Logos up and running with the one-line installer, the Windows desktop app, or a fully manual install.

## Quick Install

### Linux / macOS / WSL2

```bash
curl -fsSL https://raw.githubusercontent.com/GregsGreyCode/logos/main/scripts/install.sh | bash
```

The installer handles everything automatically — all dependencies (Python, Node.js, ripgrep, ffmpeg), the repo clone, virtual environment, global `logos` and `hermes` command setup, and LLM provider configuration.

### Windows Desktop App

Download the `.exe` installer from the [releases page](https://github.com/GregsGreyCode/logos/releases). The desktop app:

- Runs the gateway with a LocalProcessExecutor (no Docker or WSL required)
- Includes a system tray icon with auto-update notifications
- Stores configuration in `%USERPROFILE%\.logos\`

:::tip WSL2 Alternative
If you prefer the full Linux experience on Windows, install [WSL2](https://learn.microsoft.com/en-us/windows/wsl/install) and use the Linux installer above. This gives you access to all execution backends including Docker and Kubernetes.
:::

### After Installation

Reload your shell and launch the gateway:

```bash
source ~/.bashrc   # or: source ~/.zshrc
logos gateway      # Start the Logos gateway + web dashboard
```

Open http://localhost:8080 in your browser. You're running.

To reconfigure settings later:

```bash
logos model          # Choose your LLM provider and model
logos tools          # Configure which tools are enabled
logos gateway setup  # Set up messaging platforms
logos config set     # Set individual config values
logos setup          # Or run the full setup wizard
```

---

## Prerequisites

The only prerequisite is **Git**. The installer automatically handles everything else:

- **uv** (fast Python package manager)
- **Python 3.11** (via uv, no sudo needed)
- **Node.js v22** (for browser automation and WhatsApp bridge)
- **ripgrep** (fast file search)
- **ffmpeg** (audio format conversion for TTS)

:::info
You do **not** need to install Python, Node.js, ripgrep, or ffmpeg manually. The installer detects what's missing and installs it for you. Just make sure `git` is available (`git --version`).
:::

---

## Manual Installation

If you prefer full control over the installation process, follow these steps.

### Step 1: Clone the Repository

Clone with `--recurse-submodules` to pull the required submodules:

```bash
git clone --recurse-submodules https://github.com/GregsGreyCode/logos.git
cd logos
```

If you already cloned without `--recurse-submodules`:
```bash
git submodule update --init --recursive
```

### Step 2: Install uv & Create Virtual Environment

```bash
# Install uv (if not already installed)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Create venv with Python 3.11 (uv downloads it if not present — no sudo needed)
uv venv venv --python 3.11
```

:::tip
You do **not** need to activate the venv. The entry points have hardcoded shebangs pointing to the venv Python, so they work globally once symlinked.
:::

### Step 3: Install Python Dependencies

```bash
# Tell uv which venv to install into
export VIRTUAL_ENV="$(pwd)/venv"

# Install with all extras
uv pip install -e ".[all]"
```

If you only want the core agent (no messaging, MCP, or cloud support):
```bash
uv pip install -e "."
```

<details>
<summary><strong>Optional extras breakdown</strong></summary>

| Extra | What it adds | Install command |
|-------|-------------|-----------------|
| `all` | Everything below | `uv pip install -e ".[all]"` |
| `messaging` | Telegram, Discord & Slack gateway adapters | `uv pip install -e ".[messaging]"` |
| `cron` | Cron expression parsing for scheduled tasks | `uv pip install -e ".[cron]"` |
| `cli` | Terminal menu UI for setup wizard | `uv pip install -e ".[cli]"` |
| `modal` | Modal cloud execution backend | `uv pip install -e ".[modal]"` |
| `tts-premium` | ElevenLabs premium voices | `uv pip install -e ".[tts-premium]"` |
| `pty` | PTY terminal support | `uv pip install -e ".[pty]"` |
| `honcho` | AI-native memory (Honcho integration) | `uv pip install -e ".[honcho]"` |
| `mcp` | Model Context Protocol server/client support | `uv pip install -e ".[mcp]"` |
| `homeassistant` | Home Assistant integration | `uv pip install -e ".[homeassistant]"` |
| `acp` | ACP editor integration (VS Code, Zed, JetBrains) | `uv pip install -e ".[acp]"` |
| `slack` | Slack messaging platform | `uv pip install -e ".[slack]"` |
| `dev` | pytest & test utilities | `uv pip install -e ".[dev]"` |

You can combine extras: `uv pip install -e ".[messaging,cron,mcp]"`

</details>

### Step 4: Install Submodule Packages

```bash
# Terminal tool backend (required for terminal/command-execution)
uv pip install -e "./mini-swe-agent"

# RL training backend
uv pip install -e "./tinker-atropos"
```

Both are optional — if you skip them, the corresponding toolsets simply won't be available.

### Step 5: Install Node.js Dependencies (Optional)

Only needed for **browser automation** (Browserbase-powered) and **WhatsApp bridge**:

```bash
npm install
```

### Step 6: Create the Configuration Directory

```bash
# Create the directory structure
mkdir -p ~/.logos/{cron,sessions,logs,memories,skills,pairing,hooks,image_cache,audio_cache,whatsapp/session}

# Copy the example config file
cp cli-config.yaml.example ~/.logos/config.yaml

# Create an empty .env file for API keys
touch ~/.logos/.env
```

:::info Upgrading from an older install?
If you have an existing `~/.hermes` directory, the gateway automatically migrates it to `~/.logos` on first startup. You can also set `LOGOS_HOME` (or legacy `HERMES_HOME`) to override the location.
:::

### Step 7: Add Your API Keys

Open `~/.logos/.env` and add at minimum an LLM provider key:

```bash
# Required — at least one LLM provider:
OPENROUTER_API_KEY=sk-or-v1-your-key-here

# Optional — enable additional tools:
FIRECRAWL_API_KEY=fc-your-key          # Web search & scraping (or self-host, see docs)
FAL_KEY=your-fal-key                   # Image generation (FLUX)
```

Or set them via the CLI:
```bash
logos config set OPENROUTER_API_KEY sk-or-v1-your-key-here
```

### Step 8: Add `logos` and `hermes` to Your PATH

```bash
mkdir -p ~/.local/bin
ln -sf "$(pwd)/venv/bin/logos" ~/.local/bin/logos
ln -sf "$(pwd)/venv/bin/hermes" ~/.local/bin/hermes
```

If `~/.local/bin` isn't on your PATH, add it to your shell config:

```bash
# Bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc && source ~/.bashrc

# Zsh
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc && source ~/.zshrc

# Fish
fish_add_path $HOME/.local/bin
```

### Step 9: Configure Your Provider

```bash
logos model       # Select your LLM provider and model
```

### Step 10: Verify and Launch

```bash
logos version    # Check that the command is available
logos doctor     # Run diagnostics to verify everything is working
logos gateway    # Launch the gateway + web dashboard
```

Open http://localhost:8080 to verify the dashboard loads.

---

## Quick-Reference: Manual Install (Condensed)

For those who just want the commands:

```bash
# Install uv
curl -LsSf https://astral.sh/uv/install.sh | sh

# Clone & enter
git clone --recurse-submodules https://github.com/GregsGreyCode/logos.git
cd logos

# Create venv with Python 3.11
uv venv venv --python 3.11
export VIRTUAL_ENV="$(pwd)/venv"

# Install everything
uv pip install -e ".[all]"
uv pip install -e "./mini-swe-agent"
uv pip install -e "./tinker-atropos"
npm install  # optional, for browser tools and WhatsApp

# Configure
mkdir -p ~/.logos/{cron,sessions,logs,memories,skills,pairing,hooks,image_cache,audio_cache,whatsapp/session}
cp cli-config.yaml.example ~/.logos/config.yaml
touch ~/.logos/.env
echo 'OPENROUTER_API_KEY=sk-or-v1-your-key' >> ~/.logos/.env

# Make logos + hermes available globally
mkdir -p ~/.local/bin
ln -sf "$(pwd)/venv/bin/logos" ~/.local/bin/logos
ln -sf "$(pwd)/venv/bin/hermes" ~/.local/bin/hermes

# Launch
logos doctor
logos gateway
```

---

## Deployment Options

The gateway spawns agent instances using a pluggable executor. Choose based on your environment:

### Desktop / Development

The gateway runs locally and spawns agents as supervised subprocesses:

```bash
logos gateway    # Starts on :8080, uses LocalProcessExecutor
```

No Docker or Kubernetes required. This is what the Windows desktop app does.

### Docker Compose (recommended for most self-hosted setups)

```bash
docker compose up -d
```

The Dockerfile entry point is `logos gateway run` — the container runs the gateway, which then spawns isolated agent instances via the configured executor.

### Kubernetes

Logos includes K8s manifests for pod-per-agent isolation with RBAC and NetworkPolicy:

```bash
kubectl apply -f k8s/
```

In Kubernetes mode (`HERMES_RUNTIME_MODE=kubernetes`), the gateway creates a separate Deployment, Service, PVC, and ConfigMap for each agent instance — full pod-level isolation.

See the [Deployment Guide](../user-guide/deployment.md) for production configuration details.

### Gateway as a System Service

For bare-metal or VM installs where you want the gateway to survive reboots:

```bash
logos gateway install   # Install as systemd (Linux) or launchd (macOS) service
logos gateway start     # Start the service
logos gateway status    # Check it's running
```

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `logos: command not found` | Reload your shell (`source ~/.bashrc`) or check PATH |
| `API key not set` | Run `logos model` to configure your provider, or `logos config set OPENROUTER_API_KEY your_key` |
| Missing config after update | Run `logos config check` then `logos config migrate` |
| Gateway won't start | Check port 8080 isn't in use: `lsof -i :8080` |
| Docker executor fails | Ensure Docker is running and your user is in the `docker` group |

For more diagnostics, run `logos doctor` — it will tell you exactly what's missing and how to fix it.
