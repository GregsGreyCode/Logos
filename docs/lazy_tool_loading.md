# Lazy Tool Loading — Design Doc

> **Status:** Implemented in v0.6.3. Phase 1 (core/extended split) and Phase 2 (context-aware auto-enable) are complete. Phase 3 (soul-driven selection) infrastructure exists but needs wiring.

## Problem

A "Hi!" message to qwen3.5-9b generates a 17,091 token request:
- System prompt: ~8,000 tokens
- 24 tool schemas: ~9,000 tokens
- User message: ~1 token
- Available for response: **-700 tokens** (exceeds 16K context)

The agent literally cannot respond to "Hi!" because tool definitions consume more than the entire context window on small models.

## Current architecture

```
Agent init → get_tool_definitions(enabled_toolsets)
           → returns ALL tools in the enabled toolsets
           → ALL schemas injected into every API call
           → 24 tools × ~375 tokens/tool = ~9,000 tokens

Every message pays 9,000 tokens of tool tax, even for "Hi!".
```

## Proposed: 2-tier tool loading

### Tier 1 — Core tools (always loaded)

Minimal set that every agent needs for basic operation:

```
terminal          — execute commands
read_file         — read files
write_file        — write files
patch             — edit files
search_files      — find code/content
memory            — persistent memory
todo              — task tracking
clarify           — ask user questions
```

~8 tools × ~375 tokens = ~3,000 tokens. Leaves room for conversation.

### Tier 2 — Extended tools (loaded on demand)

Everything else — loaded when the agent needs them:

```
web_search, web_extract        — needs FIRECRAWL_API_KEY
image_generate                 — needs FAL_KEY
browser_*                      — needs BROWSERBASE_API_KEY
vision_analyze                 — needs aux model
text_to_speech                 — optional
delegate_task                  — subagent spawning
execute_code                   — programmatic tool calling
workflow                       — DAG workflows
schedule_cronjob, list/remove  — cron management
send_message                   — cross-platform messaging
log_inspector                  — log analysis
bug_notes                      — self-reporting
session_search                 — long-term memory search
skill_manage, skill_view, skills_list — skill management
process                        — background process management
```

### How it works

1. Agent starts with Tier 1 tools only (~3K tokens instead of ~9K)
2. Agent also gets a **meta-tool**: `request_tools(category)`
3. When the agent needs web search, it calls `request_tools("web")`
4. The gateway injects web tool schemas into the next API call
5. The agent can now use `web_search` and `web_extract`

This is the **same pattern as `request_mcp_access`** — already proven in the codebase.

### Token savings

| Scenario | Before | After | Savings |
|----------|--------|-------|---------|
| "Hi!" on 16K model | 17,091 (FAILS) | ~11,500 | Works! |
| Simple file edit | 17,200 | ~11,600 | 5,600 tokens |
| Web research task | 17,800 | ~12,500 (after request) | 5,300 tokens |
| Full toolset needed | 17,091 | 17,091 (all requested) | 0 (same) |

### The meta-tool schema

```json
{
  "name": "request_tools",
  "description": "Request additional tool capabilities. Available categories: web (search/extract), image (generate), browser (navigate/click/type), vision (analyze images), tts (text-to-speech), delegation (subagents), code (execute Python), workflows (DAG tasks), cron (scheduling), messaging (cross-platform send), logs (inspector), skills (manage/view), process (background). Call this before using tools not in your current set.",
  "parameters": {
    "type": "object",
    "properties": {
      "categories": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Tool categories to load"
      }
    },
    "required": ["categories"]
  }
}
```

~150 tokens for the meta-tool vs ~9,000 for all 24 tools.

## Context window issue

Separate from lazy loading: LM Studio divides total context across parallel slots. With 4 slots and 65K total, each slot gets 16,384 tokens. The setup benchmark should detect `n_ctx_slot` (per-slot context) not just total `n_ctx`, and the context compressor should use the per-slot value.

The benchmark already probes context by filling a slot — so the stored value should be correct. The issue is that `get_model_context_length()` may not be reading the stored benchmark value for this model. Need to verify the config.yaml `lmstudio_context_lengths` entry matches what LM Studio actually serves per-slot.

## Implementation plan

### Phase 1 — Core vs Extended split (do now)

1. Define `CORE_TOOLS` list in `core/toolsets.py`
2. Add `request_tools` meta-tool in `tools/request_tools_tool.py`
3. Modify `get_tool_definitions()` to accept a `lazy=True` flag
4. When `lazy=True`: return only CORE_TOOLS + request_tools
5. Agent loop: when `request_tools` is called, inject the requested schemas into the next API call

### Phase 2 — Context-aware tool selection (follow-up)

6. Before each API call, estimate: system_prompt + tools + conversation
7. If tools alone exceed 50% of context, auto-enable lazy mode
8. Log a warning: "Tool schemas use {N}% of context — lazy loading enabled"

### Phase 3 — Soul-driven tool selection (future)

9. Each soul specifies which tool categories it needs
10. The agent only loads tools relevant to its soul
11. A coding soul doesn't need TTS; a homelab soul doesn't need image gen
