"""The house speaker's voices (Voice V1.2): which voice says what (gateway/tts.py), the container
(gateway/tts_host.py) and the gateway side (gateway/speaker_voice.py)."""
import asyncio
import json
import re
import types
import wave
import io
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway import speaker_voice as sv
from gateway import tts, tts_host

REPO = Path(__file__).resolve().parents[2]
HOME = "en_GB-northern_english_male-medium"


def test_the_image_has_exactly_the_voices_offered():
    text = (REPO / "infra/tts/Dockerfile").read_text()
    listed = re.search(r'ARG VOICES="([^"]+)"', text).group(1).replace("\\\n", " ").split()
    assert listed == list(tts.VOICES)
    assert re.search(r"VOICES_REVISION=[0-9a-f]{40}\b", text)          # a fixed revision
    assert re.search(r"^FROM python@sha256:[0-9a-f]{64}$", text, re.M)   # a pinned base
    assert "piper-tts==" in text and "wyoming==" in text


def test_home_defaults_to_the_voice_greg_chose():
    assert tts.DEFAULT_VOICE == HOME
    import yaml
    assert yaml.safe_load((REPO / "agents/household.yaml").read_text())["speaker_voice"] == HOME


def test_key_ignores_case_spaces_and_punctuation():
    assert tts.key("It's ten past four.") == tts.key("  it's TEN past   four ")
    assert tts.key("It's ten past four.") != tts.key("It's ten past five.")


def test_words_an_agent_just_said_take_its_voice():
    state = {"default": HOME, "said": [
        {"key": tts.key("Lights off."), "agent": "finn", "voice": "en_US-ryan-medium", "until": 1000},
        {"key": tts.key("Lights off."), "agent": "household", "voice": "en_GB-alba-medium", "until": 1000},
    ]}
    # the latest wins; whatever voice Home Assistant's pipeline names
    assert tts.choose("lights off", "en_US-amy-medium", state, now=999) == ("en_GB-alba-medium", "household")
    # after two minutes it is just words again
    assert tts.choose("lights off", "en_US-amy-medium", state, now=1001) == ("en_US-amy-medium", None)


def test_anything_else_uses_the_requested_voice_else_homes():
    state = {"default": "en_GB-cori-medium", "said": []}
    assert tts.choose("Timer done", "en_US-joe-medium", state) == ("en_US-joe-medium", None)
    assert tts.choose("Timer done", "de_DE-thorsten-medium", state) == ("en_GB-cori-medium", None)
    assert tts.choose("Timer done", None, {}) == (HOME, None)
    # a damaged state file or an unknown voice in it never breaks speaking
    assert tts.choose("x", None, {"default": "nope", "said": [{"key": tts.key("x"), "voice": "nope", "until": 9e12}, "junk"]}) \
        == (HOME, None)


def test_read_state_survives_a_missing_or_broken_file(tmp_path):
    assert tts.read_state(str(tmp_path / "none.json")) == {}
    (tmp_path / "bad.json").write_text("{")
    assert tts.read_state(str(tmp_path / "bad.json")) == {}


# ── tts_host ────────────────────────────────────────────────────────────────

def env_for(tmp_path, text=None):
    env = {"HOME": str(tmp_path)}
    if text is not None:
        (tmp_path / ".config/logos").mkdir(parents=True, exist_ok=True)
        (tmp_path / ".config/logos/speaker").write_text(text)
    return env


def test_off_without_the_settings_file(tmp_path):
    assert tts_host.settings(env_for(tmp_path)) is None


def test_settings_need_an_ip_address_and_default_the_port(tmp_path):
    assert tts_host.settings(env_for(tmp_path, "TTS_BIND=192.0.2.20\n")) == {"bind": "192.0.2.20", "port": 10200}
    assert tts_host.settings(env_for(tmp_path, "# LAN\nTTS_BIND=0.0.0.0  # all\nTTS_PORT=10300\n"))["port"] == 10300
    for bad in ("TTS_BIND=speaker.local\n", "", "TTS_BIND=192.0.2.20\nTTS_PORT=99999\n"):
        with pytest.raises(tts_host.TtsError):
            tts_host.settings(env_for(tmp_path, bad))
    assert tts_host.client_address({"bind": "0.0.0.0", "port": 10200}) == ("127.0.0.1", 10200)


def test_only_agents_with_a_speaker_voice_start_it():
    assert tts_host.uses_speaker({"speaker_voice": HOME})
    assert not tts_host.uses_speaker({"voice": True})


class Runner:
    def __init__(self):
        self.calls = []

    def run(self, argv, **kw):
        self.calls.append(argv)
        return types.SimpleNamespace(returncode=0)

    def sleep(self, s):
        pass


