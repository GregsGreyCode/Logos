"""Agents speak up on the house speaker (Voice V3.1): gateway/speaker_say.py, its plugin and its wiring."""
import asyncio
import importlib.util
import json
import logging
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway import presence, speaker_say as ss, speaker_voice as sv, tts

REPO = Path(__file__).resolve().parents[2]
HOME_VOICE = "en_GB-northern_english_male-medium"
FINN = {"name": "greg", "speaker_voice": "en_GB-alan-medium", "speaker_name": "Finn", "owners": ["greg"],
        "timezone": "Europe/London"}
HOUSEHOLD = {"name": "household", "role": "household", "speaker_voice": HOME_VOICE, "owners": ["greg", "bere"]}
DAY = datetime(2026, 11, 27, 18, 30)
NIGHT = datetime(2026, 11, 27, 23, 15)


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / ".config/logos").mkdir(parents=True)
    (tmp_path / ".config/logos/speaker").write_text(
        "TTS_BIND=192.0.2.20\nANNOUNCE_ENTITY=assist_satellite.test_speaker_assist_satellite\n")
    (tmp_path / ".local/state/logos/speaker").mkdir(parents=True)
    e = {"HOME": str(tmp_path)}
    monkeypatch.setattr(sv, "_choices", lambda: {})
    monkeypatch.setattr(sv, "_definition", lambda app, agent: {"greg": FINN, "household": HOUSEHOLD}.get(agent, {}))
    sv._cache.clear(), sv._said.clear()
    presence._cache.clear()
    yield e
    sv._cache.clear(), sv._said.clear()


class Speaker:
    """Home Assistant's assist_satellite.announce, faked: what it was asked to say."""

    def __init__(self, fail=None):
        self.said, self.fail = [], fail

    def __call__(self, entity, message, chime, env=None):
        if self.fail:
            raise ss.SayError(self.fail)
        self.said.append((entity, message, chime))


def app_with(speaker, clock=None):
    return {"speaker_say_limits": ss.Limits(clock or (lambda: 1000.0)), "speaker_say_announce": speaker}


def say(app, agent, definition, text, env, private=False, person=None, now=DAY):
    return asyncio.run(ss.decide(app, agent, definition, text, private, person, env=env, now=now))


def test_a_reminder_is_said_in_the_agents_own_voice(env, caplog):
    speaker = Speaker()
    caplog.set_level(logging.INFO, logger="gateway.speaker_say")
    out = say(app_with(speaker), "greg", FINN, "Greg, it's time to call the plumber.", env)
    assert out == {"spoken": True}
    entity, message, chime = speaker.said[0]
    assert entity == "assist_satellite.test_speaker_assist_satellite" and chime is True
    assert tts.unmark(message) == ("Greg, it's time to call the plumber.", "en_GB-alan-medium")
    # and the container meeting those words without their mark would still know whose they are
    state = json.loads((Path(env["HOME"]) / ".local/state/logos/speaker/said.json").read_text())
    assert tts.choose("Greg, it's time to call the plumber.", None, state)[0] == "en_GB-alan-medium"
    # the log has who and how long, never the words
    assert "plumber" not in caplog.text and "greg: spoken (36 characters)" in caplog.text


def test_french_is_said_in_the_french_voice(env):
    speaker = Speaker()
    say(app_with(speaker), "household", HOUSEHOLD, "Béré, n'oublie pas d'appeler ta mère.", env)
    assert tts.unmark(speaker.said[0][1])[1] == "fr_FR-tom-medium"


def test_nothing_is_said_in_quiet_hours(env):
    speaker = Speaker()
    out = say(app_with(speaker), "greg", FINN, "The bins go out tonight.", env, now=NIGHT)
    assert out["spoken"] is False and out["reason"] == "quiet_hours" and "chat" in out["tell"]
    assert "22:00 to 08:00" in out["tell"] and speaker.said == []
    assert say(app_with(speaker), "greg", FINN, "x y", env, now=datetime(2026, 11, 28, 7, 59))["reason"] == "quiet_hours"
    assert say(app_with(speaker), "greg", FINN, "x y", env, now=datetime(2026, 11, 28, 8, 0))["spoken"] is True


