# Logos - Development Guide

Instructions for AI coding assistants and developers working on the Logos codebase.

## Development Environment

```bash
source .venv/bin/activate  # ALWAYS activate before running Python
```

## Project Structure

```
logos/                        ← repo root
├── hermes_state.py           # Shim → core/state.py (SessionDB, SQLite session store)
├── model_tools.py            # Shim → core/model_tools.py (tool orchestration, handle_function_call)
├── toolsets.py               # Shim → core/toolsets.py (toolset definitions, _HERMES_CORE_TOOLS)
├── hermes_constants.py       # Shim → core/constants.py (shared constants)
├── hermes_time.py            # Shim → core/clock.py (timezone-aware clock)
├── utils.py                  # Shared utilities (atomic_json_write, etc.)
├── runs.py                   # Run audit trail (RunRecorder, RunReplayer)
├── metrics.py                # Prometheus-compatible metrics engine
├── batch_runner.py           # Parallel batch processing
├── mini_swe_runner.py        # mini-swe-agent runner
├── rl_cli.py                 # RL training CLI runner
│
├── agent/                    # Agent internals
│   ├── prompt_builder.py     # System prompt assembly
│   ├── context_compressor.py # Auto context compression
│   ├── prompt_caching.py     # Anthropic prompt caching
│   ├── auxiliary_client.py   # Auxiliary LLM client (vision, summarization)
│   ├── model_metadata.py     # Model context lengths, token estimation
│   ├── display.py            # KawaiiSpinner, tool preview formatting
│   ├── skill_commands.py     # Skill slash commands (shared CLI/gateway)
│   ├── anthropic_adapter.py  # Anthropic streaming adapter
│   ├── redact.py             # PII redaction helpers
│   └── trajectory.py         # Trajectory saving helpers
│
├── hermes_cli/               # Platform CLI entry point
│   ├── main.py               # Entry point — all `hermes` subcommands
│   ├── config.py             # DEFAULT_CONFIG, OPTIONAL_ENV_VARS, migration
│   ├── commands.py           # Slash command definitions + SlashCommandCompleter
│   ├── callbacks.py          # Terminal callbacks (clarify, sudo, approval)
│   ├── setup.py              # Interactive setup wizard
│   ├── skin_engine.py        # Skin/theme engine — CLI visual customization
│   ├── skills_config.py      # `hermes skills` — enable/disable skills per platform
│   ├── tools_config.py       # `hermes tools` — enable/disable tools per platform
│   ├── skills_hub.py         # `/skills` slash command (search, browse, install)
│   ├── models.py             # Model catalog, provider model lists
│   └── auth.py               # Provider credential resolution
│
├── tools/                    # Tool implementations (one file per tool)
│   ├── registry.py           # Central tool registry (schemas, handlers, dispatch)
│   ├── approval.py           # Dangerous command detection
│   ├── terminal_tool.py      # Terminal orchestration
│   ├── process_registry.py   # Background process management
│   ├── file_tools.py         # File read/write/search/patch
│   ├── web_tools.py          # Firecrawl search/extract
│   ├── browser_tool.py       # Browserbase browser automation
│   ├── code_execution_tool.py # execute_code sandbox
│   ├── delegate_tool.py      # Subagent delegation
│   ├── mcp_tool.py           # MCP client
│   └── environments/         # Terminal backends (local, docker, ssh, modal, daytona, singularity)
│
├── gateway/                  # HTTP API, auth, web UI, messaging gateway
│   ├── run.py                # GatewayRunner — main loop, message dispatch
│   ├── session.py            # SessionStore — conversation persistence
│   ├── http_api.py           # Web dashboard API
│   ├── auth/                 # Auth + policy enforcement
│   └── platforms/            # Adapters: telegram, discord, slack, whatsapp, homeassistant, signal
│
├── logos/                    # Logos platform abstraction layer (WIP)
│   ├── agent/                # Agent interface ABC + runner
│   ├── adapters/hermes/      # Hermes adapter implementation
│   ├── blueprints/           # STAMP blueprint schema/loader/validator
│   ├── policy/               # Policy enforcement
│   ├── registry/             # Agent/tool catalog + installer
│   ├── souls/                # Soul loader
│   └── tools/                # Tool registry (logos-level)
│
├── core/                     # Canonical platform modules (import from here)
│   ├── state.py              # SessionDB — SQLite session store, thread-safe via write lock + WAL (canonical)
│   ├── constants.py          # Shared constants (canonical)
│   ├── clock.py              # Timezone-aware clock (canonical)
│   ├── model_tools.py        # Tool orchestration, handle_function_call (canonical)
│   ├── toolsets.py           # Toolset definitions, _HERMES_CORE_TOOLS (canonical)
│   ├── toolset_distributions.py  # Toolset sampling distributions (canonical)
│   ├── trajectory_compressor.py  # Trajectory compression (canonical)
│   └── utils.py              # Shared utilities (canonical)
│
├── agents/hermes/            # Production Hermes agent runtime
│   ├── agent.py              # AIAgent class — production entrypoint (agents.hermes.agent:main)
│   └── logos-agent.yaml      # Agent descriptor for Logos registry
│
├── acp_adapter/              # ACP protocol server (VS Code / Zed / JetBrains)
├── cron/                     # Cron scheduler (jobs.py, scheduler.py)
├── honcho_integration/       # Honcho AI memory integration
├── evals/                    # Eval framework and suites
├── workflows/                # DAG workflow engine
├── environments/             # RL training environments (Atropos)
├── k8s/                      # Kubernetes manifests (numbered apply order)
├── tests/                    # Pytest suite
├── skills/                   # Bundled agent skills
├── optional-skills/          # Optional skills (not activated by default)
├── souls/                    # Named agent personas
├── docs/                     # Documentation
├── website/                  # Docusaurus docs site
├── landingpage/              # Static marketing landing page
├── assets/                   # Logo, banner images
├── scripts/                  # Operational scripts (install, release, dev-setup)
├── archive/hermes-origin/    # Legacy files from hermes-agent era (pending review)
├── mini-swe-agent/           # Git submodule
└── tinker-atropos/           # Git submodule (RL training)
```

