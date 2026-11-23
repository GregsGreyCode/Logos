"""What each agent did today: counts, never content (F42's "Agents today", and the Prometheus /metrics).

Every time an agent sends a text message, its logos-alert-text plugin tells Logos (gateway/alert_text.py).
Logos already counts that: per agent and day, how many messages, and when the last one went, plus a total
that only ever grows (so a Prometheus counter behaves). Kept in ``activity.json`` beside ``push.db``;
days older than 40 are dropped. No text, no person, no room: only the agent's name and numbers.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, Optional

KEEP_DAYS = 40
FILE = "activity.json"


def day_of(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


class Activity:
    def __init__(self, folder: Path, clock=time.time):
        self.path = Path(folder) / FILE
        self.clock = clock
        self.lock = threading.Lock()
        self.seen = deque(maxlen=500)             # event ids counted, so a repeated note counts once
        self.data: Dict[str, Any] = {"days": {}, "totals": {}}
        try:
            loaded = json.loads(self.path.read_text())
            if isinstance(loaded, dict):
                self.data = {"days": dict(loaded.get("days") or {}), "totals": dict(loaded.get("totals") or {})}
        except (OSError, ValueError):
            pass

    def record(self, agent: str, event_id: str = "") -> None:
        """One message sent by ``agent`` just now."""
        with self.lock:
            if event_id:
                if event_id in self.seen:
                    return
                self.seen.append(event_id)
            now = self.clock()
            today = self.data["days"].setdefault(day_of(now), {})
            row = today.setdefault(agent, {"n": 0, "last": 0})
            row["n"] += 1
            row["last"] = int(now)
            self.data["totals"][agent] = int(self.data["totals"].get(agent, 0)) + 1
            for d in sorted(self.data["days"])[:-KEEP_DAYS]:
                del self.data["days"][d]
            self._save()

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(self.data, f)
            os.replace(tmp, self.path)
        except OSError:
            pass                                  # counting is never worth breaking a reply's alert for

    def today(self, agent: str) -> Dict[str, int]:
        row = (self.data["days"].get(day_of(self.clock())) or {}).get(agent) or {}
        return {"messages": int(row.get("n", 0)), "last": int(row.get("last", 0)) or 0}

    def last_ever(self, agent: str) -> int:
        """The latest message time on any kept day (0 when never)."""
        best = 0
        for day in self.data["days"].values():
            best = max(best, int((day.get(agent) or {}).get("last", 0)))
        return best

    def total(self, agent: str) -> int:
        return int(self.data["totals"].get(agent, 0))

    def agents(self) -> list:
        return sorted(self.data["totals"])
