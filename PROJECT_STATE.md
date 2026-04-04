# Logos ◆ Project State

> Last updated: 2026-04-04. This document captures the current state of the project
> and its direction. It is intended to be corrected by the maintainer and then distilled
> into persistent memory for AI assistants working on the codebase.

---

## What Logos Is

Logos is a **self-hosted, multi-user platform for agentic AI**. It is not a single agent
but a control plane where agent runtimes plug in, users connect from multiple interfaces,
and every interaction is recorded as a reproducible STAMP (Soul, Tools, Agent, Model, Policy).

It runs on anything from a $5 VPS to a homelab k8s cluster. Users choose their own privacy
model: local inference (LM Studio, Ollama), self-hosted endpoints, or cloud providers
(Anthropic, OpenAI, OpenRouter).

**Multi-user by design.** Multiple people share one deployment with different agents,
personalities, permission levels, and model configurations. The gateway handles concurrent
sessions across web, Telegram, Discord, Slack, WhatsApp, email, and Home Assistant.

---

## Architecture Overview

```
  Telegram / Web / Discord / Slack / WhatsApp / Email / HomeAssistant / ACP (IDE)
                              |
                         Gateway (aiohttp, port 8080)
                          ├── Auth & RBAC (SQLite, JWT)
                          ├── MCP Gateway (centralized MCP server management)
                          ├── Session Store (SQLite, FTS5 search)
                          ├── Platform Adapters (one per messaging channel)
                          └── Agent Runtime Dispatch
                                ├── Hermes (primary, OpenAI-compatible API)
                                └── Claude Direct (native Anthropic SDK)
                                      |
                                 Tool Registry (50+ tools)
                                      |
                                 Model Router
                               ┌──────┼──────┐
                            Local   Cloud   OpenRouter
                          (LM Studio, (Anthropic, (200+ models)
                           Ollama,    OpenAI)
                           llama.cpp)
```

**Key boundaries:**
- `gateway/` — always-on process: HTTP server, auth, routing, web dashboard, platform adapters
- `agents/` — runtime implementations (Hermes is primary)
- `agent/` — shared agent internals (prompt building, context compression, model metadata)
- `logos/` — platform-agnostic runtime interfaces and adapters
- `tools/` — capabilities agents can call, scoped per session and policy
- `core/` — state management, metrics, batch runner, toolset distributions
- `souls/` — personality definitions (markdown files, hot-swappable)

---

## The STAMP Model

Every run records five dimensions:

| Dimension | What it captures |
|-----------|-----------------|
| **Soul** | Personality, voice, behavioral constraints |
| **Tools** | Which capabilities were available and used |
| **Agent** | Which runtime executed the reasoning (hermes, claude-direct) |
| **Model** | Which LLM did the inference |
| **Policy** | Authorization level, workspace scoping, approval gates |

This makes every agent interaction observable, reproducible, auditable, and comparable.
The agent comparison platform (A/B testing different STAMPs) is a core differentiator.

---

## Current Version & Status

**Version:** 0.9.0 (early alpha)
**Python:** 3.11+
**License:** MIT

Working and production-usable:
- Gateway, auth, RBAC, dashboard
- Setup wizard with model auto-discovery and benchmarking
- Hermes agent runtime with full tool loop
- Claude Direct adapter (native Anthropic, simpler)
- 50+ tools (web, terminal, browser, code, vision, MCP, delegation, memory)
- Context compression (auto-summarization when approaching limits)
- 11 souls (personality templates)
- Multi-platform messaging (Telegram, Discord, Slack, WhatsApp, Email, HA)
- Docker and k3s deployment
- Eval framework with assertion-based testing

Rough / in-progress:
- Agent comparison UI (split-view A/B testing)
- Workflow engine (JSON DAG execution)
- Skills Hub (community skill sharing)
- Honcho integration (cross-session user modeling)
- RL training integration (Tinker-Atropos)

---

## Deployment Options

| Mode | Agent Isolation | Use Case |
|------|----------------|----------|
| Local process | OS process | Desktop dev/testing |
| Docker Compose | Container + child processes | Simple self-hosted |
| Docker Compose + k3s | Kubernetes pod per agent | Self-hosted with isolation |
| External Kubernetes | Pod + RBAC + NetworkPolicy | Production cluster |

The k3s option was built in v0.6.12 and embeds an entire k8s cluster inside
Docker Compose for pod-level agent isolation without needing external infra.

---

## Agent Runtimes

### Hermes (primary)
- Full agentic loop: conversation → tool calls → execution → response
- Works with any OpenAI-compatible endpoint
- Supports parallel tool execution (ThreadPoolExecutor, max 8 workers)
- Context compression via secondary model (Gemini Flash default)
- Iteration budget shared across parent + child agents
- Memory, delegation, session search built in

### Claude Direct
- Native Anthropic SDK (no OpenAI wrapper)
- Simpler tool loop, no context compression
- Good for direct Claude API usage

### Runtime Protocol
- In-process adapters (Hermes, Claude Direct run inside gateway)
- WebSocket workers (external agents connect back to gateway)
- Spawned instances (LocalProcess, Docker, Kubernetes executors)

---

## Tool Ecosystem (50+)

**Core:** terminal, file operations, code execution, web search, web extract
**Browser:** navigate, click, type, scroll, snapshot, vision (Browserbase)
**AI:** delegate_task, mixture_of_agents, handoff, vision_analyze, image_generate, TTS
**Memory:** persistent cross-session memory with FTS, knowledge store
**Integration:** MCP servers, Home Assistant, cron jobs, workflows
**Meta:** request_tools (lazy loading), clarify (user interaction), approval gates

