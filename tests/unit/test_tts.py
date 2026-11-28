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
    assert listed == tts.model_files()
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


# ── The invisible mark (V3.3) ───────────────────────────────────────────────

def test_every_voice_has_a_fixed_mark_and_it_survives_a_round_trip():
    assert set(tts.VOICES) <= set(tts.CODES) and len(set(tts.CODES)) == len(tts.CODES)
    # append-only: the first twelve keep the numbers they had when marks were first handed out
    assert tts.CODES[:12] == tuple(v for v in tts.VOICES if v.startswith("en_"))
    for vid in tts.VOICES:
        marked = tts.mark("Done.", vid)
        assert marked.endswith("Done.") and marked != "Done."
        assert tts.unmark(marked) == ("Done.", vid)
    assert tts.mark("Done.", "nope") == "Done." and tts.unmark("Done.") == ("Done.", None)


def test_the_mark_is_invisible_to_what_home_assistant_does_with_text():
    marked = tts.mark("Done.", "en_US-ryan-medium")
    assert marked.strip() == marked and len(marked.split()) == 1      # not whitespace to Python
    assert all(not c.isspace() for c in marked)
    assert tts.key(marked) == tts.key("Done.")                         # the hash fallback ignores it
    assert tts.unmark(" ".join(marked.split()))[1] == "en_US-ryan-medium"


def test_two_agents_saying_the_same_words_are_two_entries_in_home_assistants_cache():
    """Home Assistant's phrase cache key (homeassistant/components/tts, 2026.11): sha1 of the message,
    the language, a hash of the options (the pipeline's voice) and the engine."""
    import hashlib

    def ha_key(message, language="en", options="-", engine="tts.logos"):
        return f"{hashlib.sha1(bytes(message, 'utf-8')).hexdigest()}_{language}_{options}_{engine}".lower()
    finn, home = tts.mark("Done.", "en_GB-alan-medium"), tts.mark("Done.", HOME)
    assert ha_key(finn) != ha_key(home) and ha_key(finn) == ha_key(tts.mark("Done.", "en_GB-alan-medium"))


def test_a_mark_wins_over_the_hash_and_over_the_pipelines_voice():
    # Home said "Done." a moment ago; Finn's "Done." arrives marked with his voice.
    state = {"default": HOME, "said": [
        {"key": tts.key("Done."), "agent": "household", "voice": HOME, "until": 1000},
        {"key": tts.key("Done."), "agent": "greg", "voice": "en_GB-alan-medium", "until": 1000}]}
    assert tts.choose(tts.mark("Done.", HOME), "en_US-amy-medium", state, now=999) == (HOME, "household")
    assert tts.choose(tts.mark("Done.", "en_GB-alan-medium"), None, state, now=999) == ("en_GB-alan-medium", "greg")
    # long after the hash rows are gone, the mark alone still says the voice
    assert tts.choose(tts.mark("Done.", "fr_FR-tom-medium"), None, {}, now=5000) == ("fr_FR-tom-medium", "an agent")


def test_the_upmc_voices_are_one_model_with_two_speakers():
    assert tts.model_of("fr_FR-upmc_pierre-medium") == ("fr_FR-upmc-medium", 1)
    assert tts.model_of("fr_FR-upmc_jessica-medium") == ("fr_FR-upmc-medium", 0)
    assert tts.model_of(HOME) == (HOME, None)
    assert tts.model_files().count("fr_FR-upmc-medium") == 1
    assert tts.language("fr_FR-tom-medium") == "fr" and tts.language(HOME) == "en"


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
    assert sv.said(app, "household", "Hello", env) == "Hello"    # logged, not raised; unmarked


def test_said_hands_back_the_words_marked_with_the_voice(gw):
    app, env, choices, _ = gw
    assert tts.unmark(sv.said(app, "finn", "Done.", env)) == ("Done.", "en_US-ryan-medium")
    assert tts.unmark(sv.said(app, "household", "Done.", env)) == ("Done.", HOME)


def test_french_words_take_the_agents_french_voice(gw):
    app, env, choices, _ = gw
    # no choice: a French voice like the English one (Ryan is a man: Tom)
    assert tts.unmark(sv.said(app, "finn", "C'est fait, la lumière est éteinte.", env))[1] == "fr_FR-tom-medium"
    assert sv.voice_of(app, "finn") == "en_US-ryan-medium"
    choices["finn"] = {"voice": "en_GB-alba-medium", "voice_fr": None}
    sv._cache.clear()
    assert sv.voice_of(app, "finn", "fr") == "fr_FR-siwis-medium"   # Alba is a woman: Siwis
    choices["finn"] = {"voice": None, "voice_fr": "fr_FR-upmc_jessica-medium"}
    sv._cache.clear()
    assert sv.voice_of(app, "finn", "fr") == "fr_FR-upmc_jessica-medium"
    assert sv.voice_of(app, "finn", "en") == "en_US-ryan-medium"
    # speaker_voice_fr in the definition, and a French id is never taken as the English voice
    app["sandbox_backend"].runtime.defs["bare"] = {"speaker_voice": "fr_FR-tom-medium",
                                                   "speaker_voice_fr": "fr_FR-upmc_pierre-medium"}
    sv._cache.clear()
    assert (sv.voice_of(app, "bare"), sv.voice_of(app, "bare", "fr")) == (HOME, "fr_FR-upmc_pierre-medium")


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
    assert "speech = speaker_voice.said(request.app, agent, said)" in source
    assert '"speech": speech' in source


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
    monkeypatch.setattr(auth_db, "set_agent_voice", lambda n, v, by, *lang: stored.append((n, v, by, *lang)), raising=False)
    monkeypatch.setattr(sv, "_settings", lambda: (None, ""))

    def set_langs(n, off, by):
        stored.append((n, "langs_off", list(off), by))
        choices.setdefault(n, {})["langs_off"] = list(off)
    monkeypatch.setattr(auth_db, "set_agent_languages", set_langs, raising=False)

    async def run(steps):
        app = web.Application()
        app.router.add_get("/api/v2/speaker", sv.handle_speaker)
        app.router.add_get("/api/v2/speaker/sample", sv.handle_sample)
        app.router.add_put("/api/v2/agents/{name}/speaker-voice", sv.handle_agent_voice)
        app.router.add_put("/api/v2/agents/{name}/speaker-languages", sv.handle_agent_languages)
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