def test_quiet_hours_can_be_changed_or_switched_off(env):
    path = Path(env["HOME"]) / ".config/logos/speaker"
    path.write_text(path.read_text() + "QUIET_HOURS=21:30-07:00\n")
    assert ss.settings(env)["quiet"] == ((21, 30), (7, 0))
    assert ss.is_quiet(ss.settings(env)["quiet"], datetime(2026, 1, 1, 21, 45))
    assert not ss.is_quiet(ss.settings(env)["quiet"], datetime(2026, 1, 1, 7, 30))
    assert ss.is_quiet(((13, 0), (14, 0)), datetime(2026, 1, 1, 13, 30))     # a daytime span
    path.write_text("ANNOUNCE_ENTITY=assist_satellite.x\nQUIET_HOURS=off\n")
    assert ss.settings(env)["quiet"] is None
    path.write_text("ANNOUNCE_ENTITY=assist_satellite.x\nQUIET_HOURS=late\n")
    assert ss.settings(env)["quiet"] == ss.QUIET                            # nonsense: the default


def test_quiet_hours_are_in_the_agents_time_zone():
    late_in_london = ss.now_for({"timezone": "Europe/London"})
    assert late_in_london.tzinfo is not None
    assert ss.now_for({"timezone": "Not/AZone"}).tzinfo is None             # unknown: this machine's clock


def test_private_only_while_that_persons_phone_is_home(env, monkeypatch):
    speaker = Speaker()
    home = {"greg": False}

    async def at_home(user, env=None, clock=None):
        return home.get(user, False)
    monkeypatch.setattr(presence, "at_home", at_home)
    text = "Greg, your dentist is at four."
    out = say(app_with(speaker), "greg", FINN, text, env, private=True)        # Finn's person: Greg
    assert out["reason"] == "private_away" and "phone is not at home" in out["tell"] and speaker.said == []
    home["greg"] = True
    assert say(app_with(speaker), "greg", FINN, text, env, private=True)["spoken"] is True
    # the household agent has two people: it must say whose
    assert say(app_with(speaker), "household", HOUSEHOLD, text, env, private=True)["reason"] == "private_no_person"
    assert say(app_with(speaker), "household", HOUSEHOLD, text, env, private=True, person="bere")["reason"] == "private_away"
    # shared household things are said whoever is home
    assert say(app_with(speaker), "household", HOUSEHOLD, "The bins go out tonight.", env)["spoken"] is True


def test_private_with_no_presence_set_up_is_away(env):
    out = say(app_with(Speaker()), "greg", FINN, "Greg, your dentist is at four.", env, private=True)
    assert out["reason"] == "private_away"


def test_short_only(env):
    speaker = Speaker()
    out = say(app_with(speaker), "greg", FINN, "Word " * 80, env)
    assert out["reason"] == "too_long" and str(ss.MAX_CHARS) in out["tell"] and speaker.said == []
    assert say(app_with(speaker), "greg", FINN, "**Dinner** is *ready* 🍝", env)["spoken"] is True
    assert tts.unmark(speaker.said[0][1])[0] == "Dinner is ready."             # as heard: no Markdown or emoji
    assert say(app_with(speaker), "greg", FINN, "  ", env)["reason"] == "empty"