Tools are registered in a central registry. Each tool belongs to a toolset. Toolsets
are enabled/disabled per session and per policy level.

---

## Model Infrastructure

### Local Inference
- **LM Studio** — primary local target, uses native `/api/v1/` for model management
- **Ollama** — supported with context probing
- **llama.cpp** — direct server support

### Context Window Management
LM Studio defaults to `n_parallel=4` (Max Concurrent Predictions), splitting loaded
context across 4 parallel slots for concurrent users. This cannot be changed via API.

**Current thresholds (as of 2026-04-04):**
- 128K+ total context → 32K/slot → recommended (4 concurrent users, full conversations)
- 64K–128K total → 16K–32K/slot → viable but limited
- <64K total → <16K/slot → insufficient (system prompt + tools alone need ~17K)

The setup benchmark gates models at 64K minimum and recommends 128K+. Models below
these thresholds get scoring penalties. The gateway detects slot splitting at startup
and logs warnings/errors with guidance.

### Model Benchmarking (Setup Wizard)
- Auto-discovers models on LM Studio/Ollama servers via LAN scan
- Benchmarks: 6 eval tests (instruction, reasoning, arithmetic, tool selection, multi-step, JSON format)
- Hard evals (3 tests): complex tool routing, nested JSON, multi-step arithmetic
- Agent evals (3 tests): tool call format, persona adherence, constrained generation
- Composite scoring: 50% eval + 20% speed + 15% TTFT + 5% advanced + size/capability bonuses
- Persists VRAM-validated context lengths for runtime use

---

## UI / Dashboard

**Three HTML pages:**
- `login.html` — auth page with animated visual effects
- `setup.html` — multi-step onboarding wizard (7 steps)
- `main_app.html` — unified dashboard (419 KB, Alpine.js)

**Dashboard tabs:**
- **World** — agent instance visualization (Pixi.js canvas graph)
- **Chats** — chat interface with sidebar history, SSE streaming, file/voice attachments
- **Agents** — instance lifecycle, memory editor, knowledge base, soul selector
- **Infra** (permissioned) — servers, routing, tools/MCP management, proposals
- **Admin** (permissioned) — users, audit logs, workflows, runs, policies, approvals

**4 themes:** Midnight (indigo), Crimson (red), Terminal (green), Dusk (purple)

---

## Data & Persistence

Everything lives in `~/.logos/` (Docker: `/home/logos/.logos/`):

| Path | Purpose |
|------|---------|
| `config.yaml` | Runtime configuration (model, provider, settings) |
| `auth.db` | SQLite (users, sessions, audit logs) |
| `state.db` | SQLite v8 (sessions, runs, messages, workspaces, evals) with FTS5 |
| `sessions/` | Conversation history and state |
| `memories/` | Persistent agent memory files |
| `skills/` | Installed skill modules |
| `cron/` | Scheduled job definitions |
| `logs/` | Session trajectories and execution logs |
| `context_length_cache.yaml` | Model context lengths discovered via probing |

---

## Development Direction

Based on recent commits and architecture:

1. **Agent comparison platform** — A/B testing different STAMP combinations side-by-side.
   This is the core differentiator: run the same prompt through different soul/model/tool
   combos and compare results in a split-view UI.

2. **Runtime protocol maturation** — The adapter interface (`AgentAdapter`) is new.
   Claude Direct is the second runtime. The WebSocket worker protocol enables external
   agent processes. More runtimes will plug in here.

3. **Benchmark improvements** — Recent work: native LM Studio benchmarking (2-3 API calls
   vs ~12), thinking model support, agent eval tier, live streaming progress UI.

4. **Context window intelligence** — Auto-scaling context, n_parallel awareness, VRAM-validated
   probing. Making local model inference "just work" despite GPU memory constraints.

5. **Live execution UI** — Tool call cards that show in-progress work, persist after
   completion. Making agent work visible and inspectable.

6. **Multi-platform expansion** — Telegram, Discord, Slack, WhatsApp, Email, Home Assistant
   all work. The platform adapters share a common base class.

---

## Key Files

| File | Lines | What it does |
|------|-------|-------------|
| `agents/hermes/agent.py` | 6,357 | Core agentic loop (AIAgent class) |
| `gateway/run.py` | 5,338 | Gateway runner, model loading, message routing |
| `gateway/http_api.py` | 2,647 | REST/SSE endpoints, chat handler |
| `gateway/setup_handlers.py` | ~2,700 | Setup wizard, benchmarking |
| `gateway/html/main_app.html` | ~4,000 | Dashboard UI (Alpine.js) |
| `gateway/html/setup.html` | ~2,500 | Onboarding wizard |
| `core/state.py` | ~1,200 | SQLite state store |
| `agent/model_metadata.py` | ~340 | Model context resolution |
| `agent/context_compressor.py` | ~400 | Auto-summarization |
| `logos/agent/interface.py` | ~100 | Agent adapter protocol |
| `tools/registry.py` | ~200 | Tool registration system |
| `SOUL.md` | 20 | Default agent personality |

---

## What Makes Logos Different

1. **STAMP model** — every run is observable, reproducible, auditable by design
2. **Self-hosted, multi-user** — one deployment serves a household or team
3. **Runtime-agnostic** — agent runtimes plug in; the platform layer doesn't care which one
4. **Local-first inference** — GPU-aware model management with auto-discovery and benchmarking
5. **Agent comparison** — A/B test different STAMP combinations (the differentiator)
6. **Soul system** — agents have personality and voice, not just system prompts
7. **Policy enforcement** — per-user, per-session authorization with workspace isolation
8. **50+ tools** — from terminal access to browser automation to MCP integration
