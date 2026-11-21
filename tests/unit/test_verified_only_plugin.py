"""M5: the agents answer only messages from devices their person has verified."""
import asyncio
import importlib.util
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2] / "infra" / "agent-plugins" / "logos-verified-only" / "__init__.py"


def load():
    spec = importlib.util.spec_from_file_location("logos_verified_only_test", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Event(dict):
    def __init__(self, event_id, trust=None):
        super().__init__({"mautrix": {"trust_state": trust}} if trust is not None else {})
        self.event_id = event_id


class Adapter:
    async def _resolve_message_context(self, room_id, sender, event_id, body="", *rest):
        return ("ok", body)


def test_only_messages_from_devices_cross_signed_by_their_owner_get_through():
    vo = load()
    vo.patch_adapter(Adapter)
    for eid, trust in (("$verified", 300), ("$tofu", 100), ("$unverified", 0), ("$unknown", 10), ("$forwarded", 20)):
        vo.note_trust(Event(eid, trust))
    run = lambda eid: asyncio.run(Adapter()._resolve_message_context("!r", "@greg:home", eid, "hi"))
    assert run("$verified") == ("ok", "hi") and run("$tofu") == ("ok", "hi")
    assert run("$unverified") is None and run("$unknown") is None and run("$forwarded") is None
    assert run("$never-decrypted") is None                  # unencrypted: the agents' rooms are all encrypted
    vo.patch_adapter(Adapter)                               # patching twice does not wrap twice
    assert run("$verified") == ("ok", "hi")


def test_off_unless_switched_on(monkeypatch):
    vo = load()
    monkeypatch.delenv("LOGOS_ONLY_VERIFIED_DEVICES", raising=False)
    assert not vo.enabled()
    monkeypatch.setenv("LOGOS_ONLY_VERIFIED_DEVICES", "1")
    assert vo.enabled()


def test_the_trust_record_stays_bounded():
    vo = load()
    for i in range(vo.MAX_TRUST + 50):
        vo.note_trust(Event(f"$e{i}", 300))
    assert len(vo.TRUST) <= vo.MAX_TRUST + 1
