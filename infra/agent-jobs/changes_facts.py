"""Runs before each daily-changes job (story DC2). What it prints goes into the household agent's
prompt: the changes to Logos or the homelab, from the web reader's ``/changes`` (which asks
GitHub; the sandbox has no internet). When nothing changed it prints ``{"wakeAgent": false}``,
so the model is not woken and nothing is sent.

The same file is installed under four names, and the name says which list to fetch:
``changes_homelab.py``, ``changes_logos.py`` (the last day), ``changes_homelab_week.py``,
``changes_logos_week.py`` (the last seven days, on Sundays). hermes passes no arguments to a
job script, so the name is the argument. ``changes_check.py`` uses :func:`plain` for the
version it sends when the model could not write one. Standard library only; never fails."""
import json
import os
import sys
import urllib.request

SKIP = json.dumps({"wakeAgent": False})
LABEL = {"logos": "Logos", "homelab": "Homelab"}


def env():
    try:
        return dict(l.strip().split("=", 1) for l in open("/opt/data/.env") if "=" in l and not l.startswith("#"))
    except OSError:
        return {}


def which(path):
    """``changes_logos_week.py`` -> ("logos", 7)."""
    name = os.path.basename(path).rsplit(".", 1)[0]
    parts = name.split("_")[1:]
    repo = parts[0] if parts and parts[0] in LABEL else "logos"
    return repo, 7 if parts[-1:] == ["week"] else 1


def fetch(e, repo, days):
    """The reader's answer, or None when it cannot be reached."""
    try:
        req = urllib.request.Request(f"{e['LOGOS_CHANGES_URL']}/{repo}/{days}",
                                     headers={"Authorization": "Bearer " + e["LOGOS_CHANGES_TOKEN"]})
        return json.load(urllib.request.urlopen(req, timeout=60))
    except Exception:
        return None


def period(days):
    return "last 24 hours" if days == 1 else f"last {days} days"


def count(data):
    return len(data.get("prs") or []) + len(data.get("commits") or [])


def live_note(prs):
    """"all live" / "4 live, 2 not yet" / "" (unknown)."""
    marks = [p.get("live") for p in prs]
    if not marks or any(m is None for m in marks):
        return ""
    not_yet = marks.count(False)
    return "all live" if not not_yet else f"{len(marks) - not_yet} live, {not_yet} not yet"


def facts(data):
    """The lines given to the model."""
    repo, days = data["repo"], data["days"]
    prs, commits = data.get("prs") or [], data.get("commits") or []
    head = f"{LABEL[repo]}, {period(days)}"
    lines = [f"Repository: {head}. Write the heading as \"{head}\"."]
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


def plain(data):
    """The plain version: the heading and one line per change, from the titles."""
    repo, days = data["repo"], data["days"]
    prs, commits = data.get("prs") or [], data.get("commits") or []
    note = live_note(prs) if repo == "logos" else ""
    lines = [f"**{LABEL[repo]}, {period(days)}** ({count(data)} changes" + (f", {note})" if note else ")")]
    for p in prs:
        lines.append(f"- {p['title']}" + (" (not live yet)" if p.get("live") is False else ""))
    lines += [f"- {c['title']}" for c in commits]
    return "\n".join(lines)


def main(argv0=None):
    repo, days = which(argv0 or sys.argv[0])
    data = fetch(env(), repo, days)
    if not data or not count(data):
        print(SKIP)          # nothing changed, or the reader is down: changes_check.py tries again
        return
    print("\n".join(facts(data)))


if __name__ == "__main__":
    main()
