"""Runs every minute with no model call. If another agent marked a message urgent, print it so
it is delivered to this agent's person straight away. Prints nothing otherwise (which sends
nothing). The agent itself deals with the message at its normal noticeboard check."""
import json
import urllib.request

ENV = dict(l.strip().split("=", 1) for l in open("/opt/data/.env") if "=" in l and not l.startswith("#"))
try:
    req = urllib.request.Request(ENV["LOGOS_NOTICEBOARD_POLL_URL"] + "/urgent",
                                 headers={"Authorization": "Bearer " + ENV["LOGOS_NOTICEBOARD_TOKEN"]})
    for line in json.load(urllib.request.urlopen(req, timeout=10)).get("lines", []):
        print(line)
except Exception:
    pass
