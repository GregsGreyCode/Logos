"""Keep what monitor jobs last saw when `logos-agent up` re-creates them.

`up` removes the managed cron jobs and creates them again. A new job has no monitor state, and
hermes treats its first run as a change, so every agent's noticeboard check woke the model once
after each deploy, for nothing (and on 19 Nov one such run wandered for 50 minutes). So:

    carry_monitor_state.py save      before the jobs are removed: name -> state + last output
    carry_monitor_state.py restore   after they are created: give each new job its old state

A real change since the last run still differs from the carried state, so it is still noticed.
Runs as the hermes user with hermes' own Python (it uses hermes' update_job, never edits
jobs.json by hand). Never fails `up`: on any error the new job just starts fresh.
"""
import json
import os
import sys

HOME = os.environ.get("HERMES_HOME", "/opt/data")
CARRY = os.path.join(HOME, "cron", ".monitor_carry.json")
OUTPUT = os.path.join(HOME, "cron", "output")
SNAPSHOT = "monitor_last_output.txt"


def jobs():
    with open(os.path.join(HOME, "cron", "jobs.json"), encoding="utf-8") as f:
        data = json.load(f)
    found = data.get("jobs", data) if isinstance(data, dict) else data
    return list(found.values()) if isinstance(found, dict) else list(found)


def save():
    carry = {}
    for job in jobs():
        state = job.get("monitor_state")
        if not (job.get("name") and isinstance(state, dict) and state.get("last_output_hash")):
            continue
        try:
            with open(os.path.join(OUTPUT, job["id"], SNAPSHOT), encoding="utf-8") as f:
                snapshot = f.read()
        except OSError:
            snapshot = ""
        carry[job["name"]] = {"state": state, "snapshot": snapshot}
    with open(CARRY, "w", encoding="utf-8") as f:
        json.dump(carry, f)
    print(f"carried monitor state: {', '.join(sorted(carry)) or 'none'}")


def restore():
    if not os.path.exists(CARRY):
        return
    with open(CARRY, encoding="utf-8") as f:
        carry = json.load(f)
    sys.path.insert(0, "/opt/hermes")
    from cron.jobs import update_job
    done = []
    for job in jobs():
        old = carry.get(job.get("name") or "")
        if not old or job.get("monitor_state"):
            continue
        folder = os.path.join(OUTPUT, job["id"])
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, SNAPSHOT), "w", encoding="utf-8") as f:
            f.write(old["snapshot"])
        update_job(job["id"], {"monitor_state": old["state"]})
        done.append(job["name"])
    os.remove(CARRY)
    print(f"restored monitor state: {', '.join(sorted(done)) or 'none'}")


if __name__ == "__main__":
    try:
        {"save": save, "restore": restore}[sys.argv[1]]()
    except Exception as e:          # never fail `up`; the job then simply starts fresh
        print(f"monitor state not carried: {type(e).__name__}: {e}")
