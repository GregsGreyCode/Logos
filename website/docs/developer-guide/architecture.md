---
sidebar_position: 1
title: "Architecture"
description: "Logos internals — major subsystems, execution paths, and where to read next"
---

# Architecture

This page is the top-level map of Logos internals. The project is composed of the Logos gateway (the platform control plane) and the Hermes agent runtime, plus supporting subsystems.

## High-level structure

```text
logos/
├── agents/hermes/agent.py    # AIAgent core loop (Hermes runtime)
├── core/                     # shared domain: state, metrics, toolsets, model_tools, batch_runner
├── logos_cli/                # platform CLI: gateway, setup, config, doctor, auth, models
├── tools/                    # 47 tool implementations and terminal environments
├── gateway/                  # always-on HTTP server, web dashboard, executors, messaging adapters
│   ├── html/                 # web dashboard (login, main app, setup wizard)
│   ├── executors/            # agent isolation backends (kubernetes, docker, openshell, local)
│   ├── platforms/            # messaging adapters (telegram, discord, slack, whatsapp, signal, email)
│   └── auth/                 # JWT auth, RBAC, audit logging
├── agent/                    # prompt building, compression, caching, metadata, trajectories
├── cron/                     # scheduled job storage and scheduler
├── honcho_integration/       # Honcho memory integration
├── acp_adapter/              # ACP editor integration server
├── workflows/                # DAG-based workflow engine
├── launcher/                 # Windows desktop launcher (system tray)
├── environments/             # RL / benchmark environment framework
├── skills/                   # bundled skills
├── optional-skills/          # official optional skills
├── k8s/                      # Kubernetes deployment manifests
└── tests/                    # test suite
```

## Recommended reading order

If you are new to the codebase, read in this order:

1. this page
2. [Agent Loop Internals](./agent-loop.md)
3. [Prompt Assembly](./prompt-assembly.md)
4. [Provider Runtime Resolution](./provider-runtime.md)
5. [Tools Runtime](./tools-runtime.md)
6. [Session Storage](./session-storage.md)
7. [Gateway Internals](./gateway-internals.md)
8. [Context Compression & Prompt Caching](./context-compression-and-caching.md)
9. [ACP Internals](./acp-internals.md)
10. [Environments, Benchmarks & Data Generation](./environments.md)

## Major subsystems

### Agent loop

The core synchronous orchestration engine is `AIAgent` in `agents/hermes/agent.py`.

It is responsible for:

- provider/API-mode selection
- prompt construction
- tool execution
- retries and fallback
- callbacks
- compression and persistence

See [Agent Loop Internals](./agent-loop.md).

### Prompt system

Prompt-building logic is split between:

- `agents/hermes/agent.py`
- `agent/prompt_builder.py`
- `agent/prompt_caching.py`
- `agent/context_compressor.py`

See:

- [Prompt Assembly](./prompt-assembly.md)
- [Context Compression & Prompt Caching](./context-compression-and-caching.md)

### Provider/runtime resolution

Logos has a shared runtime provider resolver used by CLI, gateway, cron, ACP, and auxiliary calls.

See [Provider Runtime Resolution](./provider-runtime.md).

### Tooling runtime

The tool registry, toolsets, terminal backends, process manager, and dispatch rules form a subsystem of their own.

See [Tools Runtime](./tools-runtime.md).

### Session persistence

Historical session state is stored primarily in SQLite, with lineage preserved across compression splits.

See [Session Storage](./session-storage.md).

### Messaging gateway

The gateway is a long-running orchestration layer for platform adapters, session routing, pairing, delivery, and cron ticking.

See [Gateway Internals](./gateway-internals.md).

### ACP integration

ACP exposes Hermes as an editor-native agent over stdio/JSON-RPC.

See:

- [ACP Editor Integration](../user-guide/features/acp.md)
- [ACP Internals](./acp-internals.md)

### Cron

Cron jobs are implemented as first-class agent tasks, not just shell tasks.

See [Cron Internals](./cron-internals.md).

### RL / environments / trajectories

Hermes ships a full environment framework for evaluation, RL integration, and SFT data generation.

See:

- [Environments, Benchmarks & Data Generation](./environments.md)
- [Trajectories & Training Format](./trajectory-format.md)

## Design themes

Several cross-cutting design themes appear throughout the codebase:

- prompt stability matters
- tool execution must be observable and interruptible
- session persistence must survive long-running use
- platform frontends should share one agent core
- optional subsystems should remain loosely coupled where possible

## Implementation notes

The older mental model of “one OpenAI-compatible chat loop plus some tools” is no longer sufficient. Logos includes:

- multiple API modes
- auxiliary model routing
- ACP editor integration
- gateway-specific session and delivery semantics
- RL environment infrastructure
- prompt-caching and compression logic with lineage-aware persistence

Use this page as the map, then dive into subsystem-specific docs for the real implementation details.
