"""The house speaker's speech-to-text container (Voice VO.1): its memory, its two doors, how it is run."""
import asyncio
import json
import stat
import threading
import types
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from gateway import stt, stt_host
from gateway import voiceprint as vp


def test_heard_keeps_embeddings_by_the_hash_of_the_words_for_two_minutes():
    h = stt.Heard()
    h.add("What's on today?", [0.1, 0.2], {"speech": 1.8, "snr": 20, "clipped": False}, now=1000)
    h.add("Add milk", [0.3, 0.4], {"speech": 1.2}, now=1010)
    rows = h.find(vp.heard_key("whats on TODAY"), now=1020)
    assert len(rows) == 1 and rows[0]["embedding"] == [0.1, 0.2] and rows[0]["speech"] == 1.8
    assert "key" not in rows[0] and "text" not in rows[0]                    # neither the words nor their hash go out
    assert [r["embedding"] for r in h.find(now=1020)] == [[0.3, 0.4], [0.1, 0.2]]   # newest first
    assert h.find(vp.heard_key("What's on today?"), now=1000 + stt.KEEP_S + 1) == []
    for i in range(stt.MAX_HEARD + 5):
        h.add(f"n {i}", None, {}, now=1030)
    assert len(h.find(now=1030)) == stt.MAX_HEARD


def test_the_words_are_not_kept_anywhere():
    h = stt.Heard()
    h.add("the secret is swordfish", [0.5], {}, now=1)
    assert "swordfish" not in json.dumps(h._rows)


def test_authorised():
    assert stt.authorised("k", "Bearer k")
    assert not stt.authorised("k", "Bearer x") and not stt.authorised("", "Bearer ") and not stt.authorised("k", "k")


def test_to_float_mixes_down_and_resamples():
    import numpy as np
    pcm = np.array([1000, -1000] * 4800, dtype="<i2").tobytes()     # 0.1 s of stereo at 48 kHz
    a = stt.to_float(pcm, 48000, 2, 2)
    assert len(a) == 1600 and abs(float(a.max())) < 1e-6              # the two channels cancel out
    mono = stt.to_float(np.full(16000, 16384, dtype="<i2").tobytes(), 16000, 2, 1)
    assert len(mono) == 16000 and abs(float(mono[0]) - 0.5) < 1e-6
    with pytest.raises(ValueError):
        stt.to_float(b"\0" * 10, 16000, 1, 1)


@pytest.fixture
def door():
    heard = stt.Heard()
    heard.add("Lights off", [0.1, 0.9], {"speech": 1.5, "snr": 18, "clipped": False})
    got = []

    def recording(path):
        got.append(open(path, "rb").read())
        return {"quality": {"speech": 9.0, "snr": 22, "clipped": False}, "embedding": [1.0], "pieces": [[1.0], [0.5]]}

    server = ThreadingHTTPServer(("127.0.0.1", 0), stt.make_http("tok", heard, recording, lambda: [[0.2]]))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", got
    server.shutdown()


def call(url, token="tok", data=None):
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET",
                                 headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, None


def test_the_dashboards_door_needs_its_token(door):
    base, got = door
    assert call(base + "/health", token="")[0] == 200
    assert call(base + "/heard", token="")[0] == 401 and call(base + "/heard", token="nope")[0] == 401
    assert call(base + "/embed", token="nope", data=b"x")[0] == 401
    assert got == []


def test_the_dashboard_asks_for_what_was_heard_the_cohort_and_a_recording(door):
    base, got = door
    status, d = call(base + "/heard?key=" + vp.heard_key("lights off"))
    assert status == 200 and d["heard"][0]["embedding"] == [0.1, 0.9]
    assert call(base + "/heard?key=" + vp.heard_key("lights on"))[1]["heard"] == []
    assert call(base + "/cohort")[1] == {"cohort": [[0.2]]}
    status, d = call(base + "/embed", data=b"RIFF....")
    assert status == 200 and d["pieces"] == [[1.0], [0.5]] and got == [b"RIFF...."]
    assert call(base + "/embed", data=b"")[0] == 400
    assert call(base + "/other")[0] == 404


