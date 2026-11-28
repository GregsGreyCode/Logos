"""The house speaker wakes to each agent's name (Voice VW.1): the wake word server's choices
(gateway/wake.py), its container and the names file (gateway/wake_host.py), and how
voice/converse picks the agent that was called."""
import json
import re
import types
from pathlib import Path

import pytest

from gateway import wake, wake_host

REPO = Path(__file__).resolve().parents[2]


# ── The image ───────────────────────────────────────────────────────────────

def test_the_image_is_pinned_and_checks_the_model():
    text = (REPO / "infra/wake/Dockerfile").read_text()
    assert re.search(r"^FROM python@sha256:[0-9a-f]{64}$", text, re.M)
    assert re.search(r"KWS_SHA256=[0-9a-f]{64}\b", text) and "sha256sum -c" in text
    for pin in ("sherpa-onnx==", "sentencepiece==", "wyoming=="):
        assert pin in text
    assert "USER 10000:10000" in text


# ── gateway/wake.py: names into keywords ────────────────────────────────────

def test_names_are_spelled_the_way_the_model_knows_them():
    assert wake.spell("Hey Finn") == "HEY FINN"
    assert wake.spell("Béré") == "BERE"
    assert wake.spell("  hey,  Home! ") == "HEY HOME"
    assert wake.phrase_id("Hey Finn") == "hey_finn"
    assert wake.phrase_id("Hey Béré") == wake_host._id("Hey Béré") == "hey_bere"
    assert wake.phrase_id("!!") == ""


def test_keyword_lines_carry_each_phrases_threshold():
    phrases = wake.clean_phrases([{"phrase": "Hey Finn", "agent": "greg", "threshold": 0.3},
                                  {"phrase": "Finn", "agent": "greg"}])
    pieces = {"HEY FINN": ["▁HE", "Y", "▁F", "IN", "N"], "FINN": ["▁F", "IN", "N"]}
    lines = wake.keyword_lines(phrases, lambda t: pieces[t])
    assert lines == ["▁HE Y ▁F IN N :1 #0.3 @hey_finn", "▁F IN N :1 #0.25 @finn"]


def test_bad_rows_in_the_names_file_are_left_out():
    rows = [{"phrase": "Hey Finn", "agent": "greg"}, {"phrase": "hey finn", "agent": "bere"},   # repeat
            {"phrase": "", "agent": "x"}, {"phrase": "Home"}, "junk", {"phrase": "logos any", "agent": "x"},
            {"phrase": "Hey Home", "agent": "household", "threshold": "high"},
            {"phrase": "Bob", "agent": "b", "threshold": 7}]
    out = wake.clean_phrases(rows)
    assert [(p["id"], p["agent"]) for p in out] == [("hey_finn", "greg"), ("hey_home", "household"), ("bob", "b")]
    assert out[1]["threshold"] == wake.THRESHOLD and out[2]["threshold"] == wake.MAX_THRESHOLD


def test_which_names_to_listen_for_and_what_to_report():
    phrases = [{"id": "hey_finn"}, {"id": "hey_home"}]
    assert wake.wanted(None, phrases) == phrases
    assert wake.wanted([wake.ANY], phrases) == phrases
    assert wake.wanted(["hey_home"], phrases) == [{"id": "hey_home"}]
    # Home Assistant drops a detection named other than what it asked for
    assert wake.reported_name([wake.ANY], "hey_finn") == wake.ANY
    assert wake.reported_name(None, "hey_finn") == "hey_finn"
    assert wake.reported_name(["hey_finn"], "hey_finn") == "hey_finn"


def test_heard_keeps_a_few_recent_detections(tmp_path):
    path = str(tmp_path / "heard.json")
    wake.note_heard(path, {"agent": "greg", "phrase": "Hey Finn"}, now=1000.0)
    for i in range(wake.KEEP_HEARD + 3):
        wake.note_heard(path, {"agent": "household", "phrase": "Hey Home"}, now=1100.0 + i)
    rows = json.loads(Path(path).read_text())["heard"]
    assert len(rows) == wake.KEEP_HEARD and all(r["agent"] == "household" for r in rows)   # old one dropped
    assert oct(Path(path).stat().st_mode & 0o777) == "0o644"


def test_pcm_becomes_floats():
    import struct
    out = wake.pcm_to_float(struct.pack("<3h", 0, 16384, -32768) + b"\x01")   # odd byte ignored
    assert list(out) == [0.0, 0.5, -1.0]