def test_not_too_often(env):
    t = [1000.0]
    speaker = Speaker()
    app = app_with(speaker, clock=lambda: t[0])
    assert say(app, "greg", FINN, "One.", env)["spoken"] is True
    t[0] += 10
    assert say(app, "household", HOUSEHOLD, "Two.", env)["reason"] == "busy"   # the speaker just spoke
    for _ in range(ss.PER_HOUR - 1):
        t[0] += ss.GAP_S + 1
        assert say(app, "greg", FINN, "Again.", env)["spoken"] is True
    t[0] += ss.GAP_S + 1
    out = say(app, "greg", FINN, "Once more.", env)
    assert out["reason"] == "rate_limited" and "last hour" in out["tell"]
    assert say(app, "household", HOUSEHOLD, "Mine.", env)["spoken"] is True    # per agent
    t[0] += 3600
    assert say(app, "greg", FINN, "Later.", env)["spoken"] is True
    assert len(speaker.said) == ss.PER_HOUR + 2


def test_only_agents_on_the_speaker_and_only_where_it_is_set_up(env):
    speaker = Speaker()
    assert say(app_with(speaker), "bere", {"name": "bere"}, "Hi.", env)["reason"] == "not_on_speaker"
    assert say(app_with(speaker), "greg", {**FINN, "speaker_say": False}, "Hi.", env)["reason"] == "not_on_speaker"
    assert ss.speaks({"speaker_name": "Finn"}) and not ss.speaks({"voice": True})
    (Path(env["HOME"]) / ".config/logos/speaker").write_text("TTS_BIND=192.0.2.20\n")
    assert say(app_with(speaker), "greg", FINN, "Hi.", env)["reason"] == "off"
    (Path(env["HOME"]) / ".config/logos/speaker").write_text("ANNOUNCE_ENTITY=light.kitchen\n")
    assert say(app_with(speaker), "greg", FINN, "Hi.", env)["reason"] == "off"   # only a speaker entity
    assert speaker.said == []


def test_home_assistant_failing_is_told_not_raised(env):
    out = say(app_with(Speaker(fail="answer 500")), "greg", FINN, "Hi there.", env)
    assert out["reason"] == "failed" and "answer 500" in out["tell"] and "do not retry" in out["tell"]


# ── Home Assistant, over HTTP (a stand-in) ─────────────────────────────────────────────────

def test_announce_calls_assist_satellite_announce_with_logos_account(tmp_path):
    seen = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append((self.path, self.headers["Authorization"], body))
            self.send_response(200 if body["message"] != "fail" else 400)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"[]")
    server = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        (tmp_path / ".config/logos").mkdir(parents=True)
        ha = tmp_path / ".config/logos/home-assistant"
        ha.write_text(f"HA_URL=http://127.0.0.1:{server.server_port}\nHA_TOKEN=non-admin\n")
        ha.chmod(0o600)
        env = {"HOME": str(tmp_path)}
        ss.announce("assist_satellite.x", "Hello", False, env)
        with pytest.raises(ss.SayError):
            ss.announce("assist_satellite.x", "fail", True, env)
    finally:
        server.shutdown()
    assert seen[0] == ("/api/services/assist_satellite/announce", "Bearer non-admin",
                       {"entity_id": "assist_satellite.x", "message": "Hello", "preannounce": False})
    with pytest.raises(ss.SayError):                                 # no Home Assistant settings at all
        ss.announce("assist_satellite.x", "Hello", True, {"HOME": str(tmp_path / "none")})


# ── the endpoint ───────────────────────────────────────────────────────────────────────────

def test_the_endpoint_takes_only_the_agents_own_token(env, monkeypatch):
    monkeypatch.setenv("HOME", env["HOME"])
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    speaker = Speaker()

    async def run():
        app = web.Application()
        ss.register(app)
        app["speaker_say_announce"] = speaker
        async with TestClient(TestServer(app)) as c:
            good = ss.token_for("greg")
            ok = await c.post(ss.PATH, json={"agent": "greg", "token": good, "text": "Time for the plumber."})
            other = await c.post(ss.PATH, json={"agent": "household", "token": good, "text": "x"})
            bad = await c.post(ss.PATH, json={"agent": "greg"})
            return ok.status, await ok.json(), other.status, bad.status
    status, out, other, bad = asyncio.run(run())
    assert (status, other, bad) == (200, 403, 400)
    assert out["spoken"] is True or out["reason"] == "quiet_hours"           # it depends on when the test runs
    assert ss.token_for("greg", key=b"k") != ss.token_for("household", key=b"k")


