"""Tests for infra/agent-jobs/brain_index.py (the script-only job that lists brain pages in
hermes's built-in memory)."""
import importlib.util
import os
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "infra" / "agent-jobs" / "brain_index.py"


@pytest.fixture()
def bi(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    spec = importlib.util.spec_from_file_location("brain_index", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for d in ("people", "projects", "topics", "inbox", "daily"):
        (mod.BRAIN / d).mkdir(parents=True)
    return mod


def page(mod, rel, text, mtime):
    p = mod.BRAIN / rel
    p.write_text(text)
    os.utime(p, (mtime, mtime))
    return p


def test_describe_uses_title_and_first_line_of_substance(bi):
    p = page(bi, "people/bere.md", "# Béré\n\nGreg's partner.\n\n- Birthday: 3 June\n", 100)
    assert bi.describe(p) == "Béré: Greg's partner."


def test_describe_falls_back_to_filename_and_strips_bullets(bi):
    p = page(bi, "projects/wedding-speech.md", "- **Status:** drafting\n", 100)
    assert bi.describe(p) == "wedding speech: Status: drafting"
    assert bi.describe(page(bi, "topics/empty.md", "", 100)) == "empty"


def test_index_lists_pages_newest_first_and_mentions_inbox(bi):
    page(bi, "people/bere.md", "# Béré\nPartner.\n", 100)
    page(bi, "projects/boiler.md", "# Boiler\nService booked.\n", 300)
    page(bi, "topics/wifi.md", "# Wifi\nRouter in the hall.\n", 200)
    page(bi, "inbox/2026-11-15.md", "- 22:00 something\n", 50)
    page(bi, "daily/2026-11-15.md", "# day\n", 50)
    text = bi.build_index(bi.BRAIN, 6000, 1_800_000_000)
    lines = text.splitlines()
    assert lines[0].startswith("BRAIN INDEX")
    assert [l.split(" — ")[0] for l in lines[1:4]] == ["projects/boiler.md", "topics/wifi.md", "people/bere.md"]
    assert "inbox/ has 1 day file(s), latest 2026-11-15." in text
    assert "daily/" not in text


def test_index_respects_budget_and_says_what_it_left_out(bi):
    for i in range(40):
        page(bi, f"topics/page-{i:02}.md", f"# Page {i}\n{'x' * 60}\n", 1000 + i)
    text = bi.build_index(bi.BRAIN, 900, 1_800_000_000)
    assert len(text) <= 900
    assert "older pages not listed" in text
    assert "topics/page-39.md" in text and "topics/page-00.md" not in text


def test_sync_conflict_copies_are_not_listed(bi):
    page(bi, "people/bere.md", "# Béré\n", 100)
    page(bi, "people/bere.sync-conflict-20261115-AAAA.md", "# Béré\n", 200)
    assert "sync-conflict" not in bi.build_index(bi.BRAIN, 6000, 1_800_000_000)


def test_empty_brain(bi):
    assert "(no pages yet)" in bi.build_index(bi.BRAIN, 6000, 1_800_000_000)


def test_merge_replaces_only_the_managed_entry(bi):
    existing = "User prefers short answers\n§\nBRAIN INDEX (automatic, old).\nold line\n§\nUses LM Studio\n"
    out = bi.merge(existing, "BRAIN INDEX (automatic, new).\npeople/bere.md — Béré")
    entries = [e.strip() for e in out.split("§")]
    assert entries == ["BRAIN INDEX (automatic, new).\npeople/bere.md — Béré", "User prefers short answers", "Uses LM Studio"]
    assert "old line" not in out


def test_main_writes_memory_and_is_idempotent(bi):
    page(bi, "people/bere.md", "# Béré\nPartner.\n", 100)
    bi.MEMORY.parent.mkdir(parents=True)
    bi.MEMORY.write_text("Agent's own note\n")
    assert bi.main() == 0
    first = bi.MEMORY.read_text()
    assert first.startswith("BRAIN INDEX") and "Agent's own note" in first and "people/bere.md — Béré: Partner." in first
    stamp = first.splitlines()[0]
    assert bi.main() == 0
    assert bi.MEMORY.read_text().splitlines()[1:] == first.splitlines()[1:]
    assert bi.MEMORY.read_text().count("BRAIN INDEX") == 1 and stamp.startswith("BRAIN INDEX")
