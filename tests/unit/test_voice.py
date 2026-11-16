"""gateway/voice.py: the HTTP side of the voice service, with the model replaced by a stand-in."""
import http.client
import json
import threading
from http.server import ThreadingHTTPServer

import pytest

from gateway import voice, voice_host

TOKENS = {"greg": "tok-greg", "bere": "tok-bere"}


def form(fields, file=None, boundary="XbOuNdArY"):
    parts = []
    for k, v in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    if file:
        name, data = file
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
                     f'Content-Type: application/octet-stream\r\n\r\n'.encode() + data + b"\r\n")
    return f"multipart/form-data; boundary={boundary}", b"".join(parts) + f"--{boundary}--\r\n".encode()


def test_parse_form_keeps_binary_audio_intact():
    audio = bytes(range(256)) * 4 + b"\r\n--not-the-boundary\r\n"
    ctype, body = form({"model": "whisper-1", "language": "fr"}, ("note.ogg", audio))
    fields, upload = voice.parse_form(ctype, body)
    assert fields == {"model": "whisper-1", "language": "fr"}
    assert upload == ("note.ogg", audio)
    with pytest.raises(voice.BadRequest):
        voice.parse_form("application/json", b"{}")


def test_agent_for_needs_an_exact_bearer_token():
    assert voice.agent_for(TOKENS, "Bearer tok-bere") == "bere"
    assert voice.agent_for(TOKENS, "bearer tok-greg") == "greg"
    for bad in ("", "Bearer", "Bearer nope", "Basic tok-greg", "tok-greg"):
        assert voice.agent_for(TOKENS, bad) is None
    assert voice.agent_for({"x": ""}, "Bearer ") is None


@pytest.fixture
def server():
    seen = []

    def transcribe(path, language):
        with open(path, "rb") as f:
            data = f.read()
        if data == b"broken":
            raise EOFError("not audio")
        seen.append((path, language, data))
        return {"text": "bonjour tout le monde", "language": language or "fr", "duration": 2.5}

    srv = ThreadingHTTPServer(("127.0.0.1", 0), voice.make_handler(TOKENS, transcribe))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1], seen
    srv.shutdown()


def call(port, method="POST", path=voice.PATH, token="tok-greg", fields=None, file=("note.ogg", b"OggS-audio")):
    ctype, body = form(fields or {}, file)
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Content-Type": ctype}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    conn.request(method, path, body=body if method == "POST" else None, headers=headers)
    r = conn.getresponse()
    return r.status, r.read().decode()


def test_a_recording_comes_back_as_text_and_the_file_is_removed(server):
    import os
    port, seen = server
    status, body = call(port, fields={"model": "whisper-1", "language": "fr"})
    assert status == 200
    assert json.loads(body) == {"text": "bonjour tout le monde", "language": "fr", "duration": 2.5}
    path, language, data = seen[0]
    assert (language, data) == ("fr", b"OggS-audio") and path.endswith(".ogg")
    assert not os.path.exists(path)
    status, body = call(port, fields={"response_format": "text"})
    assert (status, body) == (200, "bonjour tout le monde\n")


def test_refusals(server):
    port, seen = server
    assert call(port, token=None)[0] == 401
    assert call(port, token="someone-else")[0] == 401
    assert call(port, path="/v1/models")[0] == 404
    assert call(port, file=None)[0] == 400
    assert call(port, file=("note.ogg", b"broken"))[0] == 422
    assert call(port, method="GET", path="/health")[0] == 200
    assert seen == []


def test_host_config_lists_only_tokens():
    assert voice_host.build_config({"greg": "a", "bere": "b"}) == {"agents": {"bere": "b", "greg": "a"}}
    assert voice_host.uses_voice({"voice": True}) and not voice_host.uses_voice({})
