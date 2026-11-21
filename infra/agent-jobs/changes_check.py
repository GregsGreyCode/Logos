"""Runs with no model at 16:20, after the daily-changes jobs (story DC2). For each of today's
changes posts that did not run, failed or was not delivered (the AI computer was off, say), it
sends the plain list of what changed, made from the same facts. A job that found nothing, or ran
and was delivered, adds nothing; when all of them did, this prints nothing (which sends
nothing). The weekly post counts only on Sundays. Standard library only."""
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import changes_facts as cf  # noqa: E402

DAILY = {"changes-daily": 1}
WEEKLY = {"changes-week": 7}


def done_today(jobs, name, today, marks=None):
    if (marks or {}).get(name) == today.isoformat():
        return False                     # it skipped because the reader was down, not because nothing changed
    for job in jobs:
        if job.get("name") != name or job.get("last_status") != "ok" or job.get("last_delivery_error"):
            continue
        try:
            ran = datetime.datetime.fromisoformat(job["last_run_at"]).astimezone().date()
        except (KeyError, TypeError, ValueError):
            continue
        if ran == today:
            return True
    return False


def due(today, jobs=()):
    """Today's posts. The weekly one only on Sundays, and only when it is installed
    (``changes: {weekly: false}`` leaves it out)."""
    weekly = today.weekday() == 6 and any(j.get("name") in WEEKLY for j in jobs)
    return {**DAILY, **(WEEKLY if weekly else {})}


def report(jobs, today, e, fetch=cf.fetch, marks=None):
    posts = []
    for name, days in due(today, jobs).items():
        if done_today(jobs, name, today, marks):
            continue
        parts = cf.gather(e, days, fetch)
        if parts:
            posts.append(cf.plain(parts, days))
    return "\n\n".join(posts)


def main():
    today = datetime.date.today()
    try:
        data = json.load(open("/opt/data/cron/jobs.json"))
        jobs = data.get("jobs", data) if isinstance(data, dict) else data
        jobs = list(jobs.values()) if isinstance(jobs, dict) else list(jobs)
    except Exception:
        jobs = []
    out = report(jobs, today, cf.env(), marks=cf.unfetched())
    if out:
        print(out)


if __name__ == "__main__":
    main()
