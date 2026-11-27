"""The house speaker's way in: Home Assistant -> /api/v2/voice/converse -> an agent (Voice V1)."""
import asyncio
import stat
from datetime import datetime
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway import voice_converse as vc
from gateway.hermes_client import HermesHTTPError


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("LOGOS_VOICE_AGENT", raising=False)
    return tmp_path


def test_the_token_is_made_once_and_kept_private(home):
    t = vc.token()
    assert t == vc.token() and len(t) > 30
    path = home / "logos" / vc.TOKEN_FILE
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert vc.authorised(f"Bearer {t}")
    assert not vc.authorised("Bearer nope") and not vc.authorised(t) and not vc.authorised("")


def test_one_conversation_per_speaker_per_day():
    day = datetime(2026, 11, 27, 9)
    a = vc.session_for("household", "abc/123", day)
    assert a == "voice-household-abc-123-20261127"
    assert vc.session_for("household", "abc/123", datetime(2026, 11, 27, 22)) == a
    assert vc.session_for("household", "abc/123", datetime(2026, 11, 28, 9)) != a
    assert vc.session_for("household", "", day) == "voice-household-speaker-20261127"


def test_what_the_speaker_says_has_no_markdown():
    assert vc.spoken("**Yes**, the bins go out *tonight*.") == "Yes, the bins go out tonight."
    assert vc.spoken("Today:\n- dentist at 10\n- gym at 6") == "Today: dentist at 10. gym at 6."
    assert vc.spoken("See [the forecast](https://x.y/z) 🌧️") == "See the forecast."
    assert vc.spoken("Here:\n```\ncode\n```\nDone.") == "Here: Done."
    long = vc.spoken("This is a sentence. " * 80)
    assert len(long) < vc.MAX_SPOKEN + 40 and long.endswith("The rest is in the chat.")


def test_a_question_keeps_the_speaker_listening():
    assert vc.expects_reply("Which room?")
    assert vc.expects_reply('Shall I add "milk"?')
    assert not vc.expects_reply("Done. The lights are off.")


class FakeClient:
    def __init__(self, events, delay=0.0, fail_start=False):
        self.events, self.delay, self.fail_start = events, delay, fail_start
        self.started = []

    async def start_run(self, text, **kw):
        if self.fail_start:
            raise HermesHTTPError(503, "no", method="POST", path="/v1/runs")
        self.started.append((text, kw))
        return "run-1"

    async def stream_events(self, run_id):
        for ev in self.events:
            await asyncio.sleep(self.delay)
            yield ev


def ev(type_, text=""):
    return SimpleNamespace(type=type_, text=text,
                           terminal=type_ in ("run_completed", "run_failed", "run_cancelled", "run_interrupted"))


def test_a_turn_is_told_it_will_be_spoken_and_returns_the_answer():
    c = FakeClient([ev("token", "It's "), ev("token", "sunny."), ev("run_completed", "It's sunny.")])
    assert asyncio.run(vc.collect(c, "weather?", session_id="s")) == ("ok", "It's sunny.")
    text, kw = c.started[0]
    assert text == "weather?" and kw["session_id"] == "s" and "read out" in kw["instructions"]


def test_a_slow_turn_says_the_answer_will_be_in_the_chat():
    c = FakeClient([ev("token", "x"), ev("run_completed", "x")], delay=0.2)
    assert asyncio.run(vc.collect(c, "hi", session_id="s", wait=0.05)) == ("slow", vc.SLOW)


def test_a_failed_or_unreachable_turn_says_so():
    assert asyncio.run(vc.collect(FakeClient([ev("run_failed")]), "hi", session_id="s")) == ("failed", vc.FAILED)
    assert asyncio.run(vc.collect(FakeClient([], fail_start=True), "hi", session_id="s")) == ("down", vc.DOWN)


class FakeLinks:
    def __init__(self, client):
        self._client = client
        self.runtime = SimpleNamespace(load=lambda name: {"name": name, "model": {}},
                                       runner=SimpleNamespace(http=None))
        self.asked = []

    async def health(self, name):
        self.asked.append(name)

    async def client(self, name):
        return self._client


def _post(make, headers, body):
    async def run():
        async with TestClient(TestServer(make())) as client:
            r = await client.post(vc.PATH, json=body, headers=headers)
            return r.status, (await r.json() if r.status == 200 else None)
    return asyncio.run(run())


def test_the_endpoint_answers_only_the_speakers_token(home):
    links = FakeLinks(FakeClient([ev("run_completed", "**Which** room?")]))

    def app():
        a = web.Application()
        a["agent_links"] = links
        vc.register(a)
        return a
    auth = {"Authorization": f"Bearer {vc.token()}"}
    assert _post(app, {}, {"text": "lights"})[0] == 401
    assert _post(app, {"Authorization": "Bearer wrong"}, {"text": "lights"})[0] == 401
    assert _post(app, auth, {"text": "  "})[0] == 400
    status, out = _post(app, auth, {"text": "turn the lights on", "device_id": "pe1"})
    assert status == 200
    assert out["speech"] == "Which room?" and out["continue"] is True and out["agent"] == "household"
    assert out["conversation_id"].startswith("voice-household-pe1-")
    assert links.asked == ["household"]


def test_an_agent_that_is_down_is_spoken_about_not_raised(home):
    class Down(FakeLinks):
        async def health(self, name):
            raise RuntimeError("not running")
    def app():
        a = web.Application()
        a["agent_links"] = Down(None)
        vc.register(a)
        return a
    status, out = _post(app, {"Authorization": f"Bearer {vc.token()}"}, {"text": "hi"})
    assert status == 200 and out["speech"] == vc.DOWN and out["continue"] is False
