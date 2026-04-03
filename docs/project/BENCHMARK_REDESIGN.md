# Benchmark Model Selection Redesign

**Status:** Planning  
**Created:** 2026-04-03  
**Problem:** The new "test all" benchmark includes models that fail to load,
are too large, or are embedding-only. The old system filtered silently.
The new system shows everything but doesn't filter enough.

## What we learned from `/api/v1/models`

LM Studio's native API returns rich metadata per model:

```json
{
  "type": "llm",                          // "llm" | "embedding" — proper filtering
  "key": "google/gemma-3-4b",
  "display_name": "Gemma 3 4B",
  "architecture": "gemma3",
  "params_string": "4B",
  "size_bytes": 3341081486,               // actual file size on disk (3.3GB)
  "max_context_length": 131072,           // native max context
  "quantization": {
    "name": "Q4_K_M",
    "bits_per_weight": 4
  },
  "capabilities": {
    "vision": true,
    "trained_for_tool_use": false          // tool calling support
  },
  "format": "gguf",
  "loaded_instances": []                   // currently loaded?
}
```

This gives us everything we need to make smart decisions without guessing
from model names.

## Proposed filtering strategy

### Hard filters (model excluded entirely)

| Filter | Field | Reason |
|--------|-------|--------|
| Embedding models | `type === "embedding"` | Can't do chat completions |
| Non-GGUF formats | `format !== "gguf"` | LM Studio only runs GGUF reliably |
| Tiny context | `max_context_length < 8192` | Agent system prompt alone is ~8K |

### Soft filters (shown but flagged, excluded from "Test All")

| Filter | Field | Reason |
|--------|-------|--------|
| Too large for server | `size_bytes > server_ram * 0.85` | Won't fit with OS + context overhead |
| No tool use | `capabilities.trained_for_tool_use === false` | Agent needs tool calling; still usable but suboptimal |

### Display enrichment

| Info | Source | Display |
|------|--------|---------|
| File size | `size_bytes` | "3.3 GB" |
| Param count | `params_string` | "4B" |
| Quantization | `quantization.name` | "Q4_K_M" |
| Max context | `max_context_length` | "131K" |
| Vision | `capabilities.vision` | Badge |
| Tool use | `capabilities.trained_for_tool_use` | Badge |

### Sort order (for "Test All")

1. Models with `trained_for_tool_use === true` first
2. Then by `size_bytes` ascending (smallest first, fastest to test)
3. Within same size tier: prefer instruct/chat variants

## Implementation plan

### Phase 1 — Fetch rich metadata during server scan

When probing a server, if it's LM Studio, also fetch `/api/v1/models` and
store the rich metadata alongside the basic model list. Pass this through
to the frontend.

**Backend change** (`setup_handlers.py`):
- In the server probe function, if type is "lmstudio", also call
  `/api/v1/models` and merge metadata into the model entries
- Return: `{id, name, type, size_bytes, params_string, max_context_length,
  quantization, capabilities, format}`

### Phase 2 — Frontend uses real metadata for filtering

**Frontend change** (`setup.html`):
- `getModels()` uses `type` field to exclude embeddings (not name matching)
- Uses `size_bytes` for real size display (not param count heuristic)
- Uses `max_context_length` to pre-flag models with <16K context
- Shows `trained_for_tool_use` and `vision` as badges
- "Test All" respects hard + soft filters

### Phase 3 — Ollama equivalent

Ollama's `/api/tags` returns `{name, size, details: {parameter_size, family,
quantization_level}}`. Map these to the same schema.

### Phase 4 — Server RAM detection (optional)

For LM Studio: `/api/v1/system/info` may return system info.
For Ollama: `/api/version` doesn't help, but we can estimate from the models
that are already loaded (if any are loaded at a large context, the server
has at least that much RAM).

Alternatively: ask the user during setup what RAM their inference server has.
Or: try to load the model and see if it fails (current approach, but slow).

## Key decision

**Should we go back to auto-selecting 4 candidates?**

No — but we should **auto-select the best candidate** and pre-check it,
while showing all others. The UX should be:

1. Show all eligible models (hard-filtered) sorted by suitability
2. Auto-start testing the **top pick** (smallest model with tool_use + adequate context)
3. Show remaining models with "Test" buttons
4. User can override by testing others
5. "Test All" tests everything except flagged models

This gives the speed of the old system (auto-starts immediately with the
best candidate) plus the transparency of the new one (all models visible,
user controls what's tested).