# ── gateway/wake_host.py: the names ─────────────────────────────────────────

class Defs:
    def __init__(self, defs):
        self.defs = defs

    def defined(self):
        return sorted(self.defs)

    def load(self, name):
        if self.defs[name] is None:
            raise ValueError("broken yaml")
        return self.defs[name]


def test_each_name_gives_hey_and_bare_with_their_thresholds():
    rows = wake_host.phrases_of("greg", {"speaker_name": "Finn"})
    assert rows == [{"phrase": "Hey Finn", "agent": "greg", "threshold": wake_host.HEY_THRESHOLD},
                    {"phrase": "Finn", "agent": "greg", "threshold": wake_host.BARE_THRESHOLD}]
    assert wake_host.phrases_of("bench", {}) == []


def test_speaker_wake_chooses_the_phrases():
    rows = wake_host.phrases_of("household", {"speaker_name": "Home", "speaker_wake": {"Hey Home": 0.2}})
    assert rows == [{"phrase": "Hey Home", "agent": "household", "threshold": 0.2}]
    rows = wake_host.phrases_of("greg", {"speaker_name": "Finn", "speaker_wake": ["Hey Finn", "Okay Finn"]})
    assert [r["phrase"] for r in rows] == ["Hey Finn", "Okay Finn"]
    with pytest.raises(wake_host.WakeError):
        wake_host.phrases_of("greg", {"speaker_name": "Finn", "speaker_wake": {"Finn": "loud"}})


def test_a_name_two_agents_share_wakes_neither():
    out = wake_host.all_phrases({"a": {"speaker_name": "Sam"}, "b": {"speaker_name": "Sam", "speaker_wake": ["Sam"]},
                                 "c": {"speaker_name": "Home"}})
    assert [(r["phrase"], r["agent"]) for r in out] == [("Hey Sam", "a"), ("Hey Home", "c"), ("Home", "c")]


def test_the_shipped_agents_have_names():
    import yaml
    greg = yaml.safe_load((REPO / "agents/greg.yaml").read_text())
    home = yaml.safe_load((REPO / "agents/household.yaml").read_text())
    assert greg["speaker_name"] == "Finn" and home["speaker_name"] == "Home"
    assert {r["phrase"] for r in wake_host.all_phrases({"greg": greg, "household": home})} == {"Hey Finn", "Finn", "Hey Home"}


def test_names_file_is_written_only_where_the_wake_words_are_set_up(tmp_path):
    env = {"HOME": str(tmp_path)}
    rt = Defs({"greg": {"speaker_name": "Finn"}, "household": {"speaker_name": "Home"}, "broken": None, "dev": {}})
    assert wake_host.write_names(rt, env) is None
    assert not wake_host.state_dir(env).exists()
    phrases = wake_host.write_names(rt, env, create=True)
    path = wake_host.state_dir(env) / "names.json"
    assert json.loads(path.read_text())["phrases"] == phrases and len(phrases) == 4
    mtime = path.stat().st_mtime_ns
    wake_host.write_names(rt, env)                                   # unchanged: not rewritten (no reload)
    assert path.stat().st_mtime_ns == mtime
    rt.defs["greg"] = {"speaker_name": "Rowan"}                      # renamed: the file follows
    wake_host.write_names(rt, env)
    assert "Hey Rowan" in path.read_text() and "Finn" not in path.read_text()
    # and the container reads it the same way
    assert {p["id"] for p in wake.clean_phrases(json.loads(path.read_text())["phrases"])} == \
        {"hey_rowan", "rowan", "hey_home", "home"}


# ── gateway/wake_host.py: the container ─────────────────────────────────────

def env_for(tmp_path, text=None):
    if text is not None:
        (tmp_path / ".config/logos").mkdir(parents=True, exist_ok=True)
        (tmp_path / ".config/logos/speaker").write_text(text)
    return {"HOME": str(tmp_path)}


