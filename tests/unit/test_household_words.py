"""Settings › Household words: putting names right after speech-to-text (gateway/household_words.py),
the list's file, getting it to the speech-to-text containers and the agents (gateway/household_words_api.py,
gateway/stt.py, gateway/voice.py) and the agents' plugin. Made-up names only."""
import asyncio
import importlib.util
import json
import os
import stat
import threading
import types
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway import household_words as hw
from gateway import household_words_api as api
from gateway import stt, voice

REPO = Path(__file__).resolve().parents[2]
WORDS = [{"word": "Zoé", "heard_as": ["Zoey", "Joey"]},
         {"word": "Anaïs", "heard_as": ["Annie Ice", "Anise"]},
         {"word": "Quill", "heard_as": ["Quil"]}]


# ── putting the words right ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("heard, meant", [
    ("Tell Zoey dinner is ready.", "Tell Zoé dinner is ready."),
    ("zoey, are you there?", "Zoé, are you there?"),                       # any case
    ("Ask JOEY now", "Ask ZOÉ now"),                                         # shouting stays shouting
    ("Is that Zoey's coat?", "Is that Zoé's coat?"),                         # a possessive keeps its 's
    ("(Zoey) and \"Joey\"!", "(Zoé) and \"Zoé\"!"),                          # punctuation kept
    ("Zoeylicious Joeys", "Zoeylicious Joeys"),                              # never inside another word
    ("Annie   Ice called", "Anaïs called"),                                  # a heard_as of two words
    ("Annie called", "Annie called"),                                        # but not half of it
    ("Zoe and Anais", "Zoé and Anaïs"),                                      # the word without its accents
    ("Quil wrote to Quill", "Quill wrote to Quill"),
    ("", ""),
])
def test_correct_puts_heard_as_right(heard, meant):
    assert hw.correct(heard, WORDS) == meant


def test_correct_replaces_once_left_to_right():
    # Zoey -> Zoé, and Zoé is itself what another word is heard as: never replaced twice.
    chain = [{"word": "Zoé", "heard_as": ["Zoey"]}, {"word": "Zed", "heard_as": ["Zoé"]}]
    assert hw.correct("Zoey met Zoé", chain) == "Zoé met Zed"


def test_correct_never_changes_the_word_itself_or_a_name_that_is_a_word():
    words = [{"word": "Home", "heard_as": []}, {"word": "Wren", "heard_as": ["Meera"]}]
    assert hw.correct("going home to Meera's", words) == "going home to Wren's"
    assert hw.correct("anything", []) == "anything"


def test_correct_handles_decomposed_accents_and_regex_characters():
    words = [{"word": "Zoé", "heard_as": ["Zo.ey", "a+b"]}]
    assert hw.correct("Zo.ey and a+b, not Zoxey", words) == "Zoé and Zoé, not Zoxey"
    assert hw.correct("Zoé is here", [{"word": "Zed", "heard_as": ["Zoé"]}]) == "Zed is here"


def test_clean_keeps_what_may_be_kept():
    got = hw.clean([
        {"word": "  Zoé ", "heard_as": "Zoey, Joey,, zoé, ZOEY", "note": "a friend"},
        {"word": "zoé", "heard_as": ["Again"]},                              # the same word twice
        {"word": "x" * 41, "heard_as": []},                                  # too long
        {"word": "Quill", "heard_as": [f"Q{i}" for i in range(15)], "note": 5},
        "not an entry", {"heard_as": ["no word"]}])
    assert got == [{"word": "Zoé", "heard_as": ["Zoey", "Joey"], "note": "a friend"},
                   {"word": "Quill", "heard_as": [f"Q{i}" for i in range(10)]}]
    assert hw.clean("nonsense") == [] and len(hw.clean([{"word": f"W{i}"} for i in range(150)])) == hw.MAX_WORDS


