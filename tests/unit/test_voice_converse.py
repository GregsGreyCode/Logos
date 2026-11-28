"""The house speaker's way in: Home Assistant -> /api/v2/voice/converse -> an agent (Voice V1)."""
import asyncio
import stat
from datetime import datetime
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway import tts
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
    assert tts.unmark(out["speech"]) == ("Which room?", "en_GB-northern_english_male-medium")   # marked (V3.3)
    assert out["continue"] is True and out["agent"] == "household"
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
    assert status == 200 and tts.unmark(out["speech"])[0] == vc.DOWN and out["continue"] is False


def test_french_is_answered_in_french_in_a_french_voice(home):
    """V3.4: Béré speaks French; Home Assistant still says the pipeline's "en"."""
    links = FakeLinks(FakeClient([ev("run_completed", "C'est fait, la cuisine est allumée.")]))

    def app():
        a = web.Application()
        a["agent_links"] = links
        vc.register(a)
        return a
    status, out = _post(app, {"Authorization": f"Bearer {vc.token()}"},
                        {"text": "Allume la lumière de la cuisine", "language": "en"})
    assert status == 200
    words, voice = tts.unmark(out["speech"])
    assert words == "C'est fait, la cuisine est allumée." and voice == "fr_FR-tom-medium"
    assert links._client.started[0][1]["instructions"].endswith(vc.IN_FRENCH)


def test_english_keeps_the_instructions_as_they_were(home):
    links = FakeLinks(FakeClient([ev("run_completed", "Done.")]))

    def app():
        a = web.Application()
        a["agent_links"] = links
        vc.register(a)
        return a
    _post(app, {"Authorization": f"Bearer {vc.token()}"}, {"text": "turn the kitchen lights on"})
    assert vc.IN_FRENCH not in links._client.started[0][1]["instructions"]


def test_the_speakers_own_lines_have_french_versions():
    assert set(vc.FRENCH) == {vc.SLOW, vc.OFF, vc.DOWN, vc.FAILED}
    from gateway import speech_lang
    assert all(speech_lang.detect(t) == "fr" for t in vc.FRENCH.values())


# ── how fast it answers (/metrics) ──────────────────────────────────────────────────────────

def test_turn_times_are_a_prometheus_histogram():
    st = vc.Stats()
    st.observe("household", "qwen3.6", "first", "ok", 40.2)
    st.observe("household", "qwen3.6", "followup", "ok", 7.7)
    st.observe("household", "qwen3.6", "followup", "ok", 8.1)
    text = st.render()
    lab = 'agent="household",model="qwen3.6",turn="followup",outcome="ok"'
    assert "# TYPE logos_voice_turn_seconds histogram" in text
    assert f'logos_voice_turn_seconds_bucket{{{lab},le="6"}} 0' in text
    assert f'logos_voice_turn_seconds_bucket{{{lab},le="10"}} 2' in text
    assert f'logos_voice_turn_seconds_bucket{{{lab},le="+Inf"}} 2' in text
    assert f"logos_voice_turn_seconds_count{{{lab}}} 2" in text
    assert f"logos_voice_turn_seconds_sum{{{lab}}} 15.800" in text
    assert vc.Stats().render() == ""


def test_the_nightly_check_time():
    assert vc.probe_time({}) == (4, 30)
    assert vc.probe_time({vc.PROBE_AT_ENV: "off"}) is None
    assert vc.probe_time({vc.PROBE_AT_ENV: "23:05"}) == (23, 5)
    assert vc.probe_time({vc.PROBE_AT_ENV: "25:00"}) is None
    assert vc.seconds_until((4, 30), datetime(2026, 11, 27, 4, 0)) == 1800
    assert vc.seconds_until((4, 30), datetime(2026, 11, 27, 5, 0)) == 23.5 * 3600


def test_the_nightly_check_asks_the_same_two_questions_in_a_fresh_conversation(home):
    c = FakeClient([ev("run_completed", "Friday.")])
    app = {"agent_links": FakeLinks(c), "voice_stats": vc.Stats()}
    asyncio.run(vc.run_probe(app))
    assert [t for t, _ in c.started] == list(vc.PROBE)
    sessions = {kw["session_id"] for _, kw in c.started}
    assert len(sessions) == 1 and next(iter(sessions)).startswith("voice-probe-household-")
    assert set(app["voice_stats"].probe) == {("household", "unknown", "first"), ("household", "unknown", "followup")}
    assert "logos_voice_probe_seconds{" in app["voice_stats"].render()


def test_a_spoken_turn_is_measured(home):
    links = FakeLinks(FakeClient([ev("run_completed", "Done.")]))
    seen = {}

    def app():
        a = web.Application()
        a["agent_links"] = links
        vc.register(a)
        seen["stats"] = a["voice_stats"]
        return a
    assert _post(app, {"Authorization": f"Bearer {vc.token()}"}, {"text": "hi", "device_id": "pe1"})[0] == 200
    (key, row), = seen["stats"].turns.items()
    assert key[0] == "household" and key[3] == "ok" and row[-1] == 1


def test_the_speaker_listens_again_after_an_answer_unless_the_person_closed():
    """Greg, 28 Nov: after "Clapham" there was no chance to say "no, Clacton"."""
    assert vc.keep_listening("What's the weather in Clacton?", "Sunny and nine degrees in Clapham.")
    assert vc.keep_listening("Add milk", "Done.")
    for closing in ("Thanks", "thank you!", "That's all", "no thanks", "Never mind.", "OK thanks", "merci", "c'est tout", "Bye"):
        assert not vc.keep_listening(closing, "You're welcome."), closing
    assert vc.keep_listening("Thanks", "Anything else?")         # a question still waits for the answer
    assert "never say you will look something up" in vc.INSTRUCTIONS
