# Logos vs GoClaw

GoClaw is an enterprise-grade multi-agent AI gateway written in Go, positioned as a production-ready alternative to OpenClaw. This document compares it honestly with Logos.

---

## TL;DR

| | Logos | GoClaw |
|---|---|---|
| Language | Python + Alpine.js | Go |
| Distribution | Docker / K8s / pip | Single 25 MB binary |
| Database | SQLite | PostgreSQL 18 + pgvector |
| Local model-first | Yes — Ollama/LM Studio default | No — cloud API-first |
| Privacy / no telemetry | Yes | Unknown |
| STAMP auditability | Yes | No equivalent |
| Run replay / clone | Yes | No |
| Eval framework | Yes | No |
| LLM providers | OpenAI-compatible + Anthropic, OpenRouter, Nous | 20+ named providers |
| Messaging channels | Telegram, Discord, Slack, WhatsApp, Signal, email, Home Assistant | Telegram, Discord, Slack, Zalo OA, Zalo Personal, Feishu/Lark, WhatsApp |
| Scheduling (cron) | Yes | Yes |
| Multi-tenant | Partial (RBAC, per-user policy) | Yes (PostgreSQL workspaces) |
| Vector search | No | Yes (pgvector) |
| Web dashboard | Yes | Yes |
| ACP (VS Code / Zed) | Yes | No |
| Open-source license | MIT | MIT |
| Windows native | No (WSL2 required) | Yes (Go binary) |

---

## Where GoClaw is stronger

### Single static binary
GoClaw ships as a ~25 MB Go binary that starts in under 1 second and runs on a $5 VPS. No Python version to manage, no pip, no Docker required. For users who want to just run something, this is a significant advantage over Logos's current install path.

### More named LLM providers
GoClaw claims 20+ providers out of the box including Anthropic, OpenAI, Groq, DeepSeek, Gemini, OpenRouter, and others — each with native SSE and provider-specific feature support (e.g. prompt caching per-provider, extended thinking modes). Logos handles any OpenAI-compatible endpoint and has first-class support for Anthropic, OpenRouter, and Nous Portal, but the breadth of named integrations is narrower.

### PostgreSQL + pgvector
GoClaw uses PostgreSQL 18 with pgvector, giving it proper multi-tenant isolation at the database level and native semantic search over conversation history. Logos uses SQLite with FTS5 — simpler and perfectly adequate for homelab or single-tenant use, but not the right substrate for hundreds of concurrent users or vector-indexed memory at scale.

### True multi-tenancy
GoClaw's PostgreSQL backing gives per-user workspaces with proper data isolation by design. Logos has RBAC, per-user policies, and conversation history isolation, but it shares a single SQLite file — not the right architecture for a hosted SaaS serving many unrelated users.

### Broader enterprise messaging
GoClaw supports Zalo (Vietnamese super-app) and Feishu/Lark (ByteDance) channels that Logos does not. More relevant for Asia-Pacific enterprise deployments.

### Windows native
GoClaw ships a binary that runs on Windows without WSL2. Logos explicitly does not support native Windows today (noted in CRITIQUE.md as a gap — Go/Wails `.exe` is on the roadmap).

---

## Where Logos is stronger

### Local-first and privacy-native
Logos defaults to local models (Ollama, LM Studio) and treats cloud providers as explicit opt-ins. GoClaw's documentation is cloud-provider-first — local model support is present (OpenAI-compatible endpoints) but not the primary design centre. Logos ships with an onboarding wizard specifically for connecting local model servers at any address on your network.

No telemetry. No phone-home. Logos is explicit about this; GoClaw does not address it.

### STAMP auditability — run replay and clone
The STAMP model (Soul + Tools + Agent + Model + Policy) is Logos's core differentiator. Every agent run records the complete configuration snapshot that produced it: which soul, which tools were available, which model ran, which policy was in force, the tool call timeline, approval events, and token counts. You can:

- **replay** any past run exactly
- **clone** it into a new session to fork from a specific point
- **compare** two runs across different STAMP configurations

GoClaw has LLM call tracing and OpenTelemetry export. That is observability for infrastructure engineers. STAMP is observability for AI behaviour — a fundamentally different thing.

### Eval framework
Logos ships eval suites (`/evals run <suite>`) that run against the agent directly. GoClaw has no equivalent. This matters for anyone who wants to validate that a policy change doesn't break expected behaviour, or that a new soul file doesn't introduce regressions.

### Workflow engine
Logos has a JSON-defined DAG workflow engine with parallel steps, conditional branching, and human approval gates. GoClaw has scheduling and agent delegation but no structured workflow graph execution.

### ACP / editor integration
Logos supports the ACP protocol, meaning VS Code, Zed, and JetBrains can connect as first-class clients. GoClaw does not mention ACP.

### Signal and email channels
Logos supports Signal (via signal-cli) and email as delivery channels. GoClaw does not.

### Home Assistant
Logos has a Home Assistant adapter. GoClaw does not.

### Homelab-native deployment story
Logos is designed for Kubernetes homelab clusters (Talos, K3s, etc.) and ships with manifests, a canary deployment pattern, and Prometheus metrics. GoClaw's docs show Docker Compose as the primary deployment target.

### Soul system
The soul (`SOUL.md`) is a first-class concept in Logos — a hot-reloadable persona file that changes agent behaviour without a restart. Multiple souls can be defined and switched per-session. GoClaw has per-agent configuration but nothing equivalent to the named, hot-reloadable soul abstraction.

### Run history and FTS
Logos stores full conversation history server-side in SQLite with FTS5 full-text search across all past conversations. GoClaw's conversation storage is session-scoped (PostgreSQL rows), but cross-session FTS search across all history is not highlighted as a feature.

---

## Architectural differences

| Dimension | Logos | GoClaw |
|---|---|---|
| Runtime | Python (asyncio / aiohttp) | Go |
| Agent model | Pluggable adapters (Hermes default) | Fixed Go runtime |
| Policy enforcement | Python-layer workspace + approval gates | Five-layer permission system (Go) |
| Memory | FTS5 + LLM-summarised memories | pgvector semantic search |
| Scheduling | First-class cron with multi-channel delivery | `at` / `every` / cron with lane concurrency |
| Deployment | K8s / Docker / bare metal pip | Docker Compose / single binary |
| Configuration | YAML + web dashboard | API / web dashboard |

---

## Honest assessment

GoClaw is the better choice if you:
- Want a drop-in binary with zero Python/container overhead
- Are running a hosted multi-tenant service with many unrelated users
- Need the specific enterprise channels (Zalo, Feishu/Lark)
- Need pgvector semantic memory at scale
- Are on Windows without WSL2

Logos is the better choice if you:
- Run local models (Ollama, LM Studio) and privacy is non-negotiable
- Want full observability into AI behaviour, not just infrastructure metrics
- Run a Kubernetes homelab and want a deployment that fits the cluster
- Need the ACP editor integration
- Value run replay, STAMP auditability, and eval-driven development
- Want a platform you can read, modify, and understand completely

The projects are not really competing for the same user. GoClaw is optimised for enterprise multi-tenant deployment where the AI is a cloud-backed service. Logos is optimised for self-hosted, privacy-first, homelab or personal-team deployment where you control the model and every run is explainable.