class Runtime:
    def __init__(self, running=""):
        self.runner, self.argvs, self.running = Runner(), [], running

    def run(self, argv, check=True, input=None):
        self.argvs.append(argv)
        if argv[:2] == ["docker", "ps"]:
            return self.running
        return ""


def test_up_runs_it_locked_down_on_the_chosen_address(tmp_path):
    rt = Runtime()
    env = env_for(tmp_path, "TTS_BIND=192.0.2.20\n")
    probed = []
    assert tts_host.TtsHost(rt, env, probe=lambda a: probed.append(a) or True).up() is True
    run = next(a for a in rt.argvs if a[:2] == ["docker", "run"])
    joined = " ".join(run)
    for part in ("--read-only", "--cap-drop ALL", "no-new-privileges", "--dns 127.0.0.1", "--user 10000:10000",
                 "-p 192.0.2.20:10200:10200", "/state:ro", "tts.py:/app/tts.py:ro", "--memory 1g"):
        assert part in joined
    assert probed == [("192.0.2.20", 10200)]
    state = tmp_path / ".local/state/logos/speaker/said.json"
    assert json.loads(state.read_text()) == {"said": []} and oct(state.stat().st_mode & 0o777) == "0o644"


def test_up_does_nothing_when_off(tmp_path):
    rt = Runtime()
    assert tts_host.TtsHost(rt, env_for(tmp_path)).up() is None
    assert rt.argvs == []


# ── speaker_voice (the gateway side) ────────────────────────────────────────

class Defs:
    def __init__(self, defs):
        self.defs = defs

    def load(self, name):
        return self.defs[name]


@pytest.fixture
def gw(monkeypatch, tmp_path):
    sv._said.clear(), sv._cache.clear(), sv._samples.clear()
    choices = {}
    monkeypatch.setattr(sv, "_choices", lambda: choices)
    runtime = Defs({"household": {"speaker_voice": HOME}, "finn": {"speaker_voice": "en_US-ryan-medium"}, "bare": {}})
    app = {"sandbox_backend": types.SimpleNamespace(runtime=runtime)}
    env = {"HOME": str(tmp_path)}
    yield app, env, choices, tmp_path
    sv._said.clear(), sv._cache.clear(), sv._samples.clear()


def test_voice_is_the_choice_else_the_definition_else_homes(gw):
    app, _, choices, _ = gw
    assert sv.voice_of(app, "finn") == "en_US-ryan-medium"
    assert sv.voice_of(app, "bare") == HOME
    choices["finn"] = {"voice": "en_GB-alan-medium"}
    sv._cache.clear()
    assert sv.voice_of(app, "finn") == "en_GB-alan-medium"
    choices["finn"] = {"voice": "gone-voice"}
    sv._cache.clear()
    assert sv.voice_of(app, "finn") == "en_US-ryan-medium"


def test_said_writes_hashes_not_words_where_the_speaker_is_set_up(gw):
    app, env, _, home = gw
    sv.said(app, "finn", "The bins go out tonight.", env)        # not set up here: nothing written, no error
    folder = tts_host.state_dir(env)
    assert not folder.exists()
    folder.mkdir(parents=True)
    sv.said(app, "household", "It's ten past four.", env)
    data = json.loads((folder / "said.json").read_text())
    assert "bins" not in json.dumps(data) and "ten past" not in json.dumps(data)
    assert data["default"] == HOME
    # what the container then does with it
    assert tts.choose("its ten past four", "en_US-amy-medium", data) == (HOME, "household")
    assert tts.choose("The bins go out tonight", "en_US-amy-medium", data) == ("en_US-ryan-medium", "finn")


def test_said_never_breaks_the_answer(gw, monkeypatch):
    app, env, _, _ = gw
    monkeypatch.setattr(sv, "voice_of", lambda *a: 1 / 0)
    sv.said(app, "household", "Hello", env)                      # logged, not raised


def test_said_forgets_old_answers(gw, monkeypatch):
    app, env, _, _ = gw
    monkeypatch.setattr(sv.time, "time", lambda: 1000.0)
    for i in range(sv.MAX_SAID + 10):
        sv.said(app, "household", f"answer {i}", env)
    assert len(sv._said) == sv.MAX_SAID
    monkeypatch.setattr(sv.time, "time", lambda: 1000.0 + sv.TTL_S + 1)
    sv.said(app, "household", "later", env)
    assert len(sv._said) == 1


def test_voice_converse_hands_every_answer_over(monkeypatch):
    from gateway import voice_converse
    source = Path(voice_converse.__file__).read_text()
    assert "speaker_voice.said(request.app, agent, said)" in source


# ── the Wyoming client for samples, against a stand-in server ───────────────

