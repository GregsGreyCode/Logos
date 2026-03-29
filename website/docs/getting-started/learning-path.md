---
sidebar_position: 3
title: 'Learning Path'
description: 'Choose your learning path through the Logos documentation based on your experience level and goals.'
---

# Learning Path

Logos is a self-hosted agent platform. The **gateway** is the core — it spawns isolated agent instances, serves a web dashboard, enforces policies, and bridges messaging platforms. **Hermes** is the agent runtime the gateway manages. You interact through the web dashboard, messaging platforms (Telegram, Discord, etc.), IDE integration, or — for quick local use — the Hermes CLI directly.

This page helps you figure out where to start based on your experience level and goals.

:::tip Start Here
If you haven't installed Logos yet, begin with the [Installation guide](/docs/getting-started/installation) and then run through the [Quickstart](/docs/getting-started/quickstart). Everything below assumes you have a working installation.
:::

## How to Use This Page

- **Know your level?** Jump to the [experience-level table](#by-experience-level) and follow the reading order for your tier.
- **Have a specific goal?** Skip to [By Use Case](#by-use-case) and find the scenario that matches.
- **Just browsing?** Check the [Key Features](#key-features-at-a-glance) table for a quick overview of everything Logos can do.

## By Experience Level

| Level | Goal | Recommended Reading |
|---|---|---|
| **Beginner** | Launch the gateway, use the web dashboard, have basic conversations | [Installation](/docs/getting-started/installation) → [Quickstart](/docs/getting-started/quickstart) → [Configuration](/docs/user-guide/configuration) |
| **Intermediate** | Connect messaging platforms, configure policies, use memory, cron, and skills | [Messaging](/docs/user-guide/messaging) → [Tools](/docs/user-guide/features/tools) → [Skills](/docs/user-guide/features/skills) → [Memory](/docs/user-guide/features/memory) → [Cron](/docs/user-guide/features/cron) → [Sessions](/docs/user-guide/sessions) |
| **Advanced** | Deploy on a server/cluster, build custom tools, train models, contribute | [Architecture](/docs/developer-guide/architecture) → [Deployment](/docs/user-guide/deployment) → [Adding Tools](/docs/developer-guide/adding-tools) → [Creating Skills](/docs/developer-guide/creating-skills) → [RL Training](/docs/user-guide/features/rl-training) → [Contributing](/docs/developer-guide/contributing) |

## By Use Case

Pick the scenario that matches what you want to do.

### "I want to run Logos on my desktop"

The simplest path — run the gateway locally, chat through the web dashboard.

1. [Installation](/docs/getting-started/installation)
2. [Quickstart](/docs/getting-started/quickstart)
3. [Configuration](/docs/user-guide/configuration)

The gateway starts on port 8080 and spawns agents as local processes. Open http://localhost:8080 and start chatting. The dashboard gives you streaming chat, real-time tool execution monitoring, and conversation history.

### "I want to deploy on my homelab or server"

Run Logos in production with container isolation, persistence, and multi-user support.

1. [Installation](/docs/getting-started/installation) (deployment section)
2. [Configuration](/docs/user-guide/configuration)
3. [Deployment](/docs/user-guide/deployment)
4. [Architecture](/docs/developer-guide/architecture)
5. [Security](/docs/user-guide/security)

The gateway spawns agent instances using a pluggable executor:

| Backend | Isolation | Best for |
|---------|-----------|----------|
| **Local process** | OS process boundary | Desktop, development |
| **Docker** | Container sandbox (`--cap-drop=ALL`) | Single-server |
| **Docker Compose** | Compose stack | Most self-hosted setups |
| **Kubernetes** | Pod per agent + RBAC/NetworkPolicy | Homelab clusters, production |

### "I want a Telegram/Discord bot"

The gateway bridges messaging platforms — each platform adapter connects to the same agent instances with the same tools and policies.

1. [Installation](/docs/getting-started/installation)
2. [Configuration](/docs/user-guide/configuration)
3. [Messaging Overview](/docs/user-guide/messaging)
4. [Telegram Setup](/docs/user-guide/messaging/telegram)
5. [Discord Setup](/docs/user-guide/messaging/discord)
6. [Security](/docs/user-guide/security)

Supported platforms: Telegram, Discord, Slack, WhatsApp, Signal, Email, Home Assistant.

For full project examples, see:
- [Daily Briefing Bot](/docs/guides/daily-briefing-bot)
- [Team Telegram Assistant](/docs/guides/team-telegram-assistant)

### "I want a local CLI coding assistant"

Use the Hermes CLI directly for quick local chat — no gateway, no containers, just an agent in your terminal.

1. [Installation](/docs/getting-started/installation)
2. [Quickstart](/docs/getting-started/quickstart) (local mode section)
3. [CLI Usage](/docs/user-guide/cli)
4. [Code Execution](/docs/user-guide/features/code-execution)
5. [Context Files](/docs/user-guide/features/context-files)

:::tip
The CLI runs the agent in-process without the gateway. It's the fastest way to get a coding assistant, but you don't get the dashboard, messaging platforms, execution isolation, or multi-instance management.
:::

### "I want to automate tasks"

Schedule recurring tasks, run batch jobs, or chain agent actions with the workflow engine.

1. [Quickstart](/docs/getting-started/quickstart)
2. [Cron Scheduling](/docs/user-guide/features/cron)
3. [Batch Processing](/docs/user-guide/features/batch-processing)
4. [Workflows](/docs/user-guide/features/workflows)
5. [Delegation](/docs/user-guide/features/delegation)
6. [Hooks](/docs/user-guide/features/hooks)

:::tip
Cron jobs run through the gateway — the agent executes tasks on a schedule without you being present. The workflow engine supports DAG-based task orchestration with conditional branches and approval gates.
:::

### "I want to build custom tools/skills"

Extend Logos with your own tools and reusable skill packages.

1. [Tools Overview](/docs/user-guide/features/tools)
2. [Skills Overview](/docs/user-guide/features/skills)
3. [MCP (Model Context Protocol)](/docs/user-guide/features/mcp)
4. [Architecture](/docs/developer-guide/architecture)
5. [Adding Tools](/docs/developer-guide/adding-tools)
6. [Creating Skills](/docs/developer-guide/creating-skills)

:::tip
Tools are individual functions the agent can call. Skills are bundles of tools, prompts, and configuration packaged together. MCP servers let you connect external tool providers. Start with tools, graduate to skills.
:::

### "I want to train models"

Use reinforcement learning to fine-tune model behavior with the built-in RL training pipeline.

1. [Quickstart](/docs/getting-started/quickstart)
2. [Configuration](/docs/user-guide/configuration)
3. [RL Training](/docs/user-guide/features/rl-training)
4. [Provider Routing](/docs/user-guide/features/provider-routing)
5. [Architecture](/docs/developer-guide/architecture)

### "I want to use it as a Python library"

Integrate Logos into your own Python applications programmatically.

1. [Installation](/docs/getting-started/installation)
2. [Quickstart](/docs/getting-started/quickstart)
3. [Python Library Guide](/docs/guides/python-library)
4. [Architecture](/docs/developer-guide/architecture)
5. [Tools](/docs/user-guide/features/tools)
6. [Sessions](/docs/user-guide/sessions)

### "I want to use it in my editor"

Run Hermes as an ACP server for VS Code, Zed, or JetBrains.

1. [Installation](/docs/getting-started/installation)
2. [ACP Editor Integration](/docs/user-guide/features/acp)
3. [Tools](/docs/user-guide/features/tools)
4. [Configuration](/docs/user-guide/configuration)

## Key Features at a Glance

| Feature | What It Does | Link |
|---|---|---|
| **Gateway & Dashboard** | Always-on server that spawns agents, serves web UI, manages instances | [Quickstart](/docs/getting-started/quickstart) |
| **Execution Backends** | Local process, Docker, Kubernetes — agent isolation at your chosen level | [Deployment](/docs/user-guide/deployment) |
| **STAMP Model** | Every run defined by Soul/Tools/Agent/Model/Policy — auditable and reproducible | [Architecture](/docs/developer-guide/architecture) |
| **Policies** | Workspace scoping, command approval gates, per-user rules | [Security](/docs/user-guide/security) |
| **Messaging Platforms** | Telegram, Discord, Slack, WhatsApp, Signal, Email, Home Assistant | [Messaging](/docs/user-guide/messaging) |
| **Tools** | 47 built-in tools (file I/O, search, shell, browser, image gen, TTS, etc.) | [Tools](/docs/user-guide/features/tools) |
| **Skills** | Installable plugin packages that add new capabilities | [Skills](/docs/user-guide/features/skills) |
| **Memory** | Persistent, FTS5-searchable memory across sessions | [Memory](/docs/user-guide/features/memory) |
| **MCP** | Connect to external tool servers via Model Context Protocol | [MCP](/docs/user-guide/features/mcp) |
| **Cron** | Schedule recurring agent tasks (runs through the gateway) | [Cron](/docs/user-guide/features/cron) |
| **Workflows** | DAG-based task orchestration with conditional branches and approval gates | [Workflows](/docs/user-guide/features/workflows) |
| **Delegation** | Spawn sub-agents for parallel work | [Delegation](/docs/user-guide/features/delegation) |
| **Code Execution** | Run code in sandboxed environments | [Code Execution](/docs/user-guide/features/code-execution) |
| **Browser** | Web browsing and scraping | [Browser](/docs/user-guide/features/browser) |
| **Hooks** | Event-driven callbacks and middleware | [Hooks](/docs/user-guide/features/hooks) |
| **Batch Processing** | Process multiple inputs in bulk | [Batch Processing](/docs/user-guide/features/batch-processing) |
| **RL Training** | Fine-tune models with reinforcement learning | [RL Training](/docs/user-guide/features/rl-training) |
| **Provider Routing** | Route requests across multiple LLM providers | [Provider Routing](/docs/user-guide/features/provider-routing) |
| **IDE Integration** | ACP adapter for VS Code, Zed, JetBrains | [ACP](/docs/user-guide/features/acp) |
| **CLI (Local Mode)** | Direct terminal chat without the gateway — quick local use | [CLI](/docs/user-guide/cli) |

## What to Read Next

Based on where you are right now:

- **Just finished installing?** → Head to the [Quickstart](/docs/getting-started/quickstart) to launch the gateway.
- **Gateway running?** → Read [Configuration](/docs/user-guide/configuration) to customize your setup, then [Messaging](/docs/user-guide/messaging) to connect platforms.
- **Comfortable with the basics?** → Explore [Tools](/docs/user-guide/features/tools), [Skills](/docs/user-guide/features/skills), and [Memory](/docs/user-guide/features/memory).
- **Deploying for real?** → Read [Deployment](/docs/user-guide/deployment) and [Security](/docs/user-guide/security) for execution backends and access control.
- **Setting up for a team?** → Read [Security](/docs/user-guide/security) and [Sessions](/docs/user-guide/sessions) for per-user policies and conversation management.
- **Ready to build?** → Jump into the [Developer Guide](/docs/developer-guide/architecture) to understand the internals.
- **Want practical examples?** → Check out the [Guides](/docs/guides/tips) section.

:::tip
You don't need to read everything. Pick the path that matches your goal, follow the links in order, and you'll be productive quickly.
:::