**User config:** `~/.hermes/config.yaml` (settings), `~/.hermes/.env` (API keys)

## File Dependency Chain

```
tools/registry.py  (no deps — imported by all tool files)
       ↑
tools/*.py  (each calls registry.register() at import time)
       ↑
core/model_tools.py  (imports tools/registry + triggers tool discovery)
       ↑
agents/hermes/agent.py, batch_runner.py, environments/
```

---

## AIAgent Class (agents/hermes/agent.py)

```python
class AIAgent:
    def __init__(self,
        model: str = "anthropic/claude-opus-4.6",
        max_iterations: int = 90,
        enabled_toolsets: list = None,
        disabled_toolsets: list = None,
        quiet_mode: bool = False,
        save_trajectories: bool = False,
        platform: str = None,           # "cli", "telegram", etc.
        session_id: str = None,
        skip_context_files: bool = False,
        skip_memory: bool = False,
        # ... plus provider, api_mode, callbacks, routing params
    ): ...

    def chat(self, message: str) -> str:
        """Simple interface — returns final response string."""

    def run_conversation(self, user_message: str, system_message: str = None,
                         conversation_history: list = None, task_id: str = None) -> dict:
        """Full interface — returns dict with final_response + messages."""
```

### Agent Loop

The core loop is inside `run_conversation()` — entirely synchronous:

```python
while api_call_count < self.max_iterations and self.iteration_budget.remaining > 0:
    response = client.chat.completions.create(model=model, messages=messages, tools=tool_schemas)
    if response.tool_calls:
        for tool_call in response.tool_calls:
            result = handle_function_call(tool_call.name, tool_call.args, task_id)
            messages.append(tool_result_message(result))
        api_call_count += 1
    else:
        return response.content
```

Messages follow OpenAI format: `{"role": "system/user/assistant/tool", ...}`. Reasoning content is stored in `assistant_msg["reasoning"]`.

---

## CLI Architecture (hermes_cli/main.py)

- **Rich** for banner/panels, **prompt_toolkit** for input with autocomplete
- **KawaiiSpinner** (`agent/display.py`) — animated faces during API calls, `┊` activity feed for tool results
- Config loaded in `hermes_cli/main.py` from `~/.hermes/config.yaml`
- **Skin engine** (`hermes_cli/skin_engine.py`) — data-driven CLI theming; initialized from `display.skin` config key at startup; skins customize banner colors, spinner faces/verbs/wings, tool prefix, response box, branding text
- Slash command dispatch lives in `hermes_cli/main.py`
- Skill slash commands: `agent/skill_commands.py` scans `~/.hermes/skills/`, injects as **user message** (not system prompt) to preserve prompt caching

### Adding CLI Commands

1. Add to `COMMANDS` dict in `hermes_cli/commands.py`
2. Add handler in `hermes_cli/main.py`
3. For persistent settings, use `save_config_value()` in `hermes_cli/config.py`

---

## Adding New Tools

Requires changes in **3 files**:

