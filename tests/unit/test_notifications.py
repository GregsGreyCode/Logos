"""Notifications (Greg, 21 Nov): the AI computer's on/off record, and what you have seen, per person."""
import json
import types

from gateway import dashboard_api as d


def fake_app(tmp_path):
    runtime = types.SimpleNamespace(env={"HOME": str(tmp_path)})
    app = {"sandbox_backend": object()}
    return app, runtime


def test_ai_record_keeps_changes_only_and_survives_a_restart(tmp_path, monkeypatch):
    app, runtime = fake_app(tmp_path)
    monkeypatch.setattr(d.agent_chat, "runtime_of", lambda backend: runtime)
    d.note_ai(app, True)                     # the first look is not a change
    assert d.ai_events(app) == []
    d.note_ai(app, True)
    d.note_ai(app, False)
    d.note_ai(app, False)
    d.note_ai(app, True)
    assert [e["up"] for e in d.ai_events(app)] == [False, True]
    saved = json.loads((tmp_path / ".config" / "logos" / "ai-events.json").read_text())
    assert [e["up"] for e in saved] == [False, True]
    again = {"sandbox_backend": object()}     # a restart: read back, and a first look that matches adds nothing
    d.note_ai(again, True)
    assert [e["up"] for e in d.ai_events(again)] == [False, True]
    d.note_ai(again, False)
    assert [e["up"] for e in d.ai_events(again)] == [False, True, False]


def test_seen_until_lives_beside_the_settings(monkeypatch):
    rows = {"u1": {"ui_prefs": json.dumps({"motion": "calm"})}}
    db = types.SimpleNamespace(get_user_settings=lambda uid: rows.get(uid),
                               update_user_settings=lambda uid, ui_prefs: rows.__setitem__(uid, {"ui_prefs": ui_prefs}))
    monkeypatch.setattr(d, "auth_db", db)
    assert d._seen_until("u1") == 0.0
    d._set_seen_until("u1", 1234.5)
    assert d._seen_until("u1") == 1234.5
    assert json.loads(rows["u1"]["ui_prefs"])["motion"] == "calm"      # the settings are kept
    assert d.prefs_of("u1") == {"motion": "calm"}                      # and seen_until is not one of them
