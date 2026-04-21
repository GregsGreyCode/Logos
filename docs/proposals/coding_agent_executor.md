# Coding-agent executor — spike

**Status:** proposal
**Date:** 2026-04-21

## Problem

Logos today spawns every agent into one sandbox image (`hermes-sandbox:m12`) that contains the **hermes-agent** runtime — a full conversational LLM loop with tools. That works for "talk to a model and get a reply" but locks us into one agent shape.

OpenShell-community now ships pre-built sandboxes for different coding CLIs: `gemini` (Gemini CLI), `ollama`, `openclaw`, and `base` (which carries claude, codex, opencode, copilot). These are single-purpose images — each one contains **a coding agent CLI, not hermes-agent**.

An earlier sketch was to "layer hermes on top of gemini". That's incoherent: two agents in one sandbox would both expect to own the user's prompt. The coding-agent CLIs are alternatives to hermes-agent, not complements.

## What we actually want

A second executor kind alongside the existing `OpenShellExecutor`, one that spawns a community sandbox and forwards the user's prompt **directly to the coding CLI inside** — no hermes-agent in the loop. The gateway routes based on the agent's declared kind:

```
Agent record:
  agent_type: "hermes"       → OpenShellExecutor → hermes-sandbox → hermes gateway (HTTP, v2)
  agent_type: "codex_cli"    → CodingAgentExecutor → base sandbox → codex CLI
  agent_type: "claude_cli"   → CodingAgentExecutor → base sandbox → claude CLI
  agent_type: "gemini_cli"   → CodingAgentExecutor → gemini sandbox → gemini CLI
  agent_type: "ollama"       → CodingAgentExecutor → ollama sandbox → ollama run
```

## Scope of this spike

Prove the pattern with **one** coding agent before generalising. Target: **Codex CLI** (`@openai/codex`), because:

- The user already has a GPT / OpenAI account, so credentialling is just an `OPENAI_API_KEY` (or `codex login --device-auth`) — no new subscription needed.
- Codex ships inside the `openshell-community/sandboxes/base` image already (see its README — "Coding agents: claude, opencode, codex, copilot"), so no custom Dockerfile is required to get started.
- OpenShell's codex provider (`crates/openshell-providers/src/providers/codex.rs`) declares `OPENAI_API_KEY` as the credential env var, so the credential-injection plumbing is already there.

Out of scope: Gemini (no subscription), Ollama's LLM-server sidecar, OpenClaw, and the other three CLIs inside `base` (claude / opencode / copilot) — once Codex works, each is a small additional wiring change, not a new architecture.

## Executor contract (first cut)

```python
class CodingAgentExecutor:
    """Spawns a coding-CLI sandbox and pipes prompts to it.

    Unlike OpenShellExecutor + hermes-server mode (v2), there is no
    persistent hermes gateway HTTP server inside this sandbox and no
    /v1/runs dispatch channel. The CLI inside the sandbox is the agent.
    We manage:
      - sandbox lifecycle (spawn, health, teardown)
      - prompt-in / stream-out over `openshell sandbox exec` (stdin/stdout)
      - credential injection at spawn time via OpenShell providers
    """

    sandbox_image: str  # "ghcr.io/nvidia/openshell-community/sandboxes/base:latest"
    cli_entrypoint: list[str]  # ["codex", "--quiet"] — binary + canonical flags
    provider_name: str  # openshell provider that injects OPENAI_API_KEY

    async def spawn(self, agent_id: str) -> SandboxRef: ...
    async def send_prompt(self, ref: SandboxRef, prompt: str) -> AsyncIterator[Chunk]: ...
    async def teardown(self, ref: SandboxRef): ...
```

Compare with `OpenShellExecutor` + hermes-server mode (v2, the current default — `gateway/executors/hermes_server_mode.py`). In v2 each sandbox runs `hermes gateway run` as a long-lived HTTP server bound to `127.0.0.1:8642` inside the sandbox's inner netns. Each chat dispatch is an `openshell sandbox exec curl … /v1/runs` call that streams SSE back to the gateway, which demuxes into `token` / `thinking` / `tool_use` frames for the frontend.

`CodingAgentExecutor` has none of that: no in-sandbox HTTP server, no SSE demux, no tool-call plumbing. One `openshell sandbox exec -- codex "<prompt>"` per message, stdout lines become chat tokens. Simpler but thinner.

