"""An agent's reply says what it wrote in the phone alert (gateway/alert_text.py, logos-alert-text)."""
import asyncio
import importlib.util
import json
import stat
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway import alert_text as at
from gateway import push

PLUGIN = Path(__file__).resolve().parents[2] / "infra" / "agent-plugins" / "logos-alert-text" / "__init__.py"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return tmp_path


def test_each_agent_has_its_own_token_and_the_key_stays_private(home):
    g, h = at.token_for("greg"), at.token_for("household")
    assert g != h and at.token_for("greg") == g
    assert at.valid("greg", g) and not at.valid("household", g) and not at.valid("greg", "") and not at.valid("", g)
    mode = stat.S_IMODE((home / "logos" / at.KEY_FILE).stat().st_mode)
    assert mode == 0o600


def test_only_its_own_messages():
    assert at.sender_is("greg", "@greg-agent:logos.local")
    assert not at.sender_is("greg", "@greg:logos.local")                 # Greg himself
    assert not at.sender_is("greg", "@household-agent:logos.local")
    assert not at.sender_is("gr", "@greg-agent:logos.local")


def test_the_words_a_phone_shows():
    assert at.shorten("**Bins** go out\n\ntonight") == "Bins go out tonight"
    assert at.shorten("# Plan\n- one\n- two") == "Plan one two"
    long = "word " * 100
    s = at.shorten(long)
    assert len(s) <= at.SHOWN + 1 and s.endswith("…") and not s.endswith(" …")


def test_kept_for_two_minutes_then_forgotten():
    now = [0.0]
    k = at.Kept(clock=lambda: now[0])
    k.put("$e", "greg", "Hello")
    assert k.get("$e") == ("greg", "Hello")
    now[0] = at.KEEP_S + 1
    assert k.get("$e") is None and k.items == {}


def test_waits_for_words_that_arrive_just_after_the_push():
    async def run():
        k = at.Kept()
        asyncio.get_running_loop().call_later(0.05, k.put, "$e", "greg", "Your dentist is at 10:30")
        return await at.wait_words(k, "$e", "@greg-agent:x", timeout=1)
    assert asyncio.run(run()) == "Your dentist is at 10:30"


def test_a_persons_message_never_waits_and_a_late_note_gives_up():
    async def run():
        k = at.Kept()
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        person = await at.wait_words(k, "$p", "@greg:x", timeout=5)
        fast = loop.time() - t0
        late = await at.wait_words(k, "$q", "@greg-agent:x", timeout=0.1)
        return person, fast, late, k.waiting
    person, fast, late, waiting = asyncio.run(run())
    assert person is None and fast < 0.05 and late is None and waiting == {}


def test_the_endpoint_takes_only_a_signed_note(home):
    app = web.Application()
    at.register(app)
    good = {"agent": "greg", "token": at.token_for("greg"), "event_id": "$e1", "text": "Done: **3 items** added"}
    bodies = [good, {**good, "event_id": "$e2", "token": at.token_for("household")},
              {**good, "event_id": "$e3", "agent": "household"}, {**good, "event_id": "not-an-event"}, {"agent": "greg"}]

    async def run():
        async with TestClient(TestServer(app)) as client:
            return [(await client.post(at.PATH, json=b)).status for b in bodies]
    assert asyncio.run(run()) == [200, 403, 403, 400, 400]
    assert app["alert_text"].get("$e1") == ("greg", "Done: 3 items added")
    assert app["alert_text"].get("$e2") is None and app["alert_text"].get("$e3") is None


# ── the alert ────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def app(tmp_path, monkeypatch):
    sent = []

    async def fake_send(session, sub, message, key):
        sent.append((sub["username"], message))
        return 201
    monkeypatch.setattr(push, "send", fake_send)
    monkeypatch.setattr(push, "alerts_private", lambda who: who == "bere")
    monkeypatch.setattr(at, "WAIT_S", 0.2)
    a = web.Application()
    push.register(a, tmp_path)
    at.register(a)
    a["sent"] = sent
    return a


