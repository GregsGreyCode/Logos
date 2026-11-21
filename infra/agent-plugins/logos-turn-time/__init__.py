"""Time since the last message, for the agent (Greg, 21 Nov).

At 11:41 Greg's agent asked "shall I add these to your to-do list?"; at 15:40 Greg asked "what is brain
freeze" (garbled by his keyboard), and the agent took it as the answer to its question from four hours
before. The conversation runs all day (it starts fresh at 03:00), and the model cannot see that hours
passed between two messages. This adds a short note to the turn when the gap is an hour or more.

A fresh conversation after a quiet spell would also have stopped it, but it would forget a daily
question or a briefing answered hours later; this keeps everything and only says how long it has been.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from typing import Any, Optional

GAP_SECONDS = 3600
logger = logging.getLogger("logos.turn_time")


def _zone():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(os.environ.get("HERMES_TIMEZONE") or os.environ.get("TZ") or "UTC")
    except Exception:
        return None


def _ts(message: Any) -> Optional[float]:
    if not isinstance(message, dict):
        return None
    t = message.get("timestamp")
    try:
        return float(t) if t is not None else None
    except (TypeError, ValueError):
        return None


def _ago(seconds: float) -> str:
    hours = seconds / 3600
    if hours < 1.5:
        return "about an hour"
    if hours < 24:
        return f"about {round(hours)} hours"
    days = round(hours / 24)
    return "about a day" if days == 1 else f"about {days} days"


def note(conversation_history=None, now: Optional[float] = None, **_) -> Optional[dict]:
    """The note for this turn, or None. The last stamped message before this one sets the gap."""
    history = list(conversation_history or [])
    now = time.time() if now is None else now
    # The message being answered is usually the last in the history (stamped just now): skip
    # anything from the last few seconds, then take the newest older stamp.
    earlier = [t for t in (_ts(m) for m in history) if t is not None and now - t > 5]
    if not earlier:
        logger.info("turn-time: no earlier stamped message (%d in history)", len(history))
        return None
    gap = now - max(earlier)
    logger.info("turn-time: %d min since the last message%s", gap // 60, "; note added" if gap >= GAP_SECONDS else "")
    if gap < GAP_SECONDS:
        return None
    tz = _zone()
    fmt = lambda t: datetime.fromtimestamp(t, tz).strftime("%a %H:%M")
    return {"context": (f"Note on timing: this message came at {fmt(now)}, {_ago(gap)} after the last message in "
                        f"this conversation ({fmt(max(earlier))}). Treat it as new unless it clearly answers "
                        "what was said then. If it could be either, ask which they mean.")}


def register(ctx):
    ctx.register_hook("pre_llm_call", note)
