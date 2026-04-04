# Agent Runtime Protocol

Logos supports pluggable agent runtimes. The default is **Hermes** (the built-in agent with full tool loop, context compression, memory, and reasoning config). Alternative runtimes can be added by implementing the `AgentAdapter` interface.

## Architecture

```
User message → Gateway (_run_agent) → Runtime dispatch
                                        ├── hermes (default) → AIAgent.run_conversation()
                                        └── claude-direct    → ClaudeDirectAdapter.run()
```

### Core interfaces (`logos/agent/interface.py`)

- **`AgentAdapter`** — abstract base class with `run(context) -> AgentResult`
- **`AgentContext`** — dataclass with user_message, history, callbacks
- **`AgentResult`** — dataclass with final_response, messages, api_calls, completed

### Adapters

| Runtime | Location | Description |
|---------|----------|-------------|
| `hermes` | `logos/adapters/hermes/adapter.py` | Full Hermes agent with tool loop, compression, memory |
| `claude-direct` | `logos/adapters/claude_direct/adapter.py` | Anthropic SDK with native tool use, no iteration loop |

## Configuration

### Global default

Set in `~/.logos/config.yaml`:

```yaml
agent_runtime: hermes   # "hermes" | "claude-direct"
```

### Per-session switching

Use the `/runtime` chat command:

```
/runtime                    # Show current runtime
/runtime hermes             # Switch to Hermes
/runtime claude-direct      # Switch to Claude Direct
```

Per-session overrides reset on gateway restart.

### Requirements

- **hermes**: Works with any OpenAI-compatible endpoint (local or cloud)
- **claude-direct**: Requires `ANTHROPIC_API_KEY` environment variable

## Adding a new runtime

1. Create `logos/adapters/<name>/adapter.py` implementing `AgentAdapter`
2. Add the runtime ID to `_resolve_runtime()` in `gateway/run.py`
3. Add the dispatch branch in `run_sync()` (around line 4600)
4. Add to the `/runtime` command's available list

### Minimal adapter example

```python
from logos.agent.interface import AgentAdapter, AgentCapabilities, AgentContext, AgentResult

class MyAdapter(AgentAdapter):
    AGENT_ID = "my-agent"

    @property
    def agent_id(self) -> str:
        return self.AGENT_ID

    def capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(agent_id=self.AGENT_ID)

    def run(self, context: AgentContext) -> AgentResult:
        # Your agent logic here
        # Use context.tool_progress_callback / context.tool_complete_callback
        # for live tool cards in the UI
        return AgentResult(
            final_response="Hello from my agent",
            messages=[],
            api_calls=1,
            completed=True,
        )
```

### Tool execution

All runtimes share the same tool implementations via `core.model_tools.handle_function_call()`. Tool definitions are loaded from `core.model_tools.get_tool_definitions()` in OpenAI format. Non-OpenAI runtimes (like Claude Direct) convert these to their native format.

## Connection Endpoints

There are three ways an agent runtime connects to Logos:

### 1. In-process adapter (recommended for built-in runtimes)

The adapter runs inside the gateway process. This is how `hermes` and `claude-direct` work.

```
Gateway process
  └── _run_agent()
        └── ClaudeDirectAdapter.run()  ← same process, direct call
```

- **Interface**: `AgentAdapter.run(context) -> AgentResult`
- **Pros**: No network overhead, direct access to tool registry, simplest to implement
- **Cons**: Must be Python, runs in gateway's thread pool

### 2. WebSocket worker (recommended for external/remote agents)

An external agent process connects to the gateway via WebSocket and receives tasks. Any language, any framework — just speak the JSON protocol.

```
External agent process ──WebSocket──→ Gateway (:8080/ws/worker)
```

**Endpoint**: `ws://<gateway>:8080/ws/worker`

**Connection flow**:

