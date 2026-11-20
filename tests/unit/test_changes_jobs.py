"""The daily-changes sandbox scripts (infra/agent-jobs/changes_*.py, story DC2)."""
import datetime
import importlib.util
import json
import sys
from pathlib import Path

JOBS = Path(__file__).resolve().parents[2] / "infra" / "agent-jobs"
SUNDAY = datetime.date(2026, 11, 22)
THURSDAY = datetime.date(2026, 11, 19)

LOGOS = {"repo": "logos", "days": 1, "running": "23a81e4", "prs": [
    {"number": 104, "title": "Morning briefing in Element X", "live": True, "author": "greg",
     "summary": "Sends the day ahead at 07:00.\n- weather"},
    {"number": 106, "title": "Model downtime D2", "live": False, "author": "greg"}]}
HOMELAB = {"repo": "homelab", "days": 1, "prs": [
    {"number": 5, "title": "Update Jellyfin to 10.10.4", "author": "renovate[bot]"}],
    "commits": [{"sha": "2ebe5e0", "title": "stack-update: automatic rollback", "author": "GregsGreyCode",
                 "detail": "One service at a time."}]}


def load(name):
    sys.path.insert(0, str(JOBS))
    try:
        spec = importlib.util.spec_from_file_location(name, JOBS / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        sys.modules[name] = mod
        return mod
    finally:
        sys.path.remove(str(JOBS))


cf = load("changes_facts")
cc = load("changes_check")


def test_the_script_name_says_how_many_days():
    assert cf.which("/opt/data/scripts/changes_daily.py") == 1
    assert cf.which("changes_week.py") == 7


def test_facts_cover_both_parts_with_live_marks_and_descriptions():
    lines = cf.facts([LOGOS, HOMELAB], 1)
    assert lines[0] == ('Write the first line, in bold, as "What changed today". '
                        "Parts that changed: Logos, Homelab.")
    assert "## Logos (the agents)" in lines and "## Homelab (the home servers)" in lines
    assert "2 merged pull requests (1 live, 1 not yet on the Logos server)." in lines
    assert "- #104 Morning briefing in Element X [live]" in lines
    assert "    Sends the day ahead at 07:00." in lines
    assert "- #106 Model downtime D2 [NOT live yet]" in lines
    assert cf.facts([HOMELAB], 7)[0].startswith('Write the first line, in bold, as "What changed this week"')


def test_homelab_facts_mark_renovate():
    lines = cf.facts_one(HOMELAB)
    assert lines[1] == "1 commits and 1 merged pull requests."
    assert "- PR #5 Update Jellyfin to 10.10.4 (Renovate: an automatic image update)" in lines
    assert "- stack-update: automatic rollback" in lines and "    One service at a time." in lines


def test_clean_titles():
    assert cf.clean_title("chore(deps): update jellyfin/jellyfin docker tag to v10.10.4") == "Jellyfin updated to 10.10.4"
    assert cf.clean_title("Update Jellyfin to 10.10.4") == "Jellyfin updated to 10.10.4"
    assert cf.clean_title("feat(h1): household agent works the lights") == "Household agent works the lights"
    assert cf.clean_title("Morning briefing in Element X") == "Morning briefing in Element X"


def test_plain_version():
    assert cf.plain([LOGOS, HOMELAB], 1) == (
        "**What changed today**\n\nLogos\n- Morning briefing in Element X\n- Model downtime D2 (not live yet)"
        "\n\nHomelab\n- Jellyfin updated to 10.10.4\n- Stack-update: automatic rollback")
    assert cf.plain([HOMELAB], 7).startswith("**What changed this week**\n\nHomelab")


def test_nothing_changed_or_reader_down_skips_the_model(monkeypatch, capsys):
    monkeypatch.setattr(cf, "env", lambda: {})
    for answer in (None, {"repo": "logos", "days": 1, "prs": []}):
        monkeypatch.setattr(cf, "fetch", lambda e, r, d, a=answer: a)
        cf.main("changes_daily.py")
        assert json.loads(capsys.readouterr().out) == {"wakeAgent": False}
    monkeypatch.setattr(cf, "fetch", lambda e, r, d: {"logos": LOGOS, "homelab": None}[r])
    cf.main("changes_daily.py")
    out = capsys.readouterr().out
    assert "#104" in out and "Homelab" not in out           # a part that could not be fetched is left out


def job(name, status="ok", when="2026-11-19T16:01:00+00:00", err=None):
    return {"name": name, "last_status": status, "last_run_at": when, "last_delivery_error": err}


def test_check_sends_the_plain_list_only_for_posts_that_did_not_go_out():
    asked = []

    def fetch(e, repo, days):
        asked.append((repo, days))
        return {"logos": LOGOS, "homelab": HOMELAB}[repo]

    assert cc.report([job("changes-daily")], THURSDAY, {}, fetch) == "" and not asked
    out = cc.report([job("changes-daily", "error")], THURSDAY, {}, fetch)
    assert asked == [("logos", 1), ("homelab", 1)] and out.startswith("**What changed today**")
    assert "Plain" not in out and "couldn't" not in out             # no system-speak footer
    asked.clear()
    cc.report([job("changes-daily", when="2026-11-18T16:01:00+00:00")], THURSDAY, {}, fetch)
    assert asked == [("logos", 1), ("homelab", 1)]                   # yesterday's run does not count
    asked.clear()
    cc.report([job("changes-daily", err="matrix down")], THURSDAY, {}, fetch)
    assert asked == [("logos", 1), ("homelab", 1)]
    asked.clear()
    cc.report([job("changes-daily", when="2026-11-22T16:01:00+00:00")], SUNDAY, {}, fetch)
    assert asked == [("logos", 7), ("homelab", 7)]                   # Sunday: the week's post counts too
    assert cc.report([], THURSDAY, {}, lambda e, r, d: None) == ""
