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


def test_the_script_name_says_which_list():
    assert cf.which("/opt/data/scripts/changes_logos.py") == ("logos", 1)
    assert cf.which("changes_homelab.py") == ("homelab", 1)
    assert cf.which("changes_logos_week.py") == ("logos", 7)
    assert cf.which("changes_homelab_week.py") == ("homelab", 7)


def test_logos_facts_carry_live_marks_and_descriptions():
    lines = cf.facts(LOGOS)
    assert lines[0] == 'Repository: Logos, last 24 hours. Write the heading as "Logos, last 24 hours".'
    assert lines[1] == "2 merged pull requests (1 live, 1 not yet on the Logos server)."
    assert "- #104 Morning briefing in Element X [live]" in lines
    assert "    Sends the day ahead at 07:00." in lines
    assert "- #106 Model downtime D2 [NOT live yet]" in lines


def test_homelab_facts_mark_renovate():
    lines = cf.facts(HOMELAB)
    assert lines[1] == "1 commits and 1 merged pull requests."
    assert "- PR #5 Update Jellyfin to 10.10.4 (Renovate: an automatic image update)" in lines
    assert "- stack-update: automatic rollback" in lines and "    One service at a time." in lines


def test_plain_version():
    assert cf.plain(LOGOS) == ("**Logos, last 24 hours** (2 changes, 1 live, 1 not yet)\n"
                               "- Morning briefing in Element X\n- Model downtime D2 (not live yet)")
    assert cf.plain({**LOGOS, "prs": LOGOS["prs"][:1]}).startswith("**Logos, last 24 hours** (1 changes, all live)")
    assert cf.plain({**HOMELAB, "days": 7}).startswith("**Homelab, last 7 days** (2 changes)")


def test_nothing_changed_or_reader_down_skips_the_model(monkeypatch, capsys):
    monkeypatch.setattr(cf, "env", lambda: {})
    for answer in (None, {"repo": "logos", "days": 1, "prs": []}):
        monkeypatch.setattr(cf, "fetch", lambda e, r, d, a=answer: a)
        cf.main("changes_logos.py")
        assert json.loads(capsys.readouterr().out) == {"wakeAgent": False}
    monkeypatch.setattr(cf, "fetch", lambda e, r, d: LOGOS)
    cf.main("changes_logos.py")
    assert "#104" in capsys.readouterr().out


def job(name, status="ok", when="2026-11-19T16:01:00+00:00", err=None):
    return {"name": name, "last_status": status, "last_run_at": when, "last_delivery_error": err}


def test_check_sends_the_plain_list_only_for_jobs_that_did_not_go_out():
    asked = []

    def fetch(e, repo, days):
        asked.append((repo, days))
        return {"logos": LOGOS, "homelab": HOMELAB}[repo]

    ok = [job("changes-homelab"), job("changes-logos")]
    assert cc.report(ok, THURSDAY, {}, fetch) == "" and not asked
    out = cc.report([job("changes-homelab"), job("changes-logos", "error")], THURSDAY, {}, fetch)
    assert asked == [("logos", 1)] and out.startswith("**Logos, last 24 hours**")
    assert out.endswith("(Plain list: I couldn't write the summary this afternoon.)")
    asked.clear()
    stale = [job("changes-homelab", when="2026-11-18T16:01:00+00:00"), job("changes-logos", err="matrix down")]
    cc.report(stale, THURSDAY, {}, fetch)
    assert asked == [("homelab", 1), ("logos", 1)]
    asked.clear()
    cc.report([job("changes-homelab", when="2026-11-22T16:01:00+00:00"),
               job("changes-logos", when="2026-11-22T16:06:00+00:00")], SUNDAY, {}, fetch)
    assert asked == [("homelab", 7), ("logos", 7)]          # Sunday: the week's roll-ups count too
    assert cc.report([], THURSDAY, {}, lambda e, r, d: None) == ""
