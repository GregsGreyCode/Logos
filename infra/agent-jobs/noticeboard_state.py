"""Monitor script for the noticeboard check: prints a fingerprint that changes only when a new
message is waiting for this agent or one of its own requests has expired. Unchanged output
means no model call is made. Standard library only.

Story D1: hermes counts a message as seen as soon as the fingerprint changes, even when the
model run that follows fails (LM Studio closed). So while a message stays unread, the
fingerprint carries a "retry" stamp for the current half hour, and the check runs again every
30 minutes until the agent has read it. Once it is read, the last stamp is kept, so reading
it does not itself look like a change."""
import json
import pathlib
import time

CACHE = pathlib.Path("/opt/data/.noticeboard_state")
RETRY = pathlib.Path("/opt/data/.noticeboard_retry")


def half_hour(now: float) -> str:
    t = time.localtime(now)
    return time.strftime("%Y-%m-%dT%H:", t) + ("00" if t.tm_min < 30 else "30")


def fingerprint(pending: dict, now: float, retry_file: pathlib.Path = RETRY) -> str:
    state = f"latest message {pending['latest']}, latest expired request {pending['latest_expired']}"
    if pending.get("unread"):
        stamp = half_hour(now)
        retry_file.write_text(stamp)
    else:
        stamp = retry_file.read_text().strip() if retry_file.exists() else ""
    return state + (f", retry {stamp}" if stamp else "")


def main() -> None:
    import urllib.request
    env = dict(l.strip().split("=", 1) for l in open("/opt/data/.env") if "=" in l and not l.startswith("#"))
    try:
        req = urllib.request.Request(env["LOGOS_NOTICEBOARD_POLL_URL"] + "/pending",
                                     headers={"Authorization": "Bearer " + env["LOGOS_NOTICEBOARD_TOKEN"]})
        state = fingerprint(json.load(urllib.request.urlopen(req, timeout=10)), time.time())
        CACHE.write_text(state)
    except Exception:
        # Noticeboard unreachable: repeat the last known state so nothing is triggered.
        state = CACHE.read_text() if CACHE.exists() else "latest message 0, latest expired request 0"
    print(state)


if __name__ == "__main__":
    main()
