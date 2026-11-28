"""What the agents' local work would cost on Claude: the one place the cloud prices live (28 Nov).

Anthropic first-party list prices, US dollars per million tokens, as of :data:`AS_OF`. Used only to
compare (Models › Local vs cloud, ``logos_cloud_equivalent_cost_usd_total``): nothing is ever sent
to these models.

- Prompt tokens the local model reused from its cache are priced at the **cache read** rate, the rest
  at the input rate. The **cache write** premium (paid once when a prompt is first cached) is left
  out, so a real cloud bill would be a little higher than shown.
- **Claude Haiku 5.5** has a long-prompt tier: a request whose prompt is over
  :data:`HAIKU_LONG_OVER` tokens is priced at the higher rates for all of its tokens. The guard logs
  each request's prompt size, so each request picks its own tier. Haiku 5.5's cache read price is
  not published: it is **assumed** to be 10 % of the input price, like the other models.

Stdlib only.
"""
from __future__ import annotations

from typing import Dict, List, Mapping

AS_OF = "2026-11"
MILLION = 1_000_000
HAIKU_LONG_OVER = 100_000

#: key -> label, rates (USD per 1M tokens), and which of them are assumptions.
MODELS: Dict[str, dict] = {
    "haiku-5.5": {"label": "Claude Haiku 5.5", "input": 0.10, "output": 0.50, "cache_read": 0.01,
                  "assumed": ["cache_read"],
                  "long": {"over": HAIKU_LONG_OVER, "input": 0.50, "output": 2.50, "cache_read": 0.05}},
    "sonnet-5.5": {"label": "Claude Sonnet 5.5", "input": 2.00, "output": 10.00, "cache_read": 0.10, "assumed": []},
    "opus-5.5": {"label": "Claude Opus 5.5", "input": 4.00, "output": 20.00, "cache_read": 0.20, "assumed": []},
}
ORDER = ("haiku-5.5", "sonnet-5.5", "opus-5.5")


def _rate(rates: Mapping[str, float], prompt: float, cached: float, completion: float) -> float:
    cached = min(max(cached, 0.0), max(prompt, 0.0))
    return ((prompt - cached) * rates["input"] + cached * rates["cache_read"] + completion * rates["output"]) / MILLION


def cost(model: str, prompt: float, cached: float, completion: float,
         long_prompt: float = 0.0, long_cached: float = 0.0, long_completion: float = 0.0) -> float:
    """USD for these tokens on ``model``. ``prompt``/``cached``/``completion`` include everything;
    the ``long_*`` part is the share from requests over the long-prompt tier (only Haiku prices it apart)."""
    m = MODELS[model]
    tier = m.get("long")
    if not tier:
        return _rate(m, prompt, cached, completion)
    return (_rate(m, prompt - long_prompt, cached - long_cached, completion - long_completion)
            + _rate(tier, long_prompt, long_cached, long_completion))


def table() -> List[dict]:
    """The prices, for the Models page's notes."""
    out = []
    for key in ORDER:
        m = MODELS[key]
        row = {"model": key, "label": m["label"], "input": m["input"], "output": m["output"],
               "cache_read": m["cache_read"], "assumed": list(m["assumed"])}
        if m.get("long"):
            row["long"] = dict(m["long"])
        out.append(row)
    return out