def test_hotwords_and_the_agents_line():
    assert hw.hotwords(WORDS) == "Zoé, Anaïs, Quill"
    assert len(hw.hotwords([{"word": f"Word{i}"} for i in range(100)])) <= hw.HOTWORDS_MAX_CHARS
    words = [{"word": "Zoé", "heard_as": ["Zoey", "Joey"], "note": "Sam's sister"}, {"word": "Quill", "heard_as": ["Quil"]},
             {"word": "Wren", "heard_as": []}]
    assert hw.agent_note(words) == ("Names here: Zoé (Sam's sister; speech-to-text may write Zoey or Joey), "
                                    "Quill (speech-to-text may write Quil), Wren.")
    assert hw.agent_note([]) == ""
    assert len(hw.agent_note([{"word": f"Name{i}", "heard_as": ["x" * 30]} for i in range(60)])) <= hw.NOTE_MAX_CHARS


def test_defaults_join_after_the_saved_words():
    auto = hw.defaults(["Sam", "Zoé"], ["Wren", "sam"])
    assert [(e["word"], e["source"]) for e in auto] == [("Sam", "person"), ("Zoé", "person"), ("Wren", "agent")]
    merged = hw.merged([{"word": "zoé", "heard_as": ["Zoey"]}], auto)
    assert [e["word"] for e in merged] == ["zoé", "Sam", "Wren"]


def test_agent_words_are_names_of_their_own():
    agents = {"a": {"display_name": "Sam's agent", "speaker_name": "Quill"}, "b": {"display_name": "Household agent", "speaker_name": "Home"},
              "c": {"display_name": "Wren"}, "d": {"display_name": "Sam’s"}}
    assert api.agent_words(agents) == ["Quill", "Home", "Wren"]


# ── the file ─────────────────────────────────────────────────────────────────────────────────

def test_the_file_is_private_and_outside_the_repo(tmp_path):
    env = {"HOME": str(tmp_path)}
    path = hw.path(env)
    assert path == tmp_path / ".config" / "logos" / "household-words.json"
    assert hw.path({"XDG_CONFIG_HOME": str(tmp_path / "x")}) == tmp_path / "x" / "logos" / "household-words.json"
    assert hw.load(path) == []
    kept = hw.save(path, WORDS + [{"word": ""}])
    assert kept == WORDS and hw.load(path) == WORDS
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text(encoding="utf-8")) == {"words": WORDS}
    assert not list(path.parent.glob("*.tmp"))
    path.write_text("{broken")
    assert hw.load(path) == []
    path.write_text(json.dumps(WORDS))                       # a bare list, written by hand
    assert hw.load(path) == WORDS


def test_watched_reads_the_file_again_when_it_changes(tmp_path):
    f = tmp_path / "words.json"
    w = hw.Watched(str(f), every_s=5)
    assert w.entries(now=0) == []
    hw.save(f, WORDS[:1])
    assert w.entries(now=1) == []                            # checked at most every 5 s
    assert w.entries(now=6) == WORDS[:1]
    hw.save(f, WORDS)
    os.utime(f, ns=(1, 1))                                   # a new stamp even within the same instant
    assert w.entries(now=12) == WORDS
    f.unlink()
    assert w.entries(now=18) == []


def test_no_real_names_are_written_into_the_repo():
    # The list lives on the server only: no household-words file anywhere in the repo.
    assert not [p for p in REPO.rglob("household-words.json") if ".venv" not in p.parts and "node_modules" not in p.parts]


# ── the house speaker's speech-to-text (gateway/stt.py) ──────────────────────────────────────

class FakeRecognizer:
    def __init__(self, text):
        self.text = text

    def create_stream(self):
        return types.SimpleNamespace(accept_waveform=lambda *a: None, result=types.SimpleNamespace(text=self.text))

    def decode_stream(self, st):
        pass


def test_the_speakers_words_are_put_right():
    ears = stt.Ears("/none", "/none", "/none")
    ears._words = FakeRecognizer(" Tell Zoey the lights are on ")
    assert ears.transcribe([0.0] * 10) == "Tell Zoey the lights are on"
    assert ears.set_household(WORDS + ["junk"]) == 3
    assert ears.transcribe([0.0] * 10) == "Tell Zoé the lights are on"


