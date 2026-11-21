"""Which phones have fallen behind on their brains (Greg, 21 Nov).

Each brain is a Syncthing folder (``brain-<agent>``, scripts/logos-sync) shared with its people's phones.
Android likes to put the Syncthing app to sleep, and then the phone's Obsidian quietly shows an old
brain: a page the agent wrote is "not found". This asks the server's Syncthing (the ``logos-sync``
container, its GUI on 127.0.0.1 only) which devices are missing changes and when each was last seen, so
Notifications can say so.

A device is *behind* when it lacks some of a brain's changes and has not been connected for a while
(``STALE_SECONDS``): a phone that is merely between syncs does not count. Everything here is read-only.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any, Dict, List, Optional

NAME = "logos-sync"
GUI = "http://127.0.0.1:8384"
STALE_SECONDS = 6 * 3600
_KEY_CMD = r'sed -n "s:.*<apikey>\(.*\)</apikey>.*:\1:p" /var/syncthing/config/config.xml'


def _when(stamp: str) -> Optional[float]:
    """Syncthing's RFC 3339 time -> epoch seconds; None for its "never" (the year 1970)."""
    try:
        t = dt.datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
    return t if t > 86400 else None


def api_key(runner: Any) -> str:
    p = runner.run(["docker", "exec", NAME, "sh", "-c", _KEY_CMD])
    return (p.stdout or "").strip() if p.returncode == 0 else ""


def behind(runner: Any, now: float, stale: float = STALE_SECONDS) -> List[Dict[str, Any]]:
    """``[{agent, device, last_seen, missing}]`` for each device missing a brain's changes and quiet for
    ``stale`` seconds. Empty when Syncthing is not set up or does not answer."""
    key = api_key(runner)
    if not key:
        return []

    def get(path: str) -> Any:
        return json.loads(runner.http(GUI + path, headers={"X-API-Key": key}, timeout=5))

    try:
        me = get("/rest/system/status").get("myID")
        devices = {d["deviceID"]: d.get("name") or d["deviceID"][:7] for d in get("/rest/config/devices")}
        connected = {k: bool(v.get("connected")) for k, v in (get("/rest/system/connections").get("connections") or {}).items()}
        seen = {k: _when(v.get("lastSeen")) for k, v in (get("/rest/stats/device") or {}).items()}
        folders = get("/rest/config/folders")
    except Exception:
        return []
    out = []
    for f in folders:
        fid = str(f.get("id") or "")
        if not fid.startswith("brain-") or f.get("paused"):
            continue
        for d in f.get("devices") or []:
            dev = d.get("deviceID")
            if not dev or dev == me or connected.get(dev):
                continue
            last = seen.get(dev)
            if last is not None and now - last < stale:
                continue
            try:
                c = get(f"/rest/db/completion?folder={fid}&device={dev}")
            except Exception:
                continue
            missing = int(c.get("needItems") or 0) + int(c.get("needDeletes") or 0)
            if missing <= 0:
                continue
            out.append({"agent": fid[len("brain-"):], "device": devices.get(dev, dev[:7]), "last_seen": last,
                        "missing": missing})
    return out
