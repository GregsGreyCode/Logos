"""Lightweight spawn-duration tracking.

Records how long each sandbox spawn takes so the UI can show a learned
estimate ("usually ~95s") instead of a hardcoded "up to 3 min" guess.

Storage: append-only JSONL at ``$HOME/.logos/spawn_metrics.jsonl``, kept
to the last MAX_RECORDS entries on every write so the file never
unbounded-grows. Loss of this file is fine — it just resets the learned
estimate to defaults until enough new spawns accumulate.

The `image_imported` flag distinguishes warm-cluster spawns (fast) from
cold-cluster spawns (slow — model switch into a fresh cluster has to
import hermes-sandbox:m12, ~60-120s on its own). The UI keys its hint
off this same flag so the user gets the right estimate from second one.
"""
from __future__ import annotations

import json
import logging
import os
import statistics
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

MAX_RECORDS = 200  # ~3 weeks of normal use; enough for stable medians
MIN_FOR_LEARNED = 3  # below this, fall back to hardcoded defaults


def _path() -> Path:
    base = Path(os.environ.get("LOGOS_DATA_DIR", str(Path.home() / ".logos")))
    return base / "spawn_metrics.jsonl"


def record(
    *,
    gateway: str,
    image: str,
    duration_ms: int,
    image_imported: bool,
    agent_name: Optional[str] = None,
) -> None:
    """Append one spawn-duration record. Best-effort; never raises."""
    try:
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        rec = {
            "ts": int(__import__("time").time()),
            "gateway": gateway,
            "image": image,
            "duration_ms": int(duration_ms),
            "image_imported": bool(image_imported),
        }
        if agent_name:
            rec["agent_name"] = agent_name
        # Append, then trim to MAX_RECORDS by reading + rewriting.
        # 200 lines * ~150 bytes = ~30 KB — fine to rewrite each time.
        existing: List[Dict[str, Any]] = []
        if p.exists():
            try:
                with open(p) as f:
                    existing = [json.loads(line) for line in f if line.strip()]
            except Exception:
                existing = []  # corrupt file — start fresh
        existing.append(rec)
        if len(existing) > MAX_RECORDS:
            existing = existing[-MAX_RECORDS:]
        tmp = p.with_suffix(p.suffix + ".tmp")
        with open(tmp, "w") as f:
            for r in existing:
                f.write(json.dumps(r) + "\n")
        tmp.replace(p)
    except Exception as exc:
        logger.debug("spawn_metrics.record skipped: %s", exc)


def stats() -> Dict[str, Dict[str, Any]]:
    """Return aggregated stats split by warm vs cold (image_imported).

    Shape:
      {
        "warm":     {"count": N, "median_ms": M, "p90_ms": P},
        "cold":     {"count": N, "median_ms": M, "p90_ms": P},
      }

    "warm" = image was already in the cluster (typical; ~30-60s).
    "cold" = image had to be imported (model switch / first spawn; ~2-3 min).

    A bucket with fewer than MIN_FOR_LEARNED samples returns
    ``{"count": N, "median_ms": null, "p90_ms": null}`` so the UI knows
    to fall back to hardcoded defaults.
    """
    out = {
        "warm": {"count": 0, "median_ms": None, "p90_ms": None},
        "cold": {"count": 0, "median_ms": None, "p90_ms": None},
    }
    p = _path()
    if not p.exists():
        return out
    try:
        with open(p) as f:
            recs = [json.loads(line) for line in f if line.strip()]
    except Exception:
        return out
    for bucket, predicate in (
        ("warm", lambda r: not r.get("image_imported")),
        ("cold", lambda r: r.get("image_imported")),
    ):
        durations = [r["duration_ms"] for r in recs if predicate(r) and isinstance(r.get("duration_ms"), int)]
        out[bucket]["count"] = len(durations)
        if len(durations) >= MIN_FOR_LEARNED:
            out[bucket]["median_ms"] = int(statistics.median(durations))
            # p90 via sorted index — quantiles() needs Python 3.8+ but is overkill
            srt = sorted(durations)
            idx = max(0, min(len(srt) - 1, int(len(srt) * 0.9)))
            out[bucket]["p90_ms"] = int(srt[idx])
    return out