@pytest.fixture
def door():
    got = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), stt.make_http("tok", stt.Heard(), lambda p: {}, lambda: [],
                                                                  lambda e: got.append(e) or len(hw.clean(e))))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", got
    server.shutdown()


def post(url, data, token="tok"):
    req = urllib.request.Request(url, data=data, method="POST", headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, None


def test_the_list_reaches_the_speaker_through_the_dashboards_door_only(door):
    base, got = door
    assert post(base + "/words", json.dumps({"words": WORDS}).encode()) == (200, {"words": 3})
    assert got == [WORDS]
    assert post(base + "/words", b"{}", token="nope")[0] == 401 and post(base + "/words", b"{}", token="")[0] == 401
    assert post(base + "/words", b"not json")[0] == 400 and post(base + "/words", b"")[0] == 400
    assert len(got) == 1


def test_the_speaker_container_mounts_the_list_module():
    from gateway import stt_host, voice_host
    assert any(f.name == "household_words.py" for f in stt_host.FILES)
    assert voice_host.WORDS_FILE.name == "household_words.py" and voice_host.WORDS_FILE.exists()


# ── voice notes and dictation (gateway/voice.py, Whisper) ───────────────────────────────────

def test_whisper_is_told_the_words_and_its_transcript_put_right(monkeypatch):
    calls = []

    class Model:
        def __init__(self, *a, **kw):
            pass

        def transcribe(self, path, **kw):
            calls.append(kw)
            return [types.SimpleNamespace(text=" Did Joey call? ")], types.SimpleNamespace(language="en", duration=1.0)

    monkeypatch.setitem(__import__("sys").modules, "faster_whisper", types.SimpleNamespace(WhisperModel=Model))
    w = voice.Whisper("/none", idle_s=0, words=lambda: WORDS)
    assert w.transcribe("x.ogg")["text"] == "Did Zoé call?"
    assert calls[-1]["hotwords"] == "Zoé, Anaïs, Quill"
    plain = voice.Whisper("/none", idle_s=0)
    assert plain.transcribe("x.ogg")["text"] == "Did Joey call?" and "hotwords" not in calls[-1]


# ── getting it everywhere (gateway/household_words_api.py) ───────────────────────────────────

class FakeRuntime:
    def __init__(self, env, agents):
        self.env, self.agents, self.puts = env, agents, []

    def defined(self):
        return sorted(self.agents)

    def load(self, name):
        return self.agents[name]

    def put(self, vol, rel, content, mode="0640"):
        self.puts.append((vol, rel, content, mode))


def test_sync_sends_changes_only_and_the_speaker_every_time(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "people_words", lambda env=None: ["Sam"])
    env = {"HOME": str(tmp_path)}
    hw.save(hw.path(env), WORDS[:1])
    rt = FakeRuntime(env, {"sam": {"display_name": "Sam's agent", "speaker_name": "Quill"}, "new": {"display_name": "Wren"}})
    stt_got, voice_got, sent = [], [], {}
    run = lambda: api.sync(rt, sent, env, stt_send=stt_got.append, voice_write=lambda e: voice_got.append(e) or True,  # noqa: E731
                           agent_volume=lambda vol: vol != "logos-home-new")
    done = run()
    assert done["stt"] and done["voice"] and done["agents"] == ["sam"]      # "new" has no home yet: not made for it
    assert [e["word"] for e in stt_got[0]] == ["Zoé", "Sam", "Wren", "Quill"] and "source" not in stt_got[0][1]
    vol, rel, body, mode = rt.puts[0]
    assert (vol, rel, mode) == ("logos-home-sam", "logos-words.json", "0644")
    assert json.loads(body)["text"] == "Names here: Zoé (speech-to-text may write Zoey or Joey), Sam, Wren, Quill."
    run()
    assert len(stt_got) == 2 and len(voice_got) == 1 and len(rt.puts) == 1   # nothing changed: the speaker only
    hw.save(hw.path(env), WORDS)
    run()
    assert len(voice_got) == 2 and len(rt.puts) == 2


def test_sync_tries_the_voice_service_again_until_it_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "people_words", lambda env=None: [])
    env = {"HOME": str(tmp_path)}
    rt = FakeRuntime(env, {})
    tries, sent = [], {}
    api.sync(rt, sent, env, stt_send=lambda e: None, voice_write=lambda e: tries.append(1) and False, agent_volume=lambda v: True)
    api.sync(rt, sent, env, stt_send=lambda e: None, voice_write=lambda e: tries.append(1) and False, agent_volume=lambda v: True)
    assert len(tries) == 2