def test_the_path_is_public_to_the_dashboards_login_but_checks_its_token():
    from gateway.auth import middleware
    assert ss.PATH in middleware._PUBLIC_PATHS


# ── the agent's plugin ─────────────────────────────────────────────────────────────────────

def load_plugin(monkeypatch, tmp_path, url):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for k, v in (("LOGOS_SPEAKER_SAY_TOKEN", "t0k"), ("LOGOS_SPEAKER_SAY_URL", url), ("LOGOS_AGENT_NAME", "greg")):
        monkeypatch.setenv(k, v)
    spec = importlib.util.spec_from_file_location(
        "logos_speaker_say_plugin", REPO / "infra/agent-plugins/logos-speaker-say/__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_plugin_posts_and_relays_the_answer(monkeypatch, tmp_path):
    seen, answers = [], [{"spoken": True}, {"spoken": False, "reason": "quiet_hours", "tell": "Put it in the chat."}]

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            seen.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            data = json.dumps(answers[len(seen) - 1]).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    server = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        mod = load_plugin(monkeypatch, tmp_path, f"http://127.0.0.1:{server.server_port}{ss.PATH}")
        assert mod.available()
        first = json.loads(mod.handle({"text": "Plumber at four.", "private": True, "person": "greg"}))
        second = json.loads(mod.handle({"text": "Bins.", "chime": False}))
    finally:
        server.shutdown()
    assert seen[0] == {"agent": "greg", "token": "t0k", "text": "Plumber at four.", "private": True, "chime": True,
                       "person": "greg"}
    assert seen[1]["chime"] is False and "person" not in seen[1]
    assert first["spoken"] is True and "chat" in first["note"]
    assert second == {"spoken": False, "reason": "quiet_hours", "tell": "Put it in the chat."}


def test_the_plugin_without_logos_says_to_use_the_chat(monkeypatch, tmp_path):
    mod = load_plugin(monkeypatch, tmp_path, "http://127.0.0.1:9/nothing")
    out = json.loads(mod.handle({"text": "Hello"}))
    assert out["spoken"] is False and "chat" in out["tell"]
    registered = []
    mod.register(SimpleNamespace(register_tool=lambda **kw: registered.append(kw)))
    assert registered[0]["name"] == "say_on_speaker" and registered[0]["schema"]["parameters"]["required"] == ["text"]


# ── the wiring: the sandbox rule, the soul, the briefing ────────────────────────────────────

def test_the_sandbox_may_post_only_that_path():
    import yaml
    rule = yaml.safe_load((REPO / "infra/agent-image/policy.yaml").read_text())["network_policies"]["speaker_say"]
    assert rule["endpoints"][0]["rules"] == [{"allow": {"method": "POST", "path": ss.PATH}}]


def test_the_briefing_can_also_be_spoken():
    from gateway import agent_runtime as art
    agent = {"calendar": {"person": "greg"}, "matrix": {"enabled": True}, "speaker_voice": "en_GB-alan-medium",
             "briefing": {"speaker": True}}
    assert art.briefing(agent)["speaker"] is True
    text = art.briefing_prompt(REPO / "infra/agent-jobs", agent)
    assert "say_on_speaker" in text and "`person: greg`" in text and text.startswith(art.LOGOS_JOB_MARK)
    plain = art.briefing_prompt(REPO / "infra/agent-jobs", {**agent, "briefing": {}})
    assert "say_on_speaker" not in plain
    # an agent that is not on the speaker never gets it, whatever the yaml says
    assert art.briefing({**agent, "speaker_voice": None})["speaker"] is False


def test_the_speaker_rules_are_in_the_soul_of_agents_on_the_speaker():
    text = (REPO / "souls/_shared/speaker.md").read_text()
    assert "say_on_speaker" in text and "private" in text and "Reminders said out loud" in text
