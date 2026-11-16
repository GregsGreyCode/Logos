"""Static model catalog used by the setup handlers' model pickers.

Curated model lists per cloud provider plus display metadata. Pure data,
no network access.
"""

from __future__ import annotations

# (model_id, display description shown in menus)
OPENROUTER_MODELS: list[tuple[str, str]] = [
    ("anthropic/claude-opus-4.6",       "recommended"),
    ("anthropic/claude-sonnet-4.5",     ""),
    ("openai/gpt-5.4-pro",              ""),
    ("openai/gpt-5.4",                  ""),
    ("openai/gpt-5.3-codex",            ""),
    ("google/gemini-3-pro-preview",     ""),
    ("google/gemini-3-flash-preview",   ""),
    ("qwen/qwen3.5-plus-02-15",         ""),
    ("qwen/qwen3.5-35b-a3b",            ""),
    ("stepfun/step-3.5-flash",          ""),
    ("z-ai/glm-5",                      ""),
    ("moonshotai/kimi-k2.5",            ""),
    ("minimax/minimax-m2.5",            ""),
]

_PROVIDER_MODELS: dict[str, list[str]] = {
    "nous": [
        "claude-opus-4-6",
        "claude-sonnet-4-6",
        "gpt-5.4",
        "gemini-3-flash",
        "gemini-3.0-pro-preview",
        "deepseek-v3.2",
    ],
    "openai-codex": [
        "gpt-5.3-codex",
        "gpt-5.2-codex",
        "gpt-5.1-codex-mini",
        "gpt-5.1-codex-max",
    ],
    "zai": [
        "glm-5",
        "glm-4.7",
        "glm-4.5",
        "glm-4.5-flash",
    ],
    "kimi-coding": [
        "kimi-for-coding",
        "kimi-k2.5",
        "kimi-k2-thinking",
        "kimi-k2-thinking-turbo",
        "kimi-k2-turbo-preview",
        "kimi-k2-0905-preview",
    ],
    "minimax": [
        "MiniMax-M2.5",
        "MiniMax-M2.5-highspeed",
        "MiniMax-M2.1",
    ],
    "minimax-cn": [
        "MiniMax-M2.5",
        "MiniMax-M2.5-highspeed",
        "MiniMax-M2.1",
    ],
    "anthropic": [
        "claude-opus-4-6",
        "claude-sonnet-4-6",
        "claude-opus-4-5-20251101",
        "claude-sonnet-4-5-20250929",
        "claude-opus-4-20250514",
        "claude-sonnet-4-20250514",
        "claude-haiku-4-5-20251001",
    ],
}

# ── Model catalog: rich metadata for UI model pickers ─────────────────────────
# Each entry provides human-readable info so users don't face raw model IDs.
# Cost tiers: 1 ($) = cheapest, 4 ($$$$) = most expensive. Relative, not exact.
# Tags: coding, reasoning, general, fast, long-context, vision

