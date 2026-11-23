"""Settings ends with which Logos this is (dashboard_api.version)."""
import re

from gateway import dashboard_api


def test_version_is_this_checkouts_commit_and_date(monkeypatch):
    monkeypatch.setattr(dashboard_api, "_VERSION", None)
    v = dashboard_api.version()
    assert re.fullmatch(r"[0-9a-f]{7,12}", v["commit"]) and v["date"] > 1_700_000_000
    assert dashboard_api.version() is v                 # read once per start


def test_without_git_it_is_simply_left_out(monkeypatch):
    monkeypatch.setattr(dashboard_api, "_VERSION", None)

    def no_git(*a, **k):
        raise OSError("no git")
    monkeypatch.setattr(dashboard_api.subprocess, "run", no_git)
    assert dashboard_api.version() == {}
