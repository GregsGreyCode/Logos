---
sidebar_position: 1
title: "Quickstart"
description: "Install Logos, launch the gateway, and start chatting in under 5 minutes"
---

# Quickstart

Logos is a self-hosted control plane for AI agents. At its core is the **gateway** — an always-on server that spawns and manages isolated agent instances, enforces policies, routes inference to any LLM provider, and bridges messaging platforms. **Hermes** is the built-in agent runtime that the gateway spawns to handle conversations, tool calls, and sub-agent delegation.

This guide gets you from zero to the web dashboard.

## 1. Install Logos

Run the one-line installer:

```bash
# Linux / macOS / WSL2
curl -fsSL https://raw.githubusercontent.com/GregsGreyCode/logos/main/scripts/install.sh | bash
```

:::tip Windows Users
A Windows desktop app (`.exe` installer) is available on the [releases page](https://github.com/GregsGreyCode/logos/releases). For the source install, use [WSL2](https://learn.microsoft.com/en-us/windows/wsl/install) and run the command above inside your WSL2 terminal.
:::

After it finishes, reload your shell:

```bash
source ~/.bashrc   # or source ~/.zshrc
```

## 2. Set Up a Provider

The installer configures your LLM provider automatically. To change it later:

```bash
logos model       # Choose your LLM provider and model
logos setup       # Or configure everything at once
```

`logos model` walks you through selecting an inference provider:

| Provider | What it is | How to set up |
|----------|-----------|---------------|
| **Nous Portal** | Subscription-based, zero-config | OAuth login via `logos model` |
| **OpenAI Codex** | ChatGPT OAuth, uses Codex models | Device code auth via `logos model` |
| **Anthropic** | Claude models directly (Pro/Max or API key) | API key or Claude Code setup-token |
| **OpenRouter** | Multi-provider routing across 200+ models | Enter your API key |
| **Z.AI** | GLM / Zhipu-hosted models | Set `GLM_API_KEY` / `ZAI_API_KEY` |
| **Kimi / Moonshot** | Moonshot-hosted coding and chat models | Set `KIMI_API_KEY` |
| **MiniMax** | International MiniMax endpoint | Set `MINIMAX_API_KEY` |
| **MiniMax China** | China-region MiniMax endpoint | Set `MINIMAX_CN_API_KEY` |
| **Custom Endpoint** | VLLM, SGLang, Ollama, or any OpenAI-compatible API | Set base URL + optional API key |

:::tip
You can switch providers at any time with `logos model` — no code changes, no lock-in.
:::

## 3. Launch the Gateway

The gateway is the Logos platform. It spawns isolated Hermes agent instances (in containers or local processes), serves the web dashboard, handles auth, and bridges messaging platforms.

```bash
logos gateway         # Run in foreground (good for first run)
logos gateway start   # Or run as a background service
```

Open your browser to **http://localhost:8080**. The web dashboard lets you:

- **Chat** with the agent via a full streaming UI
- **Watch** tool calls execute in real-time in the execution panel
- **Browse** conversation history, metrics, and message stats
- **Manage** multiple agent instances — each running in its own isolated process or container

```
                    ┌─────────────────────────────────┐
                    │     Logos Gateway (:8080)        │
 Web Dashboard ───▶ │  Auth · Routing · Policy · MCP  │
 Telegram      ───▶ │                                 │
 Discord       ───▶ │    Executor (spawns agents)     │
 WhatsApp      ───▶ │         │          │            │
                    └─────────┼──────────┼────────────┘
                              ▼          ▼
                         [Hermes]    [Hermes]
                         instance    instance
                         (isolated)  (isolated)
```

The gateway chooses an execution backend based on your environment:

| Backend | When it's used |
|---------|---------------|
| **Kubernetes** | Default when `HERMES_RUNTIME_MODE=kubernetes` (k8s deployments) |
| **Docker** | Container isolation on servers with Docker |
| **OpenShell** | Docker + egress policy layer (Linux/macOS) |
| **Local process** | Fallback for desktop / development / Windows |

## 4. Try Key Features

### Chat from the dashboard

Open http://localhost:8080 and start typing. The agent has access to 47+ tools out of the box — web search, file operations, terminal commands, code execution, image generation, TTS, and more.

### Connect messaging platforms

Chat with Logos from Telegram, Discord, Slack, WhatsApp, Signal, Email, or Home Assistant:

```bash
logos gateway setup    # Interactive platform configuration
```

The gateway bridges all platforms — messages arrive at the same agent with the same tools and policies.

### Schedule automated tasks

From any chat surface (dashboard, Telegram, etc.):

```
❯ Every morning at 9am, check Hacker News for AI news and send me a summary on Telegram.
```

The agent sets up a cron job that runs automatically through the gateway.

### Browse and install skills

```bash
logos skills search kubernetes
logos skills install openai/skills/k8s
logos skills install official/security/1password
```

### Connect MCP servers

Extend the agent's capabilities via Model Context Protocol:

```yaml
# Add to ~/.logos/config.yaml
mcp_servers:
  github:
    command: npx
    args: ["-y", "@modelcontextprotocol/server-github"]
    env:
      GITHUB_PERSONAL_ACCESS_TOKEN: "ghp_xxx"
```

---

## Local Mode (CLI)

If you just want a quick local chat session without running the gateway, you can use the Hermes CLI directly:

```bash
hermes          # Start an interactive chat session
hermes -c       # Resume the most recent session
```

This runs the agent in-process — no gateway, no containers, no isolation. It's the simplest way to use Hermes for local tasks like coding assistance, but it doesn't give you the dashboard, messaging platforms, multi-instance management, or execution isolation that the gateway provides.

Useful CLI features:

| Feature | How |
|---------|-----|
| Slash commands | Type `/` for autocomplete — `/help`, `/tools`, `/model`, `/save` |
| Multi-line input | `Alt+Enter` or `Ctrl+J` |
| Interrupt | Type a new message or `Ctrl+C` |
| Resume session | `hermes -c` or `hermes --continue` |

---

## How It Works

Every agent run in Logos is defined by five dimensions — the **STAMP** model:

| Dimension | What it controls |
|-----------|-----------------|
| **S** — Soul | Agent persona and communication style (hot-reloadable) |
| **T** — Tools | Capabilities available to the agent (scoped per session and policy) |
| **A** — Agent | The runtime adapter (Hermes is the built-in one) |
| **M** — Model | Which LLM handles inference (Claude, GPT, OpenRouter, local, etc.) |
| **P** — Policy | Rules and approval gates (workspace scoping, command restrictions) |

Every run records its complete STAMP to a local SQLite database — fully auditable, reproducible, and queryable.

---

## Quick Reference

### Platform commands (`logos`)

| Command | Description |
|---------|-------------|
| `logos gateway` | Launch the Logos gateway + web dashboard |
| `logos gateway start` | Run the gateway as a background service |
| `logos gateway setup` | Configure messaging platforms |
| `logos gateway status` | Check gateway status |
| `logos model` | Choose your LLM provider and model |
| `logos tools` | Configure which tools are enabled |
| `logos setup` | Full setup wizard (configures everything at once) |
| `logos doctor` | Diagnose issues |
| `logos update` | Update to latest version |

### Agent commands (`hermes`)

| Command | Description |
|---------|-------------|
| `hermes` | Start local CLI chat (no gateway) |
| `hermes -c` | Resume last CLI session |
| `hermes chat -q "..."` | Single query mode |
| `hermes sessions browse` | Interactive session picker |

## Next Steps

- **[Installation](./installation.md)** — Manual install, extras breakdown, server deployment
- **[Configuration](../user-guide/configuration.md)** — Customize your setup
- **[Messaging Gateway](../user-guide/messaging/index.md)** — Connect Telegram, Discord, Slack, WhatsApp, Signal, Email, or Home Assistant
- **[Tools & Toolsets](../user-guide/features/tools.md)** — Explore available capabilities
- **[Deployment](../user-guide/deployment.md)** — Docker Compose, Kubernetes, and production setup
