"""Runs before each daily-changes job (story DC2). What it prints goes into the household agent's
prompt: what changed in Logos and in the homelab, from the web reader's ``/changes`` (which asks
GitHub; the sandbox has no internet). When nothing changed in either, it prints
``{"wakeAgent": false}``, so the model is not woken and nothing is sent.

One post covers both: the same file is installed as ``changes_daily.py`` (the last day) and
``changes_week.py`` (the last seven days, on Sundays). hermes passes no arguments to a job
script, so the name is the argument. ``changes_check.py`` uses :func:`plain` for the version it
sends when the model could not write one. Standard library only; never fails."""
import json
import os
import re
import sys
import urllib.request

SKIP = json.dumps({"wakeAgent": False})
#: Today's posts that skipped because the reader could not be reached (not because nothing
#: changed), so changes_check.py knows to try them again whatever hermes records for the skip.
UNFETCHED = "/opt/data/cron/changes-unfetched.json"
REPOS = ("logos", "homelab")
#: How people know each part (docs/messages.md: no repository names).
LABEL = {"logos": "Logos", "homelab": "Homelab"}           # Greg, 20 Nov: the names, not "the home servers"


def env():
    try:
        return dict(l.strip().split("=", 1) for l in open("/opt/data/.env") if "=" in l and not l.startswith("#"))
    except OSError:
        return {}


def which(path):
    """``changes_week.py`` -> 7, ``changes_daily.py`` -> 1."""
    name = os.path.basename(path).rsplit(".", 1)[0]
    return 7 if name.endswith("week") else 1


def fetch(e, repo, days):
    """The reader's answer, or None when it cannot be reached."""
    try:
        req = urllib.request.Request(f"{e['LOGOS_CHANGES_URL']}/{repo}/{days}",
                                     headers={"Authorization": "Bearer " + e["LOGOS_CHANGES_TOKEN"]})
        return json.load(urllib.request.urlopen(req, timeout=60))
    except Exception:
        return None


def heading(days):
    return "What changed today" if days == 1 else "What changed this week"


def count(data):
    return len(data.get("prs") or []) + len(data.get("commits") or [])


def live_note(prs):
    """"all live" / "4 live, 2 not yet" / "" (unknown)."""
    marks = [p.get("live") for p in prs]
    if not marks or any(m is None for m in marks):
        return ""
    not_yet = marks.count(False)
    return "all live" if not not_yet else f"{len(marks) - not_yet} live, {not_yet} not yet"


CONVENTIONAL = re.compile(r"^(feat|fix|refactor|docs|test|chore|build|ci|perf|style)(\([^)]*\))?!?:\s*", re.I)
RENOVATE = re.compile(r"^(?:chore\(deps\):\s*)?update (?:docker tag |image |dependency )?(.+?)(?: docker tag)? to v?([\w.\-]+)$", re.I)


def clean_title(title):
    """A commit or PR title as a person would say it: 'chore(deps): update jellyfin docker tag to
    v10.10.4' -> 'Jellyfin updated to 10.10.4'; 'feat(h1): lights for the agent' -> 'Lights for the agent'."""
    t = str(title or "").strip()
    m = RENOVATE.match(t)
    if m:
        app = m.group(1).rsplit("/", 1)[-1].strip()
        return f"{app[:1].upper()}{app[1:]} updated to {m.group(2)}"
    t = CONVENTIONAL.sub("", t)
    return t[:1].upper() + t[1:]


def facts_one(data):
    """The lines given to the model for one part."""
    repo = data["repo"]
    prs, commits = data.get("prs") or [], data.get("commits") or []
    lines = [f"## {LABEL[repo]} ({'the agents' if repo == 'logos' else 'the home servers'})"]
    if repo == "logos":
        note = live_note(prs)
        lines.append(f"{len(prs)} merged pull requests" + (f" ({note} on the Logos server)." if note else "."))
        for p in prs:
            mark = {True: "live", False: "NOT live yet", None: "live: unknown"}[p.get("live")]
            lines.append(f"- #{p['number']} {p['title']} [{mark}]")
            if p.get("summary"):
                lines += ["    " + l for l in p["summary"].splitlines()]
    else:
        lines.append(f"{len(commits)} commits and {len(prs)} merged pull requests.")
        for p in prs:
            who = " (Renovate: an automatic image update)" if "renovate" in (p.get("author") or "") else ""
            lines.append(f"- PR #{p['number']} {p['title']}{who}")
        for c in commits:
            who = " (Renovate)" if "renovate" in (c.get("author") or "") else ""
            lines.append(f"- {c['title']}{who}")
            if c.get("detail"):
                lines += ["    " + l for l in c["detail"].splitlines()]
    return lines


def facts(parts, days):
    """The lines given to the model: the heading to use, then each part that changed."""
    head = heading(days)
    lines = [f'Write the first line, in bold, as "{head}". Parts that changed: '
             + ", ".join(LABEL[d["repo"]] for d in parts) + "."]
    for d in parts:
        lines += [""] + facts_one(d)
    return lines


def plain(parts, days):
    """The plain version: the heading, then each part with one line per change."""
    lines = [f"**{heading(days)}**"]
    for d in parts:
        prs, commits = d.get("prs") or [], d.get("commits") or []
        lines += ["", LABEL[d["repo"]]]
        lines += [f"- {clean_title(p['title'])}" + (" (not live yet)" if p.get("live") is False else "") for p in prs]
        lines += [f"- {clean_title(c['title'])}" for c in commits]
    return "\n".join(lines)


def gather(e, days, fetch_one=None, failed=None):
    """The parts that changed, in a fixed order; parts that could not be fetched are left out
    (and named in ``failed`` when a list is given)."""
    fetch_one = fetch_one or fetch
    out = []
    for repo in REPOS:
        data = fetch_one(e, repo, days)
        if data is None and failed is not None:
            failed.append(repo)
        if data and count(data):
            out.append(data)
    return out


def job_name(path):
    """``changes_week.py`` -> ``changes-week``."""
    return os.path.basename(path).rsplit(".", 1)[0].replace("_", "-")


def unfetched(path=UNFETCHED):
    try:
        return json.load(open(path))
    except Exception:
        return {}


def mark_unfetched(name, path=UNFETCHED, today=None):
    import datetime
    marks = unfetched(path)
    marks[name] = (today or datetime.date.today()).isoformat()
    try:
        with open(path, "w") as f:
            json.dump(marks, f)
    except OSError:
        pass


def main(argv0=None, mark_path=UNFETCHED):
    path = argv0 or sys.argv[0]
    days = which(path)
    failed = []
    parts = gather(env(), days, failed=failed)
    if not parts:
        if failed:
            mark_unfetched(job_name(path), mark_path)   # not "nothing changed": changes_check.py tries again
        print(SKIP)
        return
    print("\n".join(facts(parts, days)))


if __name__ == "__main__":
    main()