def test_the_page_lists_saves_and_keeps_only_what_was_added(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "people_words", lambda env=None: ["Sam"])
    env = {"HOME": str(tmp_path)}
    rt = FakeRuntime(env, {"sam": {"display_name": "Sam's agent", "speaker_name": "Quill"}})
    monkeypatch.setattr(api, "_runtime", lambda app: rt)
    import gateway.dashboard_api as dash
    monkeypatch.setattr(dash, "_user", lambda r: {"id": "u1", "username": "sam", "display_name": "Sam", "role": "user"})
    synced = []

    async def fake_sync(app):
        synced.append(1)
    monkeypatch.setattr(api, "sync_now", fake_sync)
    app = web.Application()
    api.register(app)

    async def run():
        async with TestClient(TestServer(app)) as c:
            first = await (await c.get(api.PATH)).json()
            csrf = {"X-CSRF-Token": "t"}
            c.session.cookie_jar.update_cookies({"csrf_token": "t"})
            put = await c.put(api.PATH, json={"words": [{"word": "Zoé", "heard_as": ["Zoey"]}, {"word": "Sam", "heard_as": []},
                                                         {"word": "Quill", "heard_as": ["Quil"]}]}, headers=csrf)
            no_csrf = await c.put(api.PATH, json={"words": []})
            bad = await c.put(api.PATH, json={"nope": 1}, headers=csrf)
            many = await c.put(api.PATH, json={"words": [{"word": f"W{i}"} for i in range(101)]}, headers=csrf)
            await asyncio.sleep(0)
            return first, put.status, await put.json(), no_csrf.status, bad.status, many.status
    first, status, saved, no_csrf, bad, many = asyncio.run(run())
    assert [(e["word"], e.get("source")) for e in first["words"]] == [("Sam", "person"), ("Quill", "agent")]
    assert status == 200 and [e["word"] for e in saved["words"]] == ["Zoé", "Quill", "Sam"]
    assert hw.load(hw.path(env)) == [{"word": "Zoé", "heard_as": ["Zoey"]}, {"word": "Quill", "heard_as": ["Quil"]}]
    assert (no_csrf, bad, many) == (403, 400, 400) and synced == [1]


# ── the agents' plugin ───────────────────────────────────────────────────────────────────────

def _plugin():
    spec = importlib.util.spec_from_file_location("logos_household_words", REPO / "infra/agent-plugins/logos-household-words/__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_plugin_adds_the_line_to_each_turn(tmp_path):
    plugin = _plugin()
    assert plugin.note(home=str(tmp_path)) is None
    (tmp_path / "logos-words.json").write_text(json.dumps({"text": "Names here: Zoé (speech-to-text may write Zoey)."}))
    assert plugin.note(home=str(tmp_path)) == {"context": "Names here: Zoé (speech-to-text may write Zoey)."}
    (tmp_path / "logos-words.json").write_text(json.dumps({"text": ""}))
    assert plugin.note(home=str(tmp_path)) is None
    (tmp_path / "logos-words.json").write_text("{bad")
    assert plugin.note(home=str(tmp_path)) is None
    hooks = []
    plugin.register(types.SimpleNamespace(register_hook=lambda name, fn: hooks.append(name)))
    assert hooks == ["pre_llm_call"]
