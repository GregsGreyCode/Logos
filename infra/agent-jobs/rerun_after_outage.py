"""Runs every five minutes with no model (story D2). A scheduled job whose last run failed because
the local model was offline runs once more when it is back, while that still makes sense:

- weekly-digest: the same day;
- consolidate (the nightly brain tidy): before 09:00.

Not the morning briefing (its plain version has already gone out), nor the noticeboard check
(it retries by itself every half hour until the message is read, story D1). Prints nothing, so
nothing is ever sent. Standard library only."""
import datetime
import json
import os
import subprocess
import urllib.request

HOME = "/opt/data"
DONE = os.path.join(HOME, ".rerun_after_outage")
OFF = ("model_server_off", "the AI computer is off", "LM Studio isn't running")   # the last: older outputs
UNTIL = {"weekly-digest": 24, "consolidate": 9}       # rerun only before this hour, the same day


def env():
    try:
        return dict(l.strip().split("=", 1) for l in open(os.path.join(HOME, ".env"))
                    if "=" in l and not l.startswith("#"))
    except OSError:
        return {}


def model_back(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return not json.load(r).get("model_server_off", True)
    except Exception:
        return False


def due(jobs, now, done):
    """Ids of jobs to run again: failed today because the model was off, still in time, not yet rerun."""
    out = []
    for job in jobs:
        until = UNTIL.get(job.get("name"))
        failure = job.get("last_failure") or {}
        if until is None or job.get("last_status") == "ok" or not any(o in str(failure.get("detail")) for o in OFF):
            continue
        try:
            failed = datetime.datetime.fromisoformat(failure["at"]).astimezone()
        except (KeyError, TypeError, ValueError):
            continue
        key = f"{job.get('id')} {failure['at']}"
        if failed.date() == now.date() and now.hour < until and key not in done:
            out.append((job["id"], key))
    return out


def main():
    try:
        data = json.load(open(os.path.join(HOME, "cron", "jobs.json")))
        jobs = data.get("jobs", data) if isinstance(data, dict) else data
        jobs = list(jobs.values()) if isinstance(jobs, dict) else list(jobs)
    except Exception:
        return
    done = set(open(DONE).read().splitlines()) if os.path.exists(DONE) else set()
    todo = due(jobs, datetime.datetime.now().astimezone(), done)
    if not todo or not model_back(env().get("LOGOS_MODEL_HEALTH_URL", "")):
        return
    for job_id, key in todo:
        subprocess.run(["hermes", "cron", "run", job_id], env={**os.environ, "HERMES_HOME": HOME},
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        with open(DONE, "a") as f:
            f.write(key + "\n")


if __name__ == "__main__":
    main()