def test_settings_need_wake_port(tmp_path):
    assert wake_host.settings(env_for(tmp_path)) is None
    assert wake_host.settings(env_for(tmp_path, "TTS_BIND=192.0.2.20\n")) is None        # speaker voices only
    assert wake_host.settings(env_for(tmp_path, "TTS_BIND=192.0.2.20\nWAKE_PORT=10400\n")) == \
        {"bind": "192.0.2.20", "port": 10400}
    assert wake_host.settings(env_for(tmp_path, "TTS_BIND=192.0.2.20\nWAKE_BIND=0.0.0.0\nWAKE_PORT=1\n"))["bind"] == "0.0.0.0"
    for bad in ("TTS_BIND=lan\nWAKE_PORT=10400\n", "TTS_BIND=192.0.2.20\nWAKE_PORT=99999\n"):
        with pytest.raises(wake_host.WakeError):
            wake_host.settings(env_for(tmp_path, bad))


class Runner:
    def run(self, argv, **kw):
        return types.SimpleNamespace(returncode=0)

    def sleep(self, s):
        pass


class Runtime(Defs):
    def __init__(self, defs, running=""):
        super().__init__(defs)
        self.runner, self.argvs, self.running = Runner(), [], running

    def run(self, argv, check=True, input=None):
        self.argvs.append(argv)
        return self.running if argv[:2] == ["docker", "ps"] else ""


def test_up_runs_it_locked_down_and_writes_the_names(tmp_path):
    rt = Runtime({"greg": {"speaker_name": "Finn"}})
    env = env_for(tmp_path, "TTS_BIND=192.0.2.20\nWAKE_PORT=10400\n")
    probed = []
    assert wake_host.WakeHost(rt, env, probe=lambda a: probed.append(a) or True, user="1000:1000").up() is True
    run = " ".join(next(a for a in rt.argvs if a[:2] == ["docker", "run"]))
    for part in ("--read-only", "--cap-drop ALL", "no-new-privileges", "--dns 127.0.0.1", "--user 1000:1000",
                 "-p 192.0.2.20:10400:10400", "wake.py:/app/wake.py:ro", "--memory 512m", "--cpus 1"):
        assert part in run
    assert probed == [("192.0.2.20", 10400)]
    assert "Hey Finn" in (wake_host.state_dir(env) / "names.json").read_text()


def test_up_when_off_starts_nothing(tmp_path):
    rt = Runtime({"greg": {"speaker_name": "Finn"}})
    assert wake_host.WakeHost(rt, env_for(tmp_path, "TTS_BIND=192.0.2.20\n")).up() is None
    assert rt.argvs == [] and not wake_host.state_dir(env_for(tmp_path)).exists()


# ── Who was called ──────────────────────────────────────────────────────────

@pytest.fixture
def heard(tmp_path):
    wake_host.forget()
    env = {"HOME": str(tmp_path)}
    folder = wake_host.state_dir(env)
    folder.mkdir(parents=True)

    def write(*rows):
        (folder / "heard.json").write_text(json.dumps({"heard": list(rows)}))
    yield env, write
    wake_host.forget()


def test_the_name_just_heard_picks_the_agent_once(heard):
    env, write = heard
    write({"agent": "greg", "phrase": "Hey Finn", "at": 1000.0})
    assert wake_host.agent_for("", env, now=1005.0) == "greg"
    assert wake_host.agent_for("", env, now=1006.0) is None            # used: a typed request later is Home's


def test_a_stale_detection_is_ignored(heard):
    env, write = heard
    write({"agent": "greg", "phrase": "Hey Finn", "at": 1000.0})
    assert wake_host.agent_for("", env, now=1000.0 + wake_host.HEARD_S + 1) is None


def test_the_latest_name_wins(heard):
    env, write = heard
    write({"agent": "greg", "at": 1000.0}, {"agent": "household", "at": 1003.0})
    assert wake_host.agent_for("", env, now=1004.0) == "household"
    assert wake_host.agent_for("", env, now=1005.0) is None             # the older one was superseded


def test_a_follow_up_stays_with_the_agent_called(heard):
    env, write = heard
    write({"agent": "greg", "at": 1000.0})
    assert wake_host.agent_for("conv-1", env, now=1004.0) == "greg"
    assert wake_host.agent_for("conv-1", env, now=1030.0) == "greg"     # no new wake word: same conversation
    assert wake_host.agent_for("conv-2", env, now=1031.0) is None
    write({"agent": "greg", "at": 1000.0}, {"agent": "household", "at": 1040.0})
    assert wake_host.agent_for("conv-1", env, now=1041.0) == "household"   # a new name moves it
    assert wake_host.agent_for("conv-1", env, now=1041.0 + wake_host.CONVERSATION_S + 1) is None