# ── how it is run (gateway/stt_host.py) ─────────────────────────────────────────────────────

def env_for(tmp_path, text=None):
    if text is not None:
        (tmp_path / ".config" / "logos").mkdir(parents=True, exist_ok=True)
        (tmp_path / ".config" / "logos" / "speaker").write_text(text)
    return {"HOME": str(tmp_path)}


def test_off_unless_the_speaker_file_names_a_port(tmp_path):
    assert stt_host.settings(env_for(tmp_path)) is None
    assert stt_host.settings(env_for(tmp_path, "TTS_BIND=192.0.2.20\n")) is None
    assert stt_host.settings(env_for(tmp_path, "TTS_BIND=192.0.2.20\nSTT_PORT=10300\n")) == {"bind": "192.0.2.20", "port": 10300}
    with pytest.raises(stt_host.SttError):
        stt_host.settings(env_for(tmp_path, "TTS_BIND=192.0.2.20\nSTT_PORT=10200\n"))     # the speaker voices' port
    with pytest.raises(stt_host.SttError):
        stt_host.settings(env_for(tmp_path, "TTS_BIND=192.0.2.20\nSTT_PORT=lots\n"))


def test_the_token_is_made_once_and_private(tmp_path):
    env = env_for(tmp_path)
    t = stt_host.token(env)
    assert len(t) > 30 and stt_host.token(env) == t
    assert stat.S_IMODE((tmp_path / ".config/logos/stt.token").stat().st_mode) == 0o600


class Runner:
    def run(self, argv, **kw):
        return types.SimpleNamespace(returncode=0)

    def sleep(self, s):
        pass


class Runtime:
    def __init__(self):
        self.runner, self.argvs = Runner(), []

    def run(self, argv, check=True, input=None):
        self.argvs.append(argv)
        return ""


def test_up_puts_wyoming_on_the_lan_and_the_dashboards_door_on_loopback(tmp_path):
    rt = Runtime()
    env = env_for(tmp_path, "TTS_BIND=192.0.2.20\nSTT_PORT=10300\n")
    probed = []
    assert stt_host.SttHost(rt, env, probe=lambda a: probed.append(a) or True).up() is True
    run = " ".join(next(a for a in rt.argvs if a[:2] == ["docker", "run"]))
    for part in ("--read-only", "--cap-drop ALL", "no-new-privileges", "--dns 127.0.0.1", "--user 10000:10000",
                 "-p 192.0.2.20:10300:10300", f"-p 127.0.0.1:{stt_host.HTTP_PORT}:{stt_host.HTTP_PORT}",
                 "stt.py:/app/stt.py:ro", "voiceprint.py:/app/voiceprint.py:ro", "--memory 2g", "logos-stt:2"):
        assert part in run, part
    assert f"STT_TOKEN={stt_host.token(env)}" in run
    assert f"192.0.2.20:{stt_host.HTTP_PORT}" not in run                     # the token's door is never on the LAN
    assert probed == [("192.0.2.20", 10300)]


def test_up_does_nothing_when_off(tmp_path):
    rt = Runtime()
    assert stt_host.SttHost(rt, env_for(tmp_path, "TTS_BIND=192.0.2.20\n")).up() is None
    assert rt.argvs == []


def test_the_image_pins_what_it_fetches():
    text = (stt_host.BUILD_DIR / "Dockerfile").read_text()
    for pin in ("SPEAKER_MODEL_SHA256=", "LIBRISPEECH_SHA256=", "WORDS_MODEL_SHA256=", "python@sha256:", "sherpa-onnx==",
                "wyoming=="):
        assert pin in text
    assert "USER 10000:10000" in text