MODEL_CATALOG: dict[str, dict] = {
    # ── Anthropic ─────────────────────────────────────────────────────────
    "claude-opus-4-6": {
        "display": "Claude Opus 4.6",
        "provider": "Anthropic",
        "description": "Most capable Claude. Best for complex reasoning, coding, and analysis.",
        "cost": 4, "speed": "moderate", "context": 200000,
        "tags": ["reasoning", "coding", "general"],
        "recommended": False,
    },
    "claude-sonnet-4-6": {
        "display": "Claude Sonnet 4.6",
        "provider": "Anthropic",
        "description": "Best balance of quality, speed, and cost. Recommended for most agents.",
        "cost": 2, "speed": "fast", "context": 200000,
        "tags": ["general", "coding", "fast"],
        "recommended": True, "recommended_label": "Best default",
    },
    "claude-opus-4-5-20251101": {
        "display": "Claude Opus 4.5",
        "provider": "Anthropic",
        "description": "Previous generation flagship. Strong reasoning and coding.",
        "cost": 4, "speed": "moderate", "context": 200000,
        "tags": ["reasoning", "coding"],
    },
    "claude-sonnet-4-5-20250929": {
        "display": "Claude Sonnet 4.5",
        "provider": "Anthropic",
        "description": "Previous generation balanced model.",
        "cost": 2, "speed": "fast", "context": 200000,
        "tags": ["general", "fast"],
    },
    "claude-opus-4-20250514": {
        "display": "Claude Opus 4",
        "provider": "Anthropic",
        "description": "Earlier flagship. Still strong for complex tasks.",
        "cost": 3, "speed": "moderate", "context": 200000,
        "tags": ["reasoning", "coding"],
    },
    "claude-sonnet-4-20250514": {
        "display": "Claude Sonnet 4",
        "provider": "Anthropic",
        "description": "Earlier balanced model. Good value.",
        "cost": 2, "speed": "fast", "context": 200000,
        "tags": ["general"],
    },
    "claude-haiku-4-5-20251001": {
        "display": "Claude Haiku 4.5",
        "provider": "Anthropic",
        "description": "Fastest and cheapest Claude. Great for quick tasks and high volume.",
        "cost": 1, "speed": "very fast", "context": 200000,
        "tags": ["fast", "general"],
        "recommended": True, "recommended_label": "Fast & cheap",
    },
    # ── OpenRouter (curated) ──────────────────────────────────────────────
    "anthropic/claude-opus-4.6": {
        "display": "Claude Opus 4.6",
        "provider": "Anthropic via OpenRouter",
        "description": "Most capable Claude. Best for complex reasoning and coding.",
        "cost": 4, "speed": "moderate", "context": 200000,
        "tags": ["reasoning", "coding", "general"],
        "recommended": True, "recommended_label": "Best quality",
    },
    "anthropic/claude-sonnet-4.5": {
        "display": "Claude Sonnet 4.5",
        "provider": "Anthropic via OpenRouter",
        "description": "Fast, capable, good default for most agents.",
        "cost": 2, "speed": "fast", "context": 200000,
        "tags": ["general", "coding", "fast"],
        "recommended": True, "recommended_label": "Best default",
    },
    "openai/gpt-5.4-pro": {
        "display": "GPT-5.4 Pro",
        "provider": "OpenAI via OpenRouter",
        "description": "OpenAI flagship. Strong reasoning and broad knowledge.",
        "cost": 4, "speed": "moderate", "context": 128000,
        "tags": ["reasoning", "general"],
    },
    "openai/gpt-5.4": {
        "display": "GPT-5.4",
        "provider": "OpenAI via OpenRouter",
        "description": "Fast GPT-5.4 variant. Good balance of speed and quality.",
        "cost": 2, "speed": "fast", "context": 128000,
        "tags": ["general", "fast"],
    },
    "openai/gpt-5.3-codex": {
        "display": "GPT-5.3 Codex",
        "provider": "OpenAI via OpenRouter",
        "description": "Optimised for code generation and software engineering.",
        "cost": 3, "speed": "fast", "context": 128000,
        "tags": ["coding"],
        "recommended": True, "recommended_label": "Best for coding",
    },
    "google/gemini-3-pro-preview": {
        "display": "Gemini 3 Pro",
        "provider": "Google via OpenRouter",
        "description": "Google flagship. Massive context window, strong reasoning.",
        "cost": 3, "speed": "moderate", "context": 1048576,
        "tags": ["reasoning", "long-context"],
        "recommended": True, "recommended_label": "Long context",
    },
    "google/gemini-3-flash-preview": {
        "display": "Gemini 3 Flash",
        "provider": "Google via OpenRouter",
        "description": "Fast Google model. Good for high-volume tasks.",
        "cost": 1, "speed": "very fast", "context": 1048576,
        "tags": ["fast", "long-context"],
    },
    "qwen/qwen3.5-plus-02-15": {
        "display": "Qwen 3.5 Plus",
        "provider": "Alibaba via OpenRouter",
        "description": "Strong open-weight model. Good multilingual support.",
        "cost": 1, "speed": "fast", "context": 131072,
        "tags": ["general", "fast"],
    },
    "qwen/qwen3.5-35b-a3b": {
        "display": "Qwen 3.5 35B",
        "provider": "Alibaba via OpenRouter",
        "description": "Efficient MoE model. Fast and cheap.",
        "cost": 1, "speed": "very fast", "context": 131072,
        "tags": ["fast"],
    },
    "stepfun/step-3.5-flash": {
        "display": "Step 3.5 Flash",
        "provider": "StepFun via OpenRouter",
        "description": "Ultra-fast Chinese model. Good for quick tasks.",
        "cost": 1, "speed": "very fast", "context": 65536,
        "tags": ["fast"],
    },
    "z-ai/glm-5": {
        "display": "GLM-5",
        "provider": "Zhipu AI via OpenRouter",
        "description": "Chinese frontier model. Strong reasoning.",
        "cost": 2, "speed": "fast", "context": 200000,
        "tags": ["reasoning", "general"],
    },
    "moonshotai/kimi-k2.5": {
        "display": "Kimi K2.5",
        "provider": "Moonshot via OpenRouter",
        "description": "Specialised for coding and technical tasks.",
        "cost": 2, "speed": "fast", "context": 262144,
        "tags": ["coding", "long-context"],
    },
    "minimax/minimax-m2.5": {
        "display": "MiniMax M2.5",
        "provider": "MiniMax via OpenRouter",
        "description": "Versatile model with good multilingual support.",
        "cost": 1, "speed": "fast", "context": 131072,
        "tags": ["general"],
    },
}


def get_model_info(model_id: str) -> dict:
    """Look up catalog info for a model. Returns a dict with display name, etc.
    Falls back to a minimal entry if the model isn't in the catalog."""
    if model_id in MODEL_CATALOG:
        return {**MODEL_CATALOG[model_id], "id": model_id}
    # Fallback: derive display name from ID
    display = model_id.replace("-", " ").replace("/", " / ").title()
    return {"id": model_id, "display": display, "provider": "", "description": "",
            "cost": 0, "speed": "", "context": 0, "tags": []}


def get_provider_catalog(provider: str) -> list[dict]:
    """Return enriched model list for a provider, with recommended models first."""
    model_ids_list = _PROVIDER_MODELS.get(provider, [])
    models = [get_model_info(mid) for mid in model_ids_list]
    # Sort: recommended first, then by cost ascending
    models.sort(key=lambda m: (not m.get("recommended", False), m.get("cost", 99)))
    return models
