"""How full each agent's vault is (F47), for the Prometheus endpoint: counts and sizes only, never a name or a word.

The vault is in the agent's home volume (``logos-home-<agent>``, under ``vault/``). It is counted by a short-lived
helper container with no network that mounts the volume read-only, and the answer is kept for five minutes so a
scrape every few seconds does not start a container every time.
"""
from __future__ import annotations

import time
from typing import Any, Dict, Optional

HELPER = "alpine/git:latest"
TTL = 300.0
_cache: Dict[str, tuple] = {}

_COUNT = r'''
cd /v/vault 2>/dev/null || cd /tmp
n=0; b=0; old=0; new=0
for f in $(find files -type f 2>/dev/null); do
  n=$((n+1)); b=$((b+$(stat -c %s "$f"))); t=$(stat -c %Y "$f")
  [ "$old" = 0 ] || [ "$t" -lt "$old" ] && old=$t
  [ "$t" -gt "$new" ] && new=$t
done
echo "files $n"; echo "bytes $b"; echo "oldest $old"; echo "newest $new"
for s in new retry done failed; do echo "state_$s $(grep -l "\"state\": \"$s\"" meta/*.json 2>/dev/null | wc -l)"; done
'''


def parse(out: str) -> Optional[Dict[str, int]]:
    got: Dict[str, int] = {}
    for line in out.splitlines():
        k, _, v = line.partition(" ")
        if v.strip().isdigit():
            got[k] = int(v)
    return got if "files" in got else None


def read(runtime: Any, agent: str, now: float = 0.0) -> Optional[Dict[str, int]]:
    """The agent's vault in numbers, or None when it has none (or cannot be read). Blocks: run it in a thread."""
    now = now or time.time()
    hit = _cache.get(agent)
    if hit and now - hit[0] < TTL:
        return hit[1]
    vol = f"logos-home-{agent}"
    stats = None
    try:
        if runtime.runner.run(["docker", "volume", "inspect", vol]).returncode == 0:
            p = runtime.runner.run(["docker", "run", "--rm", "--network", "none", "-v", f"{vol}:/v:ro", "--entrypoint", "sh", HELPER, "-c", _COUNT])
            if p.returncode == 0:
                stats = parse(p.stdout if isinstance(p.stdout, str) else p.stdout.decode(errors="replace"))
    except Exception:
        stats = None
    _cache[agent] = (now, stats)
    return stats
