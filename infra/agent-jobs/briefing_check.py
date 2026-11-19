"""Runs with no model, a quarter of an hour after the morning briefing (story C4). If the
briefing went out this morning it prints nothing (which sends nothing). If it did not (the
model server was busy or off, or delivery failed), it prints a plain version of the day, made
from the same facts, so a morning is never missed. Standard library only."""
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import briefing_today as bt  # noqa: E402

JOBS = ("morning-briefing", "morning-briefing-weekend")


def sent_today(jobs, today):
    """True when one of the briefing jobs ran today, succeeded and was delivered."""
    for job in jobs:
        if job.get("name") not in JOBS or job.get("last_status") != "ok" or job.get("last_delivery_error"):
            continue
        try:
            ran = datetime.datetime.fromisoformat(job["last_run_at"]).astimezone().date()
        except (KeyError, TypeError, ValueError):
            continue
        if ran == today:
            return True
    return False


def plain(today, e):
    sky, evs = bt.weather(e), bt.events(e, today)
    first = f"{today:%A} {today.day} {today:%B}." + (f" {sky}." if sky else "")
    lines = [first]
    if evs is None:
        lines.append("I couldn't read the calendar this morning.")
    else:
        mine = [ev for ev in evs if not ev.get("read_only")]
        for ev in mine:
            t = "" if ev.get("all_day") else ev["start"][-5:] + " "
            lines.append(f"{t}{ev['title']}" + (f", {ev['location']}" if ev.get("location") else ""))
        if not mine:
            lines.append("Nothing in the calendar today.")
    lines.append("(Plain version: I couldn't write this morning's briefing.)")
    return "\n".join(lines)


def main():
    today = datetime.date.today()
    try:
        data = json.load(open("/opt/data/cron/jobs.json"))
        jobs = data.get("jobs", data) if isinstance(data, dict) else data
        jobs = list(jobs.values()) if isinstance(jobs, dict) else list(jobs)
    except Exception:
        jobs = []
    if not sent_today(jobs, today):
        print(plain(today, bt.env()))


if __name__ == "__main__":
    main()