async def wyoming_stand_in(seen):
    async def handle(reader, writer):
        head = json.loads(await reader.readline())
        seen.append(head)
        def send(kind, data, payload=b""):
            body = json.dumps(data).encode()
            writer.write(json.dumps({"type": kind, "data_length": len(body), "payload_length": len(payload)}).encode()
                         + b"\n" + body + payload)
        send("audio-start", {"rate": 16000, "width": 2, "channels": 1})
        send("audio-chunk", {"rate": 16000, "width": 2, "channels": 1}, b"\x01\x00" * 100)
        send("audio-chunk", {"rate": 16000, "width": 2, "channels": 1}, b"\x02\x00" * 50)
        send("audio-stop", {})
        await writer.drain()
        writer.close()
    return await asyncio.start_server(handle, "127.0.0.1", 0)


def test_synthesize_speaks_wyoming():
    async def go():
        seen = []
        server = await wyoming_stand_in(seen)
        port = server.sockets[0].getsockname()[1]
        try:
            fmt, pcm = await sv.synthesize(("127.0.0.1", port), "Hello", "en_GB-alba-medium")
        finally:
            server.close()
        return seen, fmt, pcm
    seen, fmt, pcm = asyncio.run(go())
    assert seen[0] == {"type": "synthesize", "data": {"text": "Hello", "voice": {"name": "en_GB-alba-medium"}}}
    assert fmt == (16000, 2, 1) and len(pcm) == 300
    w = wave.open(io.BytesIO(sv.wav(fmt, pcm)))
    assert (w.getframerate(), w.getnframes()) == (16000, 150)


# ── the Settings endpoints ──────────────────────────────────────────────────

@pytest.fixture
def api(monkeypatch, gw):
    app_state, env, choices, home = gw
    user = {"id": "1", "username": "greg", "role": "user"}
    stored = []
    from gateway import dashboard_api as d
    import gateway.auth.db as auth_db

    async def visible(request, username):
        return {"household": {"owners": ["greg", "bere"], "definition": {"display_name": "Household agent", "speaker_voice": HOME}},
                "greg": {"owners": ["greg"], "definition": {"display_name": "Greg's agent"}}}

    async def agent_or_404(request):
        agents = await visible(request, "greg")
        name = request.match_info["name"]
        if name not in agents:
            raise web.HTTPNotFound()
        return name, agents[name], user

    monkeypatch.setattr(d, "_user", lambda r: user)
    monkeypatch.setattr(d, "_visible_agents", visible)
    monkeypatch.setattr(d, "_agent_or_404", agent_or_404)
    monkeypatch.setattr(auth_db, "set_agent_voice", lambda n, v, by: stored.append((n, v, by)), raising=False)
    monkeypatch.setattr(sv, "_settings", lambda: (None, ""))

    async def run(steps):
        app = web.Application()
        app.router.add_get("/api/v2/speaker", sv.handle_speaker)
        app.router.add_get("/api/v2/speaker/sample", sv.handle_sample)
        app.router.add_put("/api/v2/agents/{name}/speaker-voice", sv.handle_agent_voice)
        async with TestClient(TestServer(app)) as c:
            return await steps(c)
    return run, stored, choices


def test_settings_lists_your_agents_and_the_voices(api):
    run, _, choices = api
    choices["greg"] = {"voice": "en_US-joe-medium", "set_by": "greg"}

    async def steps(c):
        return await (await c.get("/api/v2/speaker")).json()
    d = asyncio.run(run(steps))
    assert d["on"] is False and len(d["voices"]) == len(tts.VOICES)
    home, greg = d["agents"]
    assert (home["name"], home["speaks"], home["shared"], home["voice"]) == ("household", True, True, HOME)
    assert (greg["name"], greg["speaks"], greg["voice"], greg["default"]) == ("greg", False, "en_US-joe-medium", HOME)


def test_only_the_agents_people_may_choose_and_only_known_voices(api):
    run, stored, _ = api

    async def steps(c):
        ok = await c.put("/api/v2/agents/household/speaker-voice", json={"voice": "en_GB-alba-medium"})
        back = await c.put("/api/v2/agents/household/speaker-voice", json={"voice": ""})
        bad = await c.put("/api/v2/agents/household/speaker-voice", json={"voice": "../../etc"})
        other = await c.put("/api/v2/agents/bere/speaker-voice", json={"voice": "en_GB-alba-medium"})
        return ok.status, (await back.json())["voice"], bad.status, other.status
    assert asyncio.run(run(steps)) == (200, HOME, 400, 404)
    assert stored == [("household", "en_GB-alba-medium", "greg"), ("household", "", "greg")]


def test_a_sample_says_when_the_speaker_is_off(api):
    run, _, _ = api

    async def steps(c):
        off = await c.get("/api/v2/speaker/sample?voice=en_GB-alba-medium")
        bad = await c.get("/api/v2/speaker/sample?voice=nope")
        return off.status, (await off.json())["error"], bad.status
    assert asyncio.run(run(steps)) == (503, "speaker_off", 400)