```
1. Connect:   WebSocket to /ws/worker
2. Register:  → {"type": "register", "worker_id": "my-agent-1", "soul": "general",
                  "toolsets": ["hermes-cli"], "instance_label": "My Agent"}
3. Confirmed: ← {"type": "registered"}
4. Heartbeat: → {"type": "heartbeat", "worker_id": "my-agent-1", "status": "idle"}
               (every 30s, timeout at 90s)
```

**Receiving tasks**:

```json
← {
    "type": "run_conversation",
    "task_id": "uuid",
    "session_id": "session-key",
    "message": "user's message",
    "history": [{"role": "user", "content": "..."}, ...],
    "context_prompt": "system prompt",
    "model": "qwen/qwen3.5-9b",
    "model_kwargs": {"api_key": "...", "base_url": "http://..."},
    "toolsets": ["hermes-cli", "hermes-web"],
    "max_iterations": 90
  }
```

**Streaming progress** (optional, during execution):

```json
→ {"type": "tool_progress", "task_id": "uuid", "tool": "web_search", "preview": "London weather"}
→ {"type": "tool_start", "task_id": "uuid", "call_id": 1, "tool": "web_search", "preview": "London weather"}
→ {"type": "tool_end", "task_id": "uuid", "call_id": 1, "tool": "web_search", "success": true, "duration_ms": 1234}
→ {"type": "token", "task_id": "uuid", "content": "The weather"}
→ {"type": "thinking", "task_id": "uuid", "content": "Let me search..."}
```

**Returning results**:

```json
→ {
    "type": "task_result",
    "task_id": "uuid",
    "status": "done",
    "final_response": "The weather in London is...",
    "api_calls": 3,
    "tools_used": ["web_search", "web_extract"],
    "messages": [...]
  }
```

**Error response**:

```json
→ {"type": "task_result", "task_id": "uuid", "status": "error", "error": "Model failed to load"}
```

**Interrupt handling**:

```json
← {"type": "interrupt", "task_id": "uuid", "new_message": "stop, do this instead"}
```

- **Pros**: Language-agnostic, can run on different machines, natural for distributed setups
- **Cons**: Must handle WebSocket lifecycle, reconnection, heartbeats

### 3. Spawned instance (managed by executor)

Logos spawns the agent as a subprocess or container. The spawned process connects back to the gateway via WebSocket (method 2). This is how multi-instance deployments work.

```
Gateway → Executor.spawn(config) → new process/container
                                      └── connects back via /ws/worker
```

**Executors available**:

| Executor | How it spawns | Config |
|----------|--------------|--------|
| `LocalProcessExecutor` | `python -m gateway.run` subprocess | Port pool 8081-8199 |
| `KubernetesExecutor` | k8s Deployment + Service + PVC | `ghcr.io/gregsgreycode/logos:latest` image |
| `DockerSandboxExecutor` | Docker container | Dockerfile.docker-sandbox |
| `OpenShellExecutor` | Policy-enforced sandbox | Dockerfile.openshell-sandbox |

**Spawn config** (`gateway/executors/base.py`):

```python
@dataclass
class InstanceConfig:
    name: str                          # unique instance name
    soul_name: str = "default"         # personality/toolset config
    model: str = ""                    # model override
    requester: str = ""                # who requested this instance
    instance_label: str = ""           # display label
    port: int = 0                      # assigned port (local executor)
    toolsets: List[str] = []           # enabled toolsets
    policy: str = ""                   # action policy level
    tool_overrides: dict = {}          # per-tool enable/disable
    machine_endpoint: Optional[str] = None  # target inference machine
    machine_name: Optional[str] = None
    machine_id: Optional[str] = None
```

- **Pros**: Fully managed lifecycle, auto-cleanup, resource isolation
- **Cons**: Currently always launches Hermes (extending to other runtimes requires passing `AGENT_RUNTIME` env var)

## Design decisions

- **Hermes path is untouched** — runtime dispatch is an if-branch, not a refactor
- **Internal operations stay Hermes** — memory flush, compression, background tasks always use AIAgent
- **Souls are runtime-agnostic** — personality and toolset constraints apply to all runtimes
- **Context upscale retry is Hermes-only** — other runtimes handle context limits differently