**1. Create `tools/your_tool.py`:**
```python
import json, os
from tools.registry import registry

def check_requirements() -> bool:
    return bool(os.getenv("EXAMPLE_API_KEY"))

def example_tool(param: str, task_id: str = None) -> str:
    return json.dumps({"success": True, "data": "..."})

registry.register(
    name="example_tool",
    toolset="example",
    schema={"name": "example_tool", "description": "...", "parameters": {...}},
    handler=lambda args, **kw: example_tool(param=args.get("param", ""), task_id=kw.get("task_id")),
    check_fn=check_requirements,
    requires_env=["EXAMPLE_API_KEY"],
)
```

**2. Add import** in `core/model_tools.py` `_discover_tools()` list.

**3. Add to `core/toolsets.py`** — either `_HERMES_CORE_TOOLS` (all platforms) or a new toolset.

The registry handles schema collection, dispatch, availability checking, and error wrapping. All handlers MUST return a JSON string.

**Agent-level tools** (todo, memory): intercepted by `agents/hermes/agent.py` before tool dispatch. See `todo_tool.py` for the pattern.

---

## Adding Configuration

### config.yaml options:
1. Add to `DEFAULT_CONFIG` in `hermes_cli/config.py`
2. Bump `_config_version` (currently 5) to trigger migration for existing users

### .env variables:
1. Add to `OPTIONAL_ENV_VARS` in `hermes_cli/config.py` with metadata:
```python
"NEW_API_KEY": {
    "description": "What it's for",
    "prompt": "Display name",
    "url": "https://...",
    "password": True,
    "category": "tool",  # provider, tool, messaging, setting
},
```

### Config loaders (two separate systems):

| Loader | Used by | Location |
|--------|---------|----------|
| Config load at startup | CLI mode | `hermes_cli/main.py` |
| `load_config()` | `hermes tools`, `hermes setup` | `hermes_cli/config.py` |
| Direct YAML load | Gateway | `gateway/run.py` |

---

## Skin/Theme System

The skin engine (`hermes_cli/skin_engine.py`) provides data-driven CLI visual customization. Skins are **pure data** — no code changes needed to add a new skin.

### Architecture

```
hermes_cli/skin_engine.py    # SkinConfig dataclass, built-in skins, YAML loader
~/.hermes/skins/*.yaml       # User-installed custom skins (drop-in)
```

- `init_skin_from_config()` — called at CLI startup, reads `display.skin` from config
- `get_active_skin()` — returns cached `SkinConfig` for the current skin
- `set_active_skin(name)` — switches skin at runtime (used by `/skin` command)
- `load_skin(name)` — loads from user skins first, then built-ins, then falls back to default
- Missing skin values inherit from the `default` skin automatically

### What skins customize

| Element | Skin Key | Used By |
|---------|----------|---------|
| Banner panel border | `colors.banner_border` | `banner.py` |
| Banner panel title | `colors.banner_title` | `banner.py` |
| Banner section headers | `colors.banner_accent` | `banner.py` |
| Banner dim text | `colors.banner_dim` | `banner.py` |
| Banner body text | `colors.banner_text` | `banner.py` |
| Response box border | `colors.response_border` | `cli.py` |
| Spinner faces (waiting) | `spinner.waiting_faces` | `display.py` |
| Spinner faces (thinking) | `spinner.thinking_faces` | `display.py` |
| Spinner verbs | `spinner.thinking_verbs` | `display.py` |
| Spinner wings (optional) | `spinner.wings` | `display.py` |
| Tool output prefix | `tool_prefix` | `display.py` |
| Agent name | `branding.agent_name` | `banner.py`, `cli.py` |
| Welcome message | `branding.welcome` | `cli.py` |
| Response box label | `branding.response_label` | `cli.py` |
| Prompt symbol | `branding.prompt_symbol` | `cli.py` |

### Built-in skins

- `default` — Classic Logos gold/kawaii (the current look)
- `ares` — Crimson/bronze war-god theme with custom spinner wings
- `mono` — Clean grayscale monochrome
- `slate` — Cool blue developer-focused theme

### Adding a built-in skin

Add to `_BUILTIN_SKINS` dict in `hermes_cli/skin_engine.py`:

```python
"mytheme": {
    "name": "mytheme",
    "description": "Short description",
    "colors": { ... },
    "spinner": { ... },
    "branding": { ... },
    "tool_prefix": "┊",
},
```

### User skins (YAML)

Users create `~/.hermes/skins/<name>.yaml`:

```yaml
name: cyberpunk
description: Neon-soaked terminal theme

colors:
  banner_border: "#FF00FF"
  banner_title: "#00FFFF"
  banner_accent: "#FF1493"

spinner:
  thinking_verbs: ["jacking in", "decrypting", "uploading"]
  wings:
    - ["⟨⚡", "⚡⟩"]

branding:
  agent_name: "Cyber Agent"
  response_label: " ⚡ Cyber "

tool_prefix: "▏"
```