def test_no_file_no_agent(tmp_path):
    wake_host.forget()
    assert wake_host.agent_for("x", {"HOME": str(tmp_path)}) is None


def test_voice_converse_answers_with_the_agent_called(heard, monkeypatch, tmp_path):
    from aiohttp import web
    from gateway import voice_converse as vc
    from tests.unit.test_voice_converse import FakeClient, FakeLinks, _post, ev
    env, write = heard
    monkeypatch.setenv("HOME", env["HOME"])
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.delenv("LOGOS_VOICE_AGENT", raising=False)
    links = FakeLinks(FakeClient([ev("run_completed", "Ten past four.")]))

    def app():
        a = web.Application()
        a["agent_links"] = links
        vc.register(a)
        return a
    auth = {"Authorization": f"Bearer {vc.token()}"}
    import time
    write({"agent": "greg", "phrase": "Hey Finn", "at": time.time()})
    status, out = _post(app, auth, {"text": "what time is it", "device_id": "pe1", "conversation_id": "c1"})
    assert status == 200 and out["agent"] == "greg" and out["conversation_id"].startswith("voice-greg-pe1-")
    status, out = _post(app, auth, {"text": "and tomorrow?", "device_id": "pe1", "conversation_id": "c1"})
    assert out["agent"] == "greg"                                        # the follow-up stays with Finn
    status, out = _post(app, auth, {"text": "lights off", "device_id": "pe1", "conversation_id": "c2"})
    assert out["agent"] == "household"                                   # nobody named: Home
    assert links.asked == ["greg", "greg", "household"]


def test_quiet_speech_is_raised_and_silence_is_left_alone():
    import array, math
    from gateway import wake
    rate = 16000

    def tone(db, s=0.1):
        a = 10 ** (db / 20) * math.sqrt(2)
        return array.array("f", (a * math.sin(2 * math.pi * 300 * i / rate) for i in range(int(rate * s))))
    g = wake.Gain()
    t = 0.0
    for _ in range(20):                                  # far speech at -45 dBFS: raised towards -24, by at most 18 dB
        out = g(tone(-45), rate, t)
        t += 0.1
    assert 17.5 <= g.db <= 18.0 and wake.Gain.level(out) > -28
    g2 = wake.Gain()
    for i in range(20):                                  # a quiet room (-70 dBFS) is not turned into hiss
        out = g2(tone(-70), rate, i * 0.1)
    assert g2.db == 0.0
    g3 = wake.Gain()
    for i in range(20):                                  # speech already loud enough is never turned down
        g3(tone(-12), rate, i * 0.1)
    assert g3.db == 0.0
    g4 = wake.Gain(on=False)
    x = tone(-45)
    assert g4(x, rate, 0.0) is x


# ── the second check (28 Nov) ───────────────────────────────────────────────

def test_the_name_is_found_in_what_speech_to_text_heard():
    from gateway import wake
    assert wake.name_words("Hey Finn") == ["finn"] and wake.name_words("Hey Home") == ["home"]
    assert wake.name_words("Finn") == ["finn"]
    for heard in ("Hey Finn.", "hey fin", "Hey, Finn's", "Finn", "hey fynn"):
        assert wake.says_name(heard, "Hey Finn"), heard
    for heard in ("", "Hey.", "the fun bit", "I'm in", "been there", "Hey, how are you?"):
        assert not wake.says_name(heard, "Hey Finn"), heard
    assert wake.says_name("hey home", "Hey Home") and not wake.says_name("hey hello", "Hey Home")
    assert wake.says_name("hey ad am", "Hey Adam")                    # a name heard as two words


def test_the_check_address_and_audio_for_it():
    from gateway import wake
    assert wake.check_address({"WAKE_CHECK": "192.0.2.5:10300"}) == ("192.0.2.5", 10300)
    assert wake.check_address({"WAKE_CHECK": ""}) is None and wake.check_address({"WAKE_CHECK": "x:no"}) is None
    pcm = wake.float_to_pcm([0.0, 0.5, -1.0, 2.0])
    assert len(pcm) == 8 and list(wake.pcm_to_float(pcm))[1] > 0.49


def test_the_wake_container_is_told_where_speech_to_text_is(tmp_path, monkeypatch):
    from gateway import wake_host
    src = open(wake_host.__file__).read()
    assert "WAKE_CHECK=" in src and "stt_host.settings" in src
