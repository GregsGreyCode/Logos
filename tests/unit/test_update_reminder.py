"""The reminder in Notifications that an update is ready (gateway/updates.reminder)."""
import asyncio

from gateway import updates


def run(app, plan, monkeypatch):
    calls = []

    def fake():
        calls.append(1)
        return plan
    monkeypatch.setattr(updates, "check_plan", fake)

    async def go():
        first = updates.reminder(app)                    # nothing known yet: checks in the background
        await asyncio.sleep(0.05)
        return first, updates.reminder(app), calls
    return asyncio.run(go())


def test_ready_update_is_a_needs_item_linking_to_updates(monkeypatch):
    app = {}
    plan = {"commits": [{"sha": "abc1234", "subject": "Lights brightness"}, {"sha": "def5678", "subject": "x"}]}
    first, second, calls = run(app, plan, monkeypatch)
    assert first is None and len(calls) == 1               # never waits on the check
    assert second["type"] == "needs" and second["link"] == "#/settings/updates"
    assert second["id"] == "logos-update:abc1234" and "2 changes" in second["text"] and "Lights brightness" in second["text"]


def test_nothing_when_up_to_date_or_refused(monkeypatch):
    assert run({}, {"commits": []}, monkeypatch)[1] is None
    assert run({}, {"refused": "local changes", "commits": [{"sha": "a"}]}, monkeypatch)[1] is None