def test_a_french_voice_is_chosen_beside_the_english_one(api):
    run, stored, choices = api
    choices["household"] = {"voice": None, "voice_fr": "fr_FR-upmc_pierre-medium", "set_by": "bere"}

    async def steps(c):
        listed = await (await c.get("/api/v2/speaker")).json()
        ok = await c.put("/api/v2/agents/greg/speaker-voice", json={"voice_fr": "fr_FR-siwis-medium"})
        english_as_french = await c.put("/api/v2/agents/greg/speaker-voice", json={"voice_fr": "en_GB-alba-medium"})
        french_as_english = await c.put("/api/v2/agents/greg/speaker-voice", json={"voice": "fr_FR-tom-medium"})
        return listed, ok.status, (await ok.json()), english_as_french.status, french_as_english.status
    listed, ok, body, bad1, bad2 = asyncio.run(run(steps))
    home, greg = listed["agents"]
    assert (home["voice"], home["voice_fr"], home["default_fr"], home["set_by"]) == \
        (HOME, "fr_FR-upmc_pierre-medium", "fr_FR-tom-medium", "bere")
    assert greg["voice_fr"] == "fr_FR-tom-medium"           # Northern English is a man's voice: Tom
    assert {v["lang"] for v in listed["voices"]} == {"en", "fr"}
    assert (ok, body["voice_fr"], bad1, bad2) == (200, "fr_FR-siwis-medium", 400, 400)
    assert stored == [("greg", "fr_FR-siwis-medium", "greg", "fr")]


def test_a_sample_says_when_the_speaker_is_off(api):
    run, _, _ = api

    async def steps(c):
        off = await c.get("/api/v2/speaker/sample?voice=en_GB-alba-medium")
        bad = await c.get("/api/v2/speaker/sample?voice=nope")
        return off.status, (await off.json())["error"], bad.status
    assert asyncio.run(run(steps)) == (503, "speaker_off", 400)


# ── languages (Settings › Speaker, 28 Nov) ──────────────────────────────────

def test_languages_are_those_with_a_voice_and_each_can_be_switched_off(api):
    run, stored, choices = api

    async def steps(c):
        listed = await (await c.get("/api/v2/speaker")).json()
        off = await c.put("/api/v2/agents/greg/speaker-languages", json={"off": ["fr"]})
        after = await (await c.get("/api/v2/speaker")).json()
        both = await c.put("/api/v2/agents/greg/speaker-languages", json={"off": ["en", "fr"]})
        unknown = await c.put("/api/v2/agents/greg/speaker-languages", json={"off": ["de"]})
        other = await c.put("/api/v2/agents/bere/speaker-languages", json={"off": ["fr"]})
        return listed, off.status, (await off.json()), after, both.status, unknown.status, other.status
    listed, ok, body, after, both, unknown, other = asyncio.run(run(steps))
    assert [l["code"] for l in listed["installed"]] == ["en", "fr"]
    assert "de" in {l["code"] for l in listed["more"]}                 # understood, but no voice installed
    assert listed["agents"][1]["languages"] == ["en", "fr"]
    assert (ok, body["languages"]) == (200, ["en"])
    assert after["agents"][1]["languages"] == ["en"]
    assert (both, unknown, other) == (400, 400, 404)                   # one stays on; only installed; only your agents
    assert stored == [("greg", "langs_off", ["fr"], "greg")]


def test_a_language_switched_off_is_answered_in_the_main_one(gw):
    app, env, choices, _ = gw
    choices["finn"] = {"langs_off": ["fr"]}
    assert sv.answer_language("finn", "fr") == "en"
    assert tts.unmark(sv.said(app, "finn", "C'est fait, la lumière est éteinte.", env))[1] == "en_US-ryan-medium"
    choices["finn"] = {"langs_off": ["en"]}
    sv._cache.clear()
    assert sv.answer_language("finn", "en") == "fr" and sv.answer_language("finn", "fr") == "fr"
    choices["finn"] = {"langs_off": ["en", "fr"]}                       # never none: the first installed one
    assert sv.languages_of("finn") == ["en"]


def test_the_speaker_says_which_language_to_answer_in():
    from gateway import voice_converse as vc
    assert vc.language_rule("fr", "fr") == vc.IN_FRENCH
    assert vc.language_rule("en", "en") == ""
    assert vc.language_rule("fr", "en") == "They spoke French, which you do not speak at the speaker: reply in English."


def test_languages_are_kept_with_the_voices(tmp_path):
    import gateway.auth.db as auth_db
    auth_db.init_db(tmp_path)
    auth_db.set_agent_languages("finn", ["fr"], "greg")
    assert auth_db.get_agent_voices()["finn"]["langs_off"] == ["fr"]
    auth_db.set_agent_voice("finn", "en_GB-alan-medium", "greg")
    auth_db.set_agent_languages("finn", [], "greg")
    assert auth_db.get_agent_voices()["finn"] == {"voice": "en_GB-alan-medium", "voice_fr": None, "set_by": "greg", "langs_off": []}
    auth_db.set_agent_voice("finn", "", "greg")
    assert "finn" not in auth_db.get_agent_voices()
