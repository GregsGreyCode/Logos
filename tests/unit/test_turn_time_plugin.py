"""logos-turn-time: the agent is told when hours passed since the last message (Greg, 21 Nov)."""
import importlib.util
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2] / "infra" / "agent-plugins" / "logos-turn-time" / "__init__.py"
spec = importlib.util.spec_from_file_location("logos_turn_time_test", PLUGIN)
tt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tt)

NOW = 1_800_000_000.0


def test_four_hours_later_gets_a_note(monkeypatch):
    monkeypatch.setenv("HERMES_TIMEZONE", "Europe/London")
    history = [{"role": "assistant", "content": "Shall I add these?", "timestamp": NOW - 4 * 3600},
               {"role": "user", "content": "what is brain freeze", "timestamp": NOW}]
    out = tt.note(conversation_history=history, now=NOW)
    assert "about 4 hours after the last message" in out["context"]
    assert "Treat it as new" in out["context"]


def test_a_quick_reply_gets_none():
    history = [{"role": "assistant", "content": "Shall I add these?", "timestamp": NOW - 120},
               {"role": "user", "content": "yes", "timestamp": NOW}]
    assert tt.note(conversation_history=history, now=NOW) is None


def test_no_stamps_or_no_history_gets_none():
    assert tt.note(conversation_history=[{"role": "user", "content": "hi"}], now=NOW) is None
    assert tt.note(conversation_history=None, now=NOW) is None
