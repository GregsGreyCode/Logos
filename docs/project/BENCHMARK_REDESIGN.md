# Benchmark Model Selection Redesign

**Status:** Implemented  
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

## Implementation status

### Phase 1 — Rich metadata from server scan — DONE

Server probe fetches `/api/v1/models` for LM Studio servers. Returns:
type, size_bytes, max_context_length, params_string, quantization,
trained_for_tool_use, vision per model.

### Phase 2 — Frontend uses real metadata — DONE

`getModels()` filters by type, size_bytes, max_context_length. Shows
real file size, quant level, max context, tool_use/vision badges.
Models sorted tool_use first, then smallest first.

### Phase 3 — Native LM Studio benchmark — DONE

Uses `/api/v1/chat` for 2-3 calls instead of ~12:
1. Load with `echo_load_config` → actual context + flash_attention
2. Combined eval via native chat → tok/s + TTFT + 6 evals in one call
3. Hard eval (if ≥5/6) → 3 advanced tests
Falls back to OpenAI-compatible path for Ollama/llama.cpp.

### Phase 4 — Ollama equivalent — TODO

Map Ollama `/api/tags` response to the same metadata schema.

### Phase 5 — Server RAM detection — TODO

Ask during setup or detect from loaded model sizes.

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