(Historical note: there was also a v1 "Plan A-prime" path where the gateway exec-invoked `sandbox_worker.py` once per dispatch. That's opt-out only now via `LOGOS_HERMES_SERVER_MODE=0`; fresh installs default to v2.)

## What breaks without hermes-agent

The following Logos features assume hermes-agent in the sandbox. They either need per-executor adapters or have to be marked unavailable for coding-CLI agents:

| Feature | hermes-agent today | coding-CLI agent |
|---|---|---|
| Tool calls (web_search, browser, etc.) | hermes-agent invokes; gateway observes | CLI has its own built-in tools; we don't see them |
| Session memory (`memory/*.md`) | hermes-agent reads/writes on startup | CLI has its own memory; different format |
| Souls (persona manifests) | injected into hermes-agent system prompt | CLI usually has `--system-prompt` or config file equivalent |
| Sub-agent delegation | hermes-agent spawns children | not supported by most CLIs |
| Reasoning (`thinking` SSE events) | hermes-agent emits | depends on model/CLI |
| Stats (tokens, elapsed, model) | hermes-agent reports | CLI prints to stderr if at all |
| MCP servers | hermes-agent mounts | Gemini CLI has its own MCP support; different config |

The honest answer is: a coding-CLI agent in Logos is a **thinner** experience than a hermes-agent. The Chat UI still works (prompt in, stream out), but Mind/Logs/STAMP badges show less.

## Data model change

One new column on `agents`:

```sql
ALTER TABLE agents ADD COLUMN agent_type TEXT NOT NULL DEFAULT 'hermes';
-- values: 'hermes' | 'gemini_cli' | 'claude_cli' | 'codex_cli' | 'opencode_cli' | 'ollama'
```

The existing `sandbox_image` field stays (hermes agents can still point at different hermes-sandbox tags); it becomes the source of truth for "which image the sandbox spawns from".

Routing in `http_api.py`. The hermes path hits `OpenShellExecutor` which (with v2 enabled by default) dispatches via HTTP to the in-sandbox hermes gateway; the coding-agent path skips that entirely and does a one-shot `sandbox exec`:

```python
if agent.agent_type == "hermes":
    # v2: hermes gateway HTTP server inside sandbox (hermes_server_mode.py)
    executor = OpenShellExecutor(sandbox_image=agent.sandbox_image)
else:
    # one-shot CLI invocation inside a community sandbox
    executor = CodingAgentExecutor.for_kind(agent.agent_type, sandbox_image=agent.sandbox_image)
```

## UI surface

- **Create-agent form:** add a "kind" radio (Hermes / Gemini / …) that defaults to Hermes. Picking a non-Hermes kind locks out Souls/Toolsets (they're hermes-specific) and unlocks a credential prompt (e.g. Google OAuth for Gemini).
- **STAMP row:** render only what applies. A Gemini agent shouldn't show a T pill with "5 tools ready" since it doesn't use hermes toolsets — show kind-specific badges instead (e.g. "G" for Gemini model family).
- **Mind tab:** same, but fall back to "memory not supported by this agent kind" if the CLI has no exposable memory.

## Open questions for the spike

1. **Credential injection mechanism.** OpenShell's provider system handles API-key bundling (`openshell provider set google`). Can we drive that from the gateway at agent-create time, or does the user have to CLI it manually first? Needs an experiment.
2. **Streaming shape.** Does the gemini CLI emit chunked JSON, line-buffered text, or raw tokens? This determines how we parse its output into `token` / `thinking` / `done` SSE events for the frontend.
3. **Interactivity.** Some coding CLIs are interactive (prompt user for confirmation before running a command). For Logos's one-shot chat semantics we probably want `--yolo` / `--non-interactive` equivalents; need to check each CLI.
4. **Session continuity.** Today a hermes agent persists a session across messages. A Gemini-CLI invocation is one-shot by default. Do we run the CLI once per message (stateless) or keep it alive as a long-lived process (stateful)? Stateful is closer to the chat UX but more fragile.
5. **Teardown.** Does OpenShell's existing sandbox GC handle community images the same way? Mostly yes — the sandbox is a sandbox to OpenShell regardless of what's inside — but worth confirming.

## Recommended next steps

1. **Manual proof.** Pull the base sandbox image (`ghcr.io/nvidia/openshell-community/sandboxes/base:latest`), `openshell sandbox create --from base`, exec in, run `codex "hello"` and confirm it streams a reply. No Logos code.
2. **Credential spike.** Wire `OPENAI_API_KEY` into the sandbox via the `codex` OpenShell provider (`openshell-providers/src/providers/codex.rs`). Alternative: `codex login --device-auth` flow documented in the base README for browserless envs. Verify `codex` inside the sandbox authenticates without further setup.
3. **Executor skeleton.** Build the minimal `CodingAgentExecutor.spawn()` + `send_prompt()` flow in a new file `gateway/executors/coding_agent.py`. No frontend, no DB changes — drive it from a pytest that sends a prompt and prints the reply.
4. **Wire one agent record by hand.** `UPDATE agents SET agent_type='codex_cli' WHERE id=…`, and branch in the dispatch path. Confirm Chats-tab chat works end to end.
5. **Ship the UI.** Radio on create-agent, STAMP-row kind awareness, Mind/Logs fallbacks.

Each step is a commit-sized unit; no single step is "all or nothing".

## Non-goals

- A plug-in system for "any coding CLI". We prefer one-by-one adapters over a generic interface until at least two are live.
- Replacing hermes-agent. It's still the right runtime for most Logos agents. This is about widening what Logos can host.
- MCP-server mounting for coding CLIs. Out of scope until we decide whether Logos's MCP registry should be shared across agent kinds or per-kind.
