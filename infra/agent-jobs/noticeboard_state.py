"""Monitor script for the noticeboard check: prints a fingerprint that changes only when a new
message is waiting for this agent or one of its own requests has expired. Unchanged output
means no model call is made. Standard library only."""
import json
import pathlib
import urllib.request

ENV = dict(l.strip().split("=", 1) for l in open("/opt/data/.env") if "=" in l and not l.startswith("#"))
CACHE = pathlib.Path("/opt/data/.noticeboard_state")
try:
    url = ENV["LOGOS_NOTICEBOARD_URL"].rsplit("/mcp", 1)[0] + "/pending"
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + ENV["LOGOS_NOTICEBOARD_TOKEN"]})
    p = json.load(urllib.request.urlopen(req, timeout=10))
    state = f"latest message {p['latest']}, latest expired request {p['latest_expired']}"
    CACHE.write_text(state)
except Exception:
    # Noticeboard unreachable: repeat the last known state so nothing is triggered.
    state = CACHE.read_text() if CACHE.exists() else "latest message 0, latest expired request 0"
print(state)
