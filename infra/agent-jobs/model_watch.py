"""Household agent only, every minute, no model (story D2, decided 19 Nov): tell the household
group chat when the AI computer (LM Studio) has been off for more than two minutes, and again when it is
back. Prints only on those two changes (printing nothing sends nothing). Standard library only."""
import json
import os
import time
import urllib.request

HOME = "/opt/data"
STATE = os.path.join(HOME, ".model_watch")
QUIET = 120                      # a reload, or D1's minute of waiting, never causes a message
OFFLINE = ("**The AI computer is off.** The agents can't reply until it's back on; "
           "anything you send now will be answered then.")
BACK = "**The AI computer is back on.** The agents are answering what was waiting."


def step(off: bool, now: float, state: dict):
    """(message or None, new state). ``state``: {"off_since": t or None, "told": bool}."""
    if off:
        since = state.get("off_since") or now
        if not state.get("told") and now - since > QUIET:
            return OFFLINE, {"off_since": since, "told": True}
        return None, {"off_since": since, "told": state.get("told", False)}
    if state.get("told"):
        return BACK, {"off_since": None, "told": False}
    return None, {"off_since": None, "told": False}


def main():
    try:
        env = dict(l.strip().split("=", 1) for l in open(os.path.join(HOME, ".env"))
                   if "=" in l and not l.startswith("#"))
        with urllib.request.urlopen(env["LOGOS_MODEL_HEALTH_URL"], timeout=10) as r:
            off = bool(json.load(r).get("model_server_off"))
    except Exception:
        return                      # cannot tell: say nothing, change nothing
    try:
        state = json.load(open(STATE))
    except (OSError, ValueError):
        state = {}
    message, state = step(off, time.time(), state)
    with open(STATE, "w") as f:
        json.dump(state, f)
    if message:
        print(message)


if __name__ == "__main__":
    main()
