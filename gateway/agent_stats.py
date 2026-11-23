"""More numbers per agent for the Prometheus endpoint: its scheduled jobs, its brain, never a word of either.

Each is read by a short-lived helper container with no network and the volume mounted read-only, and kept for five
minutes so a frequent scrape costs nothing:

- jobs: the agent's own Python reads hermes's job list and run history (``cron/jobs.json`` and ``cron/executions.db``,
  opened immutable, so it needs no write to a read-only volume): per job, when it last ran, last succeeded, whether the
  last run failed, and how many runs and failures there were in the last 24 hours;
- brain: pages, disk, the last commit's time and Syncthing conflict copies (alpine/git, on the brain volume).
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, Optional

AGENT_IMAGE = "logos-agent:0.23.1"
GIT_IMAGE = "alpine/git:latest"
TTL = 300.0
_cache: Dict[tuple, tuple] = {}

_JOBS = r'''
import json, sqlite3, time
from datetime import datetime
now = time.time()
def at(s):
    try: return datetime.fromisoformat(str(s)).timestamp()
    except Exception: return 0
names = {j["id"]: j.get("name") or j["id"] for j in json.load(open("/opt/data/cron/jobs.json")).get("jobs", [])}
db = sqlite3.connect("file:/opt/data/cron/executions.db?immutable=1", uri=True)
out = {}
for jid, status, fin, claimed in db.execute("select job_id, status, finished_at, claimed_at from executions order by claimed_at"):
    name = names.get(jid)
    if not name or status not in ("completed", "failed", "missed"):
        continue
    t = at(fin) or at(claimed)
    o = out.setdefault(name, {"last": 0, "ok": 0, "failed": 0, "runs24": 0, "fail24": 0})
    o["last"], o["failed"] = int(t), int(status != "completed")
    if status == "completed": o["ok"] = int(t)
    if now - t < 86400:
        o["runs24"] += 1
        o["fail24"] += int(status != "completed")
print(json.dumps(out))
'''

_BRAIN = r'''
cd /v 2>/dev/null || exit 0
echo "pages $(find . -name '*.md' -not -path './.*/*' | wc -l)"
echo "kb $(du -sk /v | cut -f1)"
git config --global --add safe.directory /v 2>/dev/null
c=$(git log -1 --format=%ct 2>/dev/null); echo "last_commit ${c:-0}"
echo "conflicts $(find . -name '*.sync-conflict-*' -not -path './.git/*' | wc -l)"
'''


def parse_brain(out: str) -> Optional[Dict[str, int]]:
    got: Dict[str, int] = {}
    for line in out.splitlines():
        k, _, v = line.partition(" ")
        if v.strip().isdigit():
            got[k] = int(v)
    return got if "pages" in got else None


def parse_jobs(out: str) -> Optional[Dict[str, Dict[str, int]]]:
    try:
        d = json.loads(out)
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


def _cached(key: tuple, now: float, fetch) -> Any:
    hit = _cache.get(key)
    if hit and now - hit[0] < TTL:
        return hit[1]
    try:
        value = fetch()
    except Exception:
        value = None
    _cache[key] = (now, value)
    return value


def _run(runtime: Any, volume: str, mount: str, entry: str, image: str, argv: list) -> Optional[str]:
    if runtime.runner.run(["docker", "volume", "inspect", volume]).returncode != 0:
        return None
    p = runtime.runner.run(["docker", "run", "--rm", "--network", "none", "-v", f"{volume}:{mount}:ro", "--entrypoint", entry, image] + argv)
    if p.returncode != 0:
        return None
    return p.stdout if isinstance(p.stdout, str) else p.stdout.decode(errors="replace")


def jobs(runtime: Any, agent: str, now: float = 0.0) -> Optional[Dict[str, Dict[str, int]]]:
    """``{job name: {last, ok, failed, runs24, fail24}}`` or None. Blocks: run it in a thread."""
    def fetch():
        out = _run(runtime, f"logos-home-{agent}", "/opt/data", "/usr/local/bin/python3", AGENT_IMAGE, ["-c", _JOBS])
        return parse_jobs(out) if out is not None else None
    return _cached(("jobs", agent), now or time.time(), fetch)


def brain(runtime: Any, agent: str, now: float = 0.0) -> Optional[Dict[str, int]]:
    """``{pages, kb, last_commit, conflicts}`` or None. Blocks: run it in a thread."""
    def fetch():
        out = _run(runtime, f"logos-brain-{agent}", "/v", "sh", GIT_IMAGE, ["-c", _BRAIN])
        return parse_brain(out) if out is not None else None
    return _cached(("brain", agent), now or time.time(), fetch)
