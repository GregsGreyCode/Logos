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
    pod_ms: Optional[int] = None,
    agent_ms: Optional[int] = None,
) -> None:
    """Append one spawn-duration record. Best-effort; never raises.

    ``duration_ms`` is the total spawn wall time.

    ``pod_ms`` is the openshell sandbox-create call duration — i.e.
    from spawn-start to pod phase = Ready.

    ``agent_ms`` is the hermes boot window — from pod Ready to
    /health answering 200. Typically dominated by Python module
    imports and the platform-adapter connection phase.

    Either phase can be omitted if the caller can't measure it
    cleanly; callers that omit both still produce a usable total.
    """
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
        if pod_ms is not None:
            rec["pod_ms"] = int(pod_ms)
        if agent_ms is not None:
            rec["agent_ms"] = int(agent_ms)
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


def _bucket_stats(samples: List[int]) -> Dict[str, Any]:
    """Median + p90 for a list of ms samples, or nulls below MIN_FOR_LEARNED."""
    out: Dict[str, Any] = {"count": len(samples), "median_ms": None, "p90_ms": None}
    if len(samples) >= MIN_FOR_LEARNED:
        out["median_ms"] = int(statistics.median(samples))
        srt = sorted(samples)
        idx = max(0, min(len(srt) - 1, int(len(srt) * 0.9)))
        out["p90_ms"] = int(srt[idx])
    return out


def stats() -> Dict[str, Any]:
    """Return aggregated stats split by warm vs cold AND by phase.

    Shape::

      {
        # Total wall-time buckets (kept for backwards compat)
        "warm":  {"count": N, "median_ms": M, "p90_ms": P},
        "cold":  {"count": N, "median_ms": M, "p90_ms": P},
        # Phase breakdown — pod create vs agent boot. Phase-level
        # buckets are useful because the UI wants to say different
        # things during "pod provisioning" vs "agent booting".
        "phases": {
          "pod":   {"warm": {...}, "cold": {...}},
          "agent": {"warm": {...}, "cold": {...}},
        }
      }

    warm / cold are keyed off image_imported. A bucket with fewer
    than MIN_FOR_LEARNED samples returns null median/p90 so the UI
    knows to fall back to hardcoded defaults.
    """
    out: Dict[str, Any] = {
        "warm": {"count": 0, "median_ms": None, "p90_ms": None},
        "cold": {"count": 0, "median_ms": None, "p90_ms": None},
        "phases": {
            "pod":   {"warm": {"count": 0, "median_ms": None, "p90_ms": None},
                      "cold": {"count": 0, "median_ms": None, "p90_ms": None}},
            "agent": {"warm": {"count": 0, "median_ms": None, "p90_ms": None},
                      "cold": {"count": 0, "median_ms": None, "p90_ms": None}},
        },
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
        b_recs = [r for r in recs if predicate(r)]
        durations = [r["duration_ms"] for r in b_recs if isinstance(r.get("duration_ms"), int)]
        out[bucket] = _bucket_stats(durations)
        pod = [r["pod_ms"] for r in b_recs if isinstance(r.get("pod_ms"), int)]
        agent = [r["agent_ms"] for r in b_recs if isinstance(r.get("agent_ms"), int)]
        out["phases"]["pod"][bucket] = _bucket_stats(pod)
        out["phases"]["agent"][bucket] = _bucket_stats(agent)
    return out