def notify(app, n, keys):
    async def run():
        async with TestClient(TestServer(app)) as client:
            await client.post(push.NOTIFY_PATH, json={"notification": {**n, "devices": [
                {"app_id": "logos.web", "pushkey": k} for k in keys]}})
    asyncio.run(run())
    return dict(app["sent"])


def test_an_agents_reply_says_what_it_wrote_except_to_private_phones(app):
    g = app["push_store"].add("greg", "https://push.example/1", "p", "a", "Pixel")
    b = app["push_store"].add("bere", "https://push.example/2", "p", "a", "Pixel")
    app["alert_text"].put("$e", "household", "The green bin goes out tonight")
    by = notify(app, {"event_id": "$e", "room_id": "!g", "room_name": "Household Group", "sender": "@household-agent:x",
                      "sender_display_name": "Household agent", "counts": {"unread": 1}}, [g, b])
    assert by["greg"]["title"] == "Household Group"
    assert by["greg"]["body"] == "Household agent: The green bin goes out tonight"
    assert by["bere"]["title"] == "Logos" and "bin" not in json.dumps(by["bere"])


def test_words_from_another_agent_are_not_used(app):
    g = app["push_store"].add("greg", "https://push.example/1", "p", "a", "Pixel")
    app["alert_text"].put("$e", "household", "Pretending to be Greg's agent")
    by = notify(app, {"event_id": "$e", "room_id": "!r", "sender": "@greg-agent:x",
                      "sender_display_name": "Greg's agent", "counts": {"unread": 1}}, [g])
    assert by["greg"]["body"] == "New message"


def test_without_a_note_it_says_new_message_as_before(app):
    g = app["push_store"].add("greg", "https://push.example/1", "p", "a", "Pixel")
    by = notify(app, {"event_id": "$x", "room_id": "!r", "sender": "@greg-agent:x",
                      "sender_display_name": "Greg's agent", "counts": {"unread": 1}}, [g])
    assert by["greg"] == {"title": "Greg's agent", "body": "New message", "room": "!r", "link": "#/chat/room/!r", "unread": 1}


# ── the agent's plugin ───────────────────────────────────────────────────────────────────────

def load_plugin():
    spec = importlib.util.spec_from_file_location("logos_alert_text_plugin", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_plugin_hands_over_text_replies_after_sending(monkeypatch):
    p = load_plugin()
    notes, order = [], []
    monkeypatch.setattr(p, "note", lambda event_id, text, conf: notes.append((event_id, text, conf["LOGOS_AGENT_NAME"])))

    class Adapter:
        async def _send_room_message(self, chat_id, content):
            order.append("sent")
            return "$new"

    conf = {p.TOKEN_ENV: "t", p.URL_ENV: "http://x/api/v2/alert-text", p.AGENT_ENV: "greg"}
    p.patch(Adapter, conf)

    async def run():
        a = Adapter()
        out = [await a._send_room_message("!r", {"msgtype": "m.text", "body": "Hello"}),
               await a._send_room_message("!r", {"msgtype": "m.image", "body": "photo.jpg"}),
               await a._send_room_message("!r", {"msgtype": "m.text", "body": "* edit",
                                                 "m.relates_to": {"rel_type": "m.replace", "event_id": "$new"}})]
        await asyncio.sleep(0.05)
        return out
    assert asyncio.run(run()) == ["$new"] * 3
    assert notes == [("$new", "Hello", "greg")]


def test_the_plugin_does_nothing_without_its_settings():
    p = load_plugin()

    class Adapter:
        async def _send_room_message(self, chat_id, content):
            return "$e"
    before = Adapter._send_room_message
    p.patch(Adapter, {p.TOKEN_ENV: "", p.URL_ENV: "", p.AGENT_ENV: ""})
    assert Adapter._send_room_message is before


def test_a_failed_note_never_raises():
    p = load_plugin()
    assert p.note("$e", "hi", {p.TOKEN_ENV: "t", p.URL_ENV: "http://127.0.0.1:9/nothing", p.AGENT_ENV: "greg"}, timeout=0.5) is False