Activate with `/skin cyberpunk` or `display.skin: cyberpunk` in config.yaml.

---

## Evolution System

The Evolution feature lives across four files:

| File | Role |
|------|------|
| `gateway/auth/db.py` | DB schema + CRUD (`evolution_proposals`, `evolution_settings`) |
| `gateway/auth/rbac.py` | Permissions: `view_evolution`, `manage_evolution`, `decide_evolution` |
| `gateway/evolution_handlers.py` | aiohttp request handlers for all `/evolution/*` routes |
| `skills/evolution/self-improvement/SKILL.md` | Skill instructions executed by agents on schedule |

### Proposal status FSM

```
pending → accepted
        → declined
        → questioned → pending (after agent answers)
        → in_progress (after accepted + branch created)
        → merged
        → cancelled
```

### Frontier consultation

`handle_consult_frontier` sends the proposal to a cloud AI (Claude or GPT-4o) and stores the response in `frontier_output`. The API key is read from a server-side env var named by `frontier_api_key_env` in settings — it is never sent to the client. The masked placeholder `"••••••••"` is returned for `git_pat` in all read responses; the handler ignores it on writes.

### Adding new proposal types

`proposal_type` has a SQLite CHECK constraint. To add a new type, add a migration that drops and recreates the constraint (SQLite does not support `ALTER COLUMN`), or widen the CHECK using a new schema version.

### Skills execution

Agents run `skills/evolution/self-improvement/SKILL.md` on the configured cron schedule. The cron job ID is stored in `evolution_settings.cron_job_id`. When settings are updated with a new schedule, the old cron job should be cancelled and a new one created (not yet automated — manual for now).

---

## Important Policies
### Prompt Caching Must Not Break

Hermes-Agent ensures caching remains valid throughout a conversation. **Do NOT implement changes that would:**
- Alter past context mid-conversation
- Change toolsets mid-conversation
- Reload memories or rebuild system prompts mid-conversation

Cache-breaking forces dramatically higher costs. The ONLY time we alter context is during context compression.

### Working Directory Behavior
- **CLI**: Uses current directory (`.` → `os.getcwd()`)
- **Messaging**: Uses `MESSAGING_CWD` env var (default: home directory)

### Background Process Notifications (Gateway)

When `terminal(background=true, check_interval=...)` is used, the gateway runs a watcher that
pushes status updates to the user's chat. Control verbosity with `display.background_process_notifications`
in config.yaml (or `HERMES_BACKGROUND_NOTIFICATIONS` env var):

- `all` — running-output updates + final message (default)
- `result` — only the final completion message
- `error` — only the final message when exit code != 0
- `off` — no watcher messages at all

---

## Known Pitfalls

### DO NOT use `simple_term_menu` for interactive menus
Rendering bugs in tmux/iTerm2 — ghosting on scroll. Use `curses` (stdlib) instead. See `hermes_cli/tools_config.py` for the pattern.

### DO NOT use `\033[K` (ANSI erase-to-EOL) in spinner/display code
Leaks as literal `?[K` text under `prompt_toolkit`'s `patch_stdout`. Use space-padding: `f"\r{line}{' ' * pad}"`.

### `_last_resolved_tool_names` is a process-global in `core/model_tools.py`
When subagents overwrite this global, `execute_code` calls after delegation may fail with missing tool imports. Known bug.

### Import from `core.*`, not from root-level shims
The canonical implementations live in `core/` (e.g. `core.state`, `core.model_tools`, `core.toolsets`, `core.clock`, `core.constants`). Root-level files like `hermes_state.py`, `model_tools.py`, `hermes_time.py` are thin re-export shims for backward compatibility — they work but add an indirection layer. New code should `from core.X import ...` directly.

### Tests must not write to `~/.hermes/`
The `_isolate_hermes_home` autouse fixture in `tests/conftest.py` redirects `HERMES_HOME` to a temp dir. Never hardcode `~/.hermes/` paths in tests.

---

## Testing

```bash
source .venv/bin/activate
python -m pytest tests/ -q          # Full suite (~3000 tests, ~3 min)
python -m pytest tests/test_model_tools.py -q   # Toolset resolution
python -m pytest tests/test_cli_init.py -q       # CLI config loading
python -m pytest tests/gateway/ -q               # Gateway tests
python -m pytest tests/tools/ -q                 # Tool-level tests
```

Always run the full suite before pushing changes.
